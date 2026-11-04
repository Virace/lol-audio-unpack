"""应用层共享的实体路径布局与命名规则。"""

from __future__ import annotations

from typing import Literal

from lol_audio_unpack.utils.common import sanitize_filename
from lol_audio_unpack.utils.game_layout import GAME_DIR_CHAMPIONS, GAME_DIR_MAPS

from .resource_pack import RESOURCE_PACK_ENTITY_TYPE, resource_pack_path_component

ENTITY_TYPE_CHAMPION: Literal["champion"] = "champion"
ENTITY_TYPE_MAP: Literal["map"] = "map"
ENTITY_TYPE_RESOURCE_PACK: Literal["resource_pack"] = RESOURCE_PACK_ENTITY_TYPE

AUDIO_TYPE_VO: Literal["VO"] = "VO"
AUDIO_TYPE_SFX: Literal["SFX"] = "SFX"
AUDIO_TYPE_MUSIC: Literal["MUSIC"] = "MUSIC"

DIR_CHAMPIONS: Literal["champions"] = "champions"
DIR_MAPS: Literal["maps"] = "maps"
DIR_RESOURCE_PACKS: Literal["resource_packs"] = "resource_packs"

ENTITY_NAME_SEPARATOR: Literal["·"] = "·"


def get_output_dir_name(entity_type: str) -> str:
    """获取输出目录名称。

    Args:
        entity_type: 实体类型，支持 ``champion``、``map`` 或 ``resource_pack``。

    Returns:
        对应的小写复数目录名。

    Raises:
        ValueError: 当实体类型未知时抛出。
    """
    if entity_type == ENTITY_TYPE_CHAMPION:
        return DIR_CHAMPIONS
    if entity_type == ENTITY_TYPE_MAP:
        return DIR_MAPS
    if entity_type == ENTITY_TYPE_RESOURCE_PACK:
        return DIR_RESOURCE_PACKS
    raise ValueError(f"未知的实体类型: {entity_type}")


def get_game_dir_name(entity_type: str) -> str:
    """获取游戏原始目录名称。

    Args:
        entity_type: 实体类型，支持 ``champion`` 或 ``map``。

    Returns:
        游戏资源中的原始目录名。

    Raises:
        ValueError: 当实体类型未知时抛出。
    """
    if entity_type == ENTITY_TYPE_CHAMPION:
        return GAME_DIR_CHAMPIONS
    if entity_type == ENTITY_TYPE_MAP:
        return GAME_DIR_MAPS
    raise ValueError(f"未知的实体类型: {entity_type}")


def format_entity_folder_name(
    entity_id: str,
    entity_alias: str,
    entity_name: str,
    entity_title: str | None = None,
) -> str:
    """格式化实体文件夹名称。

    Args:
        entity_id: 实体 ID。
        entity_alias: 实体别名。
        entity_name: 实体显示名称。
        entity_title: 可选标题。

    Returns:
        以 `·` 连接的实体文件夹名称。
    """
    parts = [entity_id, entity_alias, entity_name]
    if entity_title:
        parts.append(entity_title)
    return sanitize_filename(ENTITY_NAME_SEPARATOR.join(parts))


def get_entity_path_component(entity_type: str, entity_id: int | str) -> str:
    """返回可用于实体 artifact 路径的身份组件。

    Args:
        entity_type: 实体类型。
        entity_id: 实体稳定身份。

    Returns:
        champion/map 保持原身份文本；resource pack 返回 Windows-safe 组件。
    """
    if entity_type == ENTITY_TYPE_RESOURCE_PACK:
        return resource_pack_path_component(str(entity_id))
    return str(entity_id)


def format_sub_entity_folder_name(sub_id: str, sub_name: str) -> str:
    """格式化子实体文件夹名称。

    Args:
        sub_id: 子实体 ID。
        sub_name: 子实体名称。

    Returns:
        以 `·` 连接的子实体文件夹名称。
    """
    return sanitize_filename(ENTITY_NAME_SEPARATOR.join([sub_id, sub_name]))


__all__ = [
    "AUDIO_TYPE_MUSIC",
    "AUDIO_TYPE_SFX",
    "AUDIO_TYPE_VO",
    "DIR_CHAMPIONS",
    "DIR_MAPS",
    "DIR_RESOURCE_PACKS",
    "ENTITY_NAME_SEPARATOR",
    "ENTITY_TYPE_CHAMPION",
    "ENTITY_TYPE_MAP",
    "ENTITY_TYPE_RESOURCE_PACK",
    "GAME_DIR_CHAMPIONS",
    "GAME_DIR_MAPS",
    "format_entity_folder_name",
    "format_sub_entity_folder_name",
    "get_entity_path_component",
    "get_game_dir_name",
    "get_output_dir_name",
]
