"""总览专用本地版本加载器，复用现有目录展示和预览协议。"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path

from loguru import logger

from lol_audio_unpack.app.artifacts import AudioIndexProgress, AudioRef
from lol_audio_unpack.app.local_library import LibraryConfig, LibraryContext, LibraryReader, LibraryVersion
from lol_audio_unpack.app.outputs import output_path, split_audio
from lol_audio_unpack.app.special_content import build_special_content_item
from lol_audio_unpack.gui.service.data_loader import EntityDataLoader, GuiEntityType, _mapping_audio_paths
from lol_audio_unpack.model import AudioEntityData
from lol_audio_unpack.runtime.library.types import relative_path
from lol_audio_unpack.utils.common import sanitize_filename


class LibraryDataLoader(EntityDataLoader):
    """只从所选本地产物读取总览，不复用源任务的 reader 或缓存。"""

    def __init__(
        self,
        output_root: Path,
        selection: LibraryVersion,
    ):
        """建立独立浏览快照，不访问游戏客户端或校验源任务引用。"""
        self.ctx = LibraryContext(
            LibraryConfig(
                output_path=Path(output_root),
                game_region=selection.region,
            )
        )
        self.data_reader = LibraryReader(output_root, selection)
        self._mapping_cache = OrderedDict()
        self._source_entities = {}

    def _build_entity_data(self, entity_type: GuiEntityType, entity_id: str) -> AudioEntityData:
        """只投影实体身份和展示名，已有媒体不要求 WAD 或 v2 binding。"""
        if entity_type == "resource_packs":
            payload = self.data_reader.get_resource_pack_banks(entity_id) or {}
            metadata = payload.get("resourcePack", {})
            name = metadata.get("namespace") or entity_id
            alias = metadata.get("namespace") or ""
            title = None
        else:
            getter = self.data_reader.get_champion if entity_type == "champions" else self.data_reader.get_map
            metadata = getter(int(entity_id))
            names = metadata.get("names")
            names = names if isinstance(names, dict) else {}
            alias = metadata.get("alias") if entity_type == "champions" else metadata.get("mapStringId")
            name = names.get(self.ctx.game_region) or names.get("default") or entity_id
            titles = metadata.get("titles")
            titles = titles if isinstance(titles, dict) else {}
            title = titles.get(self.ctx.game_region) or titles.get("default")
        return AudioEntityData(
            entity_id=entity_id,
            entity_name=sanitize_filename(str(name)),
            entity_alias=sanitize_filename(str(alias or "").lower()),
            entity_title=sanitize_filename(str(title)) if title else None,
            entity_type={"champions": "champion", "maps": "map", "resource_packs": "resource_pack"}[entity_type],
            sub_entities={},
            wad_root="",
        )

    def _load_preview_entity(
        self, entity_type: GuiEntityType, entity_id: str, *, allow_unprepared: bool = False
    ) -> AudioEntityData:
        return self._build_entity_data(entity_type, entity_id)

    def _build_row_from_entity_data(
        self, entity_type: GuiEntityType, entity_data: AudioEntityData, version: str
    ) -> dict:
        row = super()._build_row_from_entity_data(entity_type, entity_data, version)
        if self.data_reader.get_audio_roots(entity_type, str(entity_data.entity_id)):
            row["audio"] = "已存在"
        return row

    def _build_special_row(self, champion: dict, version: str, *, display_name: str) -> dict | None:
        """保留现有特殊英雄分组，但已有媒体状态独立于资源准备状态。"""
        item = build_special_content_item(champion, display_name=display_name)
        if item is None:
            return None
        entity = self._build_entity_data("champions", str(item.champion_id))
        row = self._build_row_from_entity_data("champions", entity, version)
        row.update(
            key=item.key,
            name=item.display_name,
            display_name=item.standalone_name,
            alias=item.internal_alias,
            base_alias=item.base_alias,
            mode_key=item.profile.mode_key,
            mode_display_name=item.profile.display_name,
            mode_english_name=item.profile.english_name,
            search_text=item.search_text,
            tooltip=f"{item.standalone_name}\nID: {item.champion_id}\n资源键: {item.key}",
        )
        return row

    def _build_resource_pack_row(
        self, payload: dict, version: str, *, ordinary_names: Mapping[str, str] | None = None
    ) -> dict | None:
        """已有资源包仅用于试听，不读取原 WAD 或显示任务选择提示。"""
        row = super()._build_resource_pack_row(payload, version, ordinary_names=ordinary_names, allow_tasks=False)
        if row is not None and self.data_reader.get_audio_roots("resource_packs", row["id"]):
            row["tooltip"] = row["tooltip"].replace(f"音频: {row['audio']}", "音频: 已存在")
            row["audio"] = "已存在"
        return row

    def load_audio_roots(
        self, entity_type: GuiEntityType, entity_id: str, *, audio_refs: tuple[AudioRef, ...] = ()
    ) -> tuple[Path, ...]:
        """优先使用成功清单中的实体根，缺少清单时沿用固定音频目录。"""
        return super().load_audio_roots(
            entity_type, entity_id, audio_refs=audio_refs
        ) or self.data_reader.get_audio_roots(entity_type, entity_id)

    def load_audio_refs(
        self,
        entity_type: GuiEntityType,
        entity_id: str,
        *,
        progress: Callable[[AudioIndexProgress], None] | None = None,
    ) -> tuple[AudioRef, ...]:
        """读取当前实体清单；清单缺失时仅按需枚举这个实体的已有媒体。"""
        entity = self._build_entity_data(entity_type, entity_id)
        path = output_path(
            self.ctx.config.output_path, self.data_reader.version, self.ctx.game_region, entity.entity_type, entity_id
        )
        if path.is_file():
            return super().load_audio_refs(entity_type, entity_id, progress=progress)
        roots = self.data_reader.get_audio_roots(entity_type, entity_id)
        if roots:
            logger.warning("本地实体 {} {} 缺少音频清单，按需读取已有音频目录", entity_type, entity_id)
        refs = []
        for root in roots:
            for path in root.rglob("*"):
                if path.suffix.casefold() not in {".wem", ".ogg"} or not path.is_file():
                    continue
                _, logical, audio_type, sub_id = split_audio(path)
                refs.append(
                    AudioRef(logical, path, path.stem, audio_type, sub_id, root=root, region=self.ctx.game_region)
                )
        result = tuple(sorted(refs, key=lambda ref: ref.relative_path))
        if progress is not None:
            progress(AudioIndexProgress(len(result), len(result)))
        return result

    def load_event_audio_refs(
        self, entity_type: GuiEntityType, entity_id: str, mapping_data: dict | None
    ) -> tuple[AudioRef, ...]:
        """只解析所选版本映射明确引用的路径，不枚举整个实体。"""
        resolve = self.build_audio_resolver(entity_type, entity_id, roots=self.load_audio_roots(entity_type, entity_id))
        return tuple(ref for path in _mapping_audio_paths(mapping_data) if (ref := resolve(path)) is not None)

    def build_audio_resolver(
        self, entity_type: GuiEntityType, entity_id: str, *, roots: tuple[Path, ...]
    ) -> Callable[[str], AudioRef | None]:
        """固定本次预览的目录快照，不使用元数据名称猜测音频路径。"""
        return lambda path: self._resolve_audio_ref(path, roots=roots)

    def _resolve_audio_ref(self, path: str, *, roots: tuple[Path, ...]) -> AudioRef | None:
        """按清单记录的真实根定位事件音频，避免缺失或改变名称造成路径猜测。"""
        try:
            logical = relative_path(path)
        except ValueError:
            return None
        parts = Path(logical).parts
        if len(parts) < 2 or Path(logical).suffix.casefold() not in {".wem", ".ogg"}:  # noqa: PLR2004
            return None
        refs = []
        for root in roots:
            grouped = root.parent.parent.name in {"VO", "SFX", "MUSIC"}
            if grouped and parts[0] != root.parent.parent.name:
                continue
            physical = root.joinpath(*(parts[1:] if grouped else parts))
            _, _, audio_type, sub_id = split_audio(physical)
            refs.append(
                AudioRef(logical, physical, physical.stem, audio_type, sub_id, root=root, region=self.ctx.game_region)
            )
        if not refs:
            return None
        if len(refs) == 1:
            return refs[0]
        # 布局或实体目录名变化后可能保留多个根；仅在多根消歧时检查文件，全部缺失仍保留映射引用。
        return next((ref for ref in refs if ref.path.is_file()), refs[0])
