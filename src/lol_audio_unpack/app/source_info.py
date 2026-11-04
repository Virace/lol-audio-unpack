"""记录本地客户端声明的地区、布局和资源语言，帮助复现输入问题。"""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger
from ruamel.yaml import YAML, YAMLError

from lol_audio_unpack.utils.game_layout import GameLayoutError, resolve_layout


def _read_region(path: Path, keys: tuple[str, ...]) -> str | None:
    """只提取地区标识，诊断不输出安装配置的其他内容。"""
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


def log_source_info(game_path: Path, language: str, *, level: str = "INFO") -> None:
    """在输入预检前记录客户端声明，不推断账号实际登录服务器。

    Args:
        game_path: 用户选定的游戏根目录。
        language: 本次选择的资源语言，与安装地区分别记录。
        level: 上下文启动时正常记录，源任务预检时可补记到 DEBUG 文件日志。
    """
    try:
        layout = resolve_layout(game_path)
    except GameLayoutError as exc:
        message = f"客户端来源的布局尚不明确，继续由输入预检报告: {exc}"
        logger.warning(message)
        return
    system = layout.client_root / "system.yaml"
    settings = layout.client_root / "Config" / "LeagueClientSettings.yaml"
    installed = _read_region(system, ("default_region",))
    configured = _read_region(settings, ("install", "globals", "region"))
    resource_language = "en_US" if language.casefold() == "default" else language or "未选择"
    source = system.relative_to(game_path).as_posix() + ":default_region" if installed else "无本地声明"
    message = (
        f"客户端来源: 布局={layout.kind}，安装地区声明={installed or '未知'}，"
        f"配置地区={configured or '未知'}，资源语言={resource_language}，安装声明来源={source}"
    )
    logger.log(level, message)
