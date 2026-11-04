"""集中维护结构化特殊内容的身份、展示与本地可用性规则。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SpecialContentProfile:
    """描述一类结构化特殊英雄的固定展示规则。

    Args:
        mode_key: 面向程序的稳定分组标识。
        prefix: alias 或 WAD 文件名使用的技术前缀。
        display_name: 简体中文分组名。
        english_name: 英文分组名。
    """

    mode_key: str
    prefix: str
    display_name: str
    english_name: str


@dataclass(frozen=True, slots=True)
class SpecialContentItem:
    """供 GUI catalog 使用的结构化特殊内容投影。

    Args:
        key: 可逆的稳定选择 key。
        champion_id: 保持原有逻辑身份的英雄数值 ID。
        profile: 对应的固定展示 profile。
        display_name: 分组内展示的本地化英雄名。
        base_alias: 移除技术前缀后的基础 alias。
        internal_alias: 原始技术 alias，仅供搜索、提示与诊断。
        search_text: 归一化后的搜索字段组合。
    """

    key: str
    champion_id: int
    profile: SpecialContentProfile
    display_name: str
    base_alias: str
    internal_alias: str
    search_text: str

    @property
    def standalone_name(self) -> str:
        """返回脱离分组时使用的本地化名称。"""
        return f"{self.profile.display_name} · {self.display_name}"


DOOM_BOTS_PROFILE = SpecialContentProfile(
    mode_key="doom_bots",
    prefix="Ruby_",
    display_name="末日人机",
    english_name="Doom Bots",
)
SWARM_PROFILE = SpecialContentProfile(
    mode_key="swarm",
    prefix="Strawberry_",
    display_name="无尽狂潮",
    english_name="Swarm",
)
LEGACY_CHAMPIONS_PROFILE = SpecialContentProfile(
    mode_key="legacy_champions",
    prefix="Jade_",
    display_name="经典召唤师峡谷",
    english_name="Classic Summoner's Rift",
)
HISTORICAL_RESOURCE_PACKS_PROFILE = SpecialContentProfile(
    mode_key="historical_resource_packs",
    prefix="",
    display_name="本地资源包",
    english_name="Local Resource Packs",
)
SPECIAL_CONTENT_PROFILES = (
    LEGACY_CHAMPIONS_PROFILE,
    DOOM_BOTS_PROFILE,
    SWARM_PROFILE,
)
SPECIAL_CONTENT_GROUPS = (*SPECIAL_CONTENT_PROFILES, HISTORICAL_RESOURCE_PACKS_PROFILE)
JADE_ID_OFFSET = 60000


def get_prime_champion_id(champion: Mapping[str, Any]) -> int | None:
    """返回关联的原英雄 ID；旧 Jade 元数据沿用已有的 ID 对应规则。"""
    if champion.get("relatedPrimeItemId") is not None:
        return int(champion["relatedPrimeItemId"])
    if str(champion.get("alias", "")).casefold().startswith(LEGACY_CHAMPIONS_PROFILE.prefix.casefold()):
        return int(champion["id"]) - JADE_ID_OFFSET
    return None


def _normalized_wad_basename(champion: Mapping[str, Any]) -> str:
    """提取并归一化冠军根 WAD 的文件名。"""
    wad = champion.get("wad", {})
    root = wad.get("root", "") if isinstance(wad, Mapping) else ""
    return str(root).replace("\\", "/").rsplit("/", maxsplit=1)[-1].casefold()


def _wad_base_alias(champion: Mapping[str, Any]) -> str:
    """从根 WAD 文件名恢复可用于展示回退的基础 alias。"""
    wad = champion.get("wad", {})
    root = wad.get("root", "") if isinstance(wad, Mapping) else ""
    wad_basename = str(root).replace("\\", "/").rsplit("/", maxsplit=1)[-1]
    return wad_basename.partition(".wad")[0]


def _resolve_profile(alias: str, wad_basename: str) -> SpecialContentProfile | None:
    """根据 alias 与 WAD basename 查找特殊内容 profile。"""
    normalized_alias = alias.casefold()
    for profile in SPECIAL_CONTENT_PROFILES:
        prefix = profile.prefix.casefold()
        if normalized_alias.startswith(prefix) or wad_basename.startswith(prefix):
            return profile
    return None


def get_special_content_profile(champion: Mapping[str, Any]) -> SpecialContentProfile | None:
    """返回结构化特殊英雄的展示 profile。

    Args:
        champion: 原始英雄结构化数据。

    Returns:
        SpecialContentProfile | None: 匹配的 profile；普通英雄返回 ``None``。
    """
    alias = str(champion.get("alias", "")).strip()
    return _resolve_profile(alias, _normalized_wad_basename(champion))


def is_structured_special_champion(champion: Mapping[str, Any]) -> bool:
    """判断结构化英雄是否属于特殊内容目录。

    分类同时检查 alias 与根 WAD basename，避免仅有其中一个技术字段时
    被普通英雄目录重复展示。

    Args:
        champion: 原始英雄结构化数据。

    Returns:
        bool: 匹配 Ruby、Strawberry 或 Jade profile 时为 ``True``。
    """
    return get_special_content_profile(champion) is not None


def build_special_target_key(champion_id: int) -> str:
    """构造结构化特殊英雄的稳定选择 key。"""
    return f"champion:{int(champion_id)}"


def resolve_special_target_ids(targets: tuple[str, ...] | list[str]) -> tuple[int, ...]:
    """将稳定 special key 解析回原有英雄数值 ID。

    Args:
        targets: ``champion:<id>`` 格式的特殊内容选择 key。

    Returns:
        tuple[int, ...]: 保持输入顺序且去重后的英雄 ID。

    Raises:
        ValueError: key 非法或不属于结构化英雄时抛出。
    """
    ids: list[int] = []
    seen: set[int] = set()
    for target in targets:
        prefix, separator, raw_id = str(target).partition(":")
        if prefix != "champion" or separator != ":" or not raw_id.isdecimal():
            raise ValueError(f"特殊内容目标无效: {target}")
        champion_id = int(raw_id)
        if champion_id not in seen:
            seen.add(champion_id)
            ids.append(champion_id)
    return tuple(ids)


def merge_champion_ids(
    champion_ids: tuple[int, ...] | None,
    special_targets: tuple[str, ...] | list[str] = (),
) -> tuple[int, ...] | None:
    """合并普通英雄 ID 与 special key 归约出的逻辑英雄 ID。"""
    if champion_ids is None and not special_targets:
        return None

    merged: list[int] = []
    seen: set[int] = set()
    for champion_id in (*(champion_ids or ()), *resolve_special_target_ids(special_targets)):
        if champion_id not in seen:
            seen.add(champion_id)
            merged.append(champion_id)
    return tuple(merged)


def build_special_content_item(
    champion: Mapping[str, Any],
    *,
    display_name: str = "",
) -> SpecialContentItem | None:
    """将结构化特殊英雄投影为 GUI catalog item。

    Args:
        champion: 原始英雄结构化数据。
        display_name: 已按当前地区解析的英雄名；为空时回退基础 alias。

    Returns:
        SpecialContentItem | None: 普通英雄返回 ``None``。
    """
    profile = get_special_content_profile(champion)
    if profile is None:
        return None

    champion_id = int(champion["id"])
    internal_alias = str(champion.get("alias", "")).strip() or _wad_base_alias(champion)
    prefix = profile.prefix
    alias_for_base = internal_alias or _wad_base_alias(champion)
    base_alias = (
        alias_for_base[len(prefix) :] if alias_for_base.casefold().startswith(prefix.casefold()) else alias_for_base
    )
    localized_name = str(display_name).strip() or base_alias
    key = build_special_target_key(champion_id)
    search_text = " ".join(
        (
            profile.display_name,
            profile.english_name,
            localized_name,
            base_alias,
            internal_alias,
            str(champion_id),
            key,
        )
    ).casefold()
    return SpecialContentItem(
        key=key,
        champion_id=champion_id,
        profile=profile,
        display_name=localized_name,
        base_alias=base_alias,
        internal_alias=internal_alias,
        search_text=search_text,
    )


__all__ = [
    "DOOM_BOTS_PROFILE",
    "HISTORICAL_RESOURCE_PACKS_PROFILE",
    "LEGACY_CHAMPIONS_PROFILE",
    "SPECIAL_CONTENT_GROUPS",
    "SPECIAL_CONTENT_PROFILES",
    "SWARM_PROFILE",
    "SpecialContentItem",
    "SpecialContentProfile",
    "build_special_content_item",
    "build_special_target_key",
    "get_prime_champion_id",
    "get_special_content_profile",
    "is_structured_special_champion",
    "merge_champion_ids",
    "resolve_special_target_ids",
]
