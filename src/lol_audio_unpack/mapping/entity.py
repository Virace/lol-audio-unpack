"""单实体事件映射与整合流程。"""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from league_tools import AudioEventMapper
from loguru import logger

from lol_audio_unpack.app.artifacts import AudioRef, enumerate_audio_refs, inspect_shared_copies
from lol_audio_unpack.app.path_layout import get_entity_path_component, get_output_dir_name
from lol_audio_unpack.manager import DataReader
from lol_audio_unpack.manager.files import write_data
from lol_audio_unpack.manager.utils import build_metadata_payload
from lol_audio_unpack.model import AudioBank, AudioEntityData
from lol_audio_unpack.model.binding import SUCCESS_STATUSES, normalize_logical_path
from lol_audio_unpack.model.skin_audio import SKIN_AUDIO_VERSION, SkinAudio
from lol_audio_unpack.runtime.hirc import WwiserTool
from lol_audio_unpack.runtime.wad import extract_wad, resolve_bound_wad
from lol_audio_unpack.utils.logging import performance_monitor

from . import session as mapping_session

if TYPE_CHECKING:
    from lol_audio_unpack.app.types import AppContext


def _ensure_version_dirs(reader: DataReader, *, ctx: AppContext) -> tuple[Path, Path]:
    """创建并返回版本化的 cache/hash 目录。

    Args:
        reader: 数据读取器实例。
        ctx: 运行时上下文。

    Returns:
        tuple[Path, Path]: ``(version_cache_dir, version_hash_dir)``。
    """

    version_cache_dir = ctx.version_path("cache", reader.version)
    version_hash_dir = ctx.version_path("hash", reader.version)
    version_cache_dir.mkdir(parents=True, exist_ok=True)
    version_hash_dir.mkdir(parents=True, exist_ok=True)
    return version_cache_dir, version_hash_dir


def _build_mapping_result(entity_data: AudioEntityData, reader: DataReader) -> tuple[dict[str, Any], str]:
    """创建映射结果骨架。

    Args:
        entity_data: 当前实体数据。
        reader: 数据读取器实例。

    Returns:
        tuple[dict[str, Any], str]: 映射结果骨架与数据键名。

    Raises:
        ValueError: 实体类型未知时抛出。
    """

    base_data = build_metadata_payload(reader.version, reader.get_languages())
    base_data["mappingDiagnostics"] = {}
    if entity_data.entity_type == "champion":
        base_data["championId"] = entity_data.entity_id
        base_data["alias"] = entity_data.entity_alias
        base_data["skins"] = {}
        base_data["skinAudioVersion"] = SKIN_AUDIO_VERSION
        return base_data, "skins"
    if entity_data.entity_type == "map":
        base_data["mapId"] = entity_data.entity_id
        base_data["name"] = entity_data.entity_alias
        base_data["map"] = {}
        return base_data, "map"
    if entity_data.entity_type == "resource_pack":
        # raw mapping 以 plural data key 承载唯一 logical sub-entity；完整 key 仍是 payload identity。
        base_data["resourcePackKey"] = entity_data.entity_id
        base_data["resourcePacks"] = {}
        return base_data, "resourcePacks"
    raise ValueError(f"未知的实体类型: {entity_data.entity_type}")


def _entity_group(entity_type: str) -> str:
    """返回实体的 mapping 输出分组。

    Args:
        entity_type: 当前实体类型。

    Returns:
        对应的 hash 输出目录名。

    Raises:
        ValueError: 实体类型未知时抛出。
    """
    return get_output_dir_name(entity_type)


def _category_item(sub_id: str, category: str, **extra: str) -> dict[str, str]:
    """构造带逻辑子实体归属的 mapping 诊断分类项。"""
    return {"subEntity": sub_id, "category": category, **extra}


def _wad_namespace(wad_identity: str) -> str:
    """返回物理 WAD identity 的完整 SHA-256 缓存命名空间。"""
    return sha256(wad_identity.encode("utf-8")).hexdigest()


def _audio_ref_index(refs: tuple[AudioRef, ...]) -> dict[tuple[str, str, str], tuple[AudioRef, ...]]:
    """按子实体、类型和 WEM ID 建立保留同 ID 多路径的索引。"""
    index: dict[tuple[str, str, str], list[AudioRef]] = {}
    for ref in refs:
        if ref.sub_entity is None or ref.audio_type is None:
            continue
        index.setdefault((ref.sub_entity, ref.audio_type, ref.wem_id), []).append(ref)
    return {key: tuple(sorted(items, key=lambda item: item.relative_path)) for key, items in index.items()}


def _resolve_bank_cache_path(bank_dir: Path, logical_path: str) -> Path:
    """把 bank 逻辑路径约束到当前 WAD cache namespace。"""
    normalized = normalize_logical_path(logical_path)
    parts = PurePosixPath(normalized).parts
    if not parts or ".." in parts or ":" in parts[0]:
        raise ValueError(f"bank 逻辑路径无效或可能逃逸 cache 根: {logical_path}")

    cache_root = bank_dir.resolve()
    cache_path = cache_root.joinpath(*parts).resolve()
    try:
        cache_path.relative_to(cache_root)
    except ValueError as exc:
        raise ValueError(f"bank 逻辑路径解析后越出 cache 根: {logical_path}") from exc
    return cache_path


def _build_bound_category_mapping(  # noqa: PLR0913
    bank: AudioBank,
    event_list: list[str],
    version_cache_dir: Path,
    wwiser_manager: WwiserTool | None,
    runtime_cache: mapping_session.RuntimeCache | None,
    *,
    ctx: AppContext,
) -> tuple[Any | None, str | None]:
    """按单条 local v2 binding 提取 events BNK 并构建映射。"""
    binding = bank.binding
    if not binding.wad:
        return None, "成功 binding 缺少 WAD identity"

    try:
        wad_path = resolve_bound_wad(ctx.game_path, binding.wad)
    except ValueError as exc:
        return None, str(exc)
    if not wad_path.is_file():
        return None, "WAD文件不存在"

    namespace = _wad_namespace(binding.wad)
    bank_dir = version_cache_dir / "banks" / namespace
    hirc_dir = version_cache_dir / "hirc" / namespace
    bank_dir.mkdir(parents=True, exist_ok=True)
    hirc_dir.mkdir(parents=True, exist_ok=True)
    extract_key = (binding.wad, binding.normalized_path)

    try:
        bnk_path = _resolve_bank_cache_path(bank_dir, binding.normalized_path)
        wad_obj = mapping_session._get_wad(wad_path, runtime_cache=runtime_cache)

        def extract_bnk() -> None:
            """读取目标 entry，并只向已校验的 cache path 写入原始 BNK。"""
            raws = extract_wad(wad_obj, [binding.path], raw=True)
            raw = raws[0] if raws else None
            if raw is None:
                raise FileNotFoundError(f"WAD未返回目标BNK entry: {binding.normalized_path}")
            bnk_path.parent.mkdir(parents=True, exist_ok=True)
            bnk_path.write_bytes(raw)

        mapping_session._extract_bnk_once(extract_key, extract_bnk, runtime_cache)

        if not bnk_path.exists():
            return None, "提取的BNK文件不存在"

        hirc = mapping_session._get_cached_hirc(
            bnk_path=bnk_path,
            hirc_cache_dir=hirc_dir,
            wwiser_manager=wwiser_manager,
            runtime_cache=runtime_cache,
            wad_identity=binding.wad,
            normalized_bank_path=normalize_logical_path(binding.normalized_path),
        )
        return AudioEventMapper(event_list, hirc).build_mapping(), None
    except Exception as exc:  # noqa: BLE001
        logger.opt(exception=bool(getattr(ctx.config, "dev_mode", False))).warning(
            f"处理 binding {binding.normalized_path} 时出错: {exc}"
        )
        return None, str(exc)


def _build_bound_entity(  # noqa: PLR0913, PLR0917
    entity_data: AudioEntityData,
    reader: DataReader,
    wwiser_manager: WwiserTool | None,
    integrate_data: bool,
    runtime_cache: mapping_session.RuntimeCache | None,
    *,
    ctx: AppContext,
    persisted_mapping_callback: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """按 local v2 events bindings 构建 mapping 与覆盖诊断。"""
    version_cache_dir, version_hash_dir = _ensure_version_dirs(reader, ctx=ctx)
    mapping_result, mapping_data_key = _build_mapping_result(entity_data, reader)
    entity_group = _entity_group(entity_data.entity_type)
    mapping_save_dir = version_hash_dir / entity_group
    mapping_save_dir.mkdir(parents=True, exist_ok=True)

    refs = enumerate_audio_refs(ctx, entity_data, reader.version)
    refs_by_key = _audio_ref_index(refs)
    skin_audio = (
        SkinAudio(entity_data.resource_banks, entity_data.skin_parents)
        if entity_data.entity_type == "champion"
        else None
    )
    mapped_paths: set[str] = set()
    missing_events: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    unresolved_keys: set[tuple[str, str]] = set()
    errors: list[dict[str, str]] = []
    banks_by_category: dict[tuple[str, str], list[AudioBank]] = {}
    event_banks: dict[tuple[str, str], list[AudioBank]] = {}

    for bank in entity_data.resource_banks:
        binding = bank.binding
        key = (bank.sub_id, binding.category)
        if binding.status not in SUCCESS_STATUSES:
            unresolved.append(_category_item(bank.sub_id, binding.category, status=binding.status.value))
            unresolved_keys.add(key)
            continue
        banks_by_category.setdefault(key, []).append(bank)
        if binding.normalized_path.endswith("_events.bnk"):
            event_banks.setdefault(key, []).append(bank)

    events = entity_data.events or {}
    # 兼容旧事件缓存中省略的 Base 声明；仅相同物理来源可补齐，最终仍统一投影为差异。
    shared_events: dict[tuple[str, frozenset[tuple[str | None, str]]], list[str]] = {}
    if entity_data.entity_type == "champion":
        for key, banks in banks_by_category.items():
            sub_id, category = key
            event_list = events.get(sub_id, {}).get("events", {}).get(category, [])
            if "_Base_" in category and event_list and key not in unresolved_keys:
                physical = frozenset((bank.binding.wad, bank.binding.entry_hash) for bank in banks)
                shared_events[(category, physical)] = event_list
    shared_categories: list[dict[str, str]] = []
    mapped_event_count = 0
    errored_event_count = 0
    skipped_event_count = 0
    successful_categories = 0
    processable_banks = 0
    failed_banks = 0

    category_keys = set(banks_by_category)
    category_keys.update(
        (str(sub_id), category) for sub_id, sub_events in events.items() for category in sub_events.get("events", {})
    )

    for key in sorted(category_keys):
        sub_id, category = key
        event_list = events.get(sub_id, {}).get("events", {}).get(category, [])
        category_banks = banks_by_category.get(key, [])
        if not category_banks:
            if key not in unresolved_keys:
                unresolved.append(_category_item(sub_id, category, status="missing"))
                unresolved_keys.add(key)
            continue
        if not event_list and key not in unresolved_keys:
            physical = frozenset((bank.binding.wad, bank.binding.entry_hash) for bank in category_banks)
            event_list = shared_events.get((category, physical), [])
            if event_list:
                shared_categories.append(_category_item(sub_id, category))
        if not event_list:
            missing_events.append(_category_item(sub_id, category))
            continue

        category_event_banks = event_banks.get(key, [])
        if not category_event_banks:
            unresolved.append(_category_item(sub_id, category, status="missing_events_bank"))
            continue

        category_mapping = None
        category_failed = False
        for bank in category_event_banks:
            processable_banks += 1
            current_mapping, error = _build_bound_category_mapping(
                bank,
                event_list,
                version_cache_dir,
                wwiser_manager,
                runtime_cache,
                ctx=ctx,
            )
            if error is not None:
                failed_banks += 1
                category_failed = True
                continue
            if current_mapping is None:
                continue
            if category_mapping is None:
                category_mapping = current_mapping
            else:
                category_mapping.merge_with(current_mapping)

        if category_failed:
            errors.append(_category_item(sub_id, category))

        if category_mapping is None or not category_mapping.forward_mapping:
            skipped_event_count += len(event_list)
            continue

        successful_categories += 1
        mapped_event_count += len(category_mapping.forward_mapping)
        skipped_event_count += max(len(event_list) - len(category_mapping.forward_mapping), 0)
        sub_result = mapping_result[mapping_data_key].setdefault(sub_id, {"events": {}})
        sub_result["events"][category] = category_mapping.forward_mapping

        audio_paths: dict[str, list[str]] = {}
        audio_type = category_event_banks[0].audio_type
        owner_ids = {skin_audio.owner(bank).sub_id for bank in category_banks} if skin_audio else {sub_id}
        for event_name, wem_ids in category_mapping.forward_mapping.items():
            event_refs = [
                ref
                for wem_id in wem_ids
                for owner_id in owner_ids
                for ref in refs_by_key.get((owner_id, audio_type, str(wem_id)), ())
            ]
            paths = sorted({ref.relative_path for ref in event_refs})
            if paths:
                audio_paths[event_name] = paths
                mapped_paths.update(paths)
        # 已解析事件但没有对应文件时保留空关系，区别于未生成精确路径的旧映射。
        sub_result.setdefault("audioPaths", {})[category] = audio_paths

    if skin_audio is not None:
        mapping_result["sharedAudio"] = skin_audio.shared_payload()
        mapping_result[mapping_data_key] = skin_audio.mapping_differences(mapping_result[mapping_data_key])
        mapped_event_count = sum(
            len(category)
            for skin in mapping_result[mapping_data_key].values()
            for category in skin.get("events", {}).values()
        )
        mapped_paths = {
            path
            for skin in mapping_result[mapping_data_key].values()
            for category in skin.get("audioPaths", {}).values()
            for paths in category.values()
            for path in paths
        }

    # 缺少 events 不是 extract 失败；它只是 mapping 的可观察 partial 状态。
    if not entity_data.events:
        logger.warning(f"{entity_data.entity_name} 缺少 events 数据，仅写入 mapping 诊断")

    total_wem_count = len(refs)
    if shared_categories:
        logger.info(f"{entity_data.entity_name} 的 {len(shared_categories)} 个旧缓存分类已恢复共享来源，不重复展开")
    unmapped_wem_count = total_wem_count - len(mapped_paths)
    source_failed = (
        entity_data.binding_diagnostics is not None and entity_data.binding_diagnostics.completeness.value == "failed"
    )
    if successful_categories == 0 and (source_failed or (processable_banks > 0 and failed_banks == processable_banks)):
        completeness = "failed"
    elif missing_events or unresolved or errors or unmapped_wem_count:
        completeness = "partial"
    else:
        completeness = "complete"

    mapping_result["mappingDiagnostics"] = {
        "completeness": completeness,
        "mappedWemCount": len(mapped_paths),
        "totalWemCount": total_wem_count,
        "unmappedWemCount": unmapped_wem_count,
        "missingEventCategories": missing_events,
        "unresolvedBankCategories": unresolved,
        "errorCategories": errors,
        "sharedEventCategories": shared_categories,
    }
    if skin_audio is not None:
        legacy = inspect_shared_copies(entity_data, refs)
        mapping_result["mappingDiagnostics"].update(legacy)
        if legacy["sharedCopyPaths"] or legacy["unverifiedSharedPaths"]:
            logger.warning(
                "{} 的共享目录仍有旧文件：{} 个同内容副本，{} 个需核对；文件均已保留，可在全部音频中查看",
                entity_data.entity_name,
                len(legacy["sharedCopyPaths"]),
                len(legacy["unverifiedSharedPaths"]),
            )
    errored_event_count += sum(
        len(events.get(item["subEntity"], {}).get("events", {}).get(item["category"], [])) for item in errors
    )

    if integrate_data:
        integrated_result = integrate_entity(entity_data, reader, mapping_result)
        persisted_path = _write_integrated_result(entity_data, integrated_result, version_hash_dir, ctx=ctx)
        _notify_persisted_mapping(persisted_path, persisted_mapping_callback)
        _log_entity_summary(
            entity_data.entity_name,
            mapped_count=mapped_event_count,
            errored_count=errored_event_count,
            skipped_count=skipped_event_count,
            integrate_data=True,
        )
        return integrated_result

    persisted_path = _write_mapping_result(mapping_result, mapping_data_key, mapping_save_dir, entity_data, ctx=ctx)
    _notify_persisted_mapping(persisted_path, persisted_mapping_callback)
    _log_entity_summary(
        entity_data.entity_name,
        mapped_count=mapped_event_count,
        errored_count=errored_event_count,
        skipped_count=skipped_event_count,
        integrate_data=False,
    )
    return mapping_result


def _resolve_wad_path(entity_data: AudioEntityData, category: str, *, ctx: AppContext) -> Path | None:
    """根据类别选择当前应使用的 WAD 文件。

    Args:
        entity_data: 当前实体数据。
        category: 音频类别名称。
        ctx: 运行时上下文。

    Returns:
        Path | None: 可用的 WAD 文件路径；不可用时返回 ``None``。
    """

    # WAD 选择规则统一收口到 AudioEntityData；
    # mapping 层只负责把“类别名”翻译成模型认识的音频类型。
    audio_type = "VO" if "VO" in category else "SFX"
    wad_path = entity_data.get_wad_path(audio_type, ctx=ctx)
    if wad_path is not None:
        return wad_path

    # 这里保留 mapping 侧原有的日志语义：
    # VO 缺语言包和“路径声明存在但文件缺失”都要继续可见，而不是重构后静默失败。
    if audio_type == "VO" and not entity_data.wad_language:
        logger.warning(f"VO类别但无语言WAD文件: {category}")
        return None

    relative_path = entity_data.wad_language if audio_type == "VO" else entity_data.wad_root
    if relative_path:
        logger.warning(f"WAD文件不存在: {ctx.game_path / relative_path}")
    return None


def _build_category_mapping(  # noqa: PLR0913, PLR0917
    entity_data: AudioEntityData,
    category: str,
    paths_list: list[list[str]],
    event_list: list[str],
    version_cache_dir: Path,
    wwiser_manager: WwiserTool | None,
    runtime_cache: mapping_session.RuntimeCache | None,
    *,
    ctx: AppContext,
) -> tuple[Any | None, int]:
    """构建单个类别的映射结果。

    Args:
        entity_data: 当前实体数据。
        category: 当前类别名称。
        paths_list: 当前类别的路径组合列表。
        event_list: 当前类别的事件列表。
        version_cache_dir: 版本化 cache 目录。
        wwiser_manager: 可选的 wwiser 管理器。
        runtime_cache: 映射流程共享缓存。
        ctx: 运行时上下文。

    Returns:
        tuple[Any | None, int]: 合并后的映射对象与异常路径组数量。
    """

    if len(paths_list) > 1:
        logger.info(f"特殊情况，{entity_data.entity_id} {category} 有 {len(paths_list)} 个路径组合，将逐个处理并合并")

    category_mapping = None
    category_error_count = 0
    hirc_cache_dir = version_cache_dir / "hirc"
    hirc_cache_dir.mkdir(parents=True, exist_ok=True)

    for path_group_idx, path_group in enumerate(paths_list):
        logger.debug(f"处理路径组合 {path_group_idx + 1}/{len(paths_list)}: {path_group}")

        bnk_paths = [path for path in path_group if path.endswith("_events.bnk")]
        if len(bnk_paths) != 1:
            if len(bnk_paths) == 0:
                logger.debug(f"路径组合 {path_group_idx + 1} 无 events.bnk 文件，跳过")
            else:
                logger.warning(f"路径组合 {path_group_idx + 1} 的 events.bnk 文件数量异常: {len(bnk_paths)}")
            continue

        wad_path = _resolve_wad_path(entity_data, category, ctx=ctx)
        if wad_path is None:
            continue

        try:
            wad_obj = mapping_session._get_wad(wad_path, runtime_cache=runtime_cache)
            bnk_rel_path = bnk_paths[0]
            extract_key = (wad_path, bnk_rel_path)
            if not mapping_session._is_bnk_extracted(extract_key, runtime_cache=runtime_cache):
                extract_wad(wad_obj, bnk_paths, out_dir=version_cache_dir)
                mapping_session._mark_bnk_extracted(extract_key, runtime_cache=runtime_cache)

            bnk_path = version_cache_dir / bnk_rel_path
            if not bnk_path.exists():
                logger.warning(f"提取的BNK文件不存在: {bnk_path}")
                continue

            hirc = mapping_session._get_cached_hirc(
                bnk_path=bnk_path,
                hirc_cache_dir=hirc_cache_dir,
                wwiser_manager=wwiser_manager,
                runtime_cache=runtime_cache,
            )
            current_mapping = AudioEventMapper(event_list, hirc).build_mapping()

            if category_mapping is None:
                category_mapping = current_mapping
                logger.debug(f"路径组合 {path_group_idx + 1}: 创建基础映射，事件数: {len(event_list)}")
                continue

            category_mapping.merge_with(current_mapping)
            logger.debug(f"路径组合 {path_group_idx + 1}: 合并映射完成")
        except Exception as exc:  # noqa: BLE001
            category_error_count += 1
            logger.opt(exception=bool(getattr(ctx.config, "dev_mode", False))).warning(
                f"处理路径组合 {path_group_idx + 1} 时出错: {exc}"
            )

    return category_mapping, category_error_count


def _write_integrated_result(
    entity_data: AudioEntityData,
    integrated_result: dict[str, Any],
    version_hash_dir: Path,
    *,
    ctx: AppContext,
) -> Path | None:
    """保存整合结果到文件。

    Args:
        entity_data: 当前实体数据。
        integrated_result: 已整合的完整输出。
        version_hash_dir: 版本化 hash 目录。
        ctx: 运行时上下文。
    """

    if entity_data.entity_type == "champion":
        data_key = "skins"
    elif entity_data.entity_type == "map":
        data_key = "map"
    elif entity_data.entity_type == "resource_pack":
        data_key = "resourcePack"
    else:
        raise ValueError(f"未知的实体类型: {entity_data.entity_type}")
    if not integrated_result or (
        not integrated_result.get("data", {}).get(data_key) and not integrated_result.get("mappingDiagnostics")
    ):
        return None

    entity_group = _entity_group(entity_data.entity_type)
    integration_save_dir = version_hash_dir / "integrated" / entity_group
    integration_save_dir.mkdir(parents=True, exist_ok=True)
    integration_file_base = integration_save_dir / get_entity_path_component(
        entity_data.entity_type,
        entity_data.entity_id,
    )
    path = write_data(integrated_result, integration_file_base, dev_mode=ctx.config.dev_mode)
    logger.debug(f"整合数据已保存: {path}")
    return path


def _write_mapping_result(
    mapping_result: dict[str, Any],
    mapping_data_key: str,
    mapping_save_dir: Path,
    entity_data: AudioEntityData,
    *,
    ctx: AppContext,
) -> Path | None:
    """保存纯 mapping 结果到文件。

    Args:
        mapping_result: 当前映射结果。
        mapping_data_key: 当前实体使用的数据键。
        mapping_save_dir: 目标输出目录。
        entity_data: 当前实体数据。
        ctx: 运行时上下文。
    """

    metadata = mapping_result.get("metadata", {})
    if "languages" in metadata:
        del metadata["languages"]

    if mapping_result[mapping_data_key] or mapping_result.get("mappingDiagnostics"):
        mapping_file_base = mapping_save_dir / get_entity_path_component(
            entity_data.entity_type,
            entity_data.entity_id,
        )
        path = write_data(mapping_result, mapping_file_base, dev_mode=ctx.config.dev_mode)
        logger.debug(f"映射结果已保存: {path}")
        return path

    logger.warning(f"{entity_data.entity_name} 没有找到任何有效映射数据")
    return None


def _notify_persisted_mapping(
    path: Path | None,
    callback: Callable[[Path], None] | None,
) -> None:
    """在映射产物成功落盘后通知调用方。

    Args:
        path: 实际写入的产物路径；无产物时为 ``None``。
        callback: 可选的落盘回调。
    """
    if path is not None and callback is not None:
        callback(path)


def _log_entity_summary(
    entity_name: str,
    *,
    mapped_count: int,
    errored_count: int,
    skipped_count: int,
    integrate_data: bool,
) -> None:
    """输出实体级摘要日志。

    Args:
        entity_name: 实体名称。
        mapped_count: 成功映射事件数。
        errored_count: 异常事件数。
        skipped_count: 未映射跳过事件数。
        integrate_data: 当前是否为整合模式。
    """

    summary_prefix = "整合数据统计" if integrate_data else "事件映射统计"
    summary_message = (
        f"{entity_name} 的{summary_prefix}：成功映射事件 {mapped_count} 个，"
        f"异常事件 {errored_count} 个，未映射跳过 {skipped_count} 个"
    )
    if errored_count > 0:
        if mapped_count > 0 or skipped_count > 0:
            logger.warning(summary_message)
        else:
            logger.error(summary_message)
        return
    logger.success(summary_message)


@logger.catch(reraise=True)
@performance_monitor(level="TRACE")
def build_entity(  # noqa: PLR0913
    entity_data: AudioEntityData,
    reader: DataReader,
    wwiser_manager: WwiserTool | None = None,
    integrate_data: bool = False,
    runtime_cache: mapping_session.RuntimeCache | None = None,
    *,
    ctx: AppContext,
    persisted_mapping_callback: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """构建单个实体的事件映射。

    Args:
        entity_data: 包含 events 的实体数据。
        reader: 数据读取器实例。
        wwiser_manager: 可复用的 wwiser 管理器；为 ``None`` 时按 ``ctx`` 自动选择后端。
        integrate_data: 是否输出整合数据。
        runtime_cache: 映射流程共享缓存，用于复用 WAD/HIRC 解析结果。
        ctx: 运行时上下文。
        persisted_mapping_callback: 映射或整合结果成功落盘后的可选回调。

    Returns:
        dict[str, Any]: 映射结果或整合结果。

    Raises:
        ValueError: 实体没有可用 events 数据时抛出。
    """

    if not entity_data.events and entity_data.binding_diagnostics is None:
        raise ValueError(f"{entity_data.entity_name} 缺少事件数据，请使用 include_events=True 创建实体数据")

    logger.info(f"构建 {entity_data.entity_name} (ID:{entity_data.entity_id}) 的事件映射")
    manager = mapping_session._create_wwiser_manager(ctx) if wwiser_manager is None else wwiser_manager
    if entity_data.binding_diagnostics is not None:
        return _build_bound_entity(
            entity_data,
            reader,
            manager,
            integrate_data,
            runtime_cache,
            ctx=ctx,
            persisted_mapping_callback=persisted_mapping_callback,
        )

    version_cache_dir, version_hash_dir = _ensure_version_dirs(reader, ctx=ctx)
    mapping_result, mapping_data_key = _build_mapping_result(entity_data, reader)
    entity_group = _entity_group(entity_data.entity_type)
    mapping_save_dir = version_hash_dir / entity_group
    mapping_save_dir.mkdir(parents=True, exist_ok=True)

    mapped_event_count = 0
    errored_event_count = 0
    skipped_event_count = 0

    for sub_id, sub_data in entity_data.sub_entities.items():
        banks_data = sub_data["categories"]
        events_data = entity_data.events.get(sub_id, {}).get("events", {})
        if not events_data:
            logger.debug(f"子实体 {sub_id} 无事件数据，跳过")
            continue

        sub_mapping: dict[str, dict[str, list[int]]] = {}
        for category, paths_list in banks_data.items():
            event_list = events_data.get(category, [])
            if not event_list:
                logger.debug(f"类别 {category} 无事件列表，跳过")
                continue

            logger.debug(f"处理类别: {category}")
            category_mapping, category_error_count = _build_category_mapping(
                entity_data,
                category,
                paths_list,
                event_list,
                version_cache_dir,
                manager,
                runtime_cache,
                ctx=ctx,
            )

            if category_mapping is not None and category_mapping.forward_mapping:
                mapped_count = len(category_mapping.forward_mapping)
                skipped_count = max(len(event_list) - mapped_count, 0)
                mapped_event_count += mapped_count
                skipped_event_count += skipped_count
                sub_mapping[category] = category_mapping.forward_mapping
                logger.debug(
                    f"完成 {category} 的映射，处理了 {len(paths_list)} 个路径组合，事件数: {len(event_list)}，"
                    f"映射条目: {mapped_count}，未映射跳过: {skipped_count}"
                )
                continue

            if category_mapping is not None:
                skipped_event_count += len(event_list)
                logger.warning(f"类别 {category} 映射结果为空，跳过保存")
                continue

            if category_error_count > 0:
                errored_event_count += len(event_list)
            else:
                skipped_event_count += len(event_list)
            logger.warning(f"类别 {category} 没有生成任何有效的映射结果")

        if sub_mapping:
            mapping_result[mapping_data_key][sub_id] = {"events": sub_mapping}
            logger.debug(f"子实体 {sub_id} 保存了 {len(sub_mapping)} 个有效类别的映射")
        else:
            logger.debug(f"子实体 {sub_id} 无有效映射数据，跳过保存")

    if integrate_data:
        integrated_result = integrate_entity(entity_data, reader, mapping_result)
        persisted_path = _write_integrated_result(entity_data, integrated_result, version_hash_dir, ctx=ctx)
        _notify_persisted_mapping(persisted_path, persisted_mapping_callback)
        _log_entity_summary(
            entity_data.entity_name,
            mapped_count=mapped_event_count,
            errored_count=errored_event_count,
            skipped_count=skipped_event_count,
            integrate_data=True,
        )
        return integrated_result

    persisted_path = _write_mapping_result(mapping_result, mapping_data_key, mapping_save_dir, entity_data, ctx=ctx)
    _notify_persisted_mapping(persisted_path, persisted_mapping_callback)
    _log_entity_summary(
        entity_data.entity_name,
        mapped_count=mapped_event_count,
        errored_count=errored_event_count,
        skipped_count=skipped_event_count,
        integrate_data=False,
    )
    return mapping_result


def integrate_entity(
    entity_data: AudioEntityData,
    reader: DataReader,
    mapping_result: dict[str, Any],
) -> dict[str, Any]:
    """整合实体数据，将 banks、events 与 mapping 合并为完整结构。

    Args:
        entity_data: 音频实体数据。
        reader: 数据读取器实例。
        mapping_result: 当前实体的映射结果。

    Returns:
        dict[str, Any]: 整合后的完整实体数据。
    """

    logger.info(f"开始整合 {entity_data.entity_name} 的数据")

    resource_metadata: dict[str, Any] | None = None
    if entity_data.entity_type == "champion":
        entity_info = reader.get_champion(int(entity_data.entity_id))
        banks_data = reader.get_champion_banks(int(entity_data.entity_id))
        data_key = "skins"
    elif entity_data.entity_type == "map":
        entity_info = reader.get_map(int(entity_data.entity_id))
        banks_data = reader.get_map_banks(int(entity_data.entity_id))
        data_key = "map"
    elif entity_data.entity_type == "resource_pack":
        entity_info = None
        banks_data = reader.get_resource_pack_banks(entity_data.entity_id, require_bindings=True)
        metadata = banks_data.get("resourcePack") if banks_data else None
        if not isinstance(metadata, dict) or metadata.get("key") != entity_data.entity_id:
            logger.warning(f"无法获取 {entity_data.entity_name} 的 resource-pack metadata")
            return {}
        resource_metadata = metadata
        data_key = "resourcePacks"
    else:
        raise ValueError(f"未知的实体类型: {entity_data.entity_type}")

    if (entity_data.entity_type != "resource_pack" and not entity_info) or (
        not banks_data and entity_data.binding_diagnostics is None
    ):
        logger.warning(f"无法获取 {entity_data.entity_name} 的完整数据信息")
        return {}

    integrated_data = {"metadata": mapping_result.get("metadata", {}), "data": {}}
    for key in ("skinAudioVersion", "sharedAudio"):
        if key in mapping_result:
            integrated_data[key] = mapping_result[key]
    if diagnostics := mapping_result.get("mappingDiagnostics"):
        integrated_data["mappingDiagnostics"] = diagnostics
    wad_info = {"root": entity_data.wad_root}
    if entity_data.wad_language:
        wad_info["language"] = entity_data.wad_language

    if entity_data.entity_type == "champion":
        integrated_data["data"].update(
            {
                "championId": entity_data.entity_id,
                "alias": entity_data.entity_alias,
                "id": entity_info["id"],
                "names": entity_info.get("names", {}),
                "titles": entity_info.get("titles", {}),
                "descriptions": entity_info.get("descriptions", {}),
                "wad": wad_info,
                "skins": [],
                "skinAudio": banks_data.get("skinAudio", {}),
            }
        )
        sub_entities = []
        for skin in entity_info.get("skins", []):
            sub_entities.append(skin)
            sub_entities.extend(
                {**chroma, "skinNames": chroma.get("chromaNames", {})} for chroma in skin.get("chromas", [])
            )
    elif entity_data.entity_type == "map":
        integrated_data["data"].update(
            {
                "mapId": entity_data.entity_id,
                "name": entity_data.entity_alias,
                "id": entity_info["id"],
                "names": entity_info.get("names", {}),
                "descriptions": entity_info.get("descriptions", {}),
                "wad": wad_info,
                "map": {},
            }
        )
        sub_entities = [{"id": int(entity_data.entity_id)}]
    elif entity_data.entity_type == "resource_pack":
        integrated_data["data"].update(
            {
                "resourcePack": {
                    "key": entity_data.entity_id,
                    "name": entity_data.entity_name,
                    "namespace": resource_metadata["namespace"],
                    "wad": wad_info,
                }
            }
        )
        sub_entities = [{"id": entity_data.entity_id}]
    else:
        raise ValueError(f"未知的实体类型: {entity_data.entity_type}")

    mapping_data = mapping_result.get(data_key, {})
    processed_items = []
    bound_groups: dict[tuple[str, str, str, int | None], list[str]] = {}
    for bank in entity_data.resource_banks:
        binding = bank.binding
        if binding.status in SUCCESS_STATUSES:
            key = (bank.sub_id, binding.category, binding.source_bin, binding.group)
            bound_groups.setdefault(key, []).append(binding.path)

    for sub_entity in sub_entities:
        sub_id = str(sub_entity["id"])
        if sub_id not in mapping_data:
            continue

        if entity_data.entity_type == "resource_pack":
            integrated_item = {"key": sub_entity["id"]}
        else:
            integrated_item = {"id": sub_entity["id"]}
        if entity_data.entity_type == "champion":
            integrated_item.update(
                {
                    "isBase": sub_entity.get("isBase", False),
                    "skinNames": sub_entity.get("skinNames", {}),
                    "binPath": sub_entity.get("binPath", ""),
                }
            )
        integrated_item["events"] = {}

        if entity_data.entity_type == "champion":
            sub_banks = banks_data.get("skins", {}).get(sub_id, {})
        elif entity_data.entity_type == "map":
            sub_banks = banks_data.get("banks", {})
        elif entity_data.entity_type == "resource_pack":
            sub_banks = banks_data.get("banks", {})
        else:
            raise ValueError(f"未知的实体类型: {entity_data.entity_type}")
        if entity_data.binding_diagnostics is not None:
            # 旧投影为共享皮肤省略了 banks；整合结果也必须消费精确 binding，
            # 否则已成功继承事件的皮肤会在保存时再次丢失。
            sub_banks = {}
            for (owner, category, _source, _group), paths in bound_groups.items():
                if owner == sub_id:
                    sub_banks.setdefault(category, []).append(paths)
        sub_mapping = mapping_data.get(sub_id, {}).get("events", {})
        sub_audio_paths = mapping_data.get(sub_id, {}).get("audioPaths", {})

        for category in sub_mapping.keys():
            banks_paths = sub_banks.get(category, [])
            mapping_events = sub_mapping.get(category, {})
            if banks_paths and mapping_events:
                integrated_item["events"][category] = {"banks": banks_paths, "mapping": mapping_events}

        if isinstance(mapping_data.get(sub_id, {}).get("audioPaths"), dict):
            integrated_item["audioPaths"] = sub_audio_paths

        if integrated_item["events"]:
            processed_items.append(integrated_item)
            logger.trace(f"皮肤/地图 {sub_id} 整合完成，包含 {len(integrated_item['events'])} 个音频类别")

    if entity_data.entity_type == "champion":
        integrated_data["data"]["skins"] = processed_items
    elif processed_items:
        if entity_data.entity_type == "map":
            integrated_data["data"]["map"] = processed_items[0]
        elif entity_data.entity_type == "resource_pack":
            integrated_data["data"]["resourcePack"].update(processed_items[0])
        else:
            raise ValueError(f"未知的实体类型: {entity_data.entity_type}")

    logger.debug(f"整合完成，{entity_data.entity_name} 包含 {len(processed_items)} 个有效皮肤/地图数据")
    return integrated_data


def build_champion(  # noqa: PLR0913
    champion_id: int,
    reader: DataReader,
    wwiser_manager: WwiserTool | None = None,
    integrate_data: bool = False,
    runtime_cache: mapping_session.RuntimeCache | None = None,
    *,
    ctx: AppContext,
    persisted_mapping_callback: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """构建单个英雄的事件映射。

    Args:
        champion_id: 英雄 ID。
        reader: 数据读取器实例。
        wwiser_manager: 可复用的 wwiser 管理器。
        integrate_data: 是否输出整合数据。
        runtime_cache: 映射流程共享缓存。
        ctx: 运行时上下文。
        persisted_mapping_callback: 映射或整合结果成功落盘后的可选回调。

    Returns:
        dict[str, Any]: 英雄映射结果。

    Raises:
        ValueError: 实体数据无效时抛出，由上层 batch 统一记录并计入失败计数。
    """

    try:
        entity_data = AudioEntityData.from_entity(
            "champion",
            champion_id,
            reader,
            include_events=True,
            ctx=ctx,
        )
        return build_entity(
            entity_data,
            reader,
            wwiser_manager,
            integrate_data,
            runtime_cache=runtime_cache,
            ctx=ctx,
            persisted_mapping_callback=persisted_mapping_callback,
        )
    except ValueError as exc:
        # 显式记录后向上抛出，让 batch 统一计入失败计数，避免失败被静默吞掉返回空字典。
        logger.error(str(exc))
        raise


def build_map(  # noqa: PLR0913
    map_id: int,
    reader: DataReader,
    wwiser_manager: WwiserTool | None = None,
    integrate_data: bool = False,
    runtime_cache: mapping_session.RuntimeCache | None = None,
    *,
    ctx: AppContext,
    persisted_mapping_callback: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """构建单个地图的事件映射。

    Args:
        map_id: 地图 ID。
        reader: 数据读取器实例。
        wwiser_manager: 可复用的 wwiser 管理器。
        integrate_data: 是否输出整合数据。
        runtime_cache: 映射流程共享缓存。
        ctx: 运行时上下文。
        persisted_mapping_callback: 映射或整合结果成功落盘后的可选回调。

    Returns:
        dict[str, Any]: 地图映射结果。

    Raises:
        ValueError: 实体数据无效时抛出，由上层 batch 统一记录并计入失败计数。
    """

    try:
        entity_data = AudioEntityData.from_entity(
            "map",
            map_id,
            reader,
            include_events=True,
            ctx=ctx,
        )
        return build_entity(
            entity_data,
            reader,
            wwiser_manager,
            integrate_data,
            runtime_cache=runtime_cache,
            ctx=ctx,
            persisted_mapping_callback=persisted_mapping_callback,
        )
    except ValueError as exc:
        # 显式记录后向上抛出，让 batch 统一计入失败计数，避免失败被静默吞掉返回空字典。
        logger.error(str(exc))
        raise


def build_resource_pack(  # noqa: PLR0913
    key: str,
    reader: DataReader,
    wwiser_manager: WwiserTool | None = None,
    integrate_data: bool = False,
    runtime_cache: mapping_session.RuntimeCache | None = None,
    *,
    ctx: AppContext,
    persisted_mapping_callback: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """构建单个 resource-pack 的事件映射。

    Args:
        key: canonical resource-pack key。
        reader: 数据读取器实例。
        wwiser_manager: 可复用的 wwiser 管理器。
        integrate_data: 是否输出整合数据。
        runtime_cache: 映射流程共享缓存。
        ctx: 运行时上下文。
        persisted_mapping_callback: 映射或整合结果成功落盘后的可选回调。

    Returns:
        resource-pack 原始或整合映射结果。

    Raises:
        ValueError: resource-pack artifact 或绑定无效时抛出。
    """
    try:
        entity_data = AudioEntityData.from_entity(
            "resource_pack",
            key,
            reader,
            include_events=True,
            ctx=ctx,
        )
        return build_entity(
            entity_data,
            reader,
            wwiser_manager,
            integrate_data,
            runtime_cache=runtime_cache,
            ctx=ctx,
            persisted_mapping_callback=persisted_mapping_callback,
        )
    except ValueError as exc:
        logger.error(str(exc))
        raise
