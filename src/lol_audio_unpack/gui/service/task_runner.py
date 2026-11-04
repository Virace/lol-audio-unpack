"""执行中心后台任务运行器。"""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

from loguru import logger

from lol_audio_unpack.app.context import KNOWN_AUDIO_TYPES, create_app_context
from lol_audio_unpack.app.facade import LolAudioUnpackApp
from lol_audio_unpack.app.game_version import resolve_game_version
from lol_audio_unpack.app.operation_report import write_operation_report
from lol_audio_unpack.app.resource_pack import partition_special_targets
from lol_audio_unpack.app.results import EntityResult, ResultStatus, RunResult, StageResult
from lol_audio_unpack.app.special_content import merge_champion_ids
from lol_audio_unpack.app.targets import resolve_scope
from lol_audio_unpack.app.types import OperationOptions
from lol_audio_unpack.config import SettingKey
from lol_audio_unpack.gui.task_models import (
    ExecutionTaskProgress,
    ExecutionTaskResult,
    QueuedExecutionTask,
)
from lol_audio_unpack.model.progress import OperationProgress
from lol_audio_unpack.runtime.wav.batch import WavBatchResult, read_batch_result, retry_batch, run_batch
from lol_audio_unpack.runtime.wav.cache import WavCache
from lol_audio_unpack.runtime.wav.event_output import EventOutput
from lol_audio_unpack.unpack.batch import execute_tasks as execute_extract_tasks

if TYPE_CHECKING:
    from lol_audio_unpack.gui.workers import WorkerSignals


STAGE_KEY_BY_STEP_NAME = {
    "准备任务数据": "update",
    "前置强制更新": "update",
    "音频解包": "extract",
    "音频转码": "wav",
    "事件映射": "mapping",
}
STAGE_LABEL_BY_KEY = {
    "update": "数据准备",
    "extract": "音频解包",
    "wav": "音频转码",
    "mapping": "事件映射",
}
ENTITY_SCOPE_LABEL_BY_TYPE = {
    "champion": "英雄",
    "map": "地图",
    "resource_pack": "本地资源包",
    "wav": "音频转码",
}
MAX_SUMMARY_ISSUES = 3


def _stage_has_artifacts(result: StageResult) -> bool:
    """返回阶段是否携带本轮已确认落盘的产物。"""
    return any(entity.artifacts for entity in result.entities)


def _stage_produced_output(result: StageResult) -> bool:
    """判断阶段是否能进入 GUI 的已产生产物步骤列表。"""
    if result.status not in {ResultStatus.SUCCESS, ResultStatus.PARTIAL}:
        return False
    if result.stage == "update":
        # 普通准备与显式重建共用 update；成功返回才表示本轮前置条件已满足。
        if result.status is ResultStatus.SUCCESS:
            return result.note != "当前目标不需要更新。"
        return _stage_has_artifacts(result)
    return _stage_has_artifacts(result)


def _build_wav_options(options: OperationOptions, extract_result: StageResult) -> OperationOptions:
    """把 WAV 消费范围限制到本轮解包已确认落盘的实体。"""
    eligible_entities = tuple(
        entity
        for entity in extract_result.entities
        if entity.status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL} and entity.artifacts
    )
    return replace(
        options,
        champion_ids=tuple(int(entity.entity_id) for entity in eligible_entities if entity.entity_type == "champion"),
        map_ids=tuple(int(entity.entity_id) for entity in eligible_entities if entity.entity_type == "map"),
        special_targets=(),
        resource_pack_wads=(),
    )


def _terminal_progress_counts(result: StageResult) -> tuple[int, int]:
    """构造不会把非 success 终态伪装成满进度的计数。"""
    total = max(result.total_count, 1)
    if result.status is ResultStatus.SUCCESS:
        return total, total
    artifact_count = sum(bool(entity.artifacts) for entity in result.entities)
    return min(artifact_count, total - 1), total


def _terminal_stage_message(result: StageResult) -> str:
    """按 typed status 构造阶段收尾文案。"""
    label = STAGE_LABEL_BY_KEY.get(result.stage, result.stage)
    if result.status is ResultStatus.SUCCESS:
        return f"{label}完成" if _stage_produced_output(result) else f"{label}无需处理"
    details = []
    if result.error_message:
        details.append(result.error_message)
    affected = [entity for entity in result.entities if entity.status is not ResultStatus.SUCCESS]
    for entity in affected[:MAX_SUMMARY_ISSUES]:
        name = (
            entity.entity_name or f"{ENTITY_SCOPE_LABEL_BY_TYPE.get(entity.entity_type, '对象')} ID {entity.entity_id}"
        )
        reason = entity.error_message or "未提供具体原因，请查看任务日志"
        details.append(f"{name}：{reason}")
    if len(affected) > MAX_SUMMARY_ISSUES:
        details.append(f"另有 {len(affected) - MAX_SUMMARY_ISSUES} 个对象未完成")
    if not details and result.note:
        details.append(result.note)
    if not details:
        details.append("未提供具体原因，请查看任务日志")
    detail_text = "；".join(details)
    if result.status is ResultStatus.PARTIAL:
        return f"{label}部分完成（{detail_text}）"
    if result.status is ResultStatus.CANCELLED:
        return f"{label}已取消（{detail_text}）"
    return f"{label}失败（{detail_text}）"


def _emit_terminal_stage_progress(
    signals: WorkerSignals,
    result: StageResult,
    *,
    entity_scope_label: str,
) -> None:
    """将 typed stage result 映射为最终结构化进度。"""
    current, total = _terminal_progress_counts(result)
    _emit_stage_progress(
        signals,
        stage_key=result.stage,
        entity_scope_label=entity_scope_label,
        current=current,
        total=total,
        message=_terminal_stage_message(result),
        stage_finished=True,
    )


def _build_run_summary(
    result: RunResult,
    completed_steps: tuple[str, ...],
    duration_seconds: float,
) -> str:
    """根据权威整轮结果构造用户可观察的终态摘要。"""
    duration = f"{duration_seconds:.1f}s"
    productive_text = " -> ".join(completed_steps)
    if result.status is ResultStatus.SUCCESS:
        if productive_text:
            return f"已完成：{productive_text}（{duration}）"
        return f"执行完成：本轮没有产生新产物（{duration}）"

    # 同一英雄在解包和映射各有一个结果；跨阶段累加会把一个对象误显示成两个。
    stage_text = "；".join(_terminal_stage_message(stage) for stage in result.stages)
    if not completed_steps:
        stage_text += "；本轮没有确认的新产物"
    if result.status is ResultStatus.PARTIAL:
        return f"部分完成：{stage_text}（{duration}）"
    if result.status is ResultStatus.CANCELLED:
        return f"已取消：{stage_text}（{duration}）"
    return f"执行失败：{stage_text}（{duration}）"


def _require_stage_result(result: StageResult | None, stage_key: str) -> StageResult:
    """拒绝已选择的 GUI 阶段静默丢失后端结果。"""
    if result is None:
        raise RuntimeError(f"已选择的 {stage_key} 阶段没有返回执行结果")
    if result.stage != stage_key:
        raise RuntimeError(f"{stage_key} 阶段返回了不匹配的结果 key: {result.stage}")
    return result


def _resolve_task_scope(task: QueuedExecutionTask) -> tuple[str, bool, bool, bool]:
    """推导任务对应的后端目标范围。

    Args:
        task: 已入队任务。

    Returns:
        ``(target, include_champions, include_maps, include_resource_packs)`` 元组。
    """
    task_params = task.draft.task_params
    partition = partition_special_targets(task_params.special_targets)
    target, include_champions, include_maps = resolve_scope(
        champion_ids=merge_champion_ids(task_params.champion_ids, partition.champion_targets),
        map_ids=task_params.map_ids,
    )
    include_resource_packs = bool(partition.resource_pack_targets or task_params.resource_pack_wads)
    if (
        include_resource_packs
        and task_params.champion_ids is None
        and not partition.champion_targets
        and task_params.map_ids is None
    ):
        # resource pack 由 app facade 独立消费，不能因空的普通目标退化为全量英雄/地图。
        include_champions = False
        include_maps = False
    return target, include_champions, include_maps, include_resource_packs


def _resolve_task_options(task: QueuedExecutionTask) -> OperationOptions:
    """保留异构 special target，由应用门面按冻结合同分区。"""
    return task.draft.task_params.to_operation_options()


def _validate_special_targets(task: QueuedExecutionTask) -> None:
    """在创建 AppContext 前验证特殊内容的阶段兼容边界。"""
    task_params = task.draft.task_params
    partition = partition_special_targets(task_params.special_targets)
    has_resource_pack_scope = bool(partition.resource_pack_targets or task_params.resource_pack_wads)
    if has_resource_pack_scope and task_params.wav_enabled:
        raise ValueError("resource pack 当前不支持 WAV 转码；请先只执行 extract 或 mapping。")


def _build_runtime_settings(
    task: QueuedExecutionTask,
    *,
    force_lobby_audio: bool = False,
) -> dict[str, str | bool]:
    """合并运行任务所需的上下文覆盖配置。

    Args:
        task: 已入队任务。
        force_lobby_audio: 是否在当前阶段强制准备大厅音频资源。

    Returns:
        可直接传给 ``create_app_context`` 的共享配置映射。
    """
    settings = task.draft.context_input.to_settings()
    settings.update(task.draft.task_params.to_runtime_overrides())
    if force_lobby_audio:
        settings[SettingKey.LOBBY_AUDIO] = True
    return settings


def _build_scope_label(
    *,
    include_champions: bool,
    include_maps: bool,
    include_resource_packs: bool,
) -> str:
    """将当前任务范围格式化为用户可读文案。"""
    scope_parts: list[str] = []
    if include_champions:
        scope_parts.append("英雄")
    if include_maps:
        scope_parts.append("地图")
    if include_resource_packs:
        scope_parts.append("本地资源包")
    return " + ".join(scope_parts) if scope_parts else "未选择目标"


def _emit_stage_progress(  # noqa: PLR0913
    signals: WorkerSignals,
    *,
    stage_key: str,
    entity_scope_label: str,
    current: int = 0,
    total: int = 0,
    message: str = "",
    stage_finished: bool = False,
) -> None:
    """向 GUI 发出结构化阶段进度。"""
    signals.progress.emit(
        ExecutionTaskProgress(
            stage_key=stage_key,
            stage_label=STAGE_LABEL_BY_KEY.get(stage_key, stage_key),
            entity_scope_label=entity_scope_label,
            current=current,
            total=total,
            message=message,
            stage_finished=stage_finished,
        )
    )


def _ensure_map_banks_ready(
    runtime_app: LolAudioUnpackApp,
    task: QueuedExecutionTask,
    *,
    include_maps: bool,
) -> None:
    """在执行地图任务前确认地图 banks 已完成生成。

    Args:
        runtime_app: 当前任务使用的运行时门面。
        task: 已入队任务。
        include_maps: 当前任务范围是否包含地图。

    Raises:
        RuntimeError: 地图基础 banks 缺失时抛出，避免静默跳过地图输出。
    """
    if not include_maps:
        return

    reader = runtime_app._get_reader()
    map_ids = task.draft.task_params.map_ids
    target_ids = (
        tuple(int(map_id) for map_id in map_ids)
        if map_ids is not None
        else tuple(int(map_data["id"]) for map_data in reader.get_maps() if map_data.get("id") is not None)
    )
    missing_ids = [map_id for map_id in target_ids if not reader.get_map_banks(map_id)]
    if not missing_ids:
        return

    raise RuntimeError(f"所选地图的数据准备未完成，缺少地图 banks: {missing_ids[:10]}。请查看任务日志并重试。")


def _run_audio_export(task: QueuedExecutionTask, signals: WorkerSignals) -> ExecutionTaskResult:
    """将导出和精确 WAV 重试交给同一批处理层，不创建额外任务调度器。"""
    started = perf_counter()
    request = task.draft.export_request
    batches: list[WavBatchResult] = []

    def emit(progress) -> None:
        _emit_stage_progress(
            signals,
            stage_key="wav",
            entity_scope_label="音频文件",
            current=progress.completed_count,
            total=progress.total_count,
            message=f"已处理 {progress.completed_count}/{progress.total_count} 个文件 · 失败 {progress.failed_count} 个",
        )

    if task.draft.retry_wav:
        raise ValueError("本次转码不支持失败重试，请检查诊断后创建新任务")
    if request is not None:
        request.validate()
        cache = WavCache(request.options, request.library_root, request.version, request.region)
        with ExitStack() as stack:
            events = None
            if request.options.group_by_event and request.output_file is None:
                hash_root = None
                if request.library_root is not None:
                    hash_root = request.library_root / "hashes" / request.version
                    if request.region:
                        hash_root /= request.region
                events = stack.enter_context(
                    EventOutput(
                        audio_root=request.version_root,
                        hash_root=hash_root,
                        version=request.version,
                        scratch_root=(request.library_root or request.report_root) / ".temp",
                    )
                )
            runner = events.run_batch if events is not None else run_batch
            for target in request.targets:
                batches.append(
                    runner(
                        target.scope,
                        target.output_root,
                        options=request.options,
                        report_root=request.report_root,
                        overwrite=request.overwrite,
                        progress=emit,
                        output_file=request.output_file,
                        prechecked=task.draft.tools_checked,
                        cache=cache,
                    )
                )
            if events is not None:
                batches = list(events.finish())
        report_root = request.report_root
        version = request.version
    else:
        raise ValueError("缺少音频导出范围")

    success = sum(batch.success_count for batch in batches)
    failed = sum(batch.failed_count for batch in batches)
    skipped = sum(batch.skipped_count for batch in batches)
    unknown = sum(batch.unconfirmed_count for batch in batches)
    entities = tuple(
        EntityResult(
            "wav",
            batch.operation_id,
            ResultStatus(batch.status),
            entity_name=task.draft.source_summary,
            error_message=batch.error_message,
            artifacts=(str(batch.output_file or batch.output_root),)
            if batch.success_count or batch.output_count
            else (),
        )
        for batch in batches
    )
    summary = f"成功 {success}、失败 {failed}、跳过 {skipped} 个文件"
    summary += f"；实际转换 {sum(batch.converted_count for batch in batches)}、复用 {sum(batch.reused_count for batch in batches)}"
    if unknown:
        summary += f"；{unknown} 个文件完成状态未知"
    if request is not None and request.options.group_by_event:
        summary += (
            f"；关联事件 {sum(batch.classified_count for batch in batches)}、"
            f"未关联 {sum(batch.unclassified_count for batch in batches)}、"
            f"普通布局 {sum(batch.plain_count for batch in batches)} 个音频"
        )
    stage = StageResult(
        "wav",
        entities,
        note=summary,
        reports=tuple(str(batch.report_path) for batch in batches),
        wav_batches=tuple(batches),
    )
    result = RunResult((stage,))
    duration = perf_counter() - started
    report_path = None
    try:
        report_path = write_operation_report(
            report_root,
            operation_id=task.draft.operation_id,
            version=version,
            snapshot=asdict(task.draft),
            result=result,
            duration_seconds=duration,
            retry_of=task.draft.retry_of,
        )
    except OSError as exc:
        logger.error("任务报告保存失败，保留已完成音频与内存结果：{}", exc)
        result = RunResult((*result.stages, StageResult.from_error("report", exc)))
        summary += f"；报告保存失败：{exc}"
    return ExecutionTaskResult(
        ("音频转码",) if success or any(batch.output_count for batch in batches) else (),
        summary,
        duration,
        result,
        report_path=report_path,
        version=version,
        wav_batches=tuple(batches),
    )


def _run_retry(task: QueuedExecutionTask, signals: WorkerSignals) -> ExecutionTaskResult:
    """在同一个 worker 内依次执行限定阶段，保留每阶段独立范围和原报告。"""
    started = perf_counter()
    results: list[ExecutionTaskResult] = []
    for stage in task.draft.retry_stages:
        logger.info("[重试] {}", stage.label)
        draft = replace(
            task.draft,
            operation_id=uuid4().hex,
            task_params=stage.params,
            retry_stages=(),
            export_request=None,
            retry_extract=stage.extract_entities,
            retry_wav=stage.wav_batches,
        )
        try:
            results.append(run_execution_task(replace(task, draft=draft), signals))
        except Exception as exc:  # noqa: BLE001
            logger.exception("重试输入复核失败：{}", stage.label)
            key = next((STAGE_KEY_BY_STEP_NAME[name] for name in stage.params.selected_steps()), "run")
            results.append(ExecutionTaskResult((), str(exc), 0.0, RunResult((StageResult.from_error(key, exc),))))
    run_result = RunResult(tuple(stage for result in results for stage in result.run_result.stages))
    completed = tuple(dict.fromkeys(step for result in results for step in result.completed_steps))
    duration = perf_counter() - started
    report_root = next(
        (result.report_path.parent.parent.parent for result in results if result.report_path is not None), None
    )
    report_path = (
        write_operation_report(
            report_root,
            operation_id=task.draft.operation_id,
            version=task.draft.version,
            snapshot=asdict(task.draft),
            result=run_result,
            duration_seconds=duration,
            retry_of=task.draft.retry_of,
        )
        if report_root is not None
        else None
    )
    return ExecutionTaskResult(
        completed,
        _build_run_summary(run_result, completed, duration),
        duration,
        run_result,
        report_path=report_path,
        version=task.draft.version,
        wav_batches=tuple(batch for result in results for batch in result.wav_batches),
    )


def run_execution_task(task: QueuedExecutionTask, signals: WorkerSignals) -> ExecutionTaskResult:
    """在后台线程中执行单个队列任务。

    Args:
        task: 待执行的队列任务。
        signals: 用于回传进度的 worker 信号对象。

    Returns:
        任务完成后的结果摘要。

    Raises:
        ValueError: 无法确定任务输入契约时，在执行前拒绝请求。
    """
    if task.draft.retry_stages:
        return _run_retry(task, signals)
    if task.draft.export_request is not None or task.draft.retry_wav:
        return _run_audio_export(task, signals)
    started_at = perf_counter()
    task_params = task.draft.task_params
    options = _resolve_task_options(task)
    target, include_champions, include_maps, include_resource_packs = _resolve_task_scope(task)
    task_scope_label = _build_scope_label(
        include_champions=include_champions,
        include_maps=include_maps,
        include_resource_packs=include_resource_packs,
    )
    steps = task_params.selected_steps()
    if not task_params.run_update and (task_params.run_extract or task_params.run_mapping):
        # 所选目标的准备属于当前任务，不依赖用户提前开启全量准备或手动勾选更新。
        steps = ("准备任务数据", *steps)
    completed_steps: list[str] = []
    stage_results: list[StageResult] = []
    extract_result: StageResult | None = None
    runtime_app: LolAudioUnpackApp | None = None
    runtime_context = None
    stage_key = "run"
    event_stack = ExitStack()
    event_output = None

    def create_runtime_app(settings) -> LolAudioUnpackApp:
        nonlocal runtime_context
        runtime_context = create_app_context(settings=settings)
        runtime_context.runtime_cache["tools_prechecked"] = task.draft.tools_checked
        if task.draft.version and resolve_game_version(runtime_context) != task.draft.version:
            raise ValueError("客户端版本已变化，请刷新共享数据后重新确认任务范围")
        return LolAudioUnpackApp(runtime_context)

    map_banks_checked = False
    runtime_settings = _build_runtime_settings(task)
    _validate_special_targets(task)

    try:
        details = []
        for label, ids, included in (
            ("英雄", options.champion_ids, include_champions),
            ("地图", options.map_ids, include_maps),
        ):
            if included and ids != ():
                details.append(f"{label}={'全部' if ids is None else ','.join(map(str, ids))}")
        if options.special_targets:
            details.append(f"特殊内容={','.join(options.special_targets)}")
        details.append("音频=" + ",".join(kind for kind in KNOWN_AUDIO_TYPES if kind not in task_params.exclude_types))
        details.append(f"并发={options.max_workers}")
        if task_params.run_mapping:
            details.append("映射=" + ("整合" if options.integrate_data else "普通"))
        if options.wav_output.enabled:
            details.append(f"WAV={options.wav_output.format}，并发={options.wav_output.worker_count}")
        logger.debug(f"任务 #{task.task_id} 参数：{'；'.join(details)}")
        for step_name in steps:
            stage_key = STAGE_KEY_BY_STEP_NAME.get(step_name, "unknown")
            stage_result: StageResult | None = None

            if step_name in {"准备任务数据", "前置强制更新"}:

                def emit_prepare_progress(progress: OperationProgress) -> None:
                    _emit_stage_progress(
                        signals,
                        stage_key="update",
                        entity_scope_label=ENTITY_SCOPE_LABEL_BY_TYPE.get(progress.entity_type, task_scope_label),
                        current=progress.current or 0,
                        total=progress.total or 0,
                        message="正在准备所选对象的数据…",
                    )

                _emit_stage_progress(
                    signals,
                    stage_key=stage_key,
                    entity_scope_label=task_scope_label,
                    current=0,
                    message="正在强制刷新所选数据…" if task_params.run_update else "正在检查所选对象的数据…",
                )
                if task_params.run_update:
                    update_app = create_runtime_app(_build_runtime_settings(task, force_lobby_audio=True))
                else:
                    runtime_app = create_runtime_app(runtime_settings)
                    update_app = runtime_app
                stage_result = _require_stage_result(
                    update_app.update(options, target=target, progress_callback=emit_prepare_progress),
                    stage_key,
                )
            elif step_name == "音频解包":

                def emit_extract_progress(
                    entity_type: str,
                    current: int,
                    total: int,
                    message: str,
                    *,
                    resolved_stage_key: str = stage_key,
                ) -> None:
                    _emit_stage_progress(
                        signals,
                        stage_key=resolved_stage_key,
                        entity_scope_label=ENTITY_SCOPE_LABEL_BY_TYPE.get(entity_type, task_scope_label),
                        current=current,
                        total=total,
                        message=message,
                    )

                if runtime_app is None:
                    runtime_app = create_runtime_app(runtime_settings)
                if not map_banks_checked:
                    _ensure_map_banks_ready(runtime_app, task, include_maps=include_maps)
                    map_banks_checked = True

                _emit_stage_progress(
                    signals,
                    stage_key=stage_key,
                    entity_scope_label=task_scope_label,
                    message="正在准备解包任务…",
                )
                if task.draft.retry_extract:
                    stage_result = execute_extract_tasks(
                        [(item.entity_type, item.entity_id, item.entity_name) for item in task.draft.retry_extract],
                        runtime_app._get_reader(),
                        options.max_workers,
                        ctx=runtime_context,
                        progress_callback=emit_extract_progress,
                        retry_entities=task.draft.retry_extract,
                    )
                else:
                    stage_result = _require_stage_result(
                        runtime_app.extract(
                            options,
                            include_champions=include_champions,
                            include_maps=include_maps,
                            progress_callback=emit_extract_progress,
                        ),
                        stage_key,
                    )
            elif step_name == "音频转码":
                if extract_result is not None and extract_result.status is ResultStatus.FAILED:
                    logger.warning(f"[执行中心] 任务 #{task.task_id} 解包没有成功实体，跳过依赖的音频转码")
                    continue

                def emit_wav_progress(
                    entity_type: str,
                    current: int,
                    total: int,
                    message: str,
                    *,
                    resolved_stage_key: str = stage_key,
                ) -> None:
                    _emit_stage_progress(
                        signals,
                        stage_key=resolved_stage_key,
                        entity_scope_label=ENTITY_SCOPE_LABEL_BY_TYPE.get(entity_type, task_scope_label),
                        current=current,
                        total=total,
                        message=message,
                    )

                if runtime_app is None:
                    runtime_app = create_runtime_app(runtime_settings)

                if options.wav_output.group_by_event:
                    event_output = event_stack.enter_context(runtime_app.defer_wav_output(options))

                _emit_stage_progress(
                    signals,
                    stage_key=stage_key,
                    entity_scope_label=task_scope_label,
                    message="正在准备音频转码…",
                )
                stage_result = _require_stage_result(
                    runtime_app.transcode_wav(
                        _build_wav_options(options, extract_result) if extract_result is not None else options,
                        progress_callback=emit_wav_progress,
                        job_label=f"gui-task-{task.task_id}",
                    ),
                    stage_key,
                )
            elif step_name == "事件映射":
                mapping_progress_seen = False

                def emit_mapping_progress(
                    entity_type: str,
                    current: int,
                    total: int,
                    message: str,
                    *,
                    resolved_stage_key: str = stage_key,
                ) -> None:
                    nonlocal mapping_progress_seen
                    mapping_progress_seen = True
                    _emit_stage_progress(
                        signals,
                        stage_key=resolved_stage_key,
                        entity_scope_label=ENTITY_SCOPE_LABEL_BY_TYPE.get(entity_type, task_scope_label),
                        current=current,
                        total=total,
                        message=message,
                    )

                if runtime_app is None:
                    runtime_app = create_runtime_app(runtime_settings)
                if not map_banks_checked:
                    _ensure_map_banks_ready(runtime_app, task, include_maps=include_maps)
                    map_banks_checked = True

                _emit_stage_progress(
                    signals,
                    stage_key=stage_key,
                    entity_scope_label=task_scope_label,
                    message="正在准备事件映射任务…",
                )
                stage_result = _require_stage_result(
                    runtime_app.mapping(
                        options,
                        include_champions=include_champions,
                        include_maps=include_maps,
                        progress_callback=emit_mapping_progress,
                    ),
                    stage_key,
                )
                if not mapping_progress_seen:
                    logger.debug(f"[执行中心] 任务 #{task.task_id} 事件映射未返回增量进度")

            resolved_result = _require_stage_result(stage_result, stage_key)
            # 自动准备是依赖检查；其成功不能把后续全失败任务聚合成“部分成功”。
            # 非成功结果仍保留在报告中，避免丢失前置阶段的失败对象和恢复线索。
            if step_name != "准备任务数据" or resolved_result.status is not ResultStatus.SUCCESS:
                stage_results.append(resolved_result)
            if resolved_result.stage == "extract":
                extract_result = resolved_result
            if resolved_result.stage != "wav" or event_output is None:
                _emit_terminal_stage_progress(
                    signals,
                    resolved_result,
                    entity_scope_label=task_scope_label,
                )
            if step_name != "准备任务数据" and _stage_produced_output(resolved_result):
                completed_steps.append(step_name)
            if resolved_result.status is ResultStatus.CANCELLED:
                break
            if resolved_result.stage == "update" and resolved_result.status is ResultStatus.FAILED:
                break

    except Exception as exc:  # noqa: BLE001
        logger.exception(f"[执行中心] 任务 #{task.task_id} 执行失败")
        # 后续步骤的异常不能抹掉先前已经确认的成功与落盘事实。
        stage_results.append(StageResult.from_error(stage_key, exc))

    if event_output is not None:
        try:
            if event_output.tree_payload is not None or event_output.has_pending:
                logger.info("[执行中心] 任务 #{} 正在输出本轮 WAV 事件目录", task.task_id)
                _emit_stage_progress(
                    signals, stage_key="wav", entity_scope_label=task_scope_label, message="正在输出本轮 WAV 事件目录…"
                )
                final_wav = runtime_app.finish_wav_output(event_output)
                stage_results = [final_wav if stage.stage == "wav" else stage for stage in stage_results]
                if _stage_produced_output(final_wav) and "音频转码" not in completed_steps:
                    completed_steps.append("音频转码")
                _emit_terminal_stage_progress(signals, final_wav, entity_scope_label=task_scope_label)
        except Exception as exc:  # noqa: BLE001
            logger.exception("本轮 WAV 事件输出失败")
            stage_results.append(StageResult.from_error("wav", exc))
        finally:
            event_stack.close()

    run_result = RunResult(tuple(stage_results))
    duration_seconds = perf_counter() - started_at
    completed_step_names = tuple(completed_steps)
    summary = _build_run_summary(run_result, completed_step_names, duration_seconds)
    wav_batches = tuple(batch for stage in stage_results for batch in stage.wav_batches)
    version = task.draft.version or getattr(runtime_context, "runtime_cache", {}).get("resolved_runtime_version", "")
    report_path = None
    if version and runtime_context is not None:
        try:
            report_path = write_operation_report(
                runtime_context.version_path("report", version),
                operation_id=task.draft.operation_id,
                version=version,
                snapshot=asdict(task.draft),
                result=run_result,
                duration_seconds=duration_seconds,
                retry_of=task.draft.retry_of,
            )
        except OSError as exc:
            logger.error("任务报告保存失败，保留已完成结果：{}", exc)
            run_result = RunResult((*run_result.stages, StageResult.from_error("report", exc)))
            summary += f"；报告保存失败：{exc}"
    return ExecutionTaskResult(
        completed_steps=completed_step_names,
        summary=summary,
        duration_seconds=duration_seconds,
        run_result=run_result,
        report_path=report_path,
        version=version,
        wav_batches=wav_batches,
    )
