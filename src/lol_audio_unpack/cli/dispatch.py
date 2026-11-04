"""CLI 执行编排逻辑。

该模块负责 update / extract / wav / mapping 的执行分发与阶段日志输出。
"""

from __future__ import annotations

import argparse

from loguru import logger

from ..app.facade import LolAudioUnpackApp
from ..app.results import ResultStatus, StageResult
from ..app.targets import resolve_scope
from ..config import SettingKey
from .runtime import CliInputError, build_options, parse_int_ids, resolve_champion_ids


def _has_update(args: argparse.Namespace) -> bool:
    """返回是否包含 update 动作。"""
    return "update" in getattr(args, "actions", [])


def _has_extract(args: argparse.Namespace) -> bool:
    """返回是否包含 extract 动作。"""
    return "extract" in getattr(args, "actions", [])


def _has_mapping(args: argparse.Namespace) -> bool:
    """返回是否包含 mapping 动作。"""
    return "mapping" in getattr(args, "actions", [])


def _has_wav(args: argparse.Namespace) -> bool:
    """返回是否请求执行 WAV 转码 stage。"""
    return "wav" in getattr(args, "actions", [])


def _resolve_targets(
    args: argparse.Namespace,
    *,
    app: LolAudioUnpackApp,
) -> tuple[tuple[int, ...] | None, tuple[int, ...] | None]:
    """解析共享的 CLI 实体选择。"""
    champion_ids = resolve_champion_ids(args.champions, app=app, force_update=args.force)
    map_ids = parse_int_ids(args.maps)
    return champion_ids, map_ids


def _resolve_stage_targets(
    args: argparse.Namespace,
    *,
    app: LolAudioUnpackApp,
    label: str,
) -> tuple[tuple[int, ...] | None, tuple[int, ...] | None]:
    """解析阶段目标，并把预期输入错误提升到 CLI 顶层。"""
    try:
        return _resolve_targets(args, app=app)
    except ValueError as exc:
        message = f"{label}目标无效：{exc}"
        logger.error(message)
        raise CliInputError(message) from exc


def _target_scope(
    *,
    champion_ids: tuple[int, ...] | None,
    map_ids: tuple[int, ...] | None,
) -> tuple[str, bool, bool]:
    """根据共享目标范围推导门面目标范围。"""
    return resolve_scope(
        champion_ids=champion_ids,
        map_ids=map_ids,
    )


def _target_detail(
    *,
    champion_ids: tuple[int, ...] | None,
    map_ids: tuple[int, ...] | None,
    all_detail: str,
    champion_detail: str,
    map_detail: str,
) -> str:
    """根据共享目标范围构建日志详情。"""
    if champion_ids is None and map_ids is None:
        return all_detail
    if champion_ids is not None and map_ids is not None:
        return f"指定英雄和地图: champions={list(champion_ids)}, maps={list(map_ids)}"
    if champion_ids is not None:
        return f"{champion_detail}: {list(champion_ids)}"
    return f"{map_detail}: {list(map_ids) if map_ids is not None else []}"


def _log_stage_start(stage: str, detail: str | None = None) -> None:
    """输出 CLI 阶段开始日志，并保持调用方归属。"""
    message = f"{stage}阶段开始"
    if detail:
        message = f"{message}: {detail}"
    logger.opt(depth=1).info(message)


def _log_stage_done(stage: str, detail: str | None = None) -> None:
    """留存 CLI 阶段诊断，业务完成由应用层播报。"""
    message = f"{stage}阶段完成"
    if detail:
        message = f"{message}: {detail}"
    logger.opt(depth=1).debug(message)


def _log_stage_result(stage: str, result: StageResult, detail: str | None = None) -> None:
    """仅在 typed result 成功时输出完成结论。"""
    if result.error_type == "TargetSelectionError":
        raise CliInputError(result.error_message or "目标 ID 无效，请修正后重试。")
    if result.status is ResultStatus.SUCCESS:
        _log_stage_done(stage, detail)
        return

    message = f"{stage}阶段"
    if result.status is ResultStatus.PARTIAL:
        message += "部分完成"
        log = logger.warning
    elif result.status is ResultStatus.CANCELLED:
        message += "已取消"
        log = logger.warning
    else:
        message += "失败"
        log = logger.error
    if detail:
        message = f"{message}: {detail}"
    log(message)


def _log_top_error(error: Exception, *, dev_mode: bool) -> None:
    """统一记录 CLI 顶层未处理异常。"""
    logger.opt(depth=1, exception=dev_mode).error(f"执行过程中发生错误: {error}")


def run_update(args: argparse.Namespace, app: LolAudioUnpackApp) -> StageResult | None:
    """执行数据更新操作。"""
    if not _has_update(args):
        return

    champion_ids, map_ids = _resolve_stage_targets(args, app=app, label="数据更新")
    target, _, _ = _target_scope(champion_ids=champion_ids, map_ids=map_ids)

    if args.skip_events:
        logger.info("已启用快速模式：跳过事件数据处理")
    if args.force:
        logger.warning("已启用强制更新模式，将忽略现有文件的版本检查。")

    detail = _target_detail(
        champion_ids=champion_ids,
        map_ids=map_ids,
        all_detail="所有数据（英雄和地图）",
        champion_detail="指定英雄数据",
        map_detail="指定地图数据",
    )
    _log_stage_start("数据更新", detail)
    result = app.update(build_options(args, champion_ids=champion_ids, map_ids=map_ids), target=target)
    _log_stage_result("数据更新", result, detail)
    return result


def run_extract(args: argparse.Namespace, app: LolAudioUnpackApp) -> StageResult | None:
    """执行音频解包操作。"""
    if not _has_extract(args):
        return

    champion_ids, map_ids = _resolve_stage_targets(args, app=app, label="音频解包")

    _, include_champions, include_maps = _target_scope(champion_ids=champion_ids, map_ids=map_ids)
    detail = _target_detail(
        champion_ids=champion_ids,
        map_ids=map_ids,
        all_detail="所有音频（英雄和地图）",
        champion_detail="指定英雄音频",
        map_detail="指定地图音频",
    )
    _log_stage_start("音频解包", detail)
    result = app.extract(
        build_options(args, champion_ids=champion_ids, map_ids=map_ids),
        include_champions=include_champions,
        include_maps=include_maps,
    )
    _log_stage_result("音频解包", result, detail)
    return result


def run_wav(
    args: argparse.Namespace,
    app: LolAudioUnpackApp,
    *,
    extract_result: StageResult | None = None,
    defer_output: bool = False,
) -> StageResult | None:
    """执行独立的 WAV 转码 stage。"""
    if not _has_wav(args):
        return

    champion_ids, map_ids = _resolve_stage_targets(args, app=app, label="WAV 转码")
    if extract_result is not None:
        successful_entities = tuple(
            entity
            for entity in extract_result.entities
            if entity.status in {ResultStatus.SUCCESS, ResultStatus.PARTIAL} and entity.artifacts
        )
        champion_ids = tuple(
            int(entity.entity_id) for entity in successful_entities if entity.entity_type == "champion"
        )
        map_ids = tuple(int(entity.entity_id) for entity in successful_entities if entity.entity_type == "map")

    detail = _target_detail(
        champion_ids=champion_ids,
        map_ids=map_ids,
        all_detail="消费当前 audios 输出树",
        champion_detail="指定英雄音频目录",
        map_detail="指定地图音频目录",
    )
    _log_stage_start("WAV 转码", detail)
    result = app.transcode_wav(build_options(args, champion_ids=champion_ids, map_ids=map_ids))
    if defer_output and result.status is ResultStatus.SUCCESS:
        logger.info("WAV 转码已暂存，等待本轮映射步骤结束后交付文件")
    else:
        _log_stage_result("WAV 转码", result, detail)
    return result


def _log_mapping_error(error: ValueError) -> None:
    """记录 mapping 运行时错误，并在 wwiser 配置错误时补充指引。"""
    message = str(error)
    logger.error(f"构建事件映射失败: {message}")

    if "Wwiser 工具路径" not in message and SettingKey.WWISER_PATH not in message:
        return

    logger.error(
        "如果需要使用 WwiserHIRC 回退路径，请通过 --wwiser-path 显式传入，或在 -c 指定的 INI 中配置 wwiser_path。"
    )
    logger.error(
        "WWISER_PATH 应指向 wwiser.pyz 或 wwiser.exe 文件；如果不需要 wwiser，请移除该配置并直接使用默认 NativeHIRC。"
    )


def run_mapping(
    args: argparse.Namespace,
    app: LolAudioUnpackApp,
) -> StageResult | None:
    """执行事件映射操作。"""
    if not _has_mapping(args):
        return

    if build_options(args).integrate_data:
        logger.info("启用整合数据功能，将生成包含完整实体信息的整合文件")

    champion_ids, map_ids = _resolve_stage_targets(args, app=app, label="事件映射")

    _, include_champions, include_maps = _target_scope(champion_ids=champion_ids, map_ids=map_ids)
    detail = _target_detail(
        champion_ids=champion_ids,
        map_ids=map_ids,
        all_detail="所有实体（英雄和地图）",
        champion_detail="指定英雄事件映射",
        map_detail="指定地图事件映射",
    )
    _log_stage_start("事件映射", detail)
    mapping_options = build_options(args, champion_ids=champion_ids, map_ids=map_ids)
    try:
        result = app.mapping(
            mapping_options,
            include_champions=include_champions,
            include_maps=include_maps,
        )
    except ValueError as exc:
        _log_mapping_error(exc)
        raise CliInputError(str(exc)) from exc
    _log_stage_result("事件映射", result, detail)
    return result


__all__ = [
    "_has_extract",
    "_has_mapping",
    "_has_update",
    "_has_wav",
    "_log_stage_done",
    "_log_stage_result",
    "_log_stage_start",
    "_log_top_error",
    "run_extract",
    "run_mapping",
    "run_update",
    "run_wav",
]
