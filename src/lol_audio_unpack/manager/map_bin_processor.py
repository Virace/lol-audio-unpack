"""地图 BIN 链路处理。

负责按地图ID处理 BIN，提取 banks/events 数据，
并对非公共地图应用公共地图(ID 0)的去重逻辑。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from lol_audio_unpack.manager.bin_source import BinSource, LoadedBin
from lol_audio_unpack.manager.files import needs_update, write_data
from lol_audio_unpack.manager.update_result import UpdateEntityResult
from lol_audio_unpack.model.binding import (
    RESOURCE_SCHEMA_VERSION,
    SUCCESS_STATUSES,
    BankBinding,
    BankReference,
    Completeness,
    ResourceBindings,
    build_diagnostics,
)
from lol_audio_unpack.model.progress import OperationProgress, ProgressEvent
from lol_audio_unpack.utils.logging import performance_monitor
from lol_audio_unpack.utils.run_summary import record_runtime_note

if TYPE_CHECKING:
    from lol_audio_unpack.app.types import AppContext

# 类型别名定义
CommonEventSourceIndex = dict[str, set[str]]


class MapBinProcessor:
    """处理地图 BIN，生成 banks/events 数据并执行公共数据去重。"""

    def __init__(  # noqa: PLR0913
        self,
        bin_source: BinSource,
        *,
        ctx: AppContext,
        version: str,
        force_update: bool,
        process_events: bool,
        map_banks_dir: Path,
        map_events_dir: Path,
        languages: list[str] | None = None,
        progress_callback: Callable[[OperationProgress], None] | None = None,
    ):
        """初始化地图 BIN 处理器。

        :param bin_source: BIN 来源辅助实例。
        :param ctx: 运行时上下文。
        :param version: 当前游戏版本。
        :param force_update: 是否强制更新，忽略版本检查。
        :param process_events: 是否处理事件数据。
        :param map_banks_dir: 地图 banks 输出目录。
        :param map_events_dir: 地图 events 输出目录。
        :param languages: 元数据语言列表，在 update() 中初始化。
        """
        self.bin_source = bin_source
        self.ctx = ctx
        self.version = version
        self.force_update = force_update
        self.process_events = process_events
        self.map_banks_dir = map_banks_dir
        self.map_events_dir = map_events_dir
        self.languages: list[str] = languages if languages is not None else []
        self._progress_callback = progress_callback

    def _is_dev_mode(self) -> bool:
        """返回当前运行是否为开发模式。"""
        return bool(self.ctx.config.dev_mode)

    def _log_simple_progress(self, stage_name: str, index: int, total: int, entity_id: str) -> None:
        """输出简单的批量处理进度日志。"""
        logger.info(f"{stage_name}进度 {index}/{total}: {entity_id}")

    @performance_monitor(level="TRACE")
    def _update_maps(self, data: dict) -> tuple[UpdateEntityResult, ...]:
        """按地图 ID 更新 banks/events，并返回逐实体结果。"""
        self.map_banks_dir.mkdir(parents=True, exist_ok=True)
        self.map_events_dir.mkdir(parents=True, exist_ok=True)

        maps = data.get("maps", {})

        # 预处理公共地图(ID 0)的事件数据和Banks数据
        common_event_sources: CommonEventSourceIndex = {}
        common_banks_set = set()
        pending = [self._needs_update(map_id) for map_id in maps]
        prepare_banks = any(banks for banks, _events in pending)
        prepare_events = any(events for _banks, events in pending)
        if "0" in maps and (prepare_banks or prepare_events):
            logger.debug("正在预处理公共地图(ID 0)的数据...")
            try:
                # 预处理事件数据
                if prepare_events and (map_events := self._process_map_events_for_id("0", maps["0"])):
                    if "events" in map_events:
                        for category, events_list in map_events["events"].items():
                            for event_string in events_list:
                                common_event_sources.setdefault(event_string, set()).add(f"地图 0/{category}")

                # 预处理Banks数据
                if prepare_banks and (map_banks := self._process_map_banks_for_id("0", maps["0"])):
                    if "banks" in map_banks:
                        for paths_list in map_banks["banks"].values():
                            for path in paths_list:
                                common_banks_set.add(tuple(sorted(path)))
            except Exception:
                logger.opt(exception=True).error("预处理公共地图(ID 0)的数据时出错")
                if self._is_dev_mode():
                    raise

        map_items = list(maps.items())
        total_maps = len(map_items)
        self._emit_progress("started", current=0, total=total_maps)
        results: list[UpdateEntityResult] = []
        first_error: BaseException | None = None
        for index, (map_id, map_data) in enumerate(map_items, start=1):
            self._log_simple_progress("处理地图", index, total_maps, map_id)
            try:
                result = self._process_single_map(map_id, map_data, common_event_sources, common_banks_set)
            except Exception as exc:  # noqa: BLE001
                first_error = first_error or exc
                result = UpdateEntityResult.from_error(
                    "map",
                    map_id,
                    exc,
                    entity_name=self._get_map_name(map_data),
                )
            results.append(result)
            self._emit_progress(
                "advanced",
                current=index,
                total=total_maps,
                entity_id=map_id,
            )

        self._emit_progress("finished", current=total_maps, total=total_maps)
        failed_count = sum(result.status.value == "failed" for result in results)
        partial_count = sum(result.status.value == "partial" for result in results)
        if first_error is not None:
            logger.opt(exception=first_error).error(
                "地图 Banks 更新出现未预期异常：总计 {}，失败 {}，部分成功 {}",
                total_maps,
                failed_count,
                partial_count,
            )
        elif failed_count or partial_count:
            logger.warning(
                "地图 Banks 更新未完整：总计 {}，失败 {}，部分成功 {}",
                total_maps,
                failed_count,
                partial_count,
            )
        return tuple(results)

    def _emit_progress(
        self,
        event: ProgressEvent,
        *,
        current: int,
        total: int,
        entity_id: str | None = None,
    ) -> None:
        """发送地图 banks 阶段的结构化进度。"""
        if self._progress_callback is None:
            return
        self._progress_callback(
            OperationProgress(
                operation_key="update",
                stage_key="map_banks",
                event=event,
                current=current,
                total=total,
                entity_type="map" if entity_id is not None else None,
                entity_id=entity_id,
            )
        )

    def _needs_update(self, map_id: str) -> tuple[bool, bool]:
        """返回 banks/events 的更新需求，缓存齐全时不再预读公共地图 BIN。"""
        banks = needs_update(
            self.map_banks_dir / map_id,
            self.version,
            self.force_update,
            dev_mode=self._is_dev_mode(),
            resource_schema=RESOURCE_SCHEMA_VERSION,
        )
        events = self.process_events and needs_update(
            self.map_events_dir / map_id,
            self.version,
            self.force_update,
            dev_mode=self._is_dev_mode(),
        )
        return banks, events

    def _process_single_map(
        self,
        map_id: str,
        map_data: dict,
        common_event_sources: CommonEventSourceIndex | None = None,
        common_banks_set: set | None = None,
    ) -> UpdateEntityResult:
        """处理单个地图的资源与事件缓存。

        Args:
            map_id: 地图 ID。
            map_data: 基础地图数据。
            common_event_sources: 用于事件去重及说明的公共事件来源。
            common_banks_set: 用于旧 banks 投影去重的公共路径集合。

        Returns:
            包含本次解析诊断与已落盘产物的逐实体结果。
        """
        banks_file_base = self.map_banks_dir / map_id
        events_file_base = self.map_events_dir / map_id

        banks_need_update, events_need_update = self._needs_update(map_id)
        if not banks_need_update and not events_need_update:
            logger.trace(f"地图 {map_id} 的数据已是最新，跳过处理")
            return UpdateEntityResult.success("map", map_id, entity_name=self._get_map_name(map_data))

        if not map_data.get("binPath"):
            raise ValueError(f"地图 {map_id} 缺少 BIN 路径，无法准备 banks artifact")

        loaded = self._load_map_resource(map_id, map_data)
        bin_file = loaded.bin_file
        batch = loaded.batch

        references = self._collect_bank_references(bin_file, map_data["binPath"], map_id) if bin_file else []
        bank_bindings = self.bin_source._resolve_bank_bindings(references)
        map_banks = self._binding_map_banks(bank_bindings)

        # 即使只补事件，也必须保留本次 BIN 解析失败，不能沿用旧 banks 的成功状态。
        index_metrics, index_errors = self.bin_source._resource_index_diagnostics()
        diagnostics = build_diagnostics(
            batch.bindings,
            bank_bindings,
            index=index_metrics,
            index_errors=index_errors,
        )
        completeness = diagnostics.completeness
        self._log_binding_summary(f"地图 {map_id}", completeness, diagnostics.to_dict())
        artifacts: list[Path] = []
        # 写入Banks数据
        if banks_need_update:
            map_banks_data = self.bin_source._create_base_data(
                map_id, "map", name=self._get_map_name(map_data), banks=map_banks
            )

            resource = ResourceBindings(
                entity_type="map",
                entity_id=map_id,
                bin_bindings=tuple(batch.bindings),
                bank_bindings=tuple(bank_bindings),
                diagnostics=diagnostics,
            )
            map_banks_data.update(resource.to_payload())

            # 对非公共地图进行去重处理
            if map_id != "0" and common_banks_set:
                self._deduplicate_single_map_banks(map_banks_data, common_banks_set)

            artifacts.append(write_data(map_banks_data, banks_file_base, dev_mode=self._is_dev_mode()))

        # 处理Events数据，只有在启用事件处理时才提取
        if events_need_update and bin_file is not None:
            map_events, dedup_summary = self._extract_map_events(
                bin_file,
                common_event_sources if map_id != "0" else None,
            )
            if dedup_summary:
                self._record_map_event_dedup_summary(map_id, map_data, dedup_summary)
            # 空结果也证明当前版本已解析，不能让“没有事件”永远被当成“尚未准备”。
            final_event_data = self.bin_source._create_base_data(
                map_id, "map", name=self._get_map_name(map_data), map=map_events or {}
            )
            artifacts.append(write_data(final_event_data, events_file_base, dev_mode=self._is_dev_mode()))

        if completeness is Completeness.FAILED:
            return UpdateEntityResult.incomplete(
                "map",
                map_id,
                entity_name=self._get_map_name(map_data),
                message="resource bindings 未形成可用结果",
                failed=True,
                artifacts=tuple(artifacts),
            )
        if completeness is Completeness.PARTIAL:
            return UpdateEntityResult.incomplete(
                "map",
                map_id,
                entity_name=self._get_map_name(map_data),
                message="部分 BIN 或 bank binding 未能解析",
                artifacts=tuple(artifacts),
            )
        return UpdateEntityResult.success(
            "map",
            map_id,
            entity_name=self._get_map_name(map_data),
            artifacts=tuple(artifacts),
        )

    def _load_map_resource(self, map_id: str, map_data: dict) -> LoadedBin:
        """通过本地 FINAL WAD 索引加载地图 BIN。"""
        return self.bin_source._load_map_bin_resource(map_id, map_data)

    @staticmethod
    def _collect_bank_references(bin_file, source_bin: str, map_id: str) -> list[BankReference]:
        """从地图 BIN 收集保留 group 边界的 bank 声明。"""
        references: list[BankReference] = []
        group_index = 0
        for group in bin_file.data:
            for event_data in group.bank_units:
                if not event_data.bank_path:
                    continue
                references.extend(
                    BankReference(
                        category=event_data.category,
                        path=path,
                        source_bin=source_bin,
                        sub_entity=map_id,
                        group=group_index,
                    )
                    for path in event_data.bank_path
                )
                group_index += 1
        return references

    @staticmethod
    def _binding_map_banks(bindings: list[BankBinding]) -> dict[str, list[list[str]]]:
        """只从成功 local v2 bindings 派生 map banks 投影。"""
        groups: dict[tuple[str, str, int | None], list[str]] = {}
        for binding in bindings:
            if binding.status not in SUCCESS_STATUSES:
                continue
            key = (binding.category, binding.source_bin, binding.group)
            groups.setdefault(key, []).append(binding.path)
        return MapBinProcessor._group_map_banks(groups)

    @staticmethod
    def _group_map_banks(
        groups: dict[tuple[str, str, int | None], list[str]],
    ) -> dict[str, list[list[str]]]:
        """按 category 去重 bank path groups，保持既有 artifact 形状。"""
        banks: dict[str, list[list[str]]] = {}
        for (category, _source, _group), paths in groups.items():
            fingerprint = tuple(sorted(paths))
            category_paths = banks.setdefault(category, [])
            if fingerprint not in {tuple(sorted(existing)) for existing in category_paths}:
                category_paths.append(paths)
        return banks

    @staticmethod
    def _log_binding_summary(label: str, completeness: Completeness, diagnostics: dict) -> None:
        """为 partial/failed binding artifact 输出可观察摘要。"""
        if completeness is Completeness.COMPLETE:
            return
        message = (
            f"{label} 资源绑定结果为 {completeness.value}: "
            f"unresolved BIN={len(diagnostics['unresolvedBins'])}, "
            f"unresolved bank={len(diagnostics['unresolvedBanks'])}"
        )
        if completeness is Completeness.FAILED:
            logger.error(message)
        else:
            logger.warning(message)

    def _extract_map_events(
        self,
        bin_file,
        common_event_sources: CommonEventSourceIndex | None = None,
    ) -> tuple[dict | None, dict[str, Any] | None]:
        """
        从BIN文件中提取并根据公共事件集合进行去重

        :param bin_file: BIN文件对象
        :param common_event_sources: 公共事件来源索引，用于去重和记录来源
        :returns: 地图事件数据字典，以及公共事件去重摘要
        """
        map_events = {}
        dedup_by_category: dict[str, dict[str, set[str]]] = {}
        if bin_file.theme_music:
            map_events["theme_music"] = bin_file.theme_music

        events_by_category = {}
        for group in bin_file.data:
            if group.music:
                map_events["music"] = group.music.to_dict()
            for event_data in group.bank_units:
                if not event_data.events:
                    continue

                category = event_data.category
                event_strings = [e.string for e in event_data.events]
                unique_events = list(dict.fromkeys(event_strings))  # 保持顺序的去重

                removed_events: list[str] = []
                if common_event_sources:
                    filtered_events: list[str] = []
                    for event_name in unique_events:
                        if event_name in common_event_sources:
                            removed_events.append(event_name)
                        else:
                            filtered_events.append(event_name)
                    unique_events = filtered_events

                if removed_events:
                    category_entry = dedup_by_category.setdefault(category, {"events": set(), "sources": set()})
                    category_entry["events"].update(removed_events)
                    for event_name in removed_events:
                        category_entry["sources"].update(common_event_sources.get(event_name, set()))

                if unique_events:
                    if category not in events_by_category:
                        events_by_category[category] = []
                    events_by_category[category].extend(unique_events)

        dedup_summary = None
        if dedup_by_category:
            categories = {}
            total_removed = 0
            for category, payload in dedup_by_category.items():
                removed_event_names = sorted(payload["events"])
                total_removed += len(removed_event_names)
                categories[category] = {
                    "count": len(removed_event_names),
                    "examples": removed_event_names[:5],
                    "sources": sorted(payload["sources"]),
                }
            dedup_summary = {
                "total_removed": total_removed,
                "remaining_event_count": sum(len(events) for events in events_by_category.values()),
                "categories": categories,
            }

        if events_by_category:
            map_events["events"] = events_by_category

        return map_events if map_events else None, dedup_summary

    def _deduplicate_single_map_banks(self, map_data: dict, common_banks_set: set) -> None:
        """
        对单个地图的Banks进行去重处理，移除与公共地图(ID 0)重复的bank path

        :param map_data: 单个地图的完整数据（包含metadata和banks）
        :param common_banks_set: 公共地图的bank path集合（元组形式）
        """
        if "banks" not in map_data:
            return

        bank_paths = map_data["banks"]
        map_id = map_data.get("mapId", "unknown")

        # 记录去重前的统计信息
        original_categories = len(bank_paths)
        original_paths_count = sum(len(paths_list) for paths_list in bank_paths.values())

        # 遍历每个category，移除与公共数据重复的bank path
        categories_to_remove = []
        for category, paths_list in bank_paths.items():
            # 筛选出当前地图独有的、非公共的bank path
            unique_to_map = [path for path in paths_list if tuple(sorted(path)) not in common_banks_set]

            if unique_to_map:
                bank_paths[category] = unique_to_map
            else:
                # 如果该category下所有数据都是公共的，标记为待移除
                categories_to_remove.append(category)

        # 移除完全重复的categories
        for category in categories_to_remove:
            del bank_paths[category]

        # 记录去重后的统计信息
        remaining_categories = len(bank_paths)
        remaining_paths_count = sum(len(paths_list) for paths_list in bank_paths.values())

        logger.trace(
            f"地图 {map_id} Banks去重完成: "
            f"分类 {original_categories}→{remaining_categories}, "
            f"路径 {original_paths_count}→{remaining_paths_count}"
        )

    def _process_map_events_for_id(
        self, map_id: str, map_data: dict, common_event_sources: CommonEventSourceIndex | None = None
    ) -> dict | None:
        """
        提取、去重并保存单个地图的事件数据（兼容性方法）

        :param map_id: 地图ID
        :param map_data: 地图数据字典
        :param common_event_sources: 公共事件来源索引，用于去重
        :returns: 地图事件数据字典，失败时返回None
        """
        # 如果未启用事件处理，直接返回None
        if not self.process_events:
            return None

        if not map_data.get("binPath"):
            return None

        bin_file = self.bin_source._load_map_bin_file(map_id, map_data)
        if bin_file is None:
            return None

        return self._extract_map_events(bin_file, common_event_sources)[0]

    def _record_map_event_dedup_summary(self, map_id: str, map_data: dict, dedup_summary: dict[str, Any]) -> None:
        """记录地图公共事件去重造成的可解释差异。"""
        total_removed = int(dedup_summary.get("total_removed", 0))
        if total_removed <= 0:
            return

        map_name = self._get_map_name(map_data)
        remaining_event_count = int(dedup_summary.get("remaining_event_count", 0))
        source_labels = sorted(
            {
                source.split("/", 1)[0]
                for payload in dedup_summary.get("categories", {}).values()
                for source in payload.get("sources", [])
            }
        )
        source_text = "、".join(source_labels) if source_labels else "公共地图"

        if remaining_event_count == 0:
            message = (
                f"地图 {map_id} ({map_name}) 的事件在与 {source_text} 的公共事件去重后为空，"
                f"共省略 {total_removed} 个事件；单独处理该地图时结果可能不同。"
            )
        else:
            message = (
                f"地图 {map_id} ({map_name}) 有 {total_removed} 个事件因与 {source_text} 的公共事件去重被省略，"
                f"当前仍保留 {remaining_event_count} 个事件。"
            )

        detail_lines = []
        for category, payload in sorted(dedup_summary.get("categories", {}).items()):
            examples = ", ".join(payload.get("examples", [])) or "-"
            sources = ", ".join(payload.get("sources", [])) or "-"
            detail_lines.append(
                f"category={category}, removed={payload.get('count', 0)}, sources=[{sources}], examples=[{examples}]"
            )

        detail = "\n".join(detail_lines)
        record_runtime_note(self.ctx.runtime_cache, "update", message, label="数据更新", detail=detail)
        for line in detail_lines:
            logger.trace(f"地图 {map_id} 公共事件去重明细: {line}")

    def _process_map_banks_for_id(self, map_id: str, map_data: dict) -> dict | None:
        """
        提取单个地图的Banks数据（用于预处理公共地图数据）

        :param map_id: 地图ID
        :param map_data: 地图数据字典
        :returns: 地图Banks数据字典，失败时返回None
        """
        if not map_data.get("binPath"):
            return None

        bin_file = self.bin_source._load_map_bin_file(map_id, map_data)
        if bin_file is None:
            return None

        # 处理Banks数据
        map_banks = {}
        for group in bin_file.data:
            for event_data in group.bank_units:
                if event_data.bank_path:
                    category = event_data.category
                    if category not in map_banks:
                        map_banks[category] = []
                    map_banks[category].append(event_data.bank_path)

        # 去重处理
        for category, paths in map_banks.items():
            unique_paths_tuples = dict.fromkeys(tuple(sorted(p)) for p in paths)
            map_banks[category] = [list(p) for p in unique_paths_tuples]

        if map_banks:
            return {"banks": map_banks}
        return None

    def _get_map_name(self, map_data: dict) -> str:
        """
        获取地图名称，优先使用当前语言，回退到默认语言

        :param map_data: 地图数据
        :returns: 地图名称
        """
        names = map_data.get("names", {})
        if not names:
            return map_data.get("mapStringId", "")

        # 如果有多语言支持，尝试获取当前语言的名称
        for lang in self.languages:
            if lang in names:
                return names[lang]

        # 回退到默认语言
        return names.get("default", map_data.get("mapStringId", ""))
