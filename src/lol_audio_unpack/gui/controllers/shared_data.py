"""共享实体目录的 generation 状态机与后台编排。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from time import monotonic

from loguru import logger
from PySide6.QtCore import QObject, QTimer, Signal

from lol_audio_unpack.app.results import ResultStatus, StageResult
from lol_audio_unpack.app.targets import should_hide_champion_by_default
from lol_audio_unpack.config import SettingKey
from lol_audio_unpack.gui.controllers.contracts import (
    EntityRowsPayload,
    GuiNotice,
    RuntimeLoggingConfig,
)
from lol_audio_unpack.gui.shared_data import (
    SharedDataPhase,
    SharedDataPreparationResult,
    SharedDataPrepareTrigger,
    SharedDataProblem,
    SharedDataProblemCode,
    SharedDataProgress,
    SharedDataReadiness,
    SharedDataRepairScope,
    SharedDataScanResult,
    SharedDataState,
)
from lol_audio_unpack.gui.shared_data_view import describe_shared_data_state
from lol_audio_unpack.gui.task_models import OutputStateRefreshRequest

SHARED_CONTEXT_BUILD_TIMEOUT_MS = 15000
SHARED_PROGRESS_REFRESH_INTERVAL_MS = 50


def build_shared_entity_reader_signature(config) -> tuple[str | bool, ...]:
    """构建影响共享实体数据读取上下文的配置签名。"""
    overrides = config.to_app_context_settings()
    return (
        overrides[SettingKey.GAME_PATH],
        overrides[SettingKey.GAME_REGION],
        config.prepare_data_on_startup,
    )


def build_shared_entity_scan_signature(config) -> tuple[str | bool, ...]:
    """构建仅影响输出扫描结果的配置签名。"""
    overrides = config.to_app_context_settings()
    return (
        overrides[SettingKey.OUTPUT_PATH],
        overrides[SettingKey.GROUP_BY_TYPE],
    )


def build_shared_context_loading_message(_config) -> str:
    """返回本地共享数据加载阶段文案。"""
    return "正在读取本地共享数据…"


def build_shared_context_timeout_message(_config) -> str:
    """返回本地共享数据加载超时提示。"""
    return "读取共享数据超时，请重试。"


class SharedDataController(QObject):
    """持有共享目录单一状态，并编排检查、准备与复检。"""

    state_changed = Signal(object)
    app_context_changed = Signal(object)
    source_inventory_changed = Signal(object)
    shared_data_cleared = Signal()
    entity_data_replaced = Signal(object)
    entity_rows_updated = Signal(object)
    notice_requested = Signal(object)
    reconfigure_runtime_logging_requested = Signal(object)

    def __init__(  # noqa: PLR0913
        self,
        *,
        get_config: Callable[[], object],
        has_incomplete_tasks: Callable[[], bool],
        create_app_context_fn: Callable[..., object],
        data_load_worker_cls,
        task_worker_cls,
        entity_data_loader_cls,
        start_worker_fn: Callable[[object], None],
        prepare_shared_entity_data_fn: Callable[..., SharedDataPreparationResult],
        app_context_block_reason_fn: Callable[[object], str | None],
        parent=None,
    ) -> None:
        """初始化共享目录控制器及其异步依赖。"""
        super().__init__(parent)
        self._get_config = get_config
        self._has_incomplete_tasks = has_incomplete_tasks
        self._create_app_context = create_app_context_fn
        self._scan_worker_cls = data_load_worker_cls
        self._task_worker_cls = task_worker_cls
        self._entity_data_loader_cls = entity_data_loader_cls
        self._start_worker = start_worker_fn
        self._prepare_shared_entity_data = prepare_shared_entity_data_fn
        self._get_app_context_block_reason = app_context_block_reason_fn

        self.generation = 0
        self.app_context = None
        self.state = SharedDataState(SharedDataPhase.BLOCKED, 0)
        self.is_loading_shared_data = False
        self.is_preparing_shared_data = False
        self.pending_refresh_notice = False
        self.pending_runtime_entity_refresh = False
        self.pending_refresh_allow_prepare = False
        self.allow_auto_prepare_on_reload = True
        self.auto_prepare_attempted = False
        self.reader_signature: tuple[str | bool, ...] | None = None
        self.scan_signature: tuple[str | bool, ...] | None = None

        self.build_worker = None
        self.build_config = None
        self.build_request_id = 0
        self._build_generation = 0
        self._scan_worker = None
        self._scan_generation = 0
        self.shared_data_prepare_worker = None
        self._prepare_generation = 0
        self._prepare_scope = SharedDataRepairScope(full=True)
        self._prepare_force_update = False
        self._prepare_result: SharedDataPreparationResult | None = None
        self._trigger = SharedDataPrepareTrigger.INITIAL
        self._notice_keys: set[tuple[int, str]] = set()
        self._pending_progress: tuple[int, object] | None = None
        self._closed = False
        self._started_at = monotonic()
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(250)
        self.elapsed_timer.timeout.connect(self._refresh_elapsed)

        self._champions_worker = None
        self._maps_worker = None

        self.runtime_entity_refresh_timer = QTimer(self)
        self.runtime_entity_refresh_timer.setSingleShot(True)
        self.runtime_entity_refresh_timer.setInterval(900)
        self.runtime_entity_refresh_timer.timeout.connect(self.flush_pending_runtime_entity_refresh)

        self.build_timeout_timer = QTimer(self)
        self.build_timeout_timer.setSingleShot(True)
        self.build_timeout_timer.setInterval(SHARED_CONTEXT_BUILD_TIMEOUT_MS)
        self.build_timeout_timer.timeout.connect(self.on_shared_context_build_timeout)

        self.progress_refresh_timer = QTimer(self)
        self.progress_refresh_timer.setSingleShot(True)
        self.progress_refresh_timer.setInterval(SHARED_PROGRESS_REFRESH_INTERVAL_MS)
        self.progress_refresh_timer.timeout.connect(self._flush_pending_progress)

    def _publish_state(self, state: SharedDataState) -> None:
        """原子保存并发布类型化共享数据状态。"""
        if self._closed or state.generation != self.generation:
            return
        if not state.active or (state.phase is not self.state.phase and state.progress is None):
            self._clear_pending_progress()
        if state.active:
            if not self.elapsed_timer.isActive():
                self.elapsed_timer.start()
            state = replace(state, elapsed_seconds=monotonic() - self._started_at)
        else:
            self.elapsed_timer.stop()
            state = replace(state, elapsed_seconds=self.state.elapsed_seconds)
        self.state = state
        self.is_loading_shared_data = state.phase in {SharedDataPhase.CHECKING, SharedDataPhase.VERIFYING}
        self.is_preparing_shared_data = state.phase is SharedDataPhase.PREPARING
        self.state_changed.emit(state)

    def _refresh_elapsed(self) -> None:
        """独立于后台文件吞吐更新耗时，耗时较长的单文件也有界面反馈。"""
        if self.state.active and not self._closed:
            self._publish_state(self.state)

    def _replace_state(self, **changes) -> None:
        """在当前 generation 上发布部分字段变更。"""
        self._publish_state(replace(self.state, **changes))

    def _emit_notice_once(self, key: str, notice: GuiNotice) -> None:
        """同一 generation 的相同状态转折只发送一次通知。"""
        identity = (self.generation, key)
        if identity in self._notice_keys:
            return
        self._notice_keys.add(identity)
        self.notice_requested.emit(notice)

    @staticmethod
    def _problem_from_stage_result(result: StageResult) -> SharedDataProblem:
        """把 update 阶段终态转换为稳定 GUI 问题。"""
        affected_ids = tuple(
            str(entity.entity_id)
            for entity in result.entities
            if entity.status in {ResultStatus.PARTIAL, ResultStatus.FAILED, ResultStatus.CANCELLED}
        )
        if result.status is ResultStatus.PARTIAL:
            return SharedDataProblem(
                SharedDataProblemCode.RESOURCE_BINDING_INCOMPLETE,
                "update",
                "实体数据更新只完成了一部分；已复检当前可用目录。",
                affected_ids,
            )
        if result.status is ResultStatus.CANCELLED:
            return SharedDataProblem(
                SharedDataProblemCode.UNEXPECTED,
                "update",
                "实体数据准备已取消。",
                affected_ids,
            )
        code_by_error_type = {
            "ArtifactWriteError": SharedDataProblemCode.OUTPUT_NOT_WRITABLE,
            "DataVersionMismatchError": SharedDataProblemCode.DATASET_STALE,
            "ResourceSchemaMismatchError": SharedDataProblemCode.RESOURCE_SCHEMA_MISMATCH,
            "SharedDataCorruptError": SharedDataProblemCode.ARTIFACT_CORRUPT,
            "SharedDataMissingError": SharedDataProblemCode.BANK_ARTIFACT_MISSING,
        }
        code = code_by_error_type.get(result.error_type or "", SharedDataProblemCode.UNEXPECTED)
        message_by_code = {
            SharedDataProblemCode.OUTPUT_NOT_WRITABLE: "无法写入实体数据输出目录；请检查输出路径或权限后重试。",
            SharedDataProblemCode.DATASET_STALE: "实体基础数据与当前版本不兼容；请重试更新。",
            SharedDataProblemCode.RESOURCE_SCHEMA_MISMATCH: "实体数据仍使用旧版资源结构；请重试更新。",
            SharedDataProblemCode.ARTIFACT_CORRUPT: "实体数据文件无法读取；请重试更新。",
            SharedDataProblemCode.BANK_ARTIFACT_MISSING: "实体 banks 数据未生成；请重试更新。",
        }
        return SharedDataProblem(
            code,
            "update",
            message_by_code.get(code, "实体数据更新未能完成；请重试或查看日志。"),
            affected_ids,
        )

    def _has_running_worker(self) -> bool:
        """返回 controller 是否仍持有未结算的后台 owner。"""
        return (
            self.build_worker is not None
            or self._scan_worker is not None
            or self.shared_data_prepare_worker is not None
        )

    def has_active_background_work(self) -> bool:
        """返回共享数据链路是否仍有后台工作未结束。"""
        legacy_thread_running = any(
            worker is not None and getattr(worker, "isRunning", lambda: False)()
            for worker in (self._champions_worker, self._maps_worker)
        )
        scan_running = self._scan_worker is not None and getattr(self._scan_worker, "isRunning", lambda: False)()
        return (
            self.state.active
            or self._has_running_worker()
            or legacy_thread_running
            or scan_running
            or self.runtime_entity_refresh_timer.isActive()
            or self.build_timeout_timer.isActive()
        )

    def bootstrap(self, config=None) -> None:
        """初始化配置签名并启动首轮共享目录检查。"""
        config = config or self._get_config()
        self.reader_signature = build_shared_entity_reader_signature(config)
        self.scan_signature = build_shared_entity_scan_signature(config)
        self.load_initial_data(config, trigger=SharedDataPrepareTrigger.INITIAL)

    def set_queue_busy(self, busy: bool) -> None:
        """在任务队列忙碌状态变化时暂停或恢复待处理刷新。"""
        if busy:
            self.runtime_entity_refresh_timer.stop()
            return
        self.flush_pending_runtime_entity_refresh()

    def _next_generation(self, trigger: SharedDataPrepareTrigger) -> int:
        """使旧回调失效并初始化新 generation 的流程字段。"""
        self._clear_pending_progress()
        self.generation += 1
        self._started_at = monotonic()
        self._trigger = trigger
        self.auto_prepare_attempted = False
        self._prepare_result = None
        self._prepare_force_update = False
        self._notice_keys = {identity for identity in self._notice_keys if identity[0] == self.generation}
        return self.generation

    def load_initial_data(
        self,
        config=None,
        *,
        trigger: SharedDataPrepareTrigger = SharedDataPrepareTrigger.INITIAL,
        generation: int | None = None,
    ) -> None:
        """为新 generation 后台构建上下文并开始完整检查。"""
        config = config or self._get_config()
        block_reason = self._get_app_context_block_reason(config)
        if block_reason is not None:
            self._apply_blocked_state(block_reason, trigger=trigger)
            return

        generation = generation if generation is not None else self._next_generation(trigger)
        self._trigger = trigger
        self.build_request_id += 1
        self._build_generation = generation
        self.build_config = config
        self._publish_state(
            SharedDataState(
                phase=SharedDataPhase.CHECKING,
                generation=generation,
                prepare_attempted=self.auto_prepare_attempted,
                prepare_trigger=trigger,
            )
        )

        settings = dict(config.to_app_context_settings())

        def build(signals):
            def publish(progress):
                signals.progress.emit(
                    SharedDataProgress(generation, progress.stage_key, progress.event, progress.current, progress.total)
                )

            return self._create_app_context(settings=settings, progress_callback=publish)

        worker = self._task_worker_cls(build, pass_signals=True)
        worker.signals.progress.connect(self._on_build_progress)
        worker.signals.finished.connect(self._on_shared_context_build_payload)
        worker.signals.failed.connect(self._on_shared_context_build_error)
        self.build_worker = worker
        self.build_timeout_timer.start()
        self._start_worker(worker)

    def _on_build_progress(self, progress) -> None:
        """只接收当前上下文的后台启动进度。"""
        if progress.generation != self.generation or self.build_worker is None:
            return
        if progress.stage_key == "library_migration":
            self.build_timeout_timer.stop()
        elif progress.stage_key == "source_inventory" and not self.build_timeout_timer.isActive():
            self.build_timeout_timer.start()
        self._publish_progress(progress.generation, progress)

    def _on_shared_context_build_payload(self, app_context) -> None:
        """在 controller 所在线程消费当前 build owner 的结果。"""
        if self.build_worker is None:
            return
        self.on_shared_context_build_finished(self.build_request_id, app_context, self._build_generation)

    def _on_shared_context_build_error(self, error: str) -> None:
        """在 controller 所在线程消费当前 build owner 的失败。"""
        if self.build_worker is None:
            return
        self.on_shared_context_build_failed(self.build_request_id, error, self._build_generation)

    def on_shared_context_build_finished(self, request_id: int, app_context, generation: int | None = None) -> None:
        """上下文构建完成后启动同 generation 的原子目录扫描。"""
        generation = self._build_generation if generation is None else generation
        if request_id != self.build_request_id or generation != self.generation:
            if request_id == self.build_request_id:
                self.build_worker = None
                self.build_timeout_timer.stop()
                self.flush_pending_runtime_entity_refresh()
            return
        self.build_timeout_timer.stop()
        self.build_worker = None
        self.build_config = None
        self.app_context = app_context
        self.app_context_changed.emit(app_context)
        self.shared_data_cleared.emit()
        inventory = getattr(app_context, "runtime_cache", {}).get("source_inventory")
        if inventory is not None:
            inventory = replace(inventory, generation=generation)
            app_context.runtime_cache["source_inventory"] = inventory
            self.source_inventory_changed.emit(inventory)
            # 自动选中或清空语言会启动新 generation，不能再发布旧上下文的目录。
            if generation != self.generation:
                return
            language = inventory.get_language(app_context.config.game_region)
            if language is None or not language.available_count:
                available = [item for item in inventory.languages if item.available_count]
                if len(available) > 1:
                    message = "检测到多种本地资源语言，请在全局设置中选择想处理的语言，选定后会自动继续。"
                elif available:
                    message = "请在全局设置中选择资源语言，选定后会自动继续。"
                else:
                    message = "当前目录没有可用的资源语言，请检查本地资源或选择其他游戏目录。"
                problem = SharedDataProblem(
                    SharedDataProblemCode.CONFIGURATION_REQUIRED,
                    "language" if available else "context",
                    message,
                )
                self._publish_terminal(SharedDataPhase.BLOCKED, problem=problem)
                return
        self._start_scan(generation, SharedDataPhase.CHECKING)

    def on_shared_context_build_failed(
        self,
        request_id: int,
        error: str,
        generation: int | None = None,
    ) -> None:
        """把上下文构建失败发布为当前 generation 的 typed failed。"""
        generation = self._build_generation if generation is None else generation
        if request_id != self.build_request_id or generation != self.generation:
            if request_id == self.build_request_id:
                self.build_worker = None
                self.build_timeout_timer.stop()
                self.flush_pending_runtime_entity_refresh()
            return
        self.build_timeout_timer.stop()
        self.build_worker = None
        self.build_config = None
        self.app_context = None
        self.app_context_changed.emit(None)
        self.shared_data_cleared.emit()
        logger.error("共享 AppContext 构建失败: {}", error)
        problem = SharedDataProblem(
            SharedDataProblemCode.UNEXPECTED,
            "context",
            "无法建立共享数据上下文；请检查设置后重试。",
        )
        self._publish_terminal(SharedDataPhase.FAILED, problem=problem)

    def on_shared_context_build_timeout(self) -> None:
        """使超时 generation 失效，并发布可重试失败。"""
        if self.build_worker is None:
            return
        if self._build_generation != self.generation:
            self.build_worker = None
            self.build_config = None
            self.build_timeout_timer.stop()
            self.flush_pending_runtime_entity_refresh()
            return
        message = build_shared_context_timeout_message(self.build_config)
        self.build_request_id += 1
        self.build_worker = None
        self.build_config = None
        self.app_context = None
        self.app_context_changed.emit(None)
        self.shared_data_cleared.emit()
        problem = SharedDataProblem(SharedDataProblemCode.SOURCE_UNAVAILABLE, "context", message)
        self._publish_terminal(SharedDataPhase.FAILED, problem=problem, notice_title="共享数据加载超时")

    def _start_scan(self, generation: int, phase: SharedDataPhase) -> None:
        """启动当前 generation 唯一的完整目录扫描 owner。"""
        if generation != self.generation or self.app_context is None or self._scan_worker is not None:
            return
        self._replace_state(phase=phase, progress=None)
        worker = self._scan_worker_cls(
            self.app_context,
            generation,
            require_resources=self._get_config().prepare_data_on_startup,
        )
        worker.progress.connect(self._on_scan_progress_payload)
        worker.result_ready.connect(self._on_scan_finished_payload)
        worker.error.connect(self._on_scan_error_payload)
        self._scan_worker = worker
        self._scan_generation = generation
        worker.start()

    def _on_scan_progress_payload(self, progress) -> None:
        """按 progress 自带 generation 路由扫描进度。"""
        self.on_scan_progress(getattr(progress, "generation", self._scan_generation), progress)

    def _on_scan_finished_payload(self, scan: SharedDataScanResult) -> None:
        """按 typed result 自带 generation 路由扫描终态。"""
        self.on_scan_finished(scan.generation, scan)

    def _on_scan_error_payload(self, problem: SharedDataProblem) -> None:
        """把当前 scan owner 的 worker-level 问题路由到 controller。"""
        self.on_scan_error(self._scan_generation, problem)

    def on_scan_progress(self, generation: int, progress) -> None:
        """只接受当前 generation 的扫描进度。"""
        if generation != self.generation or getattr(progress, "generation", generation) != generation:
            return
        self._publish_progress(generation, progress)

    def on_scan_error(self, generation: int, problem: SharedDataProblem) -> None:
        """处理无法形成 typed scan result 的 worker-level 异常。"""
        if generation != self.generation:
            if generation == self._scan_generation:
                self._scan_worker = None
                self.flush_pending_runtime_entity_refresh()
            return
        self._scan_worker = None
        self._publish_terminal(SharedDataPhase.FAILED, problem=problem)

    def _publish_scan_rows(self, scan: SharedDataScanResult) -> None:
        """把同一 typed snapshot 的三个分区一次性发布给页面。"""
        self.entity_data_replaced.emit(EntityRowsPayload.from_rows("champions", scan.champions.rows))
        self.entity_data_replaced.emit(EntityRowsPayload.from_rows("special", scan.special.rows))
        self.entity_data_replaced.emit(EntityRowsPayload.from_rows("maps", scan.maps.rows))

    def on_scan_finished(self, generation: int, scan: SharedDataScanResult) -> None:
        """消费完整扫描真相，决定 ready、自动准备或阻断终态。"""
        if generation != self.generation or scan.generation != generation:
            if generation == self._scan_generation:
                self._scan_worker = None
                self.flush_pending_runtime_entity_refresh()
            return
        self._scan_worker = None
        verifying = self.state.phase is SharedDataPhase.VERIFYING
        preparation = self._prepare_result

        if scan.readiness is SharedDataReadiness.COMPLETE:
            self._publish_scan_rows(scan)
            if verifying and preparation is not None and preparation.stage_result.status is ResultStatus.PARTIAL:
                self._publish_terminal(
                    SharedDataPhase.PARTIAL,
                    scan=scan,
                    problem=self._problem_from_stage_result(preparation.stage_result),
                )
                return
            self._publish_state(
                SharedDataState(
                    SharedDataPhase.READY,
                    generation,
                    summary=scan.summary,
                    prepare_attempted=self.auto_prepare_attempted,
                    prepare_trigger=self._trigger,
                    scan=scan,
                    preparation=preparation,
                )
            )
            logger.info(
                "实体目录已就绪：{} 个英雄、{} 张地图",
                scan.summary.champion_loaded,
                scan.summary.map_loaded,
            )
            if preparation is not None:
                self._emit_notice_once(
                    "prepare_ready",
                    GuiNotice(
                        title="实体数据已更新",
                        content=f"已验证 {scan.summary.champion_loaded} 个英雄、{scan.summary.map_loaded} 张地图。",
                        level="success",
                    ),
                )
            elif self.pending_refresh_notice:
                self._emit_notice_once(
                    "manual_ready",
                    GuiNotice(
                        title="数据已刷新",
                        content="实体目录已经重新检查，可以继续查看或创建任务。",
                        level="success",
                    ),
                )
            self.pending_refresh_notice = False
            self.flush_pending_runtime_entity_refresh()
            return

        can_auto_prepare = (
            not verifying
            and self.allow_auto_prepare_on_reload
            and not self.auto_prepare_attempted
            and scan.all_blocking_problems_repairable
        )
        if can_auto_prepare:
            if any(problem.code is SharedDataProblemCode.DATASET_MISSING for problem in scan.blocking_problems):
                logger.info("当前版本尚未生成基础实体数据，开始提取")
            elif any(problem.code is SharedDataProblemCode.ARTIFACT_CORRUPT for problem in scan.blocking_problems):
                logger.warning("检测到损坏的实体数据，开始重新准备")
            else:
                logger.info("现有实体数据需要更新，开始准备")
            logger.debug(
                "自动准备原因：{}", [(problem.code.value, problem.scope) for problem in scan.blocking_problems]
            )
            self.auto_prepare_attempted = True
            self._prepare_scope = SharedDataRepairScope.from_scan(scan)
            self.start_prepare(
                self._get_config(),
                generation=generation,
                scope=self._prepare_scope,
                force_update=self._prepare_force_update,
            )
            return

        if scan.readiness is SharedDataReadiness.PARTIAL:
            self._publish_scan_rows(scan)
            phase = SharedDataPhase.PARTIAL
        else:
            self.shared_data_cleared.emit()
            phase = SharedDataPhase.FAILED
        problem = next(iter(scan.blocking_problems), None)
        self._publish_terminal(phase, scan=scan, problem=problem)

    def start_prepare(
        self,
        config=None,
        *,
        generation: int | None = None,
        scope: SharedDataRepairScope | None = None,
        force_update: bool = False,
    ) -> None:
        """在后台执行一次 typed update，并转发核心结构化进度。"""
        if self.shared_data_prepare_worker is not None:
            return
        config = config or self._get_config()
        generation = self.generation if generation is None else generation
        if generation != self.generation:
            return
        scope = scope or SharedDataRepairScope(full=True)
        overrides = dict(config.to_app_context_settings())
        prepare_resources = config.prepare_data_on_startup
        inventory = getattr(self.app_context, "runtime_cache", {}).get("source_inventory")
        language = inventory.get_language(config.game_region) if inventory else None
        if prepare_resources and language is not None:
            # 自动准备只覆盖界面已判定可用的实体；用户任务另在启动时复核原始范围。
            champions = tuple(
                int(item.key)
                for item in language.entities
                if item.kind == "champion"
                and item.available
                and (scope.full or int(item.key) in scope.champion_ids)
                and (not scope.full or not should_hide_champion_by_default({"id": item.key, "alias": item.alias}))
            )
            maps = tuple(
                int(item.key)
                for item in language.entities
                if item.kind == "map" and item.available and (scope.full or int(item.key) in scope.map_ids)
            )
            scope = SharedDataRepairScope(full=False, champion_ids=champions, map_ids=maps)
            self._prepare_scope = scope

        def run_prepare(signals) -> SharedDataPreparationResult:
            return self._prepare_shared_entity_data(
                overrides,
                generation=generation,
                scope=scope,
                force_update=force_update,
                prepare_resources=prepare_resources,
                progress_callback=signals.progress.emit,
            )

        worker = self._task_worker_cls(run_prepare, pass_signals=True)
        worker.signals.started.connect(self.on_prepare_started)
        worker.signals.progress.connect(self._on_prepare_progress_payload)
        worker.signals.finished.connect(self._on_prepare_finished_payload)
        worker.signals.failed.connect(self.on_prepare_failed)
        self.shared_data_prepare_worker = worker
        self._prepare_generation = generation
        self._start_worker(worker)

    def _on_prepare_progress_payload(self, progress) -> None:
        """在 controller 所在线程消费当前 prepare owner 的进度。"""
        self.on_prepare_progress(self._prepare_generation, progress)

    def _on_prepare_finished_payload(self, result: SharedDataPreparationResult) -> None:
        """按 typed preparation result 自带 generation 路由终态。"""
        self.on_prepare_finished(result.generation, result)

    def on_prepare_started(self, generation: int | None = None) -> None:
        """发布 preparing，并为自动迁移发送一次信息通知。"""
        generation = self._prepare_generation if generation is None else generation
        if generation != self.generation:
            return
        self._replace_state(phase=SharedDataPhase.PREPARING, progress=None, prepare_attempted=True)
        logger.debug(
            "开始共享实体数据准备: generation={} full={} champions={} maps={}",
            generation,
            self._prepare_scope.full,
            len(self._prepare_scope.champion_ids),
            len(self._prepare_scope.map_ids),
        )
        self._emit_notice_once(
            "prepare_started",
            GuiNotice(
                title="正在更新实体数据",
                content="检测到旧版或缺失的实体数据，已开始自动更新。",
                level="info",
            ),
        )

    def on_prepare_progress(self, generation: int, progress) -> None:
        """只接受当前准备 owner 的核心结构化进度。"""
        if generation == self.generation:
            self._publish_progress(generation, progress)

    def _publish_progress(self, generation: int, progress) -> None:
        """立即保留阶段边界，并把普通进度刷新节流到固定间隔。"""
        event = getattr(progress, "event", None)
        if event in {"started", "finished"}:
            self._clear_pending_progress()
            self._replace_state(progress=progress)
            if event == "started":
                self.progress_refresh_timer.start()
            return
        if not self.progress_refresh_timer.isActive():
            self._replace_state(progress=progress)
            self.progress_refresh_timer.start()
            return
        self._pending_progress = (generation, progress)

    def _flush_pending_progress(self) -> None:
        """发布节流窗口内最后一份有效进度，并继续下一窗口。"""
        pending = self._pending_progress
        self._pending_progress = None
        if pending is None:
            return
        generation, progress = pending
        if generation != self.generation or not self.state.active:
            return
        self._replace_state(progress=progress)
        self.progress_refresh_timer.start()

    def _clear_pending_progress(self) -> None:
        """清除不能跨 generation 或阶段边界复用的进度。"""
        self._pending_progress = None
        self.progress_refresh_timer.stop()

    def on_prepare_finished(self, generation: int, result: SharedDataPreparationResult) -> None:
        """消费权威 StageResult；success/partial 进入复检，其他状态直接终止。"""
        if generation != self.generation or result.generation != generation:
            if generation == self._prepare_generation:
                self.shared_data_prepare_worker = None
                self.flush_pending_runtime_entity_refresh()
            return
        self.shared_data_prepare_worker = None
        self._prepare_result = result
        status = result.stage_result.status
        if status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL}:
            self._replace_state(phase=SharedDataPhase.VERIFYING, progress=None, preparation=result)
            self._start_scan(generation, SharedDataPhase.VERIFYING)
            return
        if status is ResultStatus.CANCELLED:
            self._publish_terminal(
                SharedDataPhase.CANCELLED,
                problem=self._problem_from_stage_result(result.stage_result),
                preparation=result,
            )
            return
        self._publish_terminal(
            SharedDataPhase.FAILED,
            problem=self._problem_from_stage_result(result.stage_result),
            preparation=result,
        )

    def on_prepare_failed(self, generation: int | str, error: str | None = None) -> None:
        """处理 prepare adapter 自身未返回 typed result 的程序错误。"""
        if error is None:
            error = str(generation)
            generation = self._prepare_generation
        if generation != self.generation:
            if generation == self._prepare_generation:
                self.shared_data_prepare_worker = None
            return
        self.shared_data_prepare_worker = None
        logger.error("共享数据 prepare adapter 失败: {}", error)
        problem = SharedDataProblem(
            SharedDataProblemCode.UNEXPECTED,
            "update",
            "实体数据准备发生未预期错误；请重试或查看日志。",
        )
        self._publish_terminal(SharedDataPhase.FAILED, problem=problem)

    def _publish_terminal(
        self,
        phase: SharedDataPhase,
        *,
        problem: SharedDataProblem | None,
        scan: SharedDataScanResult | None = None,
        preparation: SharedDataPreparationResult | None = None,
        notice_title: str | None = None,
    ) -> None:
        """发布等待配置或准备终态，并发送一次可操作通知。"""
        self._publish_state(
            SharedDataState(
                phase,
                self.generation,
                summary=scan.summary if scan is not None else None,
                problem=problem,
                prepare_attempted=self.auto_prepare_attempted,
                prepare_trigger=self._trigger,
                scan=scan,
                preparation=preparation or self._prepare_result,
            )
        )
        log_message = (
            f"共享实体数据进入终态: generation={self.generation} phase={phase.value} "
            f"code={problem.code.value if problem is not None else 'unknown'}"
        )
        logger.debug(log_message)
        message = problem.message if problem is not None else "请重试实体数据更新。"
        if phase is SharedDataPhase.FAILED:
            logger.error("实体数据准备失败：{}", message)
        elif (
            phase is SharedDataPhase.BLOCKED
            and problem is not None
            and problem.code is SharedDataProblemCode.CONFIGURATION_REQUIRED
        ):
            logger.info("等待配置：{}", message)
        else:
            logger.warning("实体数据尚未就绪：{}", message)
        level = "error" if phase is SharedDataPhase.FAILED else "warning"
        title = notice_title or {
            SharedDataPhase.BLOCKED: describe_shared_data_state(self.state).status_text,
            SharedDataPhase.PARTIAL: "实体数据未完整",
            SharedDataPhase.CANCELLED: "实体数据准备已取消",
        }.get(phase, "实体数据准备失败")
        content = problem.message if problem is not None else "请重试实体数据更新。"
        self._emit_notice_once(f"terminal:{phase.value}", GuiNotice(title=title, content=content, level=level))
        self.pending_refresh_notice = False
        self.flush_pending_runtime_entity_refresh()

    def refresh_shared_output_state(self, refresh_request: object | None = None) -> None:
        """刷新输出状态；增量成功不改变已验证的共享目录 readiness。"""
        request = refresh_request if isinstance(refresh_request, OutputStateRefreshRequest) else None
        show_notice = request is None or not request.quiet
        if self._has_incomplete_tasks():
            self.notice_requested.emit(
                GuiNotice(
                    title="队列未清空",
                    content="请等待当前任务队列全部结束后再刷新列表数据。",
                    level="warning",
                )
            )
            return
        if self.state.active:
            return
        self.pending_refresh_notice = show_notice
        if self.app_context is None:
            logger.warning("共享上下文尚未就绪，回退到完整共享数据刷新")
            self.request_shared_data_reload(show_notice=show_notice, allow_auto_prepare=True)
            return

        if request is not None and not request.requires_full_refresh and request.has_incremental_targets():
            try:
                loader = self._entity_data_loader_cls(self.app_context)
                if request.champion_ids or request.special_targets or request.resource_pack_wads:
                    catalog = loader.load_champion_rows_by_targets(
                        champion_ids=request.champion_ids,
                        special_targets=request.special_targets,
                    )
                    if request.champion_ids:
                        self.entity_rows_updated.emit(EntityRowsPayload.from_rows("champions", catalog["champions"]))
                    if request.special_targets or request.resource_pack_wads:
                        self.entity_rows_updated.emit(EntityRowsPayload.from_rows("special", catalog["special"]))
                if request.map_ids:
                    rows = loader.load_entities_by_ids("maps", request.map_ids)
                    self.entity_rows_updated.emit(EntityRowsPayload.from_rows("maps", rows))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"增量刷新共享输出状态失败，回退到全量检查: {exc}")
                self.request_shared_data_reload(show_notice=show_notice, allow_auto_prepare=True)
                return
            if show_notice:
                self.notice_requested.emit(
                    GuiNotice(
                        title="数据已刷新",
                        content="列表内容已经更新，可以继续查看或创建任务。",
                        level="success",
                    )
                )
            self.pending_refresh_notice = False
            return

        self.request_shared_data_reload(show_notice=show_notice, allow_auto_prepare=True)

    def reload_unpack_data(self, config=None) -> None:
        """按当前配置显式重建共享目录。"""
        self.request_shared_data_reload(show_notice=False, allow_auto_prepare=True, config=config)

    def on_context_input_changed(self, config=None) -> None:
        """配置签名变化时使旧 generation 失效，并安排检查。"""
        config = config or self._get_config()
        reader_signature = build_shared_entity_reader_signature(config)
        scan_signature = build_shared_entity_scan_signature(config)
        reader_changed = reader_signature != self.reader_signature
        scan_changed = scan_signature != self.scan_signature
        if scan_changed:
            self.reconfigure_runtime_logging_requested.emit(RuntimeLoggingConfig.from_gui_config(config))
        self.reader_signature = reader_signature
        self.scan_signature = scan_signature
        if not reader_changed and not scan_changed:
            return

        self.pending_runtime_entity_refresh = True
        self.pending_refresh_allow_prepare = self.pending_refresh_allow_prepare or reader_changed or scan_changed
        generation = self._next_generation(SharedDataPrepareTrigger.CONTEXT_CHANGE)
        self._publish_state(
            SharedDataState(
                SharedDataPhase.WAITING,
                generation,
                prepare_trigger=SharedDataPrepareTrigger.CONTEXT_CHANGE,
            )
        )
        self.schedule_runtime_entity_refresh()

    def schedule_runtime_entity_refresh(self) -> None:
        """在任务与旧 worker 清空后安排一次配置刷新。"""
        if self._has_incomplete_tasks() or self._has_running_worker():
            return
        self.runtime_entity_refresh_timer.start()

    def flush_pending_runtime_entity_refresh(self) -> None:
        """在队列和后台 owner 均空闲时继续 waiting generation。"""
        if not self.pending_runtime_entity_refresh:
            return
        if self._has_incomplete_tasks() or self._has_running_worker():
            return
        allow_auto_prepare = self.pending_refresh_allow_prepare
        self.pending_runtime_entity_refresh = False
        self.pending_refresh_allow_prepare = False
        self.allow_auto_prepare_on_reload = allow_auto_prepare
        self.load_initial_data(
            self._get_config(),
            trigger=SharedDataPrepareTrigger.CONTEXT_CHANGE,
            generation=self.generation,
        )

    def request_shared_data_reload(
        self,
        *,
        show_notice: bool,
        allow_auto_prepare: bool,
        config=None,
    ) -> None:
        """启动一次手动或内部共享目录刷新。"""
        self.pending_refresh_notice = show_notice
        if self._has_running_worker():
            self.pending_runtime_entity_refresh = True
            self.pending_refresh_allow_prepare = self.pending_refresh_allow_prepare or allow_auto_prepare
            trigger = (
                SharedDataPrepareTrigger.MANUAL_REFRESH if show_notice else SharedDataPrepareTrigger.CONTEXT_CHANGE
            )
            generation = self._next_generation(trigger)
            self._publish_state(
                SharedDataState(
                    SharedDataPhase.WAITING,
                    generation,
                    prepare_trigger=trigger,
                )
            )
            return
        self.allow_auto_prepare_on_reload = allow_auto_prepare
        trigger = SharedDataPrepareTrigger.MANUAL_REFRESH if show_notice else SharedDataPrepareTrigger.CONTEXT_CHANGE
        self.load_initial_data(config or self._get_config(), trigger=trigger)

    def request_shared_data_retry(self, *, force_update: bool = False) -> None:
        """显式开始新的 retry generation；force 仅用于用户选择的二级恢复。"""
        if self._has_incomplete_tasks() or self._has_running_worker():
            return
        self.allow_auto_prepare_on_reload = True
        generation = self._next_generation(SharedDataPrepareTrigger.MANUAL_RETRY)
        self._prepare_force_update = force_update
        self.load_initial_data(
            self._get_config(),
            trigger=SharedDataPrepareTrigger.MANUAL_RETRY,
            generation=generation,
        )

    def _apply_blocked_state(
        self,
        message: str,
        *,
        trigger: SharedDataPrepareTrigger = SharedDataPrepareTrigger.INITIAL,
    ) -> None:
        """把必要配置缺失发布为 blocked，而不是运行失败。"""
        generation = self._next_generation(trigger)
        self.build_timeout_timer.stop()
        self.build_worker = None
        self.build_config = None
        self.app_context = None
        self.shared_data_cleared.emit()
        self.app_context_changed.emit(None)
        problem = SharedDataProblem(SharedDataProblemCode.CONFIGURATION_REQUIRED, "context", message)
        self._publish_state(
            SharedDataState(
                SharedDataPhase.BLOCKED,
                generation,
                problem=problem,
                prepare_trigger=trigger,
            )
        )
        if self.pending_refresh_notice:
            self._emit_notice_once("blocked", GuiNotice(title="无法刷新数据", content=message, level="warning"))
            self.pending_refresh_notice = False

    @staticmethod
    def _stop_thread(worker, *, wait_ms: int = 100) -> None:
        """尽力停止一个短生命周期 ``QThread``。"""
        if worker is None:
            return
        try:
            if not getattr(worker, "isRunning", lambda: False)():
                return
        except RuntimeError:
            return
        for method_name in ("requestInterruption", "quit"):
            method = getattr(worker, method_name, None)
            if callable(method):
                method()
        wait = getattr(worker, "wait", None)
        if callable(wait):
            try:
                if wait(wait_ms):
                    return
            except RuntimeError:
                return
        terminate = getattr(worker, "terminate", None)
        if callable(terminate):
            terminate()

    def shutdown_background_work(self) -> None:
        """在窗口关闭前失效 generation 并停止接受后台回调。"""
        self._closed = True
        self.elapsed_timer.stop()
        self.generation += 1
        self.runtime_entity_refresh_timer.stop()
        self.build_timeout_timer.stop()
        self._clear_pending_progress()
        self.pending_runtime_entity_refresh = False
        self.pending_refresh_allow_prepare = False
        self.pending_refresh_notice = False
        self._stop_thread(self._scan_worker)
        self._stop_thread(self._champions_worker)
        self._stop_thread(self._maps_worker)
        self._scan_worker = None
        self._champions_worker = None
        self._maps_worker = None
        self.build_worker = None
        self.shared_data_prepare_worker = None
        self.is_loading_shared_data = False
        self.is_preparing_shared_data = False
