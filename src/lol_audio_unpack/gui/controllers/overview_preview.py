"""实体总览预览加载控制器。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from lol_audio_unpack.app.artifacts import AudioRef
from lol_audio_unpack.gui.service.data_loader import EntityDataLoader

EVENT_PREVIEW_MODE = "audio"
ALL_AUDIO_PREVIEW_MODE = "all_audio"
RAW_PREVIEW_MODE = "raw"


@dataclass(slots=True, frozen=True)
class OverviewPreviewLoadResult:
    """描述一次总览预览加载的结果。"""

    entity_id: str
    mapping_path: Path | None
    mapping_data: dict[str, Any] | None
    preview_content: str
    available_audio_ids: set[str]
    group_label_map: dict[str, str]
    audio_refs: tuple[AudioRef, ...] = ()
    event_audio_refs: tuple[AudioRef, ...] = ()
    audio_refs_loaded: bool = True
    audio_roots: tuple[Path, ...] = ()
    default_preview_mode: str = ALL_AUDIO_PREVIEW_MODE
    mapping_notice: str | None = None
    placeholder_message: str | None = None
    resolve_ref: Callable[[str], AudioRef | None] | None = None
    version: str | None = None


@dataclass(slots=True, frozen=True)
class AudioPreviewToggleResult:
    """描述一次试听切换后的目标状态。"""

    audio_id: str | None
    audio_path: Path | None
    progress: float
    is_playing: bool
    is_paused: bool
    warning_message: str | None = None


class OverviewPreviewController:
    """负责把实体选择转换为可渲染的预览数据。"""

    def load_preview(
        self,
        *,
        entity_type: str,
        entity_id: str,
        entity_name: str,
        loader: EntityDataLoader | None,
    ) -> OverviewPreviewLoadResult:
        """加载指定实体的预览结果。"""
        if loader is None:
            return OverviewPreviewLoadResult(
                entity_id=entity_id,
                mapping_path=None,
                mapping_data=None,
                preview_content="",
                available_audio_ids=set(),
                group_label_map={},
                placeholder_message="当前配置尚未完成初始化，暂时无法读取预览内容。",
            )

        try:
            mapping_path, mapping_data, preview_content = loader.load_mapping_preview(entity_type, entity_id)
            event_audio_refs = ()
            audio_roots = loader.load_audio_roots(entity_type, entity_id)
        except (OSError, ValueError) as exc:
            logger.opt(exception=exc).warning("实体预览读取失败：{} {}", entity_type, entity_id)
            return OverviewPreviewLoadResult(
                entity_id=entity_id,
                mapping_path=None,
                mapping_data=None,
                preview_content="",
                available_audio_ids=set(),
                group_label_map={},
                placeholder_message=f"无法读取实体音频信息：{exc}。请检查输出目录或重新准备实体数据。",
            )
        if mapping_path is None and not event_audio_refs and not audio_roots:
            return OverviewPreviewLoadResult(
                entity_id=entity_id,
                mapping_path=None,
                mapping_data=None,
                preview_content="",
                available_audio_ids=set(),
                group_label_map={},
                placeholder_message="尚未解包",
            )
        available_audio_ids = set()
        resolve_ref = None
        if mapping_data is not None:
            resolve_ref = loader.build_audio_resolver(entity_type, entity_id, roots=audio_roots)

        group_label_map = self._build_preview_group_label_map(
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            mapping_data=mapping_data,
            loader=loader,
        )
        return OverviewPreviewLoadResult(
            entity_id=entity_id,
            mapping_path=mapping_path,
            mapping_data=mapping_data,
            preview_content=preview_content or "尚未生成事件映射。",
            available_audio_ids=available_audio_ids,
            group_label_map=group_label_map,
            event_audio_refs=event_audio_refs,
            resolve_ref=resolve_ref,
            audio_refs_loaded=False,
            audio_roots=audio_roots,
            version=loader.data_reader.version,
            default_preview_mode=EVENT_PREVIEW_MODE if mapping_path is not None else ALL_AUDIO_PREVIEW_MODE,
            mapping_notice=(
                str(mapping_data.get("previewNotice") or "") or None
                if mapping_path is not None and isinstance(mapping_data, dict)
                else f"{entity_name} 尚未生成事件映射。"
            ),
        )

    def _build_preview_group_label_map(
        self,
        *,
        entity_type: str,
        entity_id: str,
        entity_name: str,
        mapping_data: dict[str, Any] | None,
        loader: EntityDataLoader,
    ) -> dict[str, str]:
        """为试听树构造首层分组展示文案映射。"""
        if entity_type == "resource_packs":
            return {entity_id: entity_name}
        if entity_type != "champions":
            return {}
        if not isinstance(mapping_data, dict) or not isinstance(mapping_data.get("skins"), dict):
            return {}

        try:
            champion_id = int(entity_id)
        except (TypeError, ValueError):
            return {}

        champion = loader.data_reader.get_champion(champion_id)
        if not isinstance(champion, dict):
            return {}

        label_map: dict[str, str] = {}
        for skin in champion.get("skins", []):
            if not isinstance(skin, dict):
                continue

            skin_id = str(skin.get("id") or "").strip()
            if not skin_id:
                continue

            skin_name = self._resolve_localized_name(skin, names_key="skinNames", region=loader.ctx.game_region)
            if skin_name:
                label_map[skin_id] = skin_name

            for chroma in skin.get("chromas", []):
                if not isinstance(chroma, dict):
                    continue
                chroma_id = str(chroma.get("id") or "").strip()
                if not chroma_id:
                    continue
                chroma_name = self._resolve_localized_name(
                    chroma, names_key="chromaNames", region=loader.ctx.game_region
                )
                if chroma_name:
                    label_map[chroma_id] = chroma_name

        return label_map

    def resolve_audio_preview_toggle(
        self,
        *,
        requested_audio: AudioRef,
        current_audio_path: Path | None,
    ) -> AudioPreviewToggleResult | None:
        """根据精确 WEM 引用解析下一步试听请求。

        Args:
            requested_audio: 用户实际点击的路径级 WEM 引用。
            current_audio_path: 当前播放器正在管理的精确 WEM 路径。

        Returns:
            新的播放或停止状态。
        """
        if requested_audio.path == current_audio_path:
            return AudioPreviewToggleResult(
                audio_id=None,
                audio_path=None,
                progress=0.0,
                is_playing=False,
                is_paused=False,
            )

        try:
            path = requested_audio.path.resolve()
            if requested_audio.root is not None and not path.is_relative_to(requested_audio.root.resolve()):
                raise ValueError("音频路径越出当前实体目录")
            if not path.is_file():
                raise ValueError("音频文件不存在，请重新解包")
        except (OSError, ValueError) as exc:
            return AudioPreviewToggleResult(None, None, 0.0, False, False, str(exc))
        return AudioPreviewToggleResult(
            audio_id=requested_audio.wem_id,
            audio_path=requested_audio.path,
            progress=0.0,
            is_playing=False,
            is_paused=True,
        )

    @staticmethod
    def _resolve_localized_name(payload: dict[str, Any], *, names_key: str, region: str) -> str | None:
        """从皮肤或炫彩结构中提取本地化展示名。"""
        names = payload.get(names_key)
        if isinstance(names, dict):
            localized_name = str(names.get(region) or "").strip()
            if localized_name:
                return localized_name

            for key in ("default", "en_US"):
                fallback_name = str(names.get(key) or "").strip()
                if fallback_name:
                    return fallback_name

        for key in ("name", "displayName"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value

        return None
