"""读取完整 LCU 名单并生成仅检查必需文件存在性的来源快照。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from league_tools.formats import WAD
from loguru import logger

from lol_audio_unpack.utils.game_layout import (
    FINAL_DIR as FINAL_ROOT,
)
from lol_audio_unpack.utils.game_layout import GAME_DIR_CHAMPIONS, LCU_DESCRIPTION, LCU_DIRS, MAPS_DIR, resolve_layout
from lol_audio_unpack.utils.game_resources import RCP_GLOBAL_PREFIX, WAD_SUFFIX

# 保留嵌套布局的既有访问名；当前安装位置由 resolve_layout 按游戏根独立选择。
LCU_ROOT = LCU_DIRS[0]
LCU_PREFIX = f"{RCP_GLOBAL_PREFIX}/default/v1/"
JADE_ID_OFFSET = 60000
_LOCALE = re.compile(r"\.([a-z]{2}_[a-z]{2})\.wad\.client$", re.IGNORECASE)


class SourceDiscoveryError(ValueError):
    """无法从本地声明与元数据建立可靠的完整资源名单。"""


@dataclass(frozen=True)
class SourceEntity:
    """一个逻辑实体的必需文件与缺失路径，路径均相对游戏根。"""

    kind: str
    key: str
    name: str
    required: tuple[str, ...]
    missing: tuple[str, ...]
    alias: str = ""

    @property
    def available(self) -> bool:
        """返回所列必需文件是否全部存在。"""
        return not self.missing


@dataclass(frozen=True)
class MissingResource:
    """缺失文件的实体归属、所选语言和资源用途。"""

    kind: str
    key: str
    name: str
    locale: str
    path: str
    purpose: str


@dataclass(frozen=True)
class SourceLanguage:
    """一个候选语言的完整实体状态与声明问题。"""

    locale: str
    entities: tuple[SourceEntity, ...]
    diagnostic: str = ""

    @property
    def available_count(self) -> int:
        """返回文件条件满足的实体数。"""
        return 0 if self.diagnostic else sum(entity.available for entity in self.entities)

    @property
    def status(self) -> str:
        """返回 complete、partial 或 unavailable。"""
        if not self.available_count:
            return "unavailable"
        return "complete" if self.available_count == len(self.entities) else "partial"

    @property
    def missing(self) -> tuple[str, ...]:
        """按物理路径去重缺失清单，保留逐实体影响关系。"""
        return tuple(sorted({path for entity in self.entities for path in entity.missing}))

    @property
    def missing_records(self) -> tuple[MissingResource, ...]:
        """展开每个文件的实体影响，物理去重清单仍由 missing 提供。"""
        records = []
        for entity in self.entities:
            for path in entity.missing:
                if any(path.startswith(root.as_posix() + "/") for root in LCU_DIRS):
                    purpose = "LCU 元数据与大厅资源"
                elif _LOCALE.search(path):
                    purpose = "当前语言语音"
                else:
                    purpose = "基础游戏资源"
                records.append(MissingResource(entity.kind, entity.key, entity.name, self.locale, path, purpose))
        return tuple(records)


@dataclass(frozen=True)
class SourceInventory:
    """绑定游戏目录和扫描 generation 的只读文件状态。"""

    game_path: Path
    generation: int
    languages: tuple[SourceLanguage, ...]

    def get_language(self, locale: str) -> SourceLanguage | None:
        """按界面语言代码取得快照，default 仅作为英语兼容输入。"""
        key = "en_us" if locale.casefold() == "default" else locale.casefold()
        return next((item for item in self.languages if item.locale.casefold() == key), None)


def _bundle_paths(names: object, lcu_root: Path) -> tuple[str, ...]:
    """拒绝来源声明中的越界路径，转换为游戏根相对身份。"""
    if not isinstance(names, list) or not names:
        raise SourceDiscoveryError("LCU bundle 声明缺失或不是非空列表")
    result = []
    for name in names:
        if not isinstance(name, str) or not name or Path(name).name != name or name in (".", ".."):
            raise SourceDiscoveryError("LCU bundle 声明含无效文件名")
        result.append((lcu_root / name).as_posix())
    return tuple(dict.fromkeys(result))


def read_catalog(game_path: Path) -> tuple[list[dict], list[dict], dict]:
    """从默认 LCU 资源读取未按磁盘存在性过滤的英雄与地图名单。

    只读取两个已知元数据条目；不读取 GAME WAD 的 TOC 或媒体内容。

    Raises:
        SourceDiscoveryError: 声明、名单缺失或不能解析。
    """
    try:
        lcu_root = resolve_layout(game_path).lcu_relative
        description = json.loads((game_path / lcu_root / LCU_DESCRIPTION).read_text(encoding="utf-8-sig"))
        metadata = description["riotMeta"]
        bundles = _bundle_paths(metadata.get("globalAssetBundles"), lcu_root)
        paths = [LCU_PREFIX + "champion-summary.json", LCU_PREFIX + "maps.json"]
        found: dict[str, list] = {}
        for relative in bundles:
            path = game_path / relative
            if not path.is_file():
                logger.warning("目录发现跳过缺失的 LCU bundle，仍检查其他声明包: {}", relative)
                continue
            wad = WAD(path)
            for logical, payload in zip(paths, wad.extract(paths, raw=True), strict=True):
                if payload is not None:
                    data = json.loads(payload)
                    if not isinstance(data, list) or not data:
                        raise SourceDiscoveryError(f"LCU 名单不是非空列表: {logical}")
                    found[logical] = data
        if any(path not in found for path in paths):
            raise SourceDiscoveryError("无法读取完整 LCU 英雄/地图名单，请检查默认资源包")
        champions, maps = (found[path] for path in paths)
        for item in champions:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), int)
                or not isinstance(item.get("alias"), str)
                or not item["alias"]
            ):
                raise SourceDiscoveryError("LCU 英雄名单缺少有效 ID/alias")
        for item in maps:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                raise SourceDiscoveryError("LCU 地图名单缺少有效 ID")
        return [item for item in champions if item["id"] != -1], maps, metadata
    except SourceDiscoveryError:
        raise
    except Exception as exc:
        raise SourceDiscoveryError(f"LCU 目录发现失败: {exc}") from exc


def _wad_paths(folder: str, alias: str, locale: str) -> tuple[str, str]:
    """为已确认的物理身份列出基础与当前语言 WAD。"""
    if Path(alias).name != alias or alias in (".", ".."):
        raise SourceDiscoveryError("实体 alias 含无效路径")
    base = FINAL_ROOT / folder / alias
    return f"{base.as_posix()}{WAD_SUFFIX}", f"{base.as_posix()}.{locale}{WAD_SUFFIX}"


def scan_inventory(game_path: Path, *, generation: int = 0, language: str | None = None) -> SourceInventory:
    """发现候选语言并逐文件检查完整名单，返回不可变快照。

    Args:
        game_path: 已通过基础结构检查的本地游戏目录。
        generation: 调用方用来丢弃迟到扫描结果的身份。
        language: 任务复核只检查本次语言；留空时执行完整候选发现。

    Raises:
        SourceDiscoveryError: 无法可靠取得目录或明确的物理文件规则。
    """
    root = game_path.resolve()
    logger.info("开始本地资源文件扫描，generation={}", generation)
    try:
        lcu_root = resolve_layout(root).lcu_relative
        champions, maps, metadata = read_catalog(root)
        common = _bundle_paths(metadata.get("globalAssetBundles"), lcu_root)
        declarations = metadata.get("perLocaleAssetBundles", {})
        if not isinstance(declarations, dict):
            raise SourceDiscoveryError("LCU 语言声明不是字典")
        declarations = {str(key).casefold(): value for key, value in declarations.items()}
        # 声明不是安装证据：至少一个对应的 game-data 包实际存在才进入语言列表。
        locales = {
            locale
            for locale, names in declarations.items()
            if any((root / path).is_file() for path in _bundle_paths(names, lcu_root))
        }
        if any((root / path).is_file() for path in common):
            locales.add("en_us")
        if language is not None:
            locales &= {"en_us" if language.casefold() == "default" else language.casefold()}
        aliases = {item["id"]: item["alias"] for item in champions}
        languages = []
        for raw_locale in sorted(locales):
            if not re.fullmatch(r"[a-zA-Z]{2,3}_[a-zA-Z0-9]{2,3}", raw_locale):
                raise SourceDiscoveryError(f"LCU 语言声明格式无效: {raw_locale}")
            language_code, territory = raw_locale.split("_")
            locale = f"{language_code.lower()}_{territory.upper()}"
            diagnostic = ""
            try:
                bundles = (
                    common
                    if locale == "en_US"
                    else common + _bundle_paths(declarations.get(locale.casefold()), lcu_root)
                )
            except SourceDiscoveryError as exc:
                bundles = common
                diagnostic = str(exc)
            entities = []
            for item in champions:
                alias = item["alias"]
                if alias.casefold().startswith(("ruby_", "strawberry_")):
                    # 旧结构化模式只声明独立基础 WAD，没有普通英雄的逐语言文件合同。
                    # 不追加猜测的同名 VO WAD；实际媒体仍经精确 binding 在处理阶段解析。
                    required = tuple(dict.fromkeys((*bundles, _wad_paths(GAME_DIR_CHAMPIONS, alias, locale)[0])))
                    missing = tuple(path for path in required if not (root / path).is_file())
                    entities.append(
                        SourceEntity("champion", str(item["id"]), item.get("name", alias), required, missing, alias)
                    )
                    continue
                if alias.casefold().startswith("jade_"):
                    # Jade 与普通英雄共享物理 WAD；稳定 ID 同时覆盖 Wukong/MonkeyKing 的 alias 差异。
                    alias = aliases.get(item["id"] - JADE_ID_OFFSET)
                    if not alias:
                        raise SourceDiscoveryError(f"无法确认 Jade {item['id']} 的共享英雄身份")
                required = tuple(dict.fromkeys((*bundles, *_wad_paths(GAME_DIR_CHAMPIONS, alias, locale))))
                missing = tuple(path for path in required if not (root / path).is_file())
                entities.append(
                    SourceEntity("champion", str(item["id"]), item.get("name", alias), required, missing, item["alias"])
                )
            maps_folder = MAPS_DIR.relative_to(FINAL_ROOT).as_posix()
            common_wads = _wad_paths(maps_folder, "Common", locale)
            for item in maps:
                alias = "Common" if item["id"] == 0 else f"Map{item['id']}"
                required = tuple(dict.fromkeys((*bundles, *common_wads, *_wad_paths(maps_folder, alias, locale))))
                missing = tuple(path for path in required if not (root / path).is_file())
                entities.append(SourceEntity("map", str(item["id"]), item.get("name", alias), required, missing))
            languages.append(SourceLanguage(locale, tuple(entities), diagnostic))
        result = SourceInventory(root, generation, tuple(languages))
    except Exception:
        logger.opt(exception=True).error("本地资源目录发现失败，generation={}", generation)
        raise
    logger.info("本地资源文件扫描完成：{} 个候选语言，generation={}", len(result.languages), generation)
    return result
