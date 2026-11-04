"""应用层共享的游戏版本判定辅助。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from lol_audio_unpack.utils.game_layout import GAME_METADATA, LCU_EXE, resolve_layout
from lol_audio_unpack.utils.versioning import extract_windows_file_version, normalize_patch_version

if TYPE_CHECKING:
    from .types import AppContext


def get_game_version(game_path: Path) -> str:
    """读取本地 GAME 版本号。

    Args:
        game_path: 游戏根目录路径。

    Returns:
        标准化后的 ``major.minor`` 版本号。

    Raises:
        FileNotFoundError: 当缺少 `content-metadata.json` 时抛出。
    """
    meta = game_path / GAME_METADATA
    if not meta.exists():
        raise FileNotFoundError("content-metadata.json 文件不存在，无法判断版本信息")

    with open(meta, encoding="utf-8-sig") as file:
        data = json.load(file)

    return normalize_patch_version(data["version"])


def get_lcu_version(game_path: Path) -> str | None:
    """读取本地 ``LeagueClient.exe`` 的补丁版本。

    Args:
        game_path: 游戏根目录。

    Returns:
        若存在且成功解析，返回 ``major.minor`` 形式的版本号；否则返回 ``None``。
    """
    exe_path = resolve_layout(game_path).client_root / LCU_EXE
    if not exe_path.is_file():
        return None

    try:
        payload = exe_path.read_bytes()
        return normalize_patch_version(extract_windows_file_version(payload))
    except (OSError, ValueError) as exc:
        logger.warning(f"无法从 LeagueClient.exe 解析版本，已跳过 LCU 一致性校验: {exe_path} | {exc}")
        return None


def validate_install_version(game_path: Path, game_version: str) -> None:
    """校验本地 GAME 与 LCU 的主版本是否一致。

    Args:
        game_path: 游戏根目录。
        game_version: 从 `content-metadata.json` 提取的补丁版本。
    """
    lcu_version = get_lcu_version(game_path)
    if lcu_version is None:
        logger.debug("未执行本地 LCU 一致性校验：缺少或无法读取 LeagueClient.exe 版本。")
        return

    if lcu_version == game_version:
        logger.debug(f"本地 GAME / LCU 主版本一致: {game_version}")
        return

    logger.warning(
        "检测到本地 GAME / LCU 主版本不一致："
        f"GAME={game_version}, LCU={lcu_version}。请确认传入的游戏目录是否完整且自洽。"
    )


def resolve_game_version(ctx: AppContext) -> str:
    """解析当前本地数据源使用的游戏版本。

    Args:
        ctx: 运行时上下文。

    Returns:
        当前运行使用的补丁版本号。

    """
    cached_version = ctx.runtime_cache.get("resolved_runtime_version")
    if isinstance(cached_version, str) and cached_version:
        return cached_version

    version = get_game_version(Path(ctx.config.game_path))
    if not ctx.runtime_cache.get("local_version_validated", False):
        validate_install_version(Path(ctx.config.game_path), version)
        ctx.runtime_cache["local_version_validated"] = True

    ctx.runtime_cache["resolved_runtime_version"] = version
    return version


__all__ = [
    "get_game_version",
    "get_lcu_version",
    "resolve_game_version",
    "validate_install_version",
]
