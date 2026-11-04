"""应用入口与运行时版本元数据导出。"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from .app import create_app_context as _create_app_context
from .app.source_info import log_source_info, read_source_info
from .utils.logging import log_paths, setup_logging
from .utils.versioning import resolve_runtime_version

if TYPE_CHECKING:
    from .app import AppContext

_STATIC_VERSION = "3.11.0"
__version__ = resolve_runtime_version(Path(__file__).resolve().parents[2], _STATIC_VERSION)

logger.disable("lol_audio_unpack")


def setup_app(
    dev_mode: bool = False,
    log_level: str = "INFO",
    *,
    source_check: Callable[[AppContext], None] | None = None,
    **kwargs,
) -> AppContext:
    """初始化应用并返回可注入上下文。

    Args:
        dev_mode: 是否开启开发模式。
        log_level: 日志级别，例如 ``INFO``、``DEBUG``。
        source_check: 可选的所选来源文件检查，在文件日志与输出初始化前执行。
        **kwargs: 透传给 ``create_app_context`` 的参数。

    Returns:
        初始化后的 ``AppContext``。
    """
    logger.enable("lol_audio_unpack")

    logger.remove()
    try:
        logger.add(sys.stdout, level=log_level.upper(), enqueue=True, colorize=True)
    except (OSError, PermissionError):
        logger.add(sys.stdout, level=log_level.upper(), enqueue=False, colorize=True)
        logger.warning("日志队列初始化失败，已回退为非 enqueue 模式。")

    logger.info("正在检查本地资源与配置……")
    app_context = _create_app_context(dev_mode=dev_mode, **kwargs)
    if source_check is not None:
        source_check(app_context)

    setup_logging(
        dev_mode=dev_mode,
        log_level=log_level,
        log_file_path=app_context.paths.log_path,
        show_function_info=True,
    )

    log_paths(app_context.config.game_path, app_context.config.output_path)
    log_source_info(read_source_info(app_context.config.game_path))
    language = app_context.config.game_region
    logger.info("资源语言：{}", "en_US" if language == "default" else language)
    logger.debug("音频类型：{}", ",".join(app_context.config.include_types))
    return app_context


__all__ = [
    "__version__",
    "setup_app",
]
