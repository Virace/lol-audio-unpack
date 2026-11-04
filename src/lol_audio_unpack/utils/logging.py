"""集中管理项目的 Loguru 日志初始化与辅助能力。"""

import re
import sys
import time
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from loguru import logger

from lol_audio_unpack.utils.common import format_duration

__all__ = [
    "LoggingConfiguration",
    "performance_monitor",
    "setup_logging",
]


def get_log_level() -> int:
    """返回当前输出端所需的最低等级，供任务子进程采集日志。"""
    return LoggingConfiguration.log_level


def format_log_paths(record: dict) -> None:
    """将日志正文中的游戏、输出根目录改为相对路径。"""
    if record["extra"].get("show_roots"):
        return
    for pattern, label in LoggingConfiguration.path_patterns:
        record["message"] = pattern.sub(
            lambda match, name=label: "" if match.group()[-1] in "\\/" else name,
            record["message"],
        )


def log_paths(game_path: Path, output_path: Path) -> None:
    """在首次选定或切换根目录时确认实际路径。"""
    roots = (game_path.resolve(), output_path.resolve())
    if roots == LoggingConfiguration.paths:
        return
    LoggingConfiguration.paths = roots
    patterns = []
    for root, label in sorted(
        zip(roots, ("游戏目录", "输出目录"), strict=True), key=lambda item: len(str(item[0])), reverse=True
    ):
        prefix = r"[\\/]".join(re.escape(part) for part in re.split(r"[\\/]", str(root).rstrip("\\/")))
        patterns.append((re.compile(prefix + r"(?:[\\/]|(?=$|[\s,，;；:：)'\"}]))", re.IGNORECASE), label))
    LoggingConfiguration.path_patterns = tuple(patterns)
    logger.bind(show_roots=True).info("游戏目录：{}\n输出目录：{}", *roots)


class LoggingConfiguration:
    """封装项目使用的 Loguru 日志配置。"""

    log_level = 10
    paths: tuple[Path, ...] = ()
    path_patterns: tuple[tuple[re.Pattern, str], ...] = ()

    @staticmethod
    def _resolve_console_sink(stream: Any) -> Any:
        """将标准流对象转换为可安全传给 Loguru 的 sink。

        Args:
            stream: 标准输出或错误流对象。

        Returns:
            Any: 原始流对象；若传入 ``None``，则返回一个丢弃消息的 callable sink，
                用于兼容 PyInstaller ``windowed`` / ``noconsole`` 下标准流缺失的场景。
        """

        if stream is not None:
            return stream
        return lambda _message: None

    @staticmethod
    def _add_handler_with_enqueue_fallback(*args, **kwargs) -> None:
        """优先启用 enqueue，失败时回退为非 enqueue。"""
        try:
            logger.add(*args, **kwargs)
        except (OSError, PermissionError):
            fallback_kwargs = dict(kwargs)
            fallback_kwargs["enqueue"] = False
            logger.add(*args, **fallback_kwargs)
            logger.warning("日志队列初始化失败，已回退为非 enqueue 模式。")

    @staticmethod
    def setup_logging(
        *,
        dev_mode: bool = False,
        log_level: str = "INFO",
        file_log_level: str = "DEBUG",
        log_file_path: Path | str | None = None,
        show_function_info: bool = True,
    ) -> None:
        """设置日志配置。

        Args:
            dev_mode: 是否为开发模式，会影响 `diagnose` 输出。
            log_level: 控制台日志级别。
            file_log_level: 文件日志级别。
            log_file_path: 日志文件路径，传 ``None`` 时不记录文件日志。
            show_function_info: 是否显示函数名、行号等信息。
        """
        # 移除默认处理器
        LoggingConfiguration.log_level = logger.level(log_level.upper()).no
        if log_file_path:
            LoggingConfiguration.log_level = min(
                LoggingConfiguration.log_level, logger.level(file_log_level.upper()).no
            )
        logger.remove()
        logger.configure(patcher=format_log_paths)

        # 控制台日志格式
        if show_function_info:
            console_format = (
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            )
        else:
            console_format = (
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>"
            )

        # 添加控制台日志处理器
        LoggingConfiguration._add_handler_with_enqueue_fallback(
            LoggingConfiguration._resolve_console_sink(sys.stderr),
            level=log_level.upper(),
            format=console_format,
            backtrace=True,
            diagnose=dev_mode,  # 开发模式显示变量值，生产模式不显示敏感信息
            colorize=True,
            enqueue=True,  # 线程安全
        )

        # 如果指定了文件日志路径，添加文件日志处理器
        if log_file_path:
            log_path = Path(log_file_path)
            explicit_log_file = log_path.suffix.lower() == ".log"
            if explicit_log_file:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                file_sink = log_path
            else:
                # log_path 指向目录（例如 output/logs），懒创建模式下需显式创建该目录。
                log_path.mkdir(parents=True, exist_ok=True)
                file_sink = log_path / "{time:YYYY-MM-DD_HH-mm-ss}.log"

            # 文件日志保留毫秒时间和调用位置。
            file_format = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"

            LoggingConfiguration._add_handler_with_enqueue_fallback(
                file_sink,
                level=file_log_level.upper(),
                format=file_format,
                rotation="100 MB",  # 当文件达到100MB时轮转
                retention=10,  # 保留最近10份日志文件
                compression="zip",  # 压缩旧日志文件
                backtrace=True,
                diagnose=dev_mode,
                enqueue=True,
            )

    @staticmethod
    def performance_monitor(threshold_ms: float = 0, level: str = "DEBUG"):
        """为函数执行耗时打点。

        Args:
            threshold_ms: 性能阈值，单位毫秒。传 ``0`` 时只记录耗时。
            level: 正常情况下使用的日志级别。
        """

        def decorator(func: Callable) -> Callable:
            @wraps(func)
            def wrapper(*args, **kwargs):
                start_time = time.time()
                try:
                    result = func(*args, **kwargs)
                    duration_ms = (time.time() - start_time) * 1000

                    # 格式化耗时显示
                    duration_display = format_duration(duration_ms)

                    # 记录性能信息
                    if threshold_ms > 0 and duration_ms > threshold_ms:
                        # 有阈值且超过阈值，记录警告
                        threshold_display = format_duration(threshold_ms)
                        logger.warning(
                            f"函数 {func.__name__} 执行耗时较长: {duration_display} (超过阈值 {threshold_display})"
                        )
                    else:
                        # 无阈值或未超过阈值，正常记录
                        logger.log(level, f"函数 {func.__name__} 执行完成，耗时: {duration_display}")
                    return result
                except Exception:
                    duration_ms = (time.time() - start_time) * 1000
                    duration_display = format_duration(duration_ms)
                    logger.opt(exception=True).error(f"函数 {func.__name__} 执行失败，耗时: {duration_display}")
                    raise

            return wrapper

        return decorator


# 便捷的全局装饰器和函数
performance_monitor = LoggingConfiguration.performance_monitor


# 为了向后兼容，保留一些常用的便捷函数
def setup_logging(**kwargs):
    """直接调用 `LoggingConfiguration.setup_logging`。"""
    return LoggingConfiguration.setup_logging(**kwargs)
