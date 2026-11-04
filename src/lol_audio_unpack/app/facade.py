"""模块化应用门面。

该模块为 CLI 与外部模块调用提供统一编排入口，避免直接操作底层
Manager 与流程函数。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Lock

from loguru import logger

from lol_audio_unpack.app.library import library_session
from lol_audio_unpack.manager import (
    BinUpdater,
    DataReader,
    DataUpdater,
    ResourcePackDiscovery,
    ResourcePackDiscoveryResult,
)
from lol_audio_unpack.manager.errors import SharedDataNotReadyError
from lol_audio_unpack.manager.update_result import UpdateEntityResult, UpdateStatus
from lol_audio_unpack.mapping import (
    build_all,
    build_champions,
    build_maps,
    build_resource_packs,
    describe_hirc_backend,
)
from lol_audio_unpack.model import AudioEntityData
from lol_audio_unpack.model.progress import OperationProgress
from lol_audio_unpack.runtime.probe import require_tool
from lol_audio_unpack.runtime.wav import TranscodeTarget, run_tree
from lol_audio_unpack.runtime.wav.event_output import EVENT_SESSION_KEY, EventOutput
from lol_audio_unpack.unpack import unpack_all, unpack_champions, unpack_maps, unpack_resource_packs

from .artifacts import resolve_audio_paths
from .preflight import SourcePreflightError, check_source_files
from .resource_pack import partition_special_targets
from .results import EntityResult, ResultStatus, StageResult
from .source_info import log_source_info
from .special_content import merge_champion_ids
from .targets import TargetSelectionError, check_ids, with_common_map
from .types import AppContext, OperationOptions

UPDATE_PREPARED_KEY = "update_data_prepared_force"
EXPECTED_STAGE_ERRORS = (
    TargetSelectionError,
    SourcePreflightError,
    SharedDataNotReadyError,
    OSError,
)


class LolAudioUnpackApp:
    """统一业务编排门面。"""

    def __init__(self, ctx: AppContext):
        """初始化门面。

        Args:
            ctx: 应用运行上下文。
        """
        self.ctx = ctx
        self._reader: DataReader | None = None
        self._reader_lock = Lock()

    @staticmethod
    def _to_str_ids(ids: tuple[int, ...] | None) -> list[str] | None:
        """将整数 ID 元组转换为字符串列表。"""
        if ids is None:
            return None
        return [str(item) for item in ids]

    def _get_reader(self) -> DataReader:
        """懒加载并复用当前 app/context 的数据读取器。"""
        reader = self._reader
        if reader is not None:
            return reader
        with self._reader_lock:
            if self._reader is None:
                self._reader = DataReader(ctx=self.ctx)
            return self._reader

    def _reset_reader(self) -> None:
        """失效当前 reader，使下一次读取重新加载最新 artifact。"""
        with self._reader_lock:
            self._reader = None

    def check_targets(self, opts: OperationOptions) -> None:
        """依据已准备的实体目录严格校验 ID，不读取 WAD 内容。

        Args:
            opts: 本次显式目标；None 与空集合无需存在性校验。

        Raises:
            TargetSelectionError: 至少一个英雄或地图 ID 不存在。
        """
        champions = merge_champion_ids(
            opts.champion_ids, partition_special_targets(opts.special_targets).champion_targets
        )
        errors = []
        for ids, getter, label in (
            (champions, "get_champions", "英雄"),
            (opts.map_ids, "get_maps", "地图"),
        ):
            if not ids:
                continue
            result = check_ids(ids, getattr(self._get_reader(), getter)())
            if result.unknown:
                errors.append(f"{label} ID：{', '.join(map(str, result.unknown))}")
        if errors:
            raise TargetSelectionError("未找到" + "；".join(errors) + "。请修正后重试。")

    def _resolve_operation_options(self, opts: OperationOptions) -> OperationOptions:
        """分区 special target，避免 resource pack 进入数值英雄归约。"""
        for wad_ref in opts.resource_pack_wads:
            # 任务可能在文件选择后排队较久；所有执行入口都必须重新验证 FINAL containment 与 stat。
            wad_ref.resolve(Path(self.ctx.config.game_path))
        partition = partition_special_targets(opts.special_targets)
        return replace(
            opts,
            champion_ids=merge_champion_ids(opts.champion_ids, partition.champion_targets),
        )

    @staticmethod
    def _has_resource_pack_targets(opts: OperationOptions) -> bool:
        """判断当前操作是否含 resource-pack 发现或消费范围。"""
        return bool(opts.resource_pack_wads or LolAudioUnpackApp._resource_pack_targets(opts))

    @staticmethod
    def _resource_pack_targets(opts: OperationOptions) -> tuple[str, ...]:
        """返回 special target 中的 canonical resource-pack key。"""
        return partition_special_targets(opts.special_targets).resource_pack_targets

    def _describe_mapping_backend(self) -> str:
        """返回 mapping 流程使用的 HIRC 后端。"""
        return describe_hirc_backend(self.ctx)

    def _check_source(
        self, opts: OperationOptions, *, include_champions: bool = True, include_maps: bool = True
    ) -> None:
        """重新检查本次标准实体与显式资源包，失败发生在任何源处理写入之前。"""
        # 文件日志已就绪时补记实际源；独立 WAV 等无源任务不依赖客户端配置。
        log_source_info(self.ctx.config.game_path, self.ctx.game_region, level="DEBUG")
        wads = [ref.identity for ref in opts.resource_pack_wads]
        for key in self._resource_pack_targets(opts):
            payload = self._get_reader().get_resource_pack_banks(key)
            source = (payload or {}).get("resourcePack", {}).get("source", {})
            if not source.get("wad"):
                raise SourcePreflightError(f"资源包 {key} 缺少物理来源，请重新选择 WAD")
            wads.append(source["wad"])
        check_source_files(
            self.ctx,
            champion_ids=opts.champion_ids,
            map_ids=opts.map_ids,
            include_champions=include_champions,
            include_maps=include_maps,
            resource_wads=wads,
            resource_only=self._has_resource_pack_targets(opts) and opts.champion_ids is None and opts.map_ids is None,
        )

    def check_tools(self, opts: OperationOptions, *, mapping: bool = False) -> None:
        """为直接 API 调用检查所需后端，已预检的任务快照无需重复探测。"""
        if self.ctx.runtime_cache.get("tools_prechecked"):
            return
        if opts.wav_output.enabled:
            path = str(self.ctx.config.vgmstream_path or "") or opts.wav_output.backend_path
            require_tool("wav", path=path, options=opts.wav_output)
        if mapping:
            require_tool("hirc", path=str(self.ctx.config.wwiser_path or "") or None)

    @staticmethod
    def _log_stage_result(result: StageResult, *, label: str, success_detail: str | None = None) -> None:
        """按 typed result 输出与真实状态一致的阶段结论。"""
        if result.status is ResultStatus.SUCCESS:
            message = f"{label}完成"
            if success_detail:
                message = f"{message}：{success_detail}"
            logger.success(message)
            return

        detail = result.note or result.error_message
        count_summary = (
            f"成功 {result.success_count}，部分成功 {result.partial_count}，失败 {result.failed_count}"
            if result.total_count
            else None
        )
        summary = detail or count_summary
        suffix = f"：{summary}" if summary else ""
        if result.status is ResultStatus.PARTIAL:
            logger.warning(f"{label}部分完成{suffix}")
        elif result.status is ResultStatus.CANCELLED:
            logger.warning(f"{label}已取消{suffix}")
        else:
            logger.error(f"{label}失败{suffix}")

    @staticmethod
    def _adapt_discovery_result(result: ResourcePackDiscoveryResult) -> StageResult:
        """将 resource-pack discovery 合同适配为 update 子阶段结果。"""
        status_map = {
            "complete": ResultStatus.SUCCESS,
            "partial": ResultStatus.PARTIAL,
            "failed": ResultStatus.FAILED,
        }
        scans = tuple(getattr(result, "scans", ()))
        if not scans:
            raw_status = result.status
            if raw_status not in status_map:
                raise RuntimeError(f"未知 resource-pack discovery 状态: {raw_status}")
            return StageResult(
                "update",
                status=status_map[raw_status],
                note="没有待扫描的 resource-pack WAD。" if raw_status == "complete" else None,
            )

        entities: list[EntityResult] = []
        for scan in scans:
            raw_status = scan.status
            if raw_status not in status_map:
                raise RuntimeError(f"未知 resource-pack scan 状态: {raw_status}")
            reason = scan.reason
            entities.append(
                EntityResult(
                    entity_type="resource_pack_wad",
                    entity_id=scan.wad,
                    entity_name=scan.wad,
                    status=status_map[raw_status],
                    error_type="ResourcePackDiscoveryError" if reason else None,
                    error_message=reason,
                )
            )
        return StageResult.from_entities("update", entities)

    @staticmethod
    def _adapt_update_results(results: Sequence[UpdateEntityResult] | None) -> StageResult:
        """把 manager 逐实体结果适配为应用层阶段结果。"""
        if results is None:
            # 兼容仍按旧 ``None`` 合同实现的外部替身；生产 BinUpdater 已返回逐实体结果。
            return StageResult("update")
        status_map = {
            UpdateStatus.SUCCESS: ResultStatus.SUCCESS,
            UpdateStatus.PARTIAL: ResultStatus.PARTIAL,
            UpdateStatus.FAILED: ResultStatus.FAILED,
        }
        return StageResult.from_entities(
            "update",
            (
                EntityResult(
                    entity_type=result.entity_type,
                    entity_id=result.entity_id,
                    entity_name=result.entity_name,
                    status=status_map[result.status],
                    error_type=result.error_type,
                    error_message=result.error_message,
                    artifacts=result.artifacts,
                )
                for result in results
            ),
        )

    @staticmethod
    def _adapt_wav_result(payload: dict[str, object]) -> StageResult:
        """将 WAV runtime 汇总适配为公共阶段结果。"""
        raw_status = str(payload.get("status", ""))
        processed_count = int(payload.get("processed_file_count", 0))
        failed_count = int(payload.get("failed_file_count", 0))
        skipped_count = int(payload.get("skipped_file_count", 0))
        unconfirmed_count = int(payload.get("unconfirmed_file_count", 0))
        reports = tuple(str(path) for path in payload.get("batch_reports", ()))
        batches = tuple(payload.get("batches", ()))
        errors = tuple(str(error) for error in payload.get("errors", ()))
        entities: tuple[EntityResult, ...] = ()
        produced_count = int(payload.get("produced_file_count", processed_count))
        pending = bool(payload.get("pending_event_output", False))
        if (processed_count > 0 or produced_count > 0) and not pending:
            wav_roots = tuple(payload.get("wav_roots", ())) or (payload.get("wav_root"),)
            if any(not isinstance(root, str) or not root for root in wav_roots):
                raise RuntimeError("WAV 转码汇总缺少已处理文件对应的 wav_root")
            entity_status = ResultStatus.SUCCESS if failed_count == 0 and not errors else ResultStatus.PARTIAL
            entities = (EntityResult("wav", "batch", entity_status, artifacts=wav_roots),)
        if raw_status == "success" and failed_count == 0:
            note = (
                f"已生成 {processed_count} 个文件，实际转换 {payload.get('converted_file_count', processed_count)}、复用 {payload.get('reused_file_count', 0)}，跳过已有输出 {skipped_count} 个。"
                if processed_count or skipped_count
                else "没有待转换的音频文件。"
            )
            if entities:
                if "classified_file_count" in payload:
                    note += (
                        f"关联事件 {payload['classified_file_count']}、未关联 {payload['unclassified_file_count']} 个音频，"
                        f"普通布局 {payload.get('plain_file_count', 0)} 个音频，"
                        f"写入 {produced_count} 个 WAV 文件。"
                    )
                return StageResult("wav", entities, note=note, reports=reports, wav_batches=batches)
            if pending:
                note = f"已暂存 {processed_count} 个音频，等待本次映射步骤结束后输出事件目录。"
            return StageResult("wav", status=ResultStatus.SUCCESS, note=note, reports=reports, wav_batches=batches)
        if raw_status == "warning" and (failed_count > 0 or errors):
            status = ResultStatus.PARTIAL if processed_count or skipped_count or produced_count else ResultStatus.FAILED
            return StageResult(
                "wav",
                entities,
                status=status,
                note=(
                    f"已转换 {processed_count} 个文件，失败 {failed_count} 个，跳过 {skipped_count} 个。"
                    + (f"另有 {unconfirmed_count} 个文件的完成状态未知。" if unconfirmed_count else "")
                ),
                error_message="；".join(errors) or None,
                reports=reports,
                wav_batches=batches,
            )
        raise RuntimeError(f"未知 WAV 转码汇总状态: {raw_status}")

    @contextmanager
    def defer_wav_output(self, opts: OperationOptions) -> Iterator[EventOutput | None]:
        """让整轮任务在映射步骤结束后交付 WAV，供 CLI 与 GUI 共用。

        Args:
            opts: 本轮固定的转码设置。

        Yields:
            事件输出会话；未启用事件分类时为 ``None``。
        """
        if not opts.wav_output.group_by_event:
            yield None
            return
        if EVENT_SESSION_KEY in self.ctx.runtime_cache:
            raise RuntimeError("当前上下文已有 WAV 事件输出任务")
        version = self._get_reader().version
        with EventOutput(
            audio_root=self.ctx.version_path("audio", version),
            hash_root=self.ctx.version_path("hash", version),
            version=version,
            scratch_root=self.ctx.paths.temp_path,
        ) as output:
            self.ctx.runtime_cache[EVENT_SESSION_KEY] = output
            try:
                yield output
            finally:
                self.ctx.runtime_cache.pop(EVENT_SESSION_KEY, None)

    def finish_wav_output(self, output: EventOutput) -> StageResult:
        """交付本轮暂存 WAV，并返回最终目录和失败事实。

        Args:
            output: 由本轮任务持有的事件输出会话。

        Returns:
            已交付 WAV 的阶段结果。
        """
        return self._adapt_wav_result(output.finish_tree())

    def _is_update_prepared(self, *, force_update: bool) -> bool:
        """判断当前上下文是否已完成数据预热。"""
        cached_force_update = self.ctx.runtime_cache.get(UPDATE_PREPARED_KEY)
        if cached_force_update is True:
            return True
        if cached_force_update is False and not force_update:
            return True
        return False

    def prepare_update_data(self, *, force_update: bool = False) -> None:
        """预热 update 所需结构化数据，并复用当前运行中的缓存状态。

        Args:
            force_update: 是否强制刷新数据文件。

        """
        self._reset_reader()
        try:
            if not self._is_update_prepared(force_update=force_update):
                with library_session(self.ctx):
                    DataUpdater(force_update=force_update, ctx=self.ctx).check_and_update()
                self.ctx.runtime_cache[UPDATE_PREPARED_KEY] = force_update
        finally:
            # DataUpdater 即使失败也可能已替换部分 artifact，不能复用调用前的缓存。
            self._reset_reader()

    def discover_resource_packs(self, opts: OperationOptions) -> ResourcePackDiscoveryResult:
        """扫描 selected-WAD 并写入 resource-pack v2 artifact。

        Args:
            opts: 含 typed ``resource_pack_wads`` 的操作参数。

        Returns:
            各 selected-WAD 的发现状态、成本与已生成 pack rows。

        """
        opts = self._resolve_operation_options(opts)
        if not opts.resource_pack_wads:
            return ResourcePackDiscoveryResult((), ())
        self._reset_reader()
        try:
            reader = self._get_reader()
            with library_session(self.ctx):
                return ResourcePackDiscovery(self.ctx, reader=reader).discover(
                    opts.resource_pack_wads,
                    version=reader.version,
                )
        finally:
            # discovery 以 pair transaction 写 banks/events，任何退出都需丢弃旧分区 cache。
            self._reset_reader()

    def resolve_champion_ids(self, selectors: Sequence[int | str] | None) -> tuple[int, ...] | None:
        """将英雄选择器解析为稳定的英雄 ID 元组。

        Args:
            selectors: 英雄选择器序列，支持整数 ID、数字字符串或英雄 alias。

        Returns:
            解析后的英雄 ID 元组；当 ``selectors`` 为 ``None`` 时返回 ``None``。

        Raises:
            ValueError: 当选择器为空、alias 不存在或类型不支持时抛出。
        """
        if selectors is None:
            return None

        normalized_selectors: list[str | int] = []
        has_numeric_selector = False
        has_alias_selector = False
        for selector in selectors:
            if isinstance(selector, int):
                normalized_selectors.append(selector)
                has_numeric_selector = True
                continue

            raw_selector = str(selector).strip()
            if not raw_selector:
                raise ValueError("英雄选择器不能为空。")
            normalized_selectors.append(raw_selector)
            if raw_selector.isdigit():
                has_numeric_selector = True
            else:
                has_alias_selector = True

        if has_numeric_selector and has_alias_selector:
            raise ValueError("暂不支持在同一次英雄选择中混用 ID 与 alias。")
        if has_numeric_selector:
            return tuple(int(selector) for selector in normalized_selectors)

        reader = self._get_reader()
        champions = reader.get_champions()
        alias_to_id = {
            str(champion.get("alias", "")).strip().casefold(): int(champion["id"])
            for champion in champions
            if champion.get("id") is not None and champion.get("alias")
        }

        resolved_ids: list[int] = []
        unresolved_aliases: list[str] = []
        for selector in normalized_selectors:
            champion_id = alias_to_id.get(str(selector).casefold())
            if champion_id is None:
                unresolved_aliases.append(str(selector))
                continue
            resolved_ids.append(champion_id)

        if unresolved_aliases:
            available_aliases = sorted(champion.get("alias", "") for champion in champions if champion.get("alias"))
            raise ValueError(f"未找到对应的英雄 alias: {unresolved_aliases}。可用 alias 示例: {available_aliases[:10]}")

        return tuple(resolved_ids)

    def _build_entity_data(
        self,
        reader: DataReader,
        *,
        entity_type: str,
        entity_id: int | str,
        include_events: bool = False,
    ) -> AudioEntityData:
        """根据工作项构建实体数据。"""
        return AudioEntityData.from_entity(
            entity_type,
            entity_id,
            reader,
            include_events=include_events,
            ctx=self.ctx,
        )

    def _resolve_audio_paths(self, entity_data: AudioEntityData) -> tuple[Path, ...]:
        """解析实体解包后的实际输出目录。"""
        return resolve_audio_paths(self.ctx, entity_data, self._get_reader().version)

    @staticmethod
    def _build_entity_display_name(entity_data: AudioEntityData) -> str:
        """构造与实体列表一致的展示名称。"""
        if entity_data.entity_title:
            return f"{entity_data.entity_name}·{entity_data.entity_title}"
        return entity_data.entity_name

    def update(
        self,
        opts: OperationOptions,
        *,
        target: str = "all",
        progress_callback: Callable[[OperationProgress], None] | None = None,
    ) -> StageResult:
        """执行更新流程并返回权威阶段结果。

        Args:
            opts: 更新操作选项。
            target: 传给 BIN 更新器的目标范围。
            progress_callback: 可选的结构化进度回调。

        Returns:
            update 阶段结果；已知共享数据或持久化错误会成为 failed。
        """
        opts = self._resolve_operation_options(opts)
        opts = replace(opts, map_ids=with_common_map(opts.map_ids, 0))
        resource_pack_summary = f"，资源包 WAD {len(opts.resource_pack_wads)} 个" if opts.resource_pack_wads else ""
        logger.info(
            f"开始执行更新流程：target={target}，英雄 {len(opts.champion_ids or ())} 个，"
            f"地图 {len(opts.map_ids or ())} 个{resource_pack_summary}，"
            f"事件处理={'开启' if opts.process_events else '关闭'}"
        )
        self._reset_reader()
        try:
            try:
                self._check_source(
                    opts, include_champions=target in {"all", "skin"}, include_maps=target in {"all", "map"}
                )
                if opts.wav_output.enabled:
                    self.check_tools(opts)
                with library_session(self.ctx):
                    if progress_callback is not None:
                        progress_callback(OperationProgress("update", "data", "started"))
                    self.prepare_update_data(force_update=opts.force_update)
                    self.check_targets(opts)
                    if progress_callback is not None:
                        progress_callback(OperationProgress("update", "data", "finished"))
                    has_resource_pack_scope = self._has_resource_pack_targets(opts)
                    run_standard_update = (
                        not has_resource_pack_scope or opts.champion_ids is not None or opts.map_ids is not None
                    )
                    child_results: list[StageResult] = []
                    if run_standard_update:
                        updater_kwargs = {
                            "force_update": opts.force_update,
                            "process_events": opts.process_events,
                            "ctx": self.ctx,
                        }
                        if progress_callback is not None:
                            updater_kwargs["progress_callback"] = progress_callback
                        updater = BinUpdater(
                            **updater_kwargs,
                        )
                        update_results = updater.update(
                            target=target,
                            champion_ids=self._to_str_ids(opts.champion_ids),
                            map_ids=self._to_str_ids(opts.map_ids),
                        )
                        child_results.append(self._adapt_update_results(update_results))
                    if opts.resource_pack_wads:
                        discovery_result = self.discover_resource_packs(opts)
                        logger.info(
                            "资源包发现结束：status={}，packs={}，candidate={}，payload_reads={}",
                            discovery_result.status,
                            len(discovery_result.packs),
                            discovery_result.cost["candidateEntries"],
                            discovery_result.cost["payloadReads"],
                        )
                        child_results.append(self._adapt_discovery_result(discovery_result))
                    result = StageResult.combine(
                        "update",
                        child_results,
                        note="当前目标不需要更新。" if not child_results else None,
                    )
            except EXPECTED_STAGE_ERRORS as exc:
                result = StageResult.from_error("update", exc)
        finally:
            # BIN/discovery 可能在成功或失败前写入部分 artifact，阶段退出后统一换代。
            self._reset_reader()

        self._log_stage_result(
            result,
            label="更新流程",
            success_detail=(
                f"target={target}，英雄 {len(opts.champion_ids or ())} 个，"
                f"地图 {len(opts.map_ids or ())} 个{resource_pack_summary}"
            ),
        )
        return result

    def transcode_wav(
        self,
        opts: OperationOptions,
        *,
        progress_callback: Callable[[str, int, int, str], None] | None = None,
        job_label: str | None = None,
    ) -> StageResult:
        """执行独立的 WAV 转码 stage。

        Args:
            opts: 当前工作流的操作选项。
            progress_callback: 可选的统一进度回调。
            job_label: 可选报告标签。

        Returns:
            WAV 阶段结果。
        """
        opts = self._resolve_operation_options(opts)
        if self._has_resource_pack_targets(opts):
            raise ValueError("resource pack 当前不支持 WAV 转码；请先只执行 extract 或 mapping。")
        try:
            reader = self._get_reader()
            self.check_targets(opts)
            audio_targets: tuple[TranscodeTarget, ...] | None = None
            if opts.champion_ids is not None or opts.map_ids is not None:
                resolved_targets: list[TranscodeTarget] = []
                seen_roots: set[Path] = set()

                def add_roots(entity_type: str, ids: tuple[int, ...] | None) -> None:
                    if ids is None:
                        return
                    for entity_id in ids:
                        entity_data = self._build_entity_data(
                            reader,
                            entity_type=entity_type,
                            entity_id=entity_id,
                        )
                        display_label = self._build_entity_display_name(entity_data)
                        for root in self._resolve_audio_paths(entity_data):
                            if root not in seen_roots:
                                seen_roots.add(root)
                                resolved_targets.append(TranscodeTarget(root_path=root, display_label=display_label))

                add_roots("champion", opts.champion_ids)
                add_roots("map", opts.map_ids)
                audio_targets = tuple(resolved_targets)
                if audio_targets:
                    logger.info("音频转码将按目标实体目录处理，共 {} 个目录。", len(audio_targets))
                else:
                    logger.warning("音频转码未找到任何目标实体音频目录，将跳过本次执行。")
            result = self._adapt_wav_result(
                run_tree(
                    ctx=self.ctx,
                    version=reader.version,
                    wav_output=opts.wav_output,
                    audio_targets=audio_targets,
                    progress_callback=progress_callback,
                    job_label=job_label,
                )
            )
        except EXPECTED_STAGE_ERRORS as exc:
            result = StageResult.from_error("wav", exc)
        self._log_stage_result(result, label="WAV 转码")
        return result

    def extract(  # noqa: PLR0913
        self,
        opts: OperationOptions,
        *,
        include_champions: bool = True,
        include_maps: bool = True,
        progress_callback: Callable[[str, int, int, str], None] | None = None,
        persisted_wem_callback: Callable[[Path], None] | None = None,
    ) -> StageResult:
        """执行解包流程。

        Args:
            opts: 解包操作选项。
            include_champions: 是否包含英雄。
            include_maps: 是否包含地图。
            progress_callback: 每个实体处理结束后的可选进度回调。
            persisted_wem_callback: 单个 WEM 持久化成功后的可选回调。

        Returns:
            extract 阶段结果。
        """
        opts = self._resolve_operation_options(opts)
        try:
            self.check_targets(opts)
            self._check_source(opts, include_champions=include_champions, include_maps=include_maps)
            if opts.wav_output.enabled:
                self.check_tools(opts)
            reader = self._get_reader()
            logger.info(
                f"音频类型配置 - 包含: {list(self.ctx.config.include_types)}, "
                f"排除: {list(self.ctx.config.exclude_types)}"
            )
            logger.info(f"输出路径: {self.ctx.config.output_path}")
            logger.info(f"语言: {self.ctx.config.game_region}")

            has_explicit_champions = opts.champion_ids is not None
            has_explicit_maps = opts.map_ids is not None
            resource_pack_keys = self._resource_pack_targets(opts)
            child_results: list[StageResult] = []
            if has_explicit_champions:
                child_results.append(
                    unpack_champions(
                        reader=reader,
                        champion_ids=list(opts.champion_ids),
                        max_workers=opts.max_workers,
                        ctx=self.ctx,
                        progress_callback=progress_callback,
                        persisted_wem_callback=persisted_wem_callback,
                    )
                )
            if has_explicit_maps:
                child_results.append(
                    unpack_maps(
                        reader=reader,
                        map_ids=list(opts.map_ids),
                        max_workers=opts.max_workers,
                        ctx=self.ctx,
                        progress_callback=progress_callback,
                        persisted_wem_callback=persisted_wem_callback,
                    )
                )
            if resource_pack_keys:
                child_results.append(
                    unpack_resource_packs(
                        reader=reader,
                        keys=list(resource_pack_keys),
                        max_workers=opts.max_workers,
                        ctx=self.ctx,
                        progress_callback=progress_callback,
                        persisted_wem_callback=persisted_wem_callback,
                    )
                )
            if has_explicit_champions or has_explicit_maps or self._has_resource_pack_targets(opts):
                result = StageResult.combine("extract", child_results)
            else:
                result = unpack_all(
                    reader=reader,
                    max_workers=opts.max_workers,
                    include_champions=include_champions,
                    include_maps=include_maps,
                    ctx=self.ctx,
                    progress_callback=progress_callback,
                    persisted_wem_callback=persisted_wem_callback,
                )
        except EXPECTED_STAGE_ERRORS as exc:
            result = StageResult.from_error("extract", exc)
        self._log_stage_result(result, label="音频解包")
        return result

    def mapping(
        self,
        opts: OperationOptions,
        *,
        include_champions: bool = True,
        include_maps: bool = True,
        progress_callback: Callable[[str, int, int, str], None] | None = None,
    ) -> StageResult:
        """执行映射流程并返回权威阶段结果。

        Args:
            opts: 映射操作选项。
            include_champions: 是否包含英雄。
            include_maps: 是否包含地图。
            progress_callback: 每个实体处理结束后的可选进度回调。

        Returns:
            mapping 阶段结果。
        """
        opts = self._resolve_operation_options(opts)
        try:
            self.check_targets(opts)
            self.check_tools(opts, mapping=True)
            backend_label = self._describe_mapping_backend()
            self._check_source(opts, include_champions=include_champions, include_maps=include_maps)
            reader = self._get_reader()

            logger.info(f"缓存路径: {self.ctx.paths.cache_path}")
            logger.info(f"哈希路径: {self.ctx.paths.hash_path}")
            logger.info(f"HIRC 后端: {backend_label}")
            logger.info(f"语言: {self.ctx.config.game_region}")

            has_explicit_champions = opts.champion_ids is not None
            has_explicit_maps = opts.map_ids is not None
            resource_pack_keys = self._resource_pack_targets(opts)
            child_results: list[StageResult] = []
            if has_explicit_champions:
                child_results.append(
                    build_champions(
                        reader=reader,
                        champion_ids=list(opts.champion_ids),
                        max_workers=opts.max_workers,
                        integrate_data=opts.integrate_data,
                        ctx=self.ctx,
                        progress_callback=progress_callback,
                    )
                )
            if has_explicit_maps:
                child_results.append(
                    build_maps(
                        reader=reader,
                        map_ids=list(opts.map_ids),
                        max_workers=opts.max_workers,
                        integrate_data=opts.integrate_data,
                        ctx=self.ctx,
                        progress_callback=progress_callback,
                    )
                )
            if resource_pack_keys:
                child_results.append(
                    build_resource_packs(
                        reader=reader,
                        keys=list(resource_pack_keys),
                        max_workers=opts.max_workers,
                        integrate_data=opts.integrate_data,
                        ctx=self.ctx,
                        progress_callback=progress_callback,
                    )
                )
            if has_explicit_champions or has_explicit_maps or self._has_resource_pack_targets(opts):
                result = StageResult.combine("mapping", child_results)
            else:
                result = build_all(
                    reader=reader,
                    max_workers=opts.max_workers,
                    include_champions=include_champions,
                    include_maps=include_maps,
                    integrate_data=opts.integrate_data,
                    ctx=self.ctx,
                    progress_callback=progress_callback,
                )
        except EXPECTED_STAGE_ERRORS as exc:
            result = StageResult.from_error("mapping", exc)
        self._log_stage_result(result, label="事件映射")
        return result


__all__ = ["LolAudioUnpackApp"]
