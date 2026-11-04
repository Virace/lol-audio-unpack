"""执行中心任务独立进程 worker。"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import subprocess
import traceback
from queue import Empty
from typing import Any

from loguru import logger
from PySide6.QtCore import QObject, QTimer

from lol_audio_unpack.gui.service.task_runner import run_execution_task
from lol_audio_unpack.gui.task_models import QueuedExecutionTask
from lol_audio_unpack.gui.workers import WorkerSignals
from lol_audio_unpack.utils.logging import format_log_paths, get_log_level

PROCESS_EVENT_STARTED = "started"
PROCESS_EVENT_FINISHED = "finished"
PROCESS_EVENT_FAILED = "failed"
PROCESS_EVENT_PROGRESS = "progress"
PROCESS_EVENT_LOG = "log"


class _QueueSignalEmitter:
    """向跨进程队列写入单类事件。"""

    def __init__(self, event_name: str, event_queue) -> None:
        """保存事件名称与写入目标队列。"""
        self._event_name = event_name
        self._event_queue = event_queue

    def emit(self, payload: object = None) -> None:
        """将事件写入跨进程传输队列。"""
        self._event_queue.put((self._event_name, payload))


class _ProcessSignals:
    """为子进程任务构造与 GUI worker 对齐的 signal proxy。"""

    def __init__(self, event_queue) -> None:
        """为每类事件挂接单独的队列 emitter。"""
        self.started = _QueueSignalEmitter(PROCESS_EVENT_STARTED, event_queue)
        self.finished = _QueueSignalEmitter(PROCESS_EVENT_FINISHED, event_queue)
        self.failed = _QueueSignalEmitter(PROCESS_EVENT_FAILED, event_queue)
        self.progress = _QueueSignalEmitter(PROCESS_EVENT_PROGRESS, event_queue)


def _run_task_process(task: QueuedExecutionTask, event_queue, log_level) -> None:
    """在子进程中执行单条任务，并通过队列回传事件。"""
    if os.name != "nt":
        # 上游转码池继承进程组，取消时可连同其工作进程一起回收。
        os.setsid()

    def forward_log(message) -> None:
        record = message.record.copy()
        exception = record["exception"]
        if exception is not None:
            # traceback 不能直接跨进程传输，先格式化以保留原异常链和源码位置。
            record["message"] += "\n" + "".join(traceback.format_exception(*exception)).rstrip()
        record["exception"] = None
        record["extra"] = {**record["extra"], "task_id": task.task_id, "operation_id": task.draft.operation_id}
        event_queue.put((PROCESS_EVENT_LOG, record))

    # spawn 不继承 GUI 的 sink；只向父进程传递，由父进程统一过滤和写入会话文件。
    logger.remove()
    logger.enable("lol_audio_unpack")
    # 共享门槛随界面设置更新，低等级消息在入队前过滤。
    sink_id = logger.add(
        forward_log,
        level="TRACE",
        filter=lambda record: record["level"].no >= log_level.value,
        backtrace=False,
        diagnose=False,
    )
    signals = _ProcessSignals(event_queue)
    signals.started.emit()
    logger.debug("任务 #{} 进程已启动：operation_id={}", task.task_id, task.draft.operation_id)
    try:
        result = run_execution_task(task, signals)
    except Exception as exc:  # noqa: BLE001
        logger.exception("任务 #{} 执行发生未预期错误", task.task_id)
        signals.failed.emit(str(exc))
    else:
        logger.trace("任务 #{} 进程执行结束", task.task_id)
        signals.finished.emit(result)
    finally:
        logger.remove(sink_id)


class ExecutionProcessWorker(QObject):
    """用独立子进程承载单条执行中心任务。"""

    def __init__(
        self,
        task: QueuedExecutionTask,
        *,
        mp_context: Any | None = None,
        poll_interval_ms: int = 25,
        parent: QObject | None = None,
    ) -> None:
        """初始化子进程 worker。

        Args:
            task: 待执行的任务快照。
            mp_context: 可注入的 multiprocessing context；测试时可替换。
            poll_interval_ms: 主线程轮询事件队列的间隔。
            parent: Qt 父对象。
        """
        super().__init__(parent)
        self.signals = WorkerSignals()
        self._ctx = mp_context or mp.get_context("spawn")
        self._event_queue = self._ctx.Queue()
        self._log_level = self._ctx.Value("i", get_log_level())
        self._process = self._ctx.Process(target=_run_task_process, args=(task, self._event_queue, self._log_level))
        self._transport_closed = False
        self._terminal_seen = False
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(poll_interval_ms)
        self._poll_timer.timeout.connect(self._pump_events)

    def start(self) -> None:
        """启动子进程并开始轮询事件。"""
        self.set_log_level(get_log_level())
        self._process.start()
        self._poll_timer.start()

    def set_log_level(self, level: int) -> None:
        """同步当前输出端需要的最低采集等级。"""
        self._log_level.value = level

    def isRunning(self) -> bool:  # noqa: N802
        """返回子进程是否仍在运行。"""
        return bool(self._process.is_alive())

    def requestInterruption(self) -> None:  # noqa: N802
        """保留与线程 worker 一致的接口，但不做额外操作。"""
        return None

    def quit(self) -> None:
        """保留与线程 worker 一致的接口，但不做额外操作。"""
        return None

    def wait(self, timeout_ms: int) -> bool:
        """在给定超时时间内等待子进程退出。"""
        self._process.join(timeout_ms / 1000)
        self._pump_events()
        finished = not self._process.is_alive()
        if finished:
            self._finalize_transport()
        return finished

    def terminate(self) -> None:
        """结束本任务进程树，避免取消后上游转码池继续运行。"""
        if self._process.is_alive():
            pid = getattr(self._process, "pid", None)
            if pid is not None:
                if os.name == "nt":
                    result = subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                        check=False,
                    )
                    if result.returncode and self._process.is_alive():
                        raise RuntimeError("无法结束任务进程树，仍保留运行状态，请再次尝试取消。")
                else:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                logger.info("已结束任务进程树：{}", pid)
            self._process.terminate()
            self._process.join()
        self._finalize_transport()

    def _pump_events(self) -> None:
        """将子进程发回的事件转发为 Qt 信号。"""
        if self._transport_closed:
            return
        while True:
            try:
                event_name, payload = self._event_queue.get_nowait()
            except Empty:
                break

            if event_name == PROCESS_EVENT_STARTED:
                self.signals.started.emit()
            elif event_name == PROCESS_EVENT_PROGRESS:
                self.signals.progress.emit(payload)
            elif event_name == PROCESS_EVENT_LOG:
                # 恢复子进程时间和调用点，再统一转换日志路径。
                def restore_record(record, event=payload):
                    record.update(event)
                    format_log_paths(record)

                logger.patch(restore_record).log(payload["level"].no, payload["message"])
            elif event_name == PROCESS_EVENT_FINISHED:
                self._terminal_seen = True
                self.signals.finished.emit(payload)
            elif event_name == PROCESS_EVENT_FAILED:
                self._terminal_seen = True
                self.signals.failed.emit(str(payload))

        if not self._process.is_alive():
            if not self._terminal_seen:
                self._terminal_seen = True
                code = getattr(self._process, "exitcode", None)
                self.signals.failed.emit(f"任务进程异常退出（退出码 {code}），完成范围未知，请检查已有产物。")
            self._finalize_transport()

    def _finalize_transport(self) -> None:
        """关闭事件队列并停止轮询。"""
        if self._transport_closed:
            return
        self._poll_timer.stop()
        close = getattr(self._event_queue, "close", None)
        if callable(close):
            close()
        join_thread = getattr(self._event_queue, "join_thread", None)
        if callable(join_thread):
            join_thread()
        self._transport_closed = True
