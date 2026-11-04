"""批量事件映射调度。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from lol_audio_unpack.app.library import with_library
from lol_audio_unpack.app.results import EntityResult, ResultStatus, StageResult
from lol_audio_unpack.manager import DataReader
from lol_audio_unpack.model import generate_champion_tasks, generate_map_tasks

from . import session as mapping_session
from .entity import build_champion, build_map, build_resource_pack

if TYPE_CHECKING:
    from lol_audio_unpack.app.types import AppContext


EntityTask = tuple[str, int | str, str]


def _summarize_tasks(tasks: list[EntityTask]) -> tuple[list[str], dict[str, int], dict[str, int]]:
    """统计当前任务集中各实体类型的数量。

    Args:
        tasks: 当前任务列表。

    Returns:
        tuple[list[str], dict[str, int], dict[str, int]]: 摘要文案、总数和完成数。
    """

    champion_count = sum(1 for entity_type, _, _ in tasks if entity_type == "champion")
    map_count = sum(1 for entity_type, _, _ in tasks if entity_type == "map")
    resource_pack_count = sum(1 for entity_type, _, _ in tasks if entity_type == "resource_pack")

    summary_parts: list[str] = []
    if champion_count > 0:
        summary_parts.append(f"{champion_count} 个英雄")
    if map_count > 0:
        summary_parts.append(f"{map_count} 个地图")
    if resource_pack_count > 0:
        summary_parts.append(f"{resource_pack_count} 个资源包")

    totals_by_type = {"champion": champion_count, "map": map_count, "resource_pack": resource_pack_count}
    finished_by_type = {"champion": 0, "map": 0, "resource_pack": 0}
    return summary_parts, totals_by_type, finished_by_type


def _build_entity(  # noqa: PLR0913, PLR0917
    entity_type: str,
    entity_id: int | str,
    reader: DataReader,
    wwiser_manager: Any,
    integrate_data: bool,
    runtime_cache: mapping_session.RuntimeCache,
    *,
    ctx: AppContext,
    persisted_mapping_callback: Callable[[Path], None] | None = None,
) -> None:
    """执行单个实体的映射构建。

    Args:
        entity_type: 实体类型。
        entity_id: 实体 ID。
        reader: 数据读取器实例。
        wwiser_manager: 可选的 wwiser 管理器。
        integrate_data: 是否输出整合数据。
        runtime_cache: 运行时缓存。
        ctx: 运行时上下文。
        persisted_mapping_callback: 映射或整合产物成功落盘后的可选回调。

    Raises:
        ValueError: 实体类型未知时抛出。
    """

    if entity_type == "champion":
        build_champion(
            entity_id,
            reader,
            wwiser_manager,
            integrate_data,
            runtime_cache=runtime_cache,
            ctx=ctx,
            persisted_mapping_callback=persisted_mapping_callback,
        )
        return
    if entity_type == "map":
        build_map(
            entity_id,
            reader,
            wwiser_manager,
            integrate_data,
            runtime_cache=runtime_cache,
            ctx=ctx,
            persisted_mapping_callback=persisted_mapping_callback,
        )
        return
    if entity_type == "resource_pack":
        build_resource_pack(
            str(entity_id),
            reader,
            wwiser_manager,
            integrate_data,
            runtime_cache=runtime_cache,
            ctx=ctx,
            persisted_mapping_callback=persisted_mapping_callback,
        )
        return
    raise ValueError(f"未知的实体类型: {entity_type}")


def _emit_progress(  # noqa: PLR0913, PLR0917
    progress_callback: Callable[[str, int, int, str], None] | None,
    entity_type: str,
    finished_by_type: dict[str, int],
    totals_by_type: dict[str, int],
    completed_count: int,
    total_tasks: int,
    progress_message: str,
) -> None:
    """按类型回传进度信息。

    Args:
        progress_callback: 外部进度回调。
        entity_type: 当前实体类型。
        finished_by_type: 各类型已完成数。
        totals_by_type: 各类型总数。
        completed_count: 当前已完成总数。
        total_tasks: 当前总任务数。
        progress_message: 当前进度文案。
    """

    if progress_callback is None:
        return
    progress_callback(
        entity_type,
        finished_by_type.get(entity_type, completed_count),
        max(totals_by_type.get(entity_type, total_tasks), 1),
        progress_message,
    )


def _emit_running_progress(  # noqa: PLR0913, PLR0917
    progress_callback: Callable[[str, int, int, str], None] | None,
    entity_type: str,
    finished_by_type: dict[str, int],
    totals_by_type: dict[str, int],
    total_tasks: int,
    description: str,
) -> None:
    """按类型回传当前正在处理的实体信息。"""
    if progress_callback is None:
        return
    progress_callback(
        entity_type,
        finished_by_type.get(entity_type, 0),
        max(totals_by_type.get(entity_type, total_tasks), 1),
        f"正在处理: {description}",
    )


@with_library
def execute_tasks(  # noqa: PLR0913
    tasks: list[EntityTask],
    reader: DataReader,
    max_workers: int = 4,
    integrate_data: bool = False,
    *,
    ctx: AppContext,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
) -> StageResult:
    """执行映射任务集。

    Args:
        tasks: 任务元组列表 ``[(entity_type, id, description), ...]``。
        reader: 数据读取器实例。
        max_workers: 最大工作线程数。
        integrate_data: 是否生成整合数据。
        ctx: 运行时上下文。
        progress_callback: 每个实体完成后的可选进度回调。

    Returns:
        当前 mapping 阶段的执行结果。

    Raises:
        ValueError: 任务包含未知实体类型时抛出。
    """

    if not tasks:
        note = "没有任何任务需要执行"
        return StageResult.from_entities("mapping", (), note=note)

    unknown_entity_type = next(
        (entity_type for entity_type, _, _ in tasks if entity_type not in {"champion", "map", "resource_pack"}),
        None,
    )
    if unknown_entity_type is not None:
        # 任务类型是 batch 的基础输入合同，不能被归类为可继续的实体构建失败。
        raise ValueError(f"未知的实体类型: {unknown_entity_type}")

    start_time = time.time()
    total_tasks = len(tasks)
    summary_parts, totals_by_type, finished_by_type = _summarize_tasks(tasks)

    logger.info(
        f"开始构建 {total_tasks} 个实体的事件映射 ({' 和 '.join(summary_parts)})，"
        f"模式: {'多线程' if max_workers > 1 else '单线程'} (workers: {max_workers})"
    )
    logger.debug(f"HIRC 后端: {mapping_session.describe_hirc_backend(ctx)}")

    show_exception = bool(getattr(ctx.config, "dev_mode", False))
    # manager 和 runtime_cache 都按“整轮任务”复用，
    # 否则多实体并发时会重复创建 wwiser 进程态和 WAD/HIRC 缓存。
    wwiser_manager = mapping_session._create_wwiser_manager(ctx)
    runtime_cache = mapping_session.RuntimeCache(cache_lock=threading.Lock() if max_workers > 1 else None)
    progress_lock = threading.Lock() if max_workers > 1 else None
    artifact_lock = threading.Lock()
    artifact_paths: list[list[str]] = [[] for _ in tasks]

    def capture_artifact(index: int) -> Callable[[Path], None]:
        """记录当前实体已成功写入的 mapping 产物。"""

        def _capture(path: Path) -> None:
            with artifact_lock:
                artifact_paths[index].append(str(path))

        return _capture

    def get_artifacts(index: int) -> tuple[str, ...]:
        """返回当前实体的已确认 mapping 产物快照。"""
        with artifact_lock:
            return tuple(artifact_paths[index])

    if max_workers > 1:

        def build_entity_with_progress(
            index: int,
            entity_type: str,
            entity_id: int | str,
            description: str,
        ) -> None:
            if progress_lock is None:
                _emit_running_progress(
                    progress_callback,
                    entity_type,
                    finished_by_type,
                    totals_by_type,
                    total_tasks,
                    description,
                )
            else:
                with progress_lock:
                    _emit_running_progress(
                        progress_callback,
                        entity_type,
                        finished_by_type,
                        totals_by_type,
                        total_tasks,
                        description,
                    )
            _build_entity(
                entity_type,
                entity_id,
                reader,
                wwiser_manager,
                integrate_data,
                runtime_cache,
                ctx=ctx,
                persisted_mapping_callback=capture_artifact(index),
            )

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {
                executor.submit(build_entity_with_progress, index, entity_type, entity_id, description): (
                    index,
                    entity_type,
                    entity_id,
                    description,
                )
                for index, (entity_type, entity_id, description) in enumerate(tasks)
            }
            completed_count = 0
            entity_results: list[EntityResult | None] = [None] * total_tasks
            for future in as_completed(future_to_task):
                index, entity_type, entity_id, description = future_to_task[future]
                completed_count += 1
                finished_by_type[entity_type] = finished_by_type.get(entity_type, 0) + 1
                try:
                    future.result()
                    entity_results[index] = EntityResult(
                        entity_type,
                        entity_id,
                        ResultStatus.SUCCESS,
                        entity_name=description,
                        artifacts=get_artifacts(index),
                    )
                    progress_message = f"{description} 映射完成"
                    logger.info(f"进度: {completed_count}/{total_tasks} - {progress_message}。")
                except Exception as exc:  # noqa: BLE001
                    entity_results[index] = EntityResult.from_error(
                        entity_type,
                        entity_id,
                        exc,
                        entity_name=description,
                        artifacts=get_artifacts(index),
                    )
                    progress_message = f"{description} 映射失败"
                    logger.opt(exception=show_exception).warning(f"{description} 映射失败，将继续后续任务: {exc}")
                _emit_progress(
                    progress_callback,
                    entity_type,
                    finished_by_type,
                    totals_by_type,
                    completed_count,
                    total_tasks,
                    progress_message,
                )
    else:
        completed_count = 0
        entity_results = []
        for entity_type, entity_id, description in tasks:
            try:
                _emit_running_progress(
                    progress_callback,
                    entity_type,
                    finished_by_type,
                    totals_by_type,
                    total_tasks,
                    description,
                )
                _build_entity(
                    entity_type,
                    entity_id,
                    reader,
                    wwiser_manager,
                    integrate_data,
                    runtime_cache,
                    ctx=ctx,
                    persisted_mapping_callback=capture_artifact(completed_count),
                )
                entity_results.append(
                    EntityResult(
                        entity_type,
                        entity_id,
                        ResultStatus.SUCCESS,
                        entity_name=description,
                        artifacts=get_artifacts(completed_count),
                    )
                )
                progress_message = f"{description} 映射完成"
            except Exception as exc:  # noqa: BLE001
                entity_results.append(
                    EntityResult.from_error(
                        entity_type,
                        entity_id,
                        exc,
                        entity_name=description,
                        artifacts=get_artifacts(completed_count),
                    )
                )
                progress_message = f"{description} 映射失败"
                logger.opt(exception=show_exception).warning(f"{description} 映射失败，将继续后续任务: {exc}")
            completed_count += 1
            if entity_results[-1].status is ResultStatus.SUCCESS:
                logger.info(f"进度: {completed_count}/{total_tasks} - {progress_message}。")
            finished_by_type[entity_type] = finished_by_type.get(entity_type, 0) + 1
            _emit_progress(
                progress_callback,
                entity_type,
                finished_by_type,
                totals_by_type,
                completed_count,
                total_tasks,
                progress_message,
            )

    duration = time.time() - start_time
    stage_result = StageResult.from_entities("mapping", tuple(entity_results))
    summary_message = (
        f"映射结果: {' 和 '.join(summary_parts)}，"
        f"成功 {stage_result.success_count} 个，失败 {stage_result.failed_count} 个，"
        f"耗时 {duration:.2f}s"
    )
    if stage_result.status is ResultStatus.SUCCESS:
        logger.debug(summary_message)
    elif stage_result.status is ResultStatus.PARTIAL:
        logger.warning(summary_message)
    else:
        logger.error(summary_message)
    return stage_result


def build_all(  # noqa: PLR0913
    reader: DataReader,
    max_workers: int = 4,
    include_champions: bool = True,
    include_maps: bool = True,
    integrate_data: bool = False,
    *,
    ctx: AppContext,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
) -> StageResult:
    """构建所有实体的事件映射。

    Args:
        reader: 已初始化的数据读取器。
        max_workers: 最大工作线程数。
        include_champions: 是否包含英雄。
        include_maps: 是否包含地图。
        integrate_data: 是否生成整合数据。
        ctx: 运行时上下文。
        progress_callback: 每个实体完成后的可选进度回调。

    Returns:
        当前 mapping 阶段的执行结果。
    """

    tasks: list[EntityTask] = []
    if include_champions:
        champion_tasks = generate_champion_tasks(reader, None)
        tasks.extend(champion_tasks)
        logger.debug(f"已添加 {len(champion_tasks)} 个英雄映射任务")
    if include_maps:
        map_tasks = generate_map_tasks(reader, None)
        tasks.extend(map_tasks)
        logger.debug(f"已添加 {len(map_tasks)} 个地图映射任务")
    return execute_tasks(
        tasks,
        reader,
        max_workers,
        integrate_data,
        ctx=ctx,
        progress_callback=progress_callback,
    )


def build_champions(  # noqa: PLR0913
    reader: DataReader,
    champion_ids: list[int],
    max_workers: int = 4,
    integrate_data: bool = False,
    *,
    ctx: AppContext,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
) -> StageResult:
    """构建指定英雄的事件映射。

    Args:
        reader: 数据读取器实例。
        champion_ids: 英雄 ID 列表。
        max_workers: 最大工作线程数。
        integrate_data: 是否生成整合数据。
        ctx: 运行时上下文。
        progress_callback: 每个实体完成后的可选进度回调。

    Returns:
        当前 mapping 阶段的执行结果。
    """

    return execute_tasks(
        generate_champion_tasks(reader, champion_ids),
        reader,
        max_workers,
        integrate_data,
        ctx=ctx,
        progress_callback=progress_callback,
    )


def build_maps(  # noqa: PLR0913
    reader: DataReader,
    map_ids: list[int],
    max_workers: int = 4,
    integrate_data: bool = False,
    *,
    ctx: AppContext,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
) -> StageResult:
    """构建指定地图的事件映射。

    Args:
        reader: 数据读取器实例。
        map_ids: 地图 ID 列表。
        max_workers: 最大工作线程数。
        integrate_data: 是否生成整合数据。
        ctx: 运行时上下文。
        progress_callback: 每个实体完成后的可选进度回调。

    Returns:
        当前 mapping 阶段的执行结果。
    """

    return execute_tasks(
        generate_map_tasks(reader, map_ids),
        reader,
        max_workers,
        integrate_data,
        ctx=ctx,
        progress_callback=progress_callback,
    )


def build_resource_packs(  # noqa: PLR0913
    reader: DataReader,
    keys: list[str],
    max_workers: int = 4,
    integrate_data: bool = False,
    *,
    ctx: AppContext,
    progress_callback: Callable[[str, int, int, str], None] | None = None,
) -> StageResult:
    """构建指定 resource-pack 的事件映射。

    Args:
        reader: 数据读取器实例。
        keys: canonical resource-pack key 列表。
        max_workers: 最大工作线程数。
        integrate_data: 是否生成整合数据。
        ctx: 运行时上下文。
        progress_callback: 每个实体完成后的可选进度回调。

    Returns:
        当前 mapping 阶段的执行结果。
    """
    tasks: list[EntityTask] = [("resource_pack", key, f"资源包 {key}") for key in keys]
    return execute_tasks(
        tasks,
        reader,
        max_workers,
        integrate_data,
        ctx=ctx,
        progress_callback=progress_callback,
    )
