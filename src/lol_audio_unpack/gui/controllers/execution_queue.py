"""执行中心队列状态机控制器。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from threading import Event

from loguru import logger
from PySide6.QtCore import QObject, QThreadPool, Signal

from lol_audio_unpack.app.resource_pack import partition_special_targets
from lol_audio_unpack.app.results import EntityResult, ResultStatus, RunResult, StageResult
from lol_audio_unpack.gui.controllers.contracts import (
    GuiLogMessage,
    GuiNotice,
    QueueProgressUpdate,
)
from lol_audio_unpack.gui.controllers.task_queue_store import (
    TaskQueueStore,
    build_row_text,
)
from lol_audio_unpack.gui.service.execution_process_worker import ExecutionProcessWorker
from lol_audio_unpack.gui.service.task_preflight import check_task, use_builtin
from lol_audio_unpack.gui.task_models import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PARTIAL,
    TASK_STATUS_RUNNING,
    TASK_STATUS_WAITING,
    ExecutionTaskDraft,
    ExecutionTaskParamsSnapshot,
    ExecutionTaskProgress,
    ExecutionTaskResult,
    OutputStateRefreshRequest,
    QueuedExecutionTask,
)
from lol_audio_unpack.gui.workers import TaskWorker


def _build_output_state_refresh_request(
    task: QueuedExecutionTask,
    task_result: ExecutionTaskResult,
) -> OutputStateRefreshRequest | None:
    """从 typed artifact evidence 推导有界输出状态刷新范围。"""
    artifact_entities = tuple(
        entity
        for stage in task_result.run_result.stages
        if stage.stage in {"extract", "mapping"}
        for entity in stage.entities
        if entity.artifacts
    )
    if not artifact_entities:
        return None

    task_params = task.draft.task_params
    partition = partition_special_targets(task_params.special_targets)
    special_champion_by_id = {target.removeprefix("champion:"): target for target in partition.champion_targets}
    champion_ids: list[str] = []
    map_ids: list[str] = []
    special_targets: list[str] = []
    resource_pack_seen = False
    for entity in artifact_entities:
        entity_id = str(entity.entity_id)
        if entity.entity_type == "champion":
            special_target = special_champion_by_id.get(entity_id)
            if special_target is not None:
                special_targets.append(special_target)
            else:
                champion_ids.append(entity_id)
        elif entity.entity_type == "map":
            map_ids.append(entity_id)
        elif entity.entity_type == "resource_pack":
            special_targets.append(entity_id)
            resource_pack_seen = True

    def unique(values: list[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(values))

    request = OutputStateRefreshRequest(
        champion_ids=unique(champion_ids),
        map_ids=unique(map_ids),
        special_targets=unique(special_targets),
        resource_pack_wads=task_params.resource_pack_wads if resource_pack_seen else (),
        quiet=True,
    )
    return request if request.has_incremental_targets() else None


def _terminal_task_status(status: ResultStatus) -> str:
    """把核心结果状态映射为稳定 GUI 终态。"""
    return {
        ResultStatus.SUCCESS: TASK_STATUS_COMPLETED,
        ResultStatus.PARTIAL: TASK_STATUS_PARTIAL,
        ResultStatus.FAILED: TASK_STATUS_FAILED,
        ResultStatus.CANCELLED: TASK_STATUS_CANCELLED,
    }[status]


def _terminal_notice(status: ResultStatus, summary: str) -> GuiNotice:
    """按核心结果状态构造终态通知。"""
    if status is ResultStatus.SUCCESS:
        return GuiNotice(title="任务执行完成", content=summary, level="success", terminal=True)
    if status is ResultStatus.PARTIAL:
        return GuiNotice(title="任务部分完成", content=summary, level="warning", terminal=True)
    if status is ResultStatus.CANCELLED:
        return GuiNotice(title="任务已取消", content=summary, level="warning", terminal=True)
    return GuiNotice(title="任务执行失败", content=summary, level="error", terminal=True)


class ExecutionQueueController(QObject):
    """负责执行中心任务队列状态机与后台 worker 生命周期。"""

    task_running_changed = Signal(bool)
    task_queue_busy_changed = Signal(bool)
    progress_display_requested = Signal(object)
    feedback_requested = Signal(object)
    log_requested = Signal(object)
    output_state_refresh_requested = Signal(object)
    result_ready = Signal(object, object)
    preflight_decision_requested = Signal(object, object)
    tool_probes_ready = Signal(object)

    def __init__(
        self,
        *,
        build_task_item_tooltip: Callable[[QueuedExecutionTask], str],
        single_task_mode: bool = False,
        parent: QObject | None = None,
    ) -> None:
        """初始化执行队列控制器。"""
        super().__init__(parent)
        self._queue_store = TaskQueueStore()
        self._build_task_item_tooltip = build_task_item_tooltip
        self._single_task_mode = single_task_mode
        self._draft_count = 0
        self._active_task_id: int | None = None
        self._active_worker: ExecutionProcessWorker | object | None = None
        self._ignored_task_ids: set[int] = set()
        self._stage_completion_notifications: set[tuple[int, str]] = set()
        self._probe_worker: TaskWorker | None = None
        self._probe_cancel: Event | None = None
        self._probe_failures: tuple = ()

    @property
    def active_task_id(self) -> int | None:
        """返回当前运行中的任务编号。"""
        return self._active_task_id

    def is_task_running(self) -> bool:
        """返回当前是否存在运行中的任务。"""
        return self._active_task_id is not None

    def set_log_level(self, level: int) -> None:
        """把当前采集等级同步给正在运行的任务进程。"""
        if self._active_worker is not None:
            self._active_worker.set_log_level(level)

    def has_active_background_work(self) -> bool:
        """返回执行中心是否仍持有运行中的后台任务。"""
        return bool(self._active_task_id is not None or self._active_worker is not None)

    def has_incomplete_tasks(self) -> bool:
        """返回当前队列中是否仍有等待或运行中的任务。"""
        counts = self.queue_status_counts()
        return counts[TASK_STATUS_RUNNING] + counts[TASK_STATUS_WAITING] > 0

    def draft_queue_size(self) -> int:
        """返回当前任务队列中的真实任务数。"""
        return self._queue_store.task_count()

    def queue_status_counts(self) -> dict[str, int]:
        """统计当前任务队列中的状态数量。"""
        return self._queue_store.status_counts()

    def find_task_by_id(self, task_id: int) -> QueuedExecutionTask | None:
        """按任务编号查找任务快照。"""
        return self._queue_store.find_task_by_id(task_id)

    def find_running_task(self) -> QueuedExecutionTask | None:
        """返回当前队列中处于运行态的任务快照。"""
        return self._queue_store.find_running_task()

    def enqueue_task(self, *, draft: ExecutionTaskDraft, summary: str) -> QueuedExecutionTask:
        """将任务草稿加入队列，并尽可能立即启动。"""
        if self._single_task_mode:
            self._clear_single_task_history()
            current_task = self.find_running_task()
            if current_task is None:
                current_task = next(iter(self._queue_store.tasks()), None)
            if current_task is not None:
                logger.debug("[执行中心] 单任务模式已忽略重复创建请求")
                return current_task

        self._draft_count += 1
        queued_task = self._queue_store.append_task(
            QueuedExecutionTask(task_id=self._draft_count, draft=draft, summary=summary)
        )
        row_text = build_row_text(queued_task)

        started_task = self.start_next_waiting_task()
        if started_task is None:
            self.log_requested.emit(GuiLogMessage(level="info", message=f"[队列] {row_text}"))
            self.progress_display_requested.emit(
                QueueProgressUpdate(
                    note_text="0% · 当前任务等待启动。",
                    progress_current=0,
                    progress_total=1,
                )
            )
        else:
            self.progress_display_requested.emit(QueueProgressUpdate())

        if not self._single_task_mode:
            self.feedback_requested.emit(
                GuiNotice(
                    title="任务已创建" if self._single_task_mode else "已加入任务队列",
                    content=summary if self._single_task_mode else row_text,
                    level="success",
                )
            )
        return queued_task

    def update_task(self, task_id: int, **changes) -> QueuedExecutionTask:
        """更新任务快照。"""
        return self._queue_store.update_task(task_id, **changes)

    def start_next_waiting_task(self) -> QueuedExecutionTask | None:
        """将下一个等待中的任务提升为运行中。"""
        if self.find_running_task() is not None:
            return None

        for task in self._queue_store.tasks():
            if task.status != TASK_STATUS_WAITING:
                continue
            updated_task = self.update_task(
                task.task_id,
                status=TASK_STATUS_RUNNING,
                started_at=datetime.now(),
                progress_current=0,
                progress_total=0,
                progress_message="等待后台线程启动…",
                progress_detail=None,
                error_message="",
            )
            self._active_task_id = updated_task.task_id
            self.task_running_changed.emit(True)
            self.task_queue_busy_changed.emit(self.has_incomplete_tasks())
            self.progress_display_requested.emit(
                QueueProgressUpdate(
                    note_text="当前进度：准备中 · 等待后台线程启动。",
                    progress_current=0,
                    progress_total=1,
                )
            )
            self.start_task_worker(updated_task)
            return updated_task

        self.task_queue_busy_changed.emit(self.has_incomplete_tasks())
        return None

    def start_task_worker(self, task: QueuedExecutionTask) -> None:
        """先在后台检查输入与工具，全部通过后才创建正式任务进程。"""
        if not task.draft.tools_checked:
            self._probe_cancel = Event()
            cancel = self._probe_cancel
            worker = TaskWorker(lambda: check_task(task, cancel))
            worker.signals.finished.connect(lambda results: self._finish_preflight(task, results))
            worker.signals.failed.connect(lambda error: self._fail_preflight(task, error))
            self._probe_worker = worker
            self.update_task(task.task_id, progress_message="正在检查输入与处理工具…")
            QThreadPool.globalInstance().start(worker)
            return
        self._start_execution(task)

    def _start_execution(self, task: QueuedExecutionTask) -> None:
        worker = ExecutionProcessWorker(task, parent=self)
        worker.signals.started.connect(lambda task_id=task.task_id: self.on_task_started(task_id))
        worker.signals.progress.connect(lambda progress, task_id=task.task_id: self.on_task_progress(task_id, progress))
        worker.signals.finished.connect(lambda result, task_id=task.task_id: self.on_task_finished(task_id, result))
        worker.signals.failed.connect(lambda error, task_id=task.task_id: self.on_task_failed(task_id, error))
        self._active_worker = worker
        worker.start()

    def _finish_preflight(self, task: QueuedExecutionTask, results: tuple) -> None:
        if task.task_id != self._active_task_id:
            return
        self._probe_worker = None
        if self._probe_cancel is not None and self._probe_cancel.is_set():
            self._probe_cancel = None
            self.cancel_active_task()
            return
        failures = tuple(result for result in results if not result.success)
        self.tool_probes_ready.emit(results)
        if failures:
            if all(issue.can_fallback for issue in failures):
                self._probe_failures = failures
                self.update_task(task.task_id, progress_message="任务正在等待您的选择")
                self.preflight_decision_requested.emit(task, failures)
            else:
                self._fail_preflight(task, "\n".join(issue.detail for issue in failures))
            return
        self._probe_cancel = None
        request = task.draft.export_request
        if request is not None:
            request = replace(request)
        task = self.update_task(task.task_id, draft=replace(task.draft, tools_checked=True, export_request=request))
        self._start_execution(task)

    def _fail_preflight(self, task: QueuedExecutionTask, error: str) -> None:
        if task.task_id != self._active_task_id:
            return
        self._probe_worker = None
        cancelled = self._probe_cancel is not None and self._probe_cancel.is_set()
        self._probe_cancel = None
        if cancelled:
            self.cancel_active_task()
        else:
            self.on_task_failed(task.task_id, error)

    def resolve_preflight(self, task_id: int, *, builtin: bool) -> None:
        """响应启动前的两项选择；关闭或过期结果不能启动任务。"""
        if task_id != self._active_task_id or not self._probe_failures:
            return
        failures, self._probe_failures = self._probe_failures, ()
        self._probe_cancel = None
        if not builtin:
            self.cancel_active_task()
            return
        task = use_builtin(self.find_task_by_id(task_id), failures)
        self.update_task(task_id, draft=task.draft)
        self.start_task_worker(task)

    def on_task_started(self, task_id: int) -> None:
        """处理后台任务真正启动后的摘要更新。"""
        if task_id in self._ignored_task_ids:
            return
        task = self.find_task_by_id(task_id)
        if task is None:
            return
        logger.info(f"[队列] 任务 #{task_id} 已开始执行")
        self.progress_display_requested.emit(
            QueueProgressUpdate(
                note_text=f"当前进度：准备中 · {task.progress_message or '后台任务已开始执行。'}",
                progress_current=0,
                progress_total=1,
            )
        )

    def on_task_progress(self, task_id: int, progress: object) -> None:
        """接收后台任务进度并刷新队列状态。"""
        if task_id in self._ignored_task_ids:
            return
        if not isinstance(progress, ExecutionTaskProgress):
            return

        updated_task = self.update_task(
            task_id,
            progress_current=max(progress.current, 0),
            progress_total=max(progress.total, 0),
            progress_message=progress.message,
            progress_detail=progress,
        )
        if not self._single_task_mode and progress.stage_finished and progress.stage_key == "extract":
            self._emit_extract_stage_notice(updated_task, progress)
        if self._active_task_id == task_id:
            self.progress_display_requested.emit(QueueProgressUpdate())

    def _emit_extract_stage_notice(
        self,
        task: QueuedExecutionTask,
        progress: ExecutionTaskProgress,
    ) -> None:
        """仅在解包成功后发出一次性继续提示。"""
        if progress.message != "音频解包完成":
            return
        notification_key = (task.task_id, "extract")
        if notification_key in self._stage_completion_notifications:
            return
        self._stage_completion_notifications.add(notification_key)
        stage_label = "音频解包阶段"
        content = f"任务 #{task.task_id} 已结束{stage_label}。"
        if task.draft.task_params.wav_enabled and task.draft.task_params.run_mapping:
            content = f"{content} 正在继续音频转码，完成后将继续事件映射。"
        elif task.draft.task_params.wav_enabled:
            content = f"{content} 正在继续音频转码。"
        elif task.draft.task_params.run_mapping:
            content = f"{content} 正在继续事件映射。"
        self.feedback_requested.emit(GuiNotice(title=f"{stage_label}已结束", content=content, level="info"))

    def on_task_finished(self, task_id: int, result: object) -> None:
        """根据后台 typed result 收敛任务终态。"""
        if task_id in self._ignored_task_ids:
            return
        task = self.find_task_by_id(task_id)
        if task is None:
            return

        if not isinstance(result, ExecutionTaskResult) or not isinstance(result.run_result, RunResult):
            self.on_task_failed(task_id, "后台任务没有返回有效的 RunResult。")
            return

        task_result = result
        result_status = task_result.run_result.status
        terminal_status = _terminal_task_status(result_status)
        progress_total = max(task.progress_total, 1)
        if result_status is ResultStatus.SUCCESS:
            progress_current = progress_total
        else:
            progress_current = min(task.progress_current, progress_total - 1)
        terminal_progress_detail = None
        if isinstance(task.progress_detail, ExecutionTaskProgress):
            terminal_progress_detail = replace(
                task.progress_detail,
                current=progress_current,
                total=progress_total,
                message=task_result.summary,
            )

        updated_task = self.update_task(
            task_id,
            status=terminal_status,
            finished_at=datetime.now(),
            progress_current=progress_current,
            progress_total=progress_total,
            progress_message=task_result.summary,
            progress_detail=terminal_progress_detail,
            result_summary=task_result.summary,
            error_message=task_result.summary if result_status is ResultStatus.FAILED else "",
        )
        self._after_task_stopped(task_id)
        log_message = f"[队列] 任务 #{task_id} {task_result.summary}"
        if result_status is ResultStatus.SUCCESS:
            logger.success(log_message)
        elif result_status in {ResultStatus.PARTIAL, ResultStatus.CANCELLED}:
            logger.warning(log_message)
        else:
            logger.error(log_message)
        self._advance_or_finish(updated_task, task_result)
        self.result_ready.emit(updated_task, task_result)
        self.feedback_requested.emit(_terminal_notice(result_status, task_result.summary))

    def on_task_failed(self, task_id: int, error: str) -> None:
        """处理后台任务失败后的状态收敛。"""
        if task_id in self._ignored_task_ids:
            return
        task = self.find_task_by_id(task_id)
        if task is None:
            return

        failed_progress_detail = None
        if isinstance(task.progress_detail, ExecutionTaskProgress):
            failed_progress_detail = replace(task.progress_detail, message=error)
        progress_total = max(task.progress_total, 1)
        progress_current = min(task.progress_current, progress_total - 1)
        updated_task = self.update_task(
            task_id,
            status=TASK_STATUS_FAILED,
            finished_at=datetime.now(),
            progress_current=progress_current,
            progress_total=progress_total,
            progress_message=error,
            progress_detail=failed_progress_detail,
            error_message=error,
        )
        self._after_task_stopped(task_id)
        logger.error(f"[队列] 任务 #{task_id} 执行失败：{error}")
        failed_result = ExecutionTaskResult(
            completed_steps=(),
            summary=error,
            duration_seconds=0.0,
            run_result=RunResult(
                (
                    StageResult(
                        "run",
                        status=ResultStatus.FAILED,
                        error_type="WorkerError",
                        error_message=error,
                    ),
                )
            ),
        )
        self._advance_or_finish(updated_task, failed_result)
        self.result_ready.emit(updated_task, failed_result)
        self.feedback_requested.emit(_terminal_notice(ResultStatus.FAILED, error))

    def cancel_active_task(self) -> bool:
        """强制结束当前运行中的任务，并将其收口为已取消。"""
        task = self.find_running_task()
        if task is None:
            return False

        if self._probe_cancel is not None:
            self._probe_cancel.set()
            if self._probe_worker is not None:
                self.update_task(task.task_id, progress_message="正在取消预检…")
                return True
            self._probe_cancel = None
            self._probe_failures = ()

        self._ignored_task_ids.add(task.task_id)
        try:
            self._stop_active_worker()
        except (OSError, RuntimeError) as exc:
            self._ignored_task_ids.discard(task.task_id)
            logger.error("结束任务失败，保留运行状态：{}", exc)
            self.feedback_requested.emit(GuiNotice(title="结束任务失败", content=str(exc), level="error"))
            return False

        cancelled_message = "任务已被强制结束。"
        cancelled_progress_detail = None
        if isinstance(task.progress_detail, ExecutionTaskProgress):
            cancelled_progress_detail = replace(task.progress_detail, message=cancelled_message)

        progress_total = max(task.progress_total, 1)
        progress_current = min(task.progress_current, progress_total - 1)
        updated_task = self.update_task(
            task.task_id,
            status=TASK_STATUS_CANCELLED,
            finished_at=datetime.now(),
            progress_current=progress_current,
            progress_total=progress_total,
            progress_message=cancelled_message,
            progress_detail=cancelled_progress_detail,
            result_summary="",
            error_message="",
        )
        self._after_task_stopped(task.task_id)
        logger.warning(f"[队列] 任务 #{task.task_id} 已被强制结束")
        self.task_queue_busy_changed.emit(self.has_incomplete_tasks())
        self.progress_display_requested.emit(
            QueueProgressUpdate(
                note_text=cancelled_message,
                progress_current=progress_current,
                progress_total=progress_total,
            )
        )
        cancelled_result = ExecutionTaskResult(
            (),
            "任务已强制结束，完成范围未知。",
            0.0,
            RunResult((StageResult.cancelled("run", note="完成范围未知，请检查已有产物。"),)),
            version=task.draft.version,
        )
        self.result_ready.emit(updated_task, cancelled_result)
        self.feedback_requested.emit(_terminal_notice(ResultStatus.CANCELLED, cancelled_result.summary))
        return True

    def _after_task_stopped(self, task_id: int) -> None:
        """清理一个任务结束后的通用运行态。"""
        self._active_task_id = None
        self._active_worker = None
        self._stage_completion_notifications = {
            entry for entry in self._stage_completion_notifications if entry[0] != task_id
        }
        self.task_running_changed.emit(False)

    def _advance_or_finish(self, task: QueuedExecutionTask, task_result: ExecutionTaskResult) -> None:
        """推进下一个等待任务，或收口当前队列状态。"""
        next_task = self.start_next_waiting_task()
        self.task_queue_busy_changed.emit(self.has_incomplete_tasks())
        if next_task is not None:
            self.progress_display_requested.emit(QueueProgressUpdate())
            return

        if task.status == TASK_STATUS_COMPLETED:
            self.progress_display_requested.emit(
                QueueProgressUpdate(
                    note_text=f"100% · {task.result_summary or task_result.summary}",
                    progress_current=1,
                    progress_total=1,
                )
            )
        else:
            self.progress_display_requested.emit(
                QueueProgressUpdate(
                    note_text=f"当前进度：{task.progress_current}/{task.progress_total} · {task.error_message or task_result.summary}",
                    progress_current=task.progress_current,
                    progress_total=task.progress_total,
                )
            )
        refresh_request = _build_output_state_refresh_request(task, task_result)
        if refresh_request is not None:
            self.output_state_refresh_requested.emit(refresh_request)

    def _clear_single_task_history(self) -> None:
        """在单任务模式下清理已结束的历史任务快照。"""
        if not self._single_task_mode or self.find_running_task() is not None:
            return
        if self.has_incomplete_tasks() or self.draft_queue_size() == 0:
            return

        self._queue_store.clear_tasks()
        self._stage_completion_notifications.clear()
        self._ignored_task_ids.clear()

    def _stop_active_worker(self, *, wait_ms: int = 100) -> None:
        """尽力结束当前执行线程，必要时退化到强制终止。"""
        worker = self._active_worker
        if worker is None:
            return
        try:
            is_running = getattr(worker, "isRunning", lambda: False)()
        except RuntimeError:
            return
        if not is_running:
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

    def clear_draft_queue(self) -> None:
        """清空当前任务队列中可直接移除的任务项。"""
        running_task = self.find_running_task()
        if running_task is not None:
            removed_count = self._queue_store.clear_non_running_tasks()
            if removed_count == 0:
                self.log_requested.emit(
                    GuiLogMessage(level="warning", message="[队列] 当前存在运行中的任务，暂不支持直接清空。")
                )
                self.feedback_requested.emit(
                    GuiNotice(
                        title="暂无法清空",
                        content="运行中的真实任务暂不支持直接移出队列，请等待任务结束后再清空。",
                        level="warning",
                    )
                )
                return

            self.log_requested.emit(
                GuiLogMessage(level="info", message=f"[队列] 已清空 {removed_count} 条非运行中任务。")
            )
            self.task_queue_busy_changed.emit(self.has_incomplete_tasks())
            self.progress_display_requested.emit(QueueProgressUpdate())
            return

        self.clear_mock_queue()
        self.log_requested.emit(GuiLogMessage(level="info", message="[队列] 已清空当前任务队列。"))

    def fill_mock_queue(self, *, count: int) -> str:
        """填充指定数量的 mock 队列项。"""
        if count <= 0:
            raise ValueError("queue fill 需要大于 0 的整数参数。")

        self._queue_store.clear_tasks()
        self._draft_count = count
        self._active_task_id = None
        self._active_worker = None
        self._ignored_task_ids.clear()
        self._stage_completion_notifications.clear()

        for task_id in range(1, count + 1):
            task = self._build_mock_task(task_id)
            self._queue_store.append_task(task)
            if task.status == TASK_STATUS_RUNNING and self._active_task_id is None:
                self._active_task_id = task_id

        self.task_running_changed.emit(self._active_task_id is not None)
        self.task_queue_busy_changed.emit(self.has_incomplete_tasks())
        self.progress_display_requested.emit(QueueProgressUpdate())
        return f"已填充 {count} 条 mock 队列项。"

    def _build_mock_task(self, task_id: int) -> QueuedExecutionTask:
        """构造单条调试用 mock 任务。"""
        if task_id == 1:
            status = TASK_STATUS_RUNNING
            progress_current = 1
            progress_total = 3
            progress_message = "Mock 任务运行中"
        elif task_id % 3 == 0:
            status = TASK_STATUS_COMPLETED
            progress_current = 1
            progress_total = 1
            progress_message = "Mock 任务已完成"
        else:
            status = TASK_STATUS_WAITING
            progress_current = 0
            progress_total = 0
            progress_message = ""

        return QueuedExecutionTask(
            task_id=task_id,
            draft=ExecutionTaskDraft(source="dev_console", source_summary="开发控制台填充的 mock 队列"),
            summary=f"Mock任务 {task_id} · 列表调试",
            status=status,
            progress_current=progress_current,
            progress_total=progress_total,
            progress_message=progress_message,
        )

    def clear_mock_queue(self) -> str:
        """清空当前调试队列并恢复空状态。"""
        self._queue_store.clear_tasks()
        self._active_task_id = None
        self._active_worker = None
        self._ignored_task_ids.clear()
        self._stage_completion_notifications.clear()
        self.task_running_changed.emit(False)
        self.task_queue_busy_changed.emit(self.has_incomplete_tasks())
        self.progress_display_requested.emit(QueueProgressUpdate())
        return "已清空当前队列。"

    def simulate_terminal_result(self, status: str) -> str:
        """用 typed result 构造一个可人工验收的调试终态。

        Args:
            status: `success`、`partial`、`failed` 或 `cancelled`。

        Returns:
            开发控制台使用的完成说明。

        Raises:
            ValueError: 状态无效或仍有真实后台任务时抛出。
        """
        if self.has_active_background_work():
            raise ValueError("真实后台任务运行时不能注入调试终态。")
        try:
            result_status = ResultStatus(status)
        except ValueError as exc:
            raise ValueError("queue result 仅支持 success、partial、failed、cancelled。") from exc

        self._queue_store.clear_tasks()
        self._ignored_task_ids.clear()
        self._stage_completion_notifications.clear()
        self._draft_count = 1
        task = self._queue_store.append_task(
            QueuedExecutionTask(
                task_id=1,
                draft=ExecutionTaskDraft(
                    source="dev_console",
                    source_summary="受控终态人工验收 fixture",
                    task_params=ExecutionTaskParamsSnapshot(
                        champion_ids=(1, 2),
                        run_extract=True,
                        run_mapping=False,
                    ),
                ),
                summary=f"受控 {result_status.value} 终态",
                status=TASK_STATUS_RUNNING,
                started_at=datetime.now(),
                progress_current=1,
                progress_total=2,
                progress_message="受控 fixture 正在收口…",
                progress_detail=ExecutionTaskProgress(
                    stage_key="extract",
                    stage_label="音频解包",
                    entity_scope_label="英雄",
                    current=1,
                    total=2,
                    message="受控 fixture 正在收口…",
                ),
            )
        )
        self._active_task_id = task.task_id
        self.task_running_changed.emit(True)
        self.task_queue_busy_changed.emit(True)
        self.progress_display_requested.emit(QueueProgressUpdate())

        artifact = "debug-fixture/audios/champion-1/sample.wem"
        if result_status is ResultStatus.SUCCESS:
            stage_result = StageResult.from_entities(
                "extract",
                (EntityResult("champion", 1, ResultStatus.SUCCESS, artifacts=(artifact,)),),
            )
            summary = "已完成：音频解包（受控调试 fixture）"
        elif result_status is ResultStatus.PARTIAL:
            stage_result = StageResult.from_entities(
                "extract",
                (
                    EntityResult("champion", 1, ResultStatus.SUCCESS, artifacts=(artifact,)),
                    EntityResult(
                        "champion",
                        2,
                        ResultStatus.FAILED,
                        error_type="ControlledFailure",
                        error_message="受控实体失败",
                    ),
                ),
            )
            summary = "部分完成：成功 1，失败 1；英雄 1 的产物应触发有界刷新（受控调试 fixture）"
        elif result_status is ResultStatus.FAILED:
            stage_result = StageResult.from_entities(
                "extract",
                (
                    EntityResult(
                        "champion",
                        1,
                        ResultStatus.FAILED,
                        error_type="ControlledFailure",
                        error_message="受控阶段失败",
                    ),
                ),
            )
            summary = (
                "执行失败：这是用于检查错误通知层级、非 100% 终态以及长错误摘要截断的受控故障；"
                "没有确认落盘的产物，因此不应触发实体输出刷新。"
            )
        else:
            stage_result = StageResult.cancelled("extract", note="受控取消")
            summary = "已取消：受控 fixture 未启动后续阶段，也没有可靠产物快照"

        completed_steps = ("音频解包",) if result_status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL} else ()
        self.on_task_finished(
            task.task_id,
            ExecutionTaskResult(
                completed_steps=completed_steps,
                summary=summary,
                duration_seconds=0.1,
                run_result=RunResult((stage_result,)),
            ),
        )
        return f"已注入 {result_status.value} 受控终态。"

    def shutdown(self) -> None:
        """清理执行中心后台任务引用。"""
        if self._probe_cancel is not None:
            self._probe_cancel.set()
        self._stop_active_worker()
        self._active_task_id = None
        self._active_worker = None
        self._ignored_task_ids.clear()
        self._stage_completion_notifications.clear()

    def inspect_queue(self, *, builder_card_height: int) -> str:
        """返回当前队列的调试信息。"""
        counts = self.queue_status_counts()
        return "\n".join(
            [
                f"queue_count={self.draft_queue_size()}",
                f"active_task_id={self._active_task_id}",
                f"builder_card_height={builder_card_height}",
                (
                    f"running={counts[TASK_STATUS_RUNNING]} "
                    f"waiting={counts[TASK_STATUS_WAITING]} "
                    f"completed={counts[TASK_STATUS_COMPLETED]} "
                    f"partial={counts[TASK_STATUS_PARTIAL]} "
                    f"failed={counts[TASK_STATUS_FAILED]} "
                    f"cancelled={counts[TASK_STATUS_CANCELLED]}"
                ),
            ]
        )
