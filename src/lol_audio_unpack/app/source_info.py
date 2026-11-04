"""读取并记录客户端地区与目录布局。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from ruamel.yaml import YAML, YAMLError

from lol_audio_unpack.utils.game_layout import GameLayout, resolve_layout


@dataclass(frozen=True)
class SourceInfo:
    """所选客户端的地区与布局。"""

    layout: GameLayout
    installed: str | None
    configured: str | None


def _read_region(path: Path, keys: tuple[str, ...]) -> str | None:
    """读取指定的地区字段。"""
    if not path.is_file():
        return None
    try:
        value = YAML(typ="safe").load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, YAMLError) as exc:
        logger.debug("无法读取客户端地区声明: {} ({})", path.name, type(exc).__name__)
        return None
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
        return value
    return None


def read_source_info(game_path: Path) -> SourceInfo:
    """读取所选客户端的地区与布局。"""
    layout = resolve_layout(game_path.resolve())
    return SourceInfo(
        layout,
        _read_region(layout.client_root / "system.yaml", ("default_region",)),
        _read_region(layout.client_root / "Config" / "LeagueClientSettings.yaml", ("install", "globals", "region")),
    )


def log_source_info(info: SourceInfo) -> None:
    """记录客户端地区、目录布局与地区字段位置。"""
    location = {
        "flat": "LCU 位于游戏根目录",
        "nested": "LCU 位于 LeagueClient 子目录",
    }.get(info.layout.kind, "LCU 位置未识别")
    logger.info("客户端地区：{}，{}", info.installed or info.configured or "未知", location)
    if info.installed and info.configured and info.installed != info.configured:
        logger.debug("安装地区与客户端配置不同：{} / {}", info.installed, info.configured)
    logger.debug(
        "地区字段位置：{}:default_region；{}:install.globals.region",
        (info.layout.client_root / "system.yaml").relative_to(info.layout.root).as_posix(),
        (info.layout.client_root / "Config" / "LeagueClientSettings.yaml").relative_to(info.layout.root).as_posix(),
    )
