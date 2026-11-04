"""正式任务进程启动前的输入与后端检查，不访问界面控件。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import gettempdir
from threading import Event

from loguru import logger

from lol_audio_unpack.app.context import create_app_context
from lol_audio_unpack.app.facade import LolAudioUnpackApp
from lol_audio_unpack.config import SettingKey
from lol_audio_unpack.gui.task_models import AppContextInputSnapshot, QueuedExecutionTask
from lol_audio_unpack.runtime.probe import ToolProbe, probe_tool


def check_task(task: QueuedExecutionTask, cancel: Event) -> tuple[ToolProbe, ...]:
    """检查本次动作实际使用的输入和后端，汇总多个工具问题。"""
    draft = task.draft
    params = draft.task_params
    request = draft.export_request
    settings = draft.context_input.to_settings()
    scratch = Path(gettempdir()) / "lol-audio-unpack" / "preflight"
    probes = []
    if request is not None:
        request.validate()
        for target in request.targets:
            if any(not path.is_file() for path in target.scope.resolve_files()):
                raise ValueError("所选导出音频已不存在，请刷新后重新选择")
        wav = request.options
        mapping = False
    else:
        logger.info("任务 #{}：正在检查所选资源与处理工具……", task.task_id)
        settings.update(params.to_runtime_overrides())
        ctx = create_app_context(settings=settings)
        app = LolAudioUnpackApp(ctx)
        opts = app._resolve_operation_options(params.to_operation_options())
        if params.run_update or params.run_extract or params.run_mapping:
            app._check_source(opts)
        wav = replace(opts.wav_output, backend_path=str(ctx.config.vgmstream_path or "") or None)
        mapping = params.run_mapping or any(stage.params.run_mapping for stage in draft.retry_stages)
    if wav.enabled:
        probes.append(probe_tool("wav", path=wav.backend_path, options=wav, scratch_root=scratch, cancel=cancel))
    if mapping and not cancel.is_set():
        raw = str(settings.get(SettingKey.WWISER_PATH) or "").strip()
        path = str(ctx.config.wwiser_path) if raw else None
        probes.append(probe_tool("hirc", path=path, scratch_root=scratch, cancel=cancel))
    return tuple(probes)


def use_builtin(task: QueuedExecutionTask, failures: tuple[ToolProbe, ...]) -> QueuedExecutionTask:
    """只修改当前不可变任务快照，不持久化用户的工具设置。"""
    settings = task.draft.context_input.to_settings()
    request = task.draft.export_request
    for issue in failures:
        if not issue.can_fallback:
            raise ValueError("当前故障不支持切换后端")
        key = SettingKey.VGMSTREAM_PATH if issue.tool == "wav" else SettingKey.WWISER_PATH
        settings[key] = ""
        if issue.tool == "wav" and request is not None:
            request = replace(request, options=replace(request.options, backend_path=None))
    return replace(
        task,
        draft=replace(
            task.draft,
            context_input=AppContextInputSnapshot(tuple(settings.items())),
            export_request=request,
            tools_checked=False,
        ),
    )
