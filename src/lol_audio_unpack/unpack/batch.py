"""批量解包任务编排。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from league_tools.formats import WAD
from loguru import logger

from lol_audio_unpack.app.library import with_library
from lol_audio_unpack.app.results import EntityResult, FailureDetail, ResultStatus, StageResult
from lol_audio_unpack.manager import DataReader
from lol_audio_unpack.model import AudioEntityData, generate_champion_tasks, generate_map_tasks
from lol_audio_unpack.model.binding import SUCCESS_STATUSES, BindingDiagnostics, Completeness

from .entity import unpack_champion, unpack_entity, unpack_map, unpack_resource_pack
from .stats import EntityUnpackStats
from .stats import StageResult as UnpackStageResult

if TYPE_CHECKING:
    from lol_audio_unpack.app.types import AppContext


_ENTITY_TYPES = frozenset({"champion", "map", "resource_pack"})


def _validate_tasks(tasks: list[tuple[str, int | str, str]]) -> ValueError | None:
    """在提交工作线程前确认任务属于当前解包阶段。"""
    for task in tasks:
        try:
            entity_type, _, _ = task
        except (TypeError, ValueError):
            return ValueError("解包任务必须包含实体类型、ID 和说明")
        if entity_type not in _ENTITY_TYPES:
            return ValueError(f"未知的实体类型: {entity_type}")
    return None


def _result_from_stats(
    entity_type: str,
    entity_id: int | str,
    description: str,
    stats: EntityUnpackStats | None,
    *,
    artifacts: tuple[str, ...],
) -> EntityResult:
    """把保留旧报告语义的实体统计适配为公共结果。"""
    if stats is None:
        raise TypeError("解包实体未返回统计结果")

    status_map = {
        UnpackStageResult.SUCCESS: ResultStatus.SUCCESS,
        UnpackStageResult.WARNING: ResultStatus.PARTIAL,
        UnpackStageResult.ERROR: ResultStatus.FAILED,
        UnpackStageResult.SKIPPED: ResultStatus.SUCCESS,
    }
    status = status_map[stats.overall_result]
    if status is ResultStatus.SUCCESS:
        return EntityResult(
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=description,
            status=status,
            artifacts=artifacts,
        )

    failures = tuple(stats.file_failures) + tuple(
        FailureDetail(
            unit="container",
            source_path=str(item.get("path", "")),
            error_message=str(item.get("error", "原因未分类")),
            sub_entity=str(item.get("subEntity", "")),
            audio_type=str(item.get("audioType", "")),
            category=str(item.get("category", "")),
            wad=str(item.get("wad") or ""),
            entry_hash=str(item.get("entryHash", "")),
            retryable=item.get("outcome") == "failed" and bool(item.get("wad") and item.get("entryHash")),
        )
        for item in stats.binding_details
        if item.get("outcome") in {"failed", "unresolved"}
    )
    return EntityResult(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=description,
        status=status,
        error_type=f"Unpack{stats.overall_result.name.title()}",
        error_message=stats.get_simple_summary(),
        artifacts=artifacts,
        failures=failures,
    )


def _describe_entity_result(result: EntityResult) -> tuple[str, str]:
    """返回实体结果对应的进度文案与日志等级。"""
    if result.status is ResultStatus.SUCCESS:
        return f"{result.entity_name} 解包完成", "info"
    if result.status is ResultStatus.PARTIAL:
        return f"{result.entity_name} 解包部分完成", "warning"
    return f"{result.entity_name} 解包失败", "warning"


@with_library
def execute_tasks(  # noqa: PLR0913
    tasks: list[tuple[str, int | str, str]],
    reader: DataReader,
    max_workers: int = 4,
    *,
    ctx: AppContext,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
    persisted_wem_callback: Callable[[Path], None] | None = None,
    retry_entities: tuple[EntityResult, ...] = (),
) -> StageResult:
    """执行批量解包任务。

    Args:
        tasks: 任务元组列表 ``[(entity_type, id, description), ...]``。
        reader: 数据读取器实例。
        max_workers: 最大并发线程数。
        ctx: 运行时上下文。
        progress_callback: 每个实体处理结束后的可选进度回调。
        persisted_wem_callback: WEM 落盘后的附加回调。
        retry_entities: 已核对的失败实体及其精确容器或文件范围。

    Returns:
        按输入任务顺序保存实体事实的解包阶段结果。
    """
    if not tasks:
        return StageResult.from_entities("extract", (), note="没有任何任务需要执行")

    if error := _validate_tasks(tasks):
        logger.error(str(error))
        return StageResult.from_error("extract", error)

    start_time = time.time()
    total_tasks = len(tasks)
    champion_count = sum(1 for entity_type, _, _ in tasks if entity_type == "champion")
    map_count = sum(1 for entity_type, _, _ in tasks if entity_type == "map")
    resource_pack_count = sum(1 for entity_type, _, _ in tasks if entity_type == "resource_pack")
    summary_parts = []
    if champion_count > 0:
        summary_parts.append(f"{champion_count} 个英雄")
    if map_count > 0:
        summary_parts.append(f"{map_count} 个地图")
    if resource_pack_count > 0:
        summary_parts.append(f"{resource_pack_count} 个资源包")
    totals_by_type = {
        "champion": champion_count,
        "map": map_count,
        "resource_pack": resource_pack_count,
    }
    finished_by_type = {
        "champion": 0,
        "map": 0,
        "resource_pack": 0,
    }
    progress_lock = threading.Lock() if max_workers > 1 else None

    logger.info(
        f"开始解包 {total_tasks} 个实体 ({' 和 '.join(summary_parts)})，"
        f"模式: {'多线程' if max_workers > 1 else '单线程'} (workers: {max_workers})"
    )

    show_exception = bool(getattr(ctx.config, "dev_mode", False))

    # 解包阶段的 WAD 缓存以整轮 batch 为单位共享，
    # 这样同一个实体/多个实体命中同一 WAD 时都不会重复打开文件句柄。
    wad_cache: dict[Path, WAD] = {}
    cache_lock = threading.Lock() if max_workers > 1 else None
    artifact_lock = threading.Lock()
    artifact_paths: list[list[str]] = [[] for _ in tasks]

    def capture_artifact(index: int, *, forward_wem: bool) -> Callable[[Path], None]:
        """记录实体的真实音频路径，并按需转发既有 WEM 回调。"""

        def _capture(path: Path) -> None:
            # WEM 回调在工作线程触发；同一锁同时保护 artifact 与既有回调的共享消费边界。
            with artifact_lock:
                artifact_paths[index].append(str(path))
                if forward_wem and persisted_wem_callback is not None:
                    persisted_wem_callback(path)

        return _capture

    def get_artifacts(index: int) -> tuple[str, ...]:
        """返回当前实体已确认落盘的 WEM 路径快照。"""
        with artifact_lock:
            return tuple(artifact_paths[index])

    def unpack_one(index: int, entity_type: str, entity_id: int | str) -> EntityUnpackStats | None:
        common_kwargs: dict[str, object] = {
            "wad_cache": wad_cache,
            "cache_lock": cache_lock,
            "ctx": ctx,
            "persisted_wem_callback": capture_artifact(index, forward_wem=True),
        }
        # 多实体优先沿用实体并发，少量实体才把空余配额分给文件写出；
        # 避免每个实体再各自启动 max_workers 个写线程。
        write_workers = max_workers // min(total_tasks, max_workers) if max_workers > 1 else 1
        if write_workers > 1:
            common_kwargs["max_workers"] = write_workers
        if retry_entities:
            previous = next(
                item
                for item in retry_entities
                if item.entity_type == entity_type and str(item.entity_id) == str(entity_id)
            )
            entity_data = AudioEntityData.from_entity(entity_type, entity_id, reader, ctx=ctx)
            keys = {failure.binding_key for failure in previous.failures if failure.retryable}
            banks = tuple(
                bank
                for bank in entity_data.resource_banks
                if (
                    bank.sub_id,
                    bank.audio_type,
                    bank.binding.category,
                    bank.binding.wad or "",
                    bank.binding.entry_hash,
                )
                in keys
                and bank.binding.status in SUCCESS_STATUSES
            )
            if len(
                {
                    (
                        bank.sub_id,
                        bank.audio_type,
                        bank.binding.category,
                        bank.binding.wad or "",
                        bank.binding.entry_hash,
                    )
                    for bank in banks
                }
            ) != len(keys):
                raise ValueError("原失败容器的 binding 已变化或不可用，请重新生成数据后确认范围")
            if not banks:
                raise ValueError("没有可精确重试的失败容器")
            # 只消费批准的绑定；原实体中无关的未解析 BIN 不扩大这次重试。
            selected = replace(
                entity_data, resource_banks=banks, binding_diagnostics=BindingDiagnostics(Completeness.COMPLETE)
            )
            file_limits = {
                key: frozenset(
                    Path(item.output_path)
                    for item in previous.failures
                    if item.binding_key == key and item.unit == "file"
                )
                for key in keys
                if not any(item.binding_key == key and item.unit == "container" for item in previous.failures)
            }
            logger.info(
                "按失败范围重试 {}：{} 个容器，{} 个精确文件",
                previous.entity_name,
                len(keys),
                sum(len(paths) for paths in file_limits.values()),
            )
            return unpack_entity(selected, reader, **common_kwargs, file_limits=file_limits)
        if entity_type == "champion":
            return unpack_champion(
                entity_id,
                reader,
                **common_kwargs,
                persisted_artifact_callback=capture_artifact(index, forward_wem=False),
            )
        if entity_type == "map":
            return unpack_map(entity_id, reader, **common_kwargs)
        if entity_type == "resource_pack":
            return unpack_resource_pack(str(entity_id), reader, **common_kwargs)
        raise AssertionError(f"未经验证的实体类型: {entity_type}")

    def emit_running_progress(entity_type: str, description: str) -> None:
        if progress_callback is None:
            return

        def _emit() -> None:
            progress_callback(
                entity_type,
                finished_by_type.get(entity_type, 0),
                max(totals_by_type.get(entity_type, total_tasks), 1),
                f"正在处理: {description}",
            )

        if progress_lock is None:
            _emit()
            return

        with progress_lock:
            _emit()

    def unpack_one_with_progress(
        index: int,
        entity_type: str,
        entity_id: int | str,
        description: str,
    ) -> EntityUnpackStats | None:
        emit_running_progress(entity_type, description)
        return unpack_one(index, entity_type, entity_id)

    entity_results: list[EntityResult | None] = [None] * total_tasks

    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_index = {
                executor.submit(unpack_one_with_progress, index, entity_type, entity_id, description): index
                for index, (entity_type, entity_id, description) in enumerate(tasks)
            }
            finished_count = 0
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                entity_type, entity_id, description = tasks[index]
                finished_count += 1
                finished_by_type[entity_type] = finished_by_type.get(entity_type, 0) + 1

                try:
                    stats = future.result()
                    entity_results[index] = _result_from_stats(
                        entity_type,
                        entity_id,
                        description,
                        stats,
                        artifacts=get_artifacts(index),
                    )
                    progress_message, log_level = _describe_entity_result(entity_results[index])
                    getattr(logger, log_level)(f"进度: {finished_count}/{total_tasks} - {progress_message}。")
                except Exception as exc:  # noqa: BLE001
                    entity_results[index] = EntityResult.from_error(
                        entity_type,
                        entity_id,
                        exc,
                        entity_name=description,
                        artifacts=get_artifacts(index),
                    )
                    progress_message = f"{description} 解包失败"
                    logger.opt(exception=show_exception).warning(f"{description} 解包失败，将继续后续任务: {exc}")

                if progress_callback is not None:
                    progress_callback(
                        entity_type,
                        finished_by_type.get(entity_type, finished_count),
                        max(totals_by_type.get(entity_type, total_tasks), 1),
                        progress_message,
                    )
    else:
        finished_count = 0
        for index, (entity_type, entity_id, description) in enumerate(tasks):
            try:
                emit_running_progress(entity_type, description)
                stats = unpack_one(index, entity_type, entity_id)
                entity_results[index] = _result_from_stats(
                    entity_type,
                    entity_id,
                    description,
                    stats,
                    artifacts=get_artifacts(index),
                )
                progress_message, log_level = _describe_entity_result(entity_results[index])
                getattr(logger, log_level)(f"进度: {finished_count + 1}/{total_tasks} - {progress_message}。")
            except Exception as exc:  # noqa: BLE001
                entity_results[index] = EntityResult.from_error(
                    entity_type,
                    entity_id,
                    exc,
                    entity_name=description,
                    artifacts=get_artifacts(index),
                )
                progress_message = f"{description} 解包失败"
                logger.opt(exception=show_exception).warning(f"{description} 解包失败，将继续后续任务: {exc}")

            finished_count += 1
            finished_by_type[entity_type] = finished_by_type.get(entity_type, 0) + 1
            if progress_callback is not None:
                progress_callback(
                    entity_type,
                    finished_by_type.get(entity_type, finished_count),
                    max(totals_by_type.get(entity_type, total_tasks), 1),
                    progress_message,
                )

    if any(entity_result is None for entity_result in entity_results):
        raise RuntimeError("解包批处理没有为全部任务生成实体结果")
    results = tuple(entity_result for entity_result in entity_results if entity_result is not None)
    result = StageResult.from_entities("extract", results)
    reader.write_unknown_categories()

    end_time = time.time()
    summary_message = (
        f"解包结果: {' 和 '.join(summary_parts)}，"
        f"成功 {result.success_count} 个，失败 {result.failed_count} 个，部分完成 {result.partial_count} 个，"
        f"耗时 {end_time - start_time:.2f}s"
    )
    if result.status is ResultStatus.FAILED:
        logger.error(summary_message)
    elif result.status is ResultStatus.PARTIAL:
        logger.warning(summary_message)
    else:
        logger.debug(summary_message)

    return result


def unpack_all(  # noqa: PLR0913
    reader: DataReader,
    max_workers: int = 4,
    *,
    include_champions: bool = True,
    include_maps: bool = True,
    ctx: AppContext,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
    persisted_wem_callback: Callable[[Path], None] | None = None,
) -> StageResult:
    """解包全部实体音频。

    Returns:
        解包阶段的聚合结果。
    """
    champion_tasks = generate_champion_tasks(reader) if include_champions else []
    map_tasks = generate_map_tasks(reader) if include_maps else []
    return execute_tasks(
        champion_tasks + map_tasks,
        reader,
        max_workers=max_workers,
        ctx=ctx,
        progress_callback=progress_callback,
        persisted_wem_callback=persisted_wem_callback,
    )


def unpack_champions(  # noqa: PLR0913
    reader: DataReader,
    champion_ids: list[int] | None = None,
    max_workers: int = 4,
    *,
    ctx: AppContext,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
    persisted_wem_callback: Callable[[Path], None] | None = None,
) -> StageResult:
    """解包指定英雄音频。

    Returns:
        解包阶段的聚合结果。
    """
    tasks = generate_champion_tasks(reader, champion_ids)
    return execute_tasks(
        tasks,
        reader,
        max_workers=max_workers,
        ctx=ctx,
        progress_callback=progress_callback,
        persisted_wem_callback=persisted_wem_callback,
    )


def unpack_maps(  # noqa: PLR0913
    reader: DataReader,
    map_ids: list[int] | None = None,
    max_workers: int = 4,
    *,
    ctx: AppContext,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
    persisted_wem_callback: Callable[[Path], None] | None = None,
) -> StageResult:
    """解包指定地图音频。

    Returns:
        解包阶段的聚合结果。
    """
    tasks = generate_map_tasks(reader, map_ids)
    return execute_tasks(
        tasks,
        reader,
        max_workers=max_workers,
        ctx=ctx,
        progress_callback=progress_callback,
        persisted_wem_callback=persisted_wem_callback,
    )


def unpack_resource_packs(  # noqa: PLR0913
    reader: DataReader,
    keys: list[str],
    max_workers: int = 4,
    *,
    ctx: AppContext,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
    persisted_wem_callback: Callable[[Path], None] | None = None,
) -> StageResult:
    """解包指定 resource-pack 音频。

    Args:
        reader: 数据读取器实例。
        keys: canonical resource-pack key 列表。
        max_workers: 最大工作线程数。
        ctx: 运行时上下文。
        progress_callback: 每个实体完成后的可选进度回调。
        persisted_wem_callback: WEM 落盘后的附加回调。

    Returns:
        解包阶段的聚合结果。
    """
    tasks = [("resource_pack", key, f"资源包 {key}") for key in keys]
    return execute_tasks(
        tasks,
        reader,
        max_workers=max_workers,
        ctx=ctx,
        progress_callback=progress_callback,
        persisted_wem_callback=persisted_wem_callback,
    )
