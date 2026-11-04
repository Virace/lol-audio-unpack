"""按固定版本和语言读取本地产物，不初始化游戏源或执行任务。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from loguru import logger

from lol_audio_unpack.app.resource_pack import parse_resource_pack_key, resource_pack_path_component
from lol_audio_unpack.manager.errors import SharedDataCorruptError
from lol_audio_unpack.manager.files import read_data
from lol_audio_unpack.runtime.library.types import normalize_region, resolve_path

_VERSION = re.compile(r"\d+\.\d+")
_OUTPUT_DIRS = {"audio": "audios", "wav": "wavs", "hash": "hashes", "report": "reports", "manifest": "manifest"}


@dataclass(frozen=True, slots=True)
class LibraryVersion:
    """标识输出库中的一份补丁版本和语言。"""

    version: str
    region: str

    def __post_init__(self) -> None:
        if not _VERSION.fullmatch(self.version):
            raise ValueError(f"无效的本地补丁版本: {self.version}")
        object.__setattr__(self, "region", normalize_region(self.region))


@dataclass(frozen=True, slots=True)
class LibraryConfig:
    """提供已有产物定位所需的只读配置，不含任务选项。"""

    output_path: Path
    game_region: str
    dev_mode: bool = False
    group_by_type: bool = False


@dataclass(frozen=True, slots=True)
class LibraryContext:
    """只读产物路径视图；不能代替源任务的 AppContext。"""

    config: LibraryConfig

    @property
    def game_region(self) -> str:
        """返回当前浏览语言。"""
        return self.config.game_region

    def version_path(self, kind: str, version: str) -> Path:
        """按已有布局定位指定产物，不创建目录。"""
        selection = LibraryVersion(version, self.game_region)
        return resolve_path(self.config.output_path / _OUTPUT_DIRS[kind], f"{selection.version}/{selection.region}")


def discover_versions(output_root: Path) -> tuple[LibraryVersion, ...]:
    """发现实际含有音频的版本与语言，不把初始化资料当作试听内容。"""
    versions: set[LibraryVersion] = set()
    root = Path(output_root) / "audios"
    if not root.is_dir():
        return ()
    for folder in root.iterdir():
        if not folder.is_dir() or not _VERSION.fullmatch(folder.name):
            continue
        for language in folder.iterdir():
            if not language.is_dir():
                continue
            try:
                selection = LibraryVersion(folder.name, language.name)
            except ValueError:
                logger.debug("忽略非语言目录: {}", language)
                continue
            # 只列出规范目录；旧 default 目录须先沿用既有迁移流程。
            if selection.region == language.name and any(
                next(_iter_audio_roots(language, kind), None) is not None
                for kind in ("champions", "maps", "resource_packs")
            ):
                versions.add(selection)
    return tuple(
        sorted(
            versions, key=lambda item: (tuple(int(part) for part in item.version.split(".")), item.region), reverse=True
        )
    )


def _iter_audio_roots(root: Path, kind: str):
    """按实体寻找首个音频即停止，排除空目录而不枚举完整音频清单。"""
    for base in (root, *(root / name for name in ("VO", "SFX", "MUSIC"))):
        folder = base / kind
        if not folder.is_dir():
            continue
        for path in folder.iterdir():
            if not path.is_dir():
                continue
            key = unquote(path.name.split("·", 1)[0])
            if kind == "resource_packs":
                try:
                    parse_resource_pack_key(key)
                except ValueError:
                    logger.warning("忽略无法识别的音频资源包目录: {}", path.name)
                    continue
            elif not key.isdecimal():
                continue
            if any(media.suffix.casefold() in {".wem", ".ogg"} and media.is_file() for media in path.rglob("*")):
                yield key, path


class LibraryReader:
    """读取所选版本的实体元数据，缺失时从同版产物恢复稳定 ID。"""

    def __init__(self, output_root: Path, selection: LibraryVersion):
        """载入固定版本目录，保留没有名称元数据的实体。"""
        self.root = Path(output_root)
        self.version = selection.version
        self.region = selection.region
        self.manifest = resolve_path(self.root / "manifest", f"{self.version}/{self.region}")
        self._audio_roots: dict[tuple[str, str], tuple[Path, ...]] = {}
        self.data = self._load_metadata()
        for kind in ("champions", "maps"):
            catalog = self.data.get(kind, {})
            self.data[kind] = {
                key: catalog.get(key, {"id": int(key)}) for key in sorted(self._discover_ids(kind), key=int)
            }
        self._packs: dict[str, dict] | None = None

    def _load_metadata(self) -> dict:
        path = self.manifest / "data.msgpack"
        if not path.is_file():
            logger.warning("本地版本 {} / {} 缺少实体资料，将按已有产物 ID 浏览", self.version, self.region)
            return {}
        try:
            data = read_data(path, log_errors=False)
        except SharedDataCorruptError as exc:
            if isinstance(exc.__cause__, OSError):
                raise exc.__cause__ from exc
            logger.warning("本地版本 {} / {} 实体资料损坏，将按已有产物 ID 浏览: {}", self.version, self.region, exc)
            return {}
        metadata = data.get("metadata")
        version = metadata.get("gameVersion") if isinstance(metadata, dict) else None
        if version and version != self.version:
            logger.warning("本地目录 {} 的实体资料标记为版本 {}，仅按已有产物 ID 浏览", self.version, version)
            return {}
        for kind in ("champions", "maps"):
            entries = data.get(kind)
            if not isinstance(entries, dict):
                logger.warning("本地版本 {} / {} 缺少 {} 目录，将从产物补充 ID", self.version, self.region, kind)
                data[kind] = {}
                continue
            data[kind] = {
                str(key): {**value, "id": int(key)}
                for key, value in entries.items()
                if str(key).isdecimal() and isinstance(value, dict)
            }
        return data

    def _discover_ids(self, kind: str) -> set[str]:
        """仅以实体已有音频确定可见性，资料与映射不单独产生目录项。"""
        ids: set[str] = set()
        audio_root = self.root / "audios" / self.version / self.region
        for key, path in _iter_audio_roots(audio_root, kind):
            ids.add(key)
            roots = self._audio_roots.get((kind, key), ())
            self._audio_roots[kind, key] = (*roots, path)
        return ids

    def get_audio_roots(self, kind: str, key: str) -> tuple[Path, ...]:
        """返回已确认含音频的实体根，供缺少成功清单时按需浏览。"""
        return self._audio_roots.get((kind, key), ())

    def get_champion(self, champion_id: int) -> dict:
        """返回所选版本的英雄资料，缺失时返回空字典。"""
        return self.data.get("champions", {}).get(str(champion_id), {})

    def get_champions(self) -> list[dict]:
        """返回所选版本中可确认身份的英雄目录。"""
        return list(self.data.get("champions", {}).values())

    def get_map(self, map_id: int) -> dict:
        """返回所选版本的地图资料，缺失时返回空字典。"""
        return self.data.get("maps", {}).get(str(map_id), {})

    def get_maps(self) -> list[dict]:
        """返回所选版本中可确认身份的地图目录。"""
        return list(self.data.get("maps", {}).values())

    def list_resource_pack_banks(self) -> list[dict]:
        """读取已有资源包身份，不要求原 WAD 或资源 binding 仍然存在。"""
        if self._packs is not None:
            return list(self._packs.values())
        self._packs = {}
        ids = self._discover_ids("resource_packs")
        folder = self.manifest / "banks" / "resource_packs"
        if folder.is_dir():
            for path in folder.glob("*.msgpack"):
                try:
                    payload = read_data(path, log_errors=False)
                except SharedDataCorruptError as exc:
                    if isinstance(exc.__cause__, OSError):
                        raise exc.__cause__ from exc
                    logger.warning("忽略无法读取的本地资源包资料 {}: {}", path.name, exc)
                    continue
                metadata = payload.get("resourcePack") if isinstance(payload, dict) else None
                key = metadata.get("key") if isinstance(metadata, dict) else None
                if isinstance(key, str):
                    try:
                        parse_resource_pack_key(key)
                    except ValueError:
                        logger.warning("本地资源包身份无效，已跳过: {}", path.name)
                        continue
                    if key in ids:
                        self._packs[key] = payload
                else:
                    logger.warning("本地资源包缺少有效身份，已跳过: {}", path.name)
        for key in sorted(ids):
            parsed = parse_resource_pack_key(key)
            self._packs.setdefault(
                key,
                {"resourcePack": {"key": key, "wad": parsed.wad + ".wad.client", "namespace": parsed.namespace}},
            )
        return list(self._packs.values())

    def get_resource_pack_banks(self, key: str, *, require_bindings: bool = False) -> dict | None:
        """返回资源包展示资料；浏览入口不提供任务所需的 binding 校验。"""
        if require_bindings:
            raise ValueError("本地浏览读取器不能用于源任务")
        resource_pack_path_component(key)
        self.list_resource_pack_banks()
        return self._packs.get(key)
