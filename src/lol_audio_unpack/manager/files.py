"""manager 子域共享的数据文件读写辅助。"""

from __future__ import annotations

import shutil
from pathlib import Path

import msgpack
from loguru import logger

from lol_audio_unpack.manager.errors import ArtifactWriteError, SharedDataCorruptError
from lol_audio_unpack.utils.atomic import replace_file as _replace_file
from lol_audio_unpack.utils.common import dump_msgpack


def find_data_file(path: Path, *, dev_mode: bool = False) -> Path | None:
    """定位唯一的 MessagePack 产物，不读取旧格式。

    Args:
        path: 文件路径，可带或不带后缀。
        dev_mode: 兼容参数，不再改变存储格式。

    Returns:
        实际存在的 MessagePack 路径，缺失时返回 ``None``。
    """
    target = path.with_suffix(".msgpack")
    if target.is_file():
        return target
    for suffix in (".yml", ".yaml", ".json"):
        legacy = path.with_suffix(suffix)
        if legacy.is_file():
            logger.warning(
                f"旧格式数据不再自动读取: {legacy}。请离线转换："
                f'uv run scripts/convert_data.py --input "{legacy}" --output "{target}" '
                f"--from {suffix[1:]} --to msgpack；容器转换不会升级 schema。"
            )
            break
    return None


def read_data(path: Path, *, dev_mode: bool = False, log_errors: bool = True) -> dict:
    """读取 MessagePack 字典，区分缺失与损坏。

    Args:
        path: 文件路径，可带或不带后缀。
        dev_mode: 兼容参数，不再改变存储格式。
        log_errors: 反序列化失败时是否在当前边界记录 traceback。

    Returns:
        读取到的字典；文件缺失时返回空字典。

    Raises:
        SharedDataCorruptError: 文件损坏或顶层不是字典；不回退旧格式。
    """
    actual_file = find_data_file(path, dev_mode=dev_mode)
    if not actual_file:
        logger.debug(f"MessagePack 数据尚未生成: {path.with_suffix('.msgpack')}")
        return {}
    try:
        # 实体目录存在整数键；不能用会吞掉解析异常的通用兼容 loader 读取权威产物。
        with actual_file.open("rb") as stream:
            result = msgpack.load(stream, raw=False, strict_map_key=False)
        if not isinstance(result, dict):
            raise ValueError("结构化产物顶层必须是字典")
        return result
    except Exception as exc:
        if log_errors:
            logger.opt(exception=True).error(f"读取文件时出错: {actual_file}, 错误: {exc}")
        raise SharedDataCorruptError(f"结构化数据损坏，未回退旧格式: {actual_file}") from exc


def write_data(data: dict, base_path: Path, *, dev_mode: bool = False) -> Path:
    """以 MessagePack 原子替换可再生数据文件。

    Args:
        data: 要写入的数据。
        base_path: 不带后缀的基础文件路径。
        dev_mode: 兼容参数，不再改变存储格式。

    Returns:
        成功替换的实际目标路径。

    Raises:
        ArtifactWriteError: 无法完成序列化、同步或原子替换。
    """
    path = base_path.with_suffix(".msgpack")
    target = _replace_file(path, lambda temp: dump_msgpack(data, temp), write_stage="serialize")
    logger.trace(f"成功写入数据到: {target}")
    return target


def write_bytes_atomic(data: bytes, path: Path) -> Path:
    """以原子替换方式写入精确字节。

    Args:
        data: 要持久化的完整字节。
        path: 带后缀的正式目标路径。

    Returns:
        成功替换的目标路径。

    Raises:
        ArtifactWriteError: 无法完成写入、同步或原子替换。
    """
    return _replace_file(path, lambda temp: temp.write_bytes(data), write_stage="write")


def copy_file_atomic(source: Path, target: Path) -> Path:
    """复制完整文件并以原子替换方式发布。

    Args:
        source: 要复制的源文件。
        target: 带后缀的正式目标路径。

    Returns:
        成功替换的目标路径。

    Raises:
        ArtifactWriteError: 无法完成复制、同步或原子替换。
    """
    return _replace_file(target, lambda temp: shutil.copy2(source, temp), write_stage="copy")


def needs_update(
    base_path: Path,
    current_version: str,
    force_update: bool,
    *,
    dev_mode: bool,
    resource_schema: int | None = None,
) -> bool:
    """检查目标文件是否需要更新。

    Args:
        base_path: 要检查的文件基础路径，不带后缀。
        current_version: 当前游戏版本。
        force_update: 是否强制更新。
        dev_mode: 是否启用开发模式。
        resource_schema: 本地派生产物要求的 resource schema；为空时不检查。

    Returns:
        若需要更新则返回 ``True``。
    """
    actual_file = find_data_file(base_path, dev_mode=dev_mode)
    if force_update or not actual_file:
        return True

    try:
        data = read_data(base_path, dev_mode=dev_mode)
    except SharedDataCorruptError:
        logger.warning(f"可再生数据损坏，需要重新生成: {base_path}")
        return True
    data_version = data.get("metadata", {}).get("gameVersion") if data else None
    if not data_version:
        return True

    if resource_schema is not None and data.get("resourceSchemaVersion") != resource_schema:
        logger.debug(f"资源 schema 已过期，需要重新生成: {base_path.name}")
        return True

    if data_version != current_version:
        return True

    return False


__all__ = [
    "copy_file_atomic",
    "find_data_file",
    "needs_update",
    "read_data",
    "write_bytes_atomic",
    "write_data",
]
