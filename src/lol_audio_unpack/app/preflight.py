"""在源处理任务写入之前，按显式范围复核必需文件的存在性。"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from lol_audio_unpack.manager.source_inventory import SourceDiscoveryError, SourceEntity, scan_inventory
from lol_audio_unpack.utils.game_layout import FINAL_DIR

from .targets import should_hide_champion_by_default
from .types import AppContext


class SourcePreflightError(ValueError):
    """所选源范围不可用，保留具体实体与路径供入口展示。"""

    def __init__(self, message: str, entities: tuple[SourceEntity, ...] = ()) -> None:
        """保存失败实体，消息只合并去重后的必需文件路径。"""
        self.entities = entities
        self.missing = tuple(sorted({path for entity in entities for path in entity.missing}))
        super().__init__(message + ("\n" + "\n".join(self.missing) if self.missing else ""))


def check_source_files(  # noqa: PLR0913
    ctx: AppContext,
    *,
    champion_ids: Sequence[int | str] | None = None,
    map_ids: Sequence[int | str] | None = None,
    include_champions: bool = True,
    include_maps: bool = True,
    resource_wads: Sequence[str] = (),
    resource_only: bool = False,
) -> None:
    """复查本次实际范围，不读取 GAME WAD 内容、不缩小失败选择。

    英雄选择支持稳定 ID 或 alias；地图显式选择同时检查 Common。
    资源包只检查已选物理 WAD，不为它附加普通英雄语音依赖。

    Raises:
        SourcePreflightError: 语言、选择、目录发现或必需文件不可用。
    """
    if not ctx.game_region:
        raise SourcePreflightError("请选择游戏资源语言")
    missing_packs = []
    root = ctx.config.game_path.resolve()
    for relative in resource_wads:
        path = (root / relative).resolve()
        if not path.is_relative_to(root / FINAL_DIR):
            raise SourcePreflightError("资源包来源路径不在游戏 FINAL 目录内")
        if not path.is_file():
            missing_packs.append(SourceEntity("resource_pack", relative, Path(relative).name, (relative,), (relative,)))
    if missing_packs:
        raise SourcePreflightError("资源包缺少必需文件，请补齐后刷新", tuple(missing_packs))
    if resource_only:
        return
    try:
        inventory = scan_inventory(root, language=ctx.game_region)
    except SourceDiscoveryError as exc:
        raise SourcePreflightError(str(exc)) from exc
    language = inventory.get_language(ctx.game_region)
    if language is None or language.diagnostic:
        raise SourcePreflightError(
            f"无法使用语言 {ctx.game_region}：" + (language.diagnostic if language else "没有本地资源声明")
        )
    champions = {str(key).casefold() for key in champion_ids} if champion_ids is not None else None
    maps = {str(key) for key in map_ids} if map_ids is not None else None
    if maps:
        maps.add("0")
    explicit = champions is not None or maps is not None
    selected = []
    matched_champions = set()
    matched_maps = set()
    for entity in language.entities:
        if entity.kind == "champion":
            keys = {entity.key, entity.alias.casefold()}
            matches = keys & champions if champions is not None else set()
            requested = (
                bool(matches)
                if explicit
                else include_champions
                and not should_hide_champion_by_default({"id": entity.key, "alias": entity.alias})
            )
            matched_champions.update(matches)
        else:
            requested = entity.key in (maps or ()) if explicit else include_maps
            if requested:
                matched_maps.add(entity.key)
        if requested:
            selected.append(entity)
    unknown = (champions or set()) - matched_champions | (maps or set()) - matched_maps
    if unknown:
        raise SourcePreflightError("未知实体选择：" + ", ".join(sorted(unknown)))
    missing = tuple(entity for entity in selected if not entity.available)
    if missing:
        names = ", ".join(f"{item.kind}:{item.key}" for item in missing)
        raise SourcePreflightError(f"所选实体缺少必需文件（{names}），请补齐后刷新", missing)
