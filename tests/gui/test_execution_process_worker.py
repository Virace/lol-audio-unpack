"""执行中心独立进程 worker 测试。"""

from __future__ import annotations

import multiprocessing as mp
import os
from dataclasses import replace
from queue import Empty
from types import SimpleNamespace

import pytest
from loguru import logger

from lol_audio_unpack.app.results import ResultStatus, RunResult, StageResult
from lol_audio_unpack.gui.service import execution_process_worker as process_worker
from lol_audio_unpack.gui.service.execution_process_worker import ExecutionProcessWorker
from lol_audio_unpack.gui.task_models import (
    AppContextInputSnapshot,
    ExecutionTaskDraft,
    ExecutionTaskParamsSnapshot,
    ExecutionTaskProgress,
    ExecutionTaskResult,
    QueuedExecutionTask,
)
from lol_audio_unpack.utils.logging import LoggingConfiguration, log_paths


class _FakeQueue:
    """模拟跨进程事件队列。"""

    def __init__(self, events: list[tuple[str, object]]) -> None:
        self._events = list(events)
        self.closed = False
        self.joined = False

    def get_nowait(self) -> tuple[str, object]:
        """依次返回预设事件。"""
        if not self._events:
            raise Empty
        return self._events.pop(0)

    def close(self) -> None:
        """记录关闭调用。"""
        self.closed = True

    def join_thread(self) -> None:
        """记录回收 feeder 线程调用。"""
        self.joined = True


class _FakeProcess:
    """模拟 multiprocessing.Process。"""

    def __init__(self) -> None:
        self.started = False
        self.alive = True
        self.join_calls: list[float] = []
        self.terminated = False

    def start(self) -> None:
        """记录启动调用。"""
        self.started = True

    def is_alive(self) -> bool:
        """返回当前存活状态。"""
        return self.alive

    def join(self, timeout: float | None = None) -> None:
        """记录等待调用。"""
        self.join_calls.append(timeout if timeout is not None else -1.0)

    def terminate(self) -> None:
        """记录强制结束调用。"""
        self.terminated = True
        self.alive = False


class _FakeContext:
    """模拟 multiprocessing context。"""

    def __init__(self, queue_obj: _FakeQueue) -> None:
        self._queue = queue_obj
        self.process: _FakeProcess | None = None
        self.target = None
        self.args = None

    def Queue(self) -> _FakeQueue:  # noqa: N802
        """返回预设事件队列。"""
        return self._queue

    def Value(self, _typecode, value):  # noqa: N802
        """为假进程提供与共享等级值一致的字段。"""
        return SimpleNamespace(value=value)

    def Process(self, *, target, args):  # noqa: N802
        """返回预设进程对象，并记录入口。"""
        self.target = target
        self.args = args
        self.process = _FakeProcess()
        return self.process


def _build_task() -> QueuedExecutionTask:
    """构造测试用任务快照。"""
    return QueuedExecutionTask(
        task_id=1,
        draft=ExecutionTaskDraft(source="manual_input", source_summary="手动输入"),
        summary="测试任务",
    )


def test_execution_process_worker_forwards_child_events(qtbot) -> None:
    """子进程发回的 started/progress/finished 事件应原样转发到 Qt 信号。"""
    result = ExecutionTaskResult(
        completed_steps=("音频解包",),
        summary="执行完成",
        duration_seconds=1.2,
        run_result=RunResult((StageResult("extract"),)),
    )
    progress = ExecutionTaskProgress(
        stage_key="extract",
        stage_label="音频解包",
        current=1,
        total=3,
        message="正在处理: Annie",
    )
    queue_obj = _FakeQueue(
        [
            ("started", None),
            ("progress", progress),
            ("finished", result),
        ]
    )
    context = _FakeContext(queue_obj)
    worker = ExecutionProcessWorker(
        _build_task(),
        mp_context=context,
        poll_interval_ms=1,
    )

    started = []
    progress_events = []
    finished = []
    worker.signals.started.connect(lambda: started.append(True))
    worker.signals.progress.connect(progress_events.append)
    worker.signals.finished.connect(finished.append)

    worker.start()
    assert context.process is not None
    context.process.alive = False

    worker._pump_events()

    assert started == [True]
    assert progress_events == [progress]
    assert finished == [result]
    assert queue_obj.closed is True
    assert queue_obj.joined is True


def test_execution_process_worker_terminate_only_stops_child_process(qtbot) -> None:
    """强制结束时只应终止子进程并回收事件通道。"""
    queue_obj = _FakeQueue([])
    context = _FakeContext(queue_obj)
    worker = ExecutionProcessWorker(
        replace(_build_task(), task_id=2),
        mp_context=context,
        poll_interval_ms=1,
    )

    worker.start()
    assert context.process is not None

    worker.terminate()

    assert context.process.terminated is True
    assert queue_obj.closed is True
    assert queue_obj.joined is True


def test_worker_exit_without_terminal_event_reports_unknown_scope(qtbot) -> None:
    """进程崩溃而未回传终态时必须解除等待并报告未知范围，不能伪装成功。"""
    context = _FakeContext(_FakeQueue([]))
    worker = ExecutionProcessWorker(_build_task(), mp_context=context)
    failures = []
    worker.signals.failed.connect(failures.append)
    worker.start()
    context.process.alive = False
    worker._pump_events()
    assert len(failures) == 1 and "完成范围未知" in failures[0]
    worker._pump_events()
    assert len(failures) == 1


def test_spawn_task_logs_reach_parent_before_result(qtbot, tmp_path, monkeypatch) -> None:
    """真实 spawn 中的诊断和异常须保留来源，并接受各输出端等级过滤。"""
    task = replace(
        _build_task(),
        draft=ExecutionTaskDraft(
            "test",
            "invalid source",
            context_input=AppContextInputSnapshot(
                (
                    ("GAME_PATH", str(tmp_path / "missing-game")),
                    ("OUTPUT_PATH", str(tmp_path / "output")),
                    ("GAME_REGION", "en_US"),
                )
            ),
            task_params=ExecutionTaskParamsSnapshot(run_extract=True, run_mapping=False),
        ),
    )
    monkeypatch.setattr(LoggingConfiguration, "log_level", logger.level("DEBUG").no)
    monkeypatch.setattr(LoggingConfiguration, "paths", ())
    monkeypatch.setattr(LoggingConfiguration, "path_patterns", ())
    logger.enable("lol_audio_unpack")
    log_paths(tmp_path / "missing-game", tmp_path / "output")
    records = []
    visible = []
    debug_sink = logger.add(lambda message: records.append(message.record.copy()), level="DEBUG")
    info_sink = logger.add(lambda message: visible.append(message.record.copy()), level="INFO")
    worker = ExecutionProcessWorker(task)
    results = []
    failures = []
    logs_at_result = []
    worker.signals.finished.connect(lambda result: (results.append(result), logs_at_result.append(len(records))))
    worker.signals.failed.connect(failures.append)
    try:
        worker.start()
        qtbot.waitUntil(lambda: bool(results or failures), timeout=20000)
        assert worker.wait(5000)
    finally:
        if worker.isRunning():
            worker.terminate()
        logger.remove(debug_sink)
        logger.remove(info_sink)

    assert not failures
    assert results[0].run_result.status is ResultStatus.FAILED
    child = [record for record in records if record["process"].id != os.getpid()]
    assert child and len(child) == len(records) == logs_at_result[0]
    assert len({record["message"] for record in child}) == len(child)
    assert any(record["level"].name == "DEBUG" for record in child)
    assert any(record["level"].name == "ERROR" for record in child)
    assert all(record["level"].no >= logger.level("INFO").no for record in visible)
    assert all(record["extra"]["operation_id"] == task.draft.operation_id for record in child)
    assert all(record["function"] != "_pump_events" for record in child)
    assert all(str(tmp_path / "missing-game") not in record["message"] for record in child)
    assert any("AppContextValidationError" in record["message"] for record in child)


def _run_log_switch(task, event_queue, log_level, ready, resume):
    """用进程事件在两组真实日志之间留出设置切换点。"""

    def run(_task, _signals):
        logger.trace("切换前 TRACE")
        ready.set()
        if not resume.wait(10):
            raise TimeoutError("未收到日志等级切换后的继续事件")
        logger.trace("切换后 TRACE")
        logger.debug("切换后 DEBUG")
        logger.info("切换后 INFO")
        return ExecutionTaskResult(
            completed_steps=(),
            summary="日志切换完成",
            duration_seconds=0,
            run_result=RunResult((StageResult("extract"),)),
        )

    process_worker.run_execution_task = run
    process_worker._run_task_process(task, event_queue, log_level)


@pytest.mark.parametrize(("initial_level", "next_level"), [(10, 5), (5, 20)])
def test_running_process_applies_log_level_changes(qtbot, monkeypatch, initial_level, next_level):
    """真实子进程随运行时设置展开或收起日志细节，过滤发生在采集端。"""
    context = mp.get_context("spawn")
    ready, resume = context.Event(), context.Event()

    def create_process(*, target, args):
        return context.Process(target=_run_log_switch, args=(*args, ready, resume))

    monkeypatch.setattr(LoggingConfiguration, "log_level", initial_level)
    logger.enable("lol_audio_unpack")
    records = []
    sink = logger.add(lambda message: records.append(message.record.copy()), level="TRACE")
    worker = ExecutionProcessWorker(
        _build_task(),
        mp_context=SimpleNamespace(Queue=context.Queue, Value=context.Value, Process=create_process),
    )
    results, failures = [], []
    worker.signals.finished.connect(results.append)
    worker.signals.failed.connect(failures.append)
    try:
        worker.start()
        qtbot.waitUntil(ready.is_set, timeout=20000)
        worker.set_log_level(next_level)
        resume.set()
        qtbot.waitUntil(lambda: bool(results or failures), timeout=20000)
        assert worker.wait(5000)
    finally:
        resume.set()
        if worker.isRunning():
            worker.terminate()
        logger.remove(sink)

    assert not failures and results[0].run_result.status is ResultStatus.SUCCESS
    messages = {record["message"] for record in records if record["process"].id != os.getpid()}
    assert ("切换前 TRACE" in messages) == (initial_level == logger.level("TRACE").no)
    assert ("切换后 TRACE" in messages) == (next_level == logger.level("TRACE").no)
    assert ("切换后 DEBUG" in messages) == (next_level <= logger.level("DEBUG").no)
    assert "切换后 INFO" in messages
