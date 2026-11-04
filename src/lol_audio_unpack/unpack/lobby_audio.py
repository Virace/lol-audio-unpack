"""将缓存中的大厅音频附加到英雄输出目录。"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from lol_audio_unpack.app.outputs import save_outputs
from lol_audio_unpack.app.path_layout import format_entity_folder_name, get_output_dir_name
from lol_audio_unpack.manager import DataReader, DataUpdater
from lol_audio_unpack.manager.lobby import LOBBY_FILES, find_lobby_source
from lol_audio_unpack.model import AudioEntityData
from lol_audio_unpack.utils.atomic import replace_file

if TYPE_CHECKING:
    from lol_audio_unpack.app.types import AppContext


def find_lobby_audio_source(
    reader: DataReader,
    champion_id: str,
    category: str,
    *,
    ctx: AppContext,
) -> Path | None:
    """查找大厅音频源文件。

    Args:
        reader: 数据读取器。
        champion_id: 英雄 ID。
        category: 大厅语音分类。
        ctx: 运行时上下文。

    Returns:
        命中的语音文件路径；未找到时返回 ``None``。
    """
    return find_lobby_source(ctx.version_path("manifest", reader.version), ctx.game_region, champion_id, category)


def link_audio(source: Path, target: Path) -> str:
    """创建硬链接，失败时保留原目标并报告错误。

    Args:
        source: 源文件路径。
        target: 目标文件路径。

    Returns:
        实际写入模式 ``hardlink``。
    """
    if target.is_file() and source.samefile(target):
        return "hardlink"
    mode = "hardlink"

    def write(temp: Path) -> None:
        temp.unlink()
        try:
            os.link(source, temp)
        except OSError as exc:
            logger.error("大厅音频硬链接失败，保留原文件：{} | {}", target, exc)
            raise

    replace_file(target, write, write_stage="lobby")
    return mode


def attach_lobby_audio(
    entity: AudioEntityData,
    reader: DataReader,
    *,
    ctx: AppContext,
    persisted_artifact_callback: Callable[[Path], None] | None = None,
) -> tuple[Path, ...]:
    """将大厅音频附加到英雄输出目录。

    Args:
        entity: 英雄实体数据。
        reader: 数据读取器。
        ctx: 运行时上下文。
        persisted_artifact_callback: 大厅音频成功落盘后的可选内部回调。

    Returns:
        本轮成功写入的大厅音频路径。
    """
    if not bool(ctx.config.lobby_audio):
        return ()

    if any(find_lobby_audio_source(reader, entity.entity_id, category, ctx=ctx) is None for category in LOBBY_FILES):
        DataUpdater(ctx).ensure_lobby_audio((entity.entity_id,))

    audio_root = ctx.version_path("audio", reader.version)
    entity_folder = format_entity_folder_name(
        entity.entity_id,
        entity.entity_alias,
        entity.entity_name,
        entity.entity_title,
    )

    target_dir = _build_lobby_dir(
        audio_root=audio_root,
        version=reader.version,
        entity_type=entity.entity_type,
        entity_folder=entity_folder,
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    persisted_paths: list[Path] = []

    for category, target_name in LOBBY_FILES.items():
        source = find_lobby_audio_source(reader, entity.entity_id, category, ctx=ctx)
        if source is None:
            logger.warning("英雄 {} 的大厅音频未找到：{}（语言={}）", entity.entity_id, category, ctx.game_region)
            continue

        target = target_dir / target_name
        mode = link_audio(source, target)
        persisted_paths.append(target)
        if persisted_artifact_callback is not None:
            persisted_artifact_callback(target)
        source_path = source.relative_to(ctx.version_path("manifest", reader.version))
        logger.debug("大厅音频已写入：{}（来源：{}，mode={}）", target, source_path, mode)
    save_outputs(
        ctx.config.output_path,
        reader.version,
        ctx.game_region,
        entity.entity_type,
        str(entity.entity_id),
        persisted_paths,
    )
    return tuple(persisted_paths)


def _build_lobby_dir(
    *,
    audio_root: Path,
    version: str,
    entity_type: str,
    entity_folder: str,
) -> Path:
    """构建统一大厅音频输出目录。"""
    return audio_root / get_output_dir_name(entity_type) / entity_folder / "lobby"
