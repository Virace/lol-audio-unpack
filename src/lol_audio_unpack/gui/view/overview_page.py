"""实体总览页面，负责展示实体状态并预留右侧资源预览区。"""

from __future__ import annotations

import json
import os
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from loguru import logger
from PySide6.QtCore import (
    QItemSelectionModel,
    QModelIndex,
    QPoint,
    QSignalBlocker,
    Qt,
    QThread,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    CaptionLabel,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    MenuAnimationType,
    RoundMenu,
    SubtitleLabel,
    Theme,
    qconfig,
)

from lol_audio_unpack.app.artifacts import AudioIndexProgress, AudioRef
from lol_audio_unpack.app.audio_export import AudioExportRequest, ExportTarget
from lol_audio_unpack.app.audio_scope import AudioScope
from lol_audio_unpack.app.facade import LolAudioUnpackApp
from lol_audio_unpack.app.resource_pack import ResourcePackSelectionError, ResourcePackWadRef
from lol_audio_unpack.app.types import OperationOptions, WavOutputOptions
from lol_audio_unpack.gui.common import apply_smooth_scroll_enabled
from lol_audio_unpack.gui.common.page_style import apply_page_content_margins
from lol_audio_unpack.gui.common.styles import get_fluent_frame_stroke_pair
from lol_audio_unpack.gui.components.overview_entity_list import OVERVIEW_ROW_ROLE, OverviewEntityListView
from lol_audio_unpack.gui.components.preview_tree import (
    collect_tree_stats,
    extract_tree_groups,
    filter_preview_mapping_data,
)
from lol_audio_unpack.gui.controllers import (
    OverviewPreviewController,
    PreviewPlaybackController,
)
from lol_audio_unpack.gui.controllers.audio_export import AudioExportController
from lol_audio_unpack.gui.controllers.contracts import OverviewSelectionSyncRequest
from lol_audio_unpack.gui.controllers.entity_data_store import EntityDataStore
from lol_audio_unpack.gui.controllers.overview_preview import (
    ALL_AUDIO_PREVIEW_MODE,
    EVENT_PREVIEW_MODE,
    RAW_PREVIEW_MODE,
)
from lol_audio_unpack.gui.controllers.preview_playback import PreviewPlaybackState
from lol_audio_unpack.gui.service.data_loader import EntityDataLoader
from lol_audio_unpack.gui.service.preview_export import resolve_wav_path
from lol_audio_unpack.gui.shared_data import SharedDataPhase, SharedDataState
from lol_audio_unpack.gui.shared_data_view import describe_shared_data_state
from lol_audio_unpack.gui.theme import get_accent_text_color_pair
from lol_audio_unpack.gui.view.home.widgets import StatusLine
from lol_audio_unpack.gui.view.overview.audio_preview_panel import OverviewAudioPreviewPanel
from lol_audio_unpack.gui.view.overview.entity_list_panel import OverviewEntityListPanel
from lol_audio_unpack.gui.view.overview.preview_panel import (
    DEFAULT_PREVIEW_PLACEHOLDER_TEXT,
    OverviewPreviewPanel,
)
from lol_audio_unpack.gui.workers import TaskWorker
from lol_audio_unpack.manager.files import read_data
from lol_audio_unpack.utils.game_layout import FINAL_DIR

DEFAULT_PREVIEW_AUDIO_VOLUME_PERCENT = 10
DEFAULT_PREVIEW_AUDIO_OUTPUT_DEVICE_KEY = "default"


@dataclass(frozen=True, slots=True)
class _AudioRefsRequest:
    """标识一次可被实体切换作废的全部音频后台加载。"""

    token: int
    entity_type: str
    entity_id: str

    @property
    def key(self) -> tuple[str, str]:
        """返回当前上下文内可复用的实体缓存键。"""
        return self.entity_type, self.entity_id


def build_preview_path_text(resource_path: Path | None) -> str:
    """构造右侧预览区域顶部路径文本。

    Args:
        resource_path: 当前模式对应的资源路径。

    Returns:
        存在资源路径时返回完整路径，否则返回空字符串。
    """
    if resource_path is None:
        return ""
    return str(resource_path)


def create_preview_path_edit(parent: QWidget | None = None) -> LineEdit:
    """创建跟随 Fluent 主题的预览路径输入框。"""
    line_edit = LineEdit(parent)
    line_edit.setAccessibleName("预览资源路径")
    line_edit.setReadOnly(True)
    line_edit.setClearButtonEnabled(False)
    line_edit.setPlaceholderText(DEFAULT_PREVIEW_PLACEHOLDER_TEXT)
    line_edit.setMinimumWidth(0)
    line_edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
    return line_edit


class OverviewPage(QWidget):
    """实体总览页面。

    英雄与地图分别维护独立列表缓存，仅在数据刷新时重建；
    tab 切换与搜索只在现有列表项上切换或隐藏，不再 rebuild。
    """

    selection_sync_requested = Signal(object)
    audio_export_requested = Signal(object)
    background_work_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.setObjectName("OverviewPage")
        self.setStyleSheet("QWidget#OverviewPage{background: transparent}")
        self.gui_config = None
        self._app_context = None
        self._loader = None
        self._shared_data_state = SharedDataState(SharedDataPhase.BLOCKED, 0)
        self._entity_data_store = EntityDataStore(entity_types=("champions", "maps", "special"))
        self._preview_controller = OverviewPreviewController()
        self._selected_entity_ids: dict[str, set[str]] = {"champions": set(), "maps": set(), "special": set()}
        self._current_preview_ids: dict[str, str | None] = {"champions": None, "maps": None, "special": None}
        self._current_preview_key: tuple[str, str, str, str] | None = None
        self._current_preview_entity_type: str | None = None
        self._current_preview_entity_id: str | None = None
        self._current_mapping_path: Path | None = None
        self._current_audio_preview_audio_id: str | None = None
        self._current_audio_preview_path: Path | None = None
        self._current_audio_preview_progress = 0.0
        self._current_audio_preview_is_playing = False
        self._current_audio_preview_is_paused = False
        self._current_preview_mapping_data: dict[str, Any] | None = None
        self._current_preview_group_label_map: dict[str, str] = {}
        self._current_preview_entity_name = ""
        self._current_event_audio_refs: tuple[AudioRef, ...] = ()
        self._current_audio_refs: tuple[AudioRef, ...] = ()
        self._current_audio_roots: tuple[Path, ...] = ()
        self._audio_refs_loaded = False
        self._audio_list_ready = False
        self._audio_refs_error: str | None = None
        self._audio_refs_progress: AudioIndexProgress | None = None
        self._audio_refs_token = 0
        self._audio_refs_cache: OrderedDict[tuple[str, str], tuple[AudioRef, ...]] = OrderedDict()
        self._preview_loading = False
        self._pending_event_search: tuple[int, str] | None = None
        self._preview_placeholder: str | None = None
        self._preview_pool = QThreadPool(self)
        self._preview_pool.setMaxThreadCount(1)
        self._preview_workers = {}
        self._resolve_event_ref = None
        self._raw_loaded = False
        self._resource_stats = None
        self._audio_refs_pool = QThreadPool(self)
        self._audio_refs_pool.setMaxThreadCount(1)
        self._audio_refs_pool.setThreadPriority(QThread.Priority.LowPriority)
        self._audio_refs_worker: TaskWorker | None = None
        self._audio_refs_request: _AudioRefsRequest | None = None
        self._pending_audio_refs_request: _AudioRefsRequest | None = None
        self._selected_audio_refs: dict[str, AudioRef | None] = {
            EVENT_PREVIEW_MODE: None,
            ALL_AUDIO_PREVIEW_MODE: None,
        }
        self._current_mapping_notice: str | None = None
        self._audio_preview_placeholder = DEFAULT_PREVIEW_PLACEHOLDER_TEXT
        self._active_preview_mode = EVENT_PREVIEW_MODE
        self._event_preview_summary = self._audio_preview_placeholder
        self._all_audio_preview_summary = self._audio_preview_placeholder
        self._preview_search_keywords = {
            EVENT_PREVIEW_MODE: "",
            ALL_AUDIO_PREVIEW_MODE: "",
        }
        self._event_search_pending = False
        self._event_search_timer = QTimer(self)
        self._event_search_timer.setSingleShot(True)
        self._event_search_timer.setInterval(300)
        self._event_search_timer.timeout.connect(self._apply_event_search)
        self._preview_audio_volume_percent = DEFAULT_PREVIEW_AUDIO_VOLUME_PERCENT
        self._preview_audio_output_device_key = DEFAULT_PREVIEW_AUDIO_OUTPUT_DEVICE_KEY
        self._resource_pack_scan_worker: TaskWorker | None = None
        self._task_busy = False
        self._entity_lists: dict[str, OverviewEntityListView] = {}
        self._build_ui()
        self.export_controller = AudioExportController(
            self.audioPreviewPanel.export_bar,
            self.audio_preview_tree,
            self.audio_list,
            parent=self,
            group_by_event=lambda: bool(getattr(self.gui_config, "wav_group_by_event", False)),
        )
        self.export_controller.export_requested.connect(self.audio_export_requested.emit)
        self.export_controller.index_requested.connect(self._ensure_audio_refs)
        self._preview_playback_controller = PreviewPlaybackController(parent=self)
        self._preview_playback_controller.playback_state_changed.connect(self._apply_audio_preview_playback_state)
        self._preview_playback_controller.playback_error.connect(self._show_audio_preview_playback_error)
        self.set_shared_data_state(self._shared_data_state)
        self._setup_connections()
        self.destroyed.connect(self._disconnect_theme_refresh_listeners)
        self.destroyed.connect(self._preview_playback_controller.shutdown)
        self.destroyed.connect(self._audio_refs_pool.clear)

    def showEvent(self, event):
        """页面首次展示时同步当前缓存。"""
        super().showEvent(event)
        if self._current_preview_ids[self._current_entity_type()] is None:
            self._set_splitter_sizes_evenly()
        self._sync_current_list_view()
        if (self.preview_mode_pivot.currentRouteKey() or self._active_preview_mode) == ALL_AUDIO_PREVIEW_MODE:
            self._ensure_audio_refs()
            self._refresh_all_audio_preview()

    def resizeEvent(self, event) -> None:
        """窗口尺寸变化时，重新收敛左右面板宽度。"""
        super().resizeEvent(event)
        self._set_splitter_sizes_evenly()

    def set_gui_config(self, cfg) -> None:
        """注入 GUI 配置。"""
        self.gui_config = cfg
        fallback_enabled = bool(getattr(cfg, "smooth_scroll_enabled", False))
        self.set_smooth_scroll_enabled(bool(getattr(cfg, "widget_smooth_scroll_enabled", fallback_enabled)))
        self.set_preview_audio_volume(
            int(getattr(cfg, "preview_audio_volume_percent", DEFAULT_PREVIEW_AUDIO_VOLUME_PERCENT))
        )
        self.set_preview_audio_output_device(
            str(getattr(cfg, "preview_audio_output_device_key", DEFAULT_PREVIEW_AUDIO_OUTPUT_DEVICE_KEY))
        )

    def set_shared_data_state(self, state: SharedDataState) -> None:
        """同步共享目录阶段、摘要与总览页选择门禁。

        Args:
            state: 当前 generation 的类型化共享状态。
        """
        previous_phase = self._shared_data_state.phase
        self._shared_data_state = state
        self.export_controller.set_busy(self._task_busy or self._resource_pack_scan_worker is not None)
        display = describe_shared_data_state(state)
        status_text = display.detail_text if state.active else display.status_text
        self.shared_data_status.set_status(status_text, role=display.status_role)
        self.shared_data_status.setToolTip(display.detail_text)
        self.shared_data_status.setVisible(state.phase is not SharedDataPhase.READY)
        if state.phase is not previous_phase:
            self._update_selection_summary()
            self._sync_current_list_view()

    def set_preview_audio_volume(self, value: int) -> None:
        """缓存试听音量设置并同步到底层播放器。"""
        self._preview_audio_volume_percent = int(value)
        self._preview_playback_controller.set_volume_percent(self._preview_audio_volume_percent)

    def set_preview_audio_output_device(self, value: str) -> None:
        """缓存试听输出设备设置并同步到播放控制器。"""
        self._preview_audio_output_device_key = str(value or "").strip() or DEFAULT_PREVIEW_AUDIO_OUTPUT_DEVICE_KEY
        self._preview_playback_controller.set_output_device_key(self._preview_audio_output_device_key)

    def set_app_context(self, app_context) -> None:
        """注入应用上下文。

        Args:
            app_context: 当前应用上下文；为 ``None`` 时仅保留占位提示。
        """
        self._audio_refs_token += 1
        self.export_controller.reset()
        self._audio_refs_cache.clear()
        self._current_preview_key = None
        self._audio_refs_progress = None
        self._pending_audio_refs_request = None
        self._app_context = app_context
        self._loader = None
        if app_context is None:
            self.entityListPanel.set_special_interaction_enabled(False)
            self.entityListPanel.set_resource_pack_scan_enabled(False)
            self.entityListPanel.set_special_availability_message(None)
            self._update_catalog_subtitle()
            self._show_placeholder("当前配置尚未完成初始化，暂时无法读取预览内容。")
            return

        self.entityListPanel.set_special_interaction_enabled(True)
        self.entityListPanel.set_resource_pack_scan_enabled(self._resource_pack_scan_worker is None)
        self.entityListPanel.set_special_availability_message(None)
        self._update_catalog_subtitle()

        current_index = self._current_entity_list().currentIndex()
        if current_index.isValid():
            self._load_preview_for_item(self._current_entity_type(), current_index)
        else:
            self._sync_current_list_view()

    def search_item_events(self, item_id: str) -> bool:
        """定位常规地图的事件视图，并以原始装备 ID 筛选。

        Args:
            item_id: 装备查询页提供的 ID，不推断模式变体与基础装备的关系。

        Returns:
            已定位常规地图时返回 True；目录缺失或用户取消切换时返回 False。
        """
        if not any(str(row["id"]) == "0" for row in self._entity_data_store.rows_for("maps")):
            InfoBar.warning(
                "常规地图尚未就绪",
                "请先在主页完成数据准备，再查找装备事件。",
                parent=self.window(),
                position=InfoBarPosition.TOP,
            )
            return False

        request = self.export_controller.request
        if request is not None and (request.entity_type, request.entity_id) != ("maps", "0"):
            if not self.export_controller.confirm_change():
                return False
            self.export_controller.reset()

        # 一次更新目录与预览对象，避免旧筛选隐藏常规地图或触发中间实体加载。
        blockers = [QSignalBlocker(self.nav_pivot), QSignalBlocker(self.search_input)]
        self.nav_pivot.setCurrentItem("maps")
        self.search_input.clear()
        self._current_preview_ids["maps"] = "0"
        del blockers
        self._update_catalog_subtitle()
        self._update_catalog_search_placeholder()
        self._sync_current_list_view()
        self._current_entity_list().scrollTo(self._current_entity_list().currentIndex())

        # 预览完成会重置搜索；将本次搜索绑定到加载代次，避免被旧请求覆盖。
        self._pending_event_search = (self._audio_refs_token, item_id) if self._preview_loading else None
        self._preview_search_keywords[EVENT_PREVIEW_MODE] = item_id
        blocker = QSignalBlocker(self.preview_mode_pivot)
        self.preview_mode_pivot.setCurrentItem(EVENT_PREVIEW_MODE)
        del blocker
        self._active_preview_mode = EVENT_PREVIEW_MODE
        self._apply_preview_mode(EVENT_PREVIEW_MODE)
        if not self._preview_loading:
            self._refresh_audio_preview_tree()
        self.previewPanel.preview_search_input.setFocus()
        return True

    def set_entity_data(self, entity_type: str, data: list[dict[str, Any]]) -> None:
        """更新页面缓存的实体数据。"""
        if not self._entity_data_store.set_rows(entity_type, data):
            return

        self._loader = None
        for row in data:
            self._audio_refs_cache.pop((str(row.get("entity_type", entity_type)), str(row["id"])), None)

        if self._current_entity_type() == entity_type:
            self._current_preview_key = None
        self._rebuild_entity_list(entity_type)
        if self._current_entity_type() == entity_type:
            self._sync_current_list_view()
        else:
            self._update_selection_summary()

    def update_entity_rows(self, entity_type: str, rows: list[dict[str, Any]]) -> None:
        """按实体 ID 增量更新页面缓存并刷新当前列表。"""
        merged_rows = self._entity_data_store.update_rows(entity_type, rows)
        if merged_rows is None:
            return

        # 产物可在目录状态文字不变时更新；不能用行数据是否相等判断缓存新鲜度。
        self._loader = None
        keys = {(str(row.get("entity_type", entity_type)), str(row["id"])) for row in rows}
        for key in keys:
            self._audio_refs_cache.pop(key, None)
        refresh_current = (self._current_preview_entity_type, self._current_preview_entity_id) in keys
        if refresh_current:
            self._current_preview_key = None
            self._audio_refs_token += 1
        self._rebuild_entity_list(entity_type)
        if self._current_entity_type() == entity_type:
            self._sync_current_list_view()
        else:
            self._update_selection_summary()

    def clear_data(self) -> None:
        """清空页面缓存并恢复占位内容。"""
        if any(self._selected_entity_ids.values()):
            InfoBar.warning(
                "目标选择已清除",
                "来源目录正在刷新，请在检查完成后重新选择任务目标。",
                parent=self.window(),
                position=InfoBarPosition.TOP,
            )
        self._entity_data_store.clear()
        self._loader = None
        self._audio_refs_cache.clear()
        self.entityListPanel.set_special_catalog_notice(None)
        self._selected_entity_ids = {"champions": set(), "maps": set(), "special": set()}
        self._current_preview_ids = {"champions": None, "maps": None, "special": None}
        self._current_mapping_path = None
        for _entity_type, list_widget in self._entity_lists.items():
            selection_model = list_widget.selectionModel()
            blockers = [QSignalBlocker(list_widget)]
            if selection_model is not None:
                blockers.append(QSignalBlocker(selection_model))
                selection_model.clearSelection()
                selection_model.setCurrentIndex(QModelIndex(), QItemSelectionModel.SelectionFlag.NoUpdate)
            list_widget.set_rows([])
            list_widget.setCurrentIndex(QModelIndex())
            del blockers
        self._update_selection_summary()
        self._show_placeholder("当前暂无可预览的内容。")

    def _setup_connections(self) -> None:
        self.nav_pivot.currentItemChanged.connect(self._on_nav_changed)
        self.preview_mode_pivot.currentItemChanged.connect(self._on_preview_mode_changed)
        self.search_input.textChanged.connect(self._on_search_text_changed)
        self.previewPanel.preview_search_input.textChanged.connect(self._on_preview_search_text_changed)
        self.previewPanel.preview_search_input.returnPressed.connect(self._apply_event_search)
        self.sync_selection_btn.clicked.connect(self._sync_selected_entities)
        self.clear_selection_btn.clicked.connect(self._clear_selected_entities)
        self.entityListPanel.scan_resource_packs_action.triggered.connect(self._select_resource_pack_wads)
        self.reveal_file_btn.clicked.connect(self._reveal_current_preview_target)
        self.previewPanel.resource_source_open_requested.connect(self._open_resource_info_source)
        self.previewPanel.resource_info_requested.connect(self._load_resource_stats)
        self.audio_preview_tree.audio_ref_toggle_requested.connect(self._on_audio_preview_toggle_requested)
        self.audio_preview_tree.audio_context_menu_requested.connect(self._show_audio_menu)
        self.audio_preview_tree.audio_ref_selected.connect(self._on_audio_ref_selected)
        self.audioPreviewPanel.audio_list.audio_ref_toggle_requested.connect(self._on_audio_preview_toggle_requested)
        self.audioPreviewPanel.audio_list.audio_context_menu_requested.connect(self._show_audio_menu)
        self.audioPreviewPanel.audio_list.audio_ref_selected.connect(self._on_audio_ref_selected)
        qconfig.themeChanged.connect(self._refresh_theme_styles)
        qconfig.themeColorChanged.connect(self._refresh_theme_styles)

        for entity_type, list_widget in self._entity_lists.items():
            selection_model = list_widget.selectionModel()
            if selection_model is None:
                continue
            selection_model.currentChanged.connect(
                lambda current, previous, et=entity_type: self._on_current_item_changed(et, current, previous)
            )
            selection_model.selectionChanged.connect(
                lambda _selected, _deselected, et=entity_type: self._on_entity_selection_changed(et)
            )

    def _disconnect_theme_refresh_listeners(self, *_args: object) -> None:
        """断开实体总览页注册的全局主题监听。"""
        for signal, callback in (
            (qconfig.themeChanged, self._refresh_theme_styles),
            (qconfig.themeColorChanged, self._refresh_theme_styles),
        ):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass

    def _refresh_entity_list_theme(self) -> None:
        """在主题或主题色变化后刷新列表绘制。"""
        for list_widget in self._entity_lists.values():
            try:
                list_widget.refresh_theme()
            except RuntimeError:
                continue

    def _refresh_panel_shell_theme(self) -> None:
        """刷新总览页文本预览的主题样式。"""
        # 分隔线复用主题描边，避免原生 QFrame 在深色主题下仍绘制黑线。
        self.selection_bar.setStyleSheet("")
        self.audio_preview_summary_card.setStyleSheet("")
        stroke = get_fluent_frame_stroke_pair()[int(qconfig.theme == Theme.DARK)]
        for separator in (self.entityListPanel.selection_separator, self.audioPreviewPanel.export_bar.separator):
            separator.setStyleSheet(f"border: none; background: {stroke};")
        self.previewPanel.refresh_theme()

    def _refresh_theme_styles(self, *_args: object) -> None:
        """统一刷新总览页当前主题相关样式。"""
        accent_light, accent_dark = get_accent_text_color_pair()
        self.subtitle_label.setTextColor(accent_light, accent_dark)
        self._refresh_entity_list_theme()
        self._refresh_panel_shell_theme()

    def _on_preview_mode_changed(self, mode_key: str) -> None:
        """切换右侧事件、全部音频与原始数据视图。"""
        previous_mode = self._active_preview_mode
        if previous_mode in self._preview_search_keywords:
            self._preview_search_keywords[previous_mode] = self.previewPanel.preview_search_input.text()
        self._active_preview_mode = mode_key
        self._apply_preview_mode(mode_key)

    def _apply_preview_mode(self, mode_key: str) -> None:
        """应用模式壳层状态，不重建另一种预览的模型或滚动位置。"""
        self._event_search_timer.stop()
        search = self.previewPanel.preview_search_input
        if mode_key == RAW_PREVIEW_MODE:
            self._ensure_raw_preview()
            blocker = QSignalBlocker(search)
            search.clear()
            del blocker
            search.setEnabled(False)
            search.setPlaceholderText("原始数据暂不支持搜索")
        else:
            search.setEnabled(True)
            search.setPlaceholderText(
                "搜索事件、类型或音频 ID" if mode_key == EVENT_PREVIEW_MODE else "搜索 WEM ID、相对路径或音频类型"
            )
            keyword = self._preview_search_keywords.get(mode_key, "")
            blocker = QSignalBlocker(search)
            search.setText(keyword)
            del blocker

        self.previewPanel.set_preview_mode(mode_key)

        if mode_key == EVENT_PREVIEW_MODE:
            self.audioPreviewPanel.clear_load_progress()
            self.audioPreviewPanel.set_summary_text(self._event_preview_summary)
            if self._event_search_pending:
                self._event_search_timer.start()
        elif mode_key == ALL_AUDIO_PREVIEW_MODE:
            self._ensure_audio_refs()
            self._refresh_all_audio_preview()
            self.audioPreviewPanel.set_summary_text(self._all_audio_preview_summary)
        self._sync_preview_path()

    def _current_entity_type(self) -> str:
        return self.nav_pivot.currentRouteKey() or "champions"

    def _current_entity_list(self) -> OverviewEntityListView:
        return self._entity_lists[self._current_entity_type()]

    def _ensure_loader(self) -> EntityDataLoader | None:
        if self._loader is None and self._app_context is not None:
            self._loader = EntityDataLoader(self._app_context)
        return self._loader

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        apply_page_content_margins(root_layout)
        root_layout.setSpacing(16)

        header_layout = QHBoxLayout()
        title_label = SubtitleLabel("实体总览", self)
        self.subtitle_label = CaptionLabel("查看英雄、地图与特殊内容状态，选好后可直接发送到执行中心。", self)
        self.subtitle_label.setWordWrap(False)

        title_column = QVBoxLayout()
        title_column.setSpacing(2)
        title_column.addWidget(title_label)
        title_column.addWidget(self.subtitle_label)

        header_layout.addLayout(title_column)
        header_layout.addStretch(1)
        root_layout.addLayout(header_layout)

        self.shared_data_status = StatusLine("正在检查实体数据…", self)
        root_layout.addWidget(self.shared_data_status)

        self.splitter = QSplitter(Qt.Horizontal, self)
        self.splitter.setObjectName("OverviewSplitter")
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(0)

        self.entityListPanel = OverviewEntityListPanel(self.splitter)
        self.nav_pivot = self.entityListPanel.nav_pivot
        self.search_input = self.entityListPanel.search_input
        self.selection_status_label = self.entityListPanel.selection_status_label
        self.list_stack = self.entityListPanel.list_stack
        self.selection_bar = self.entityListPanel.selection_bar
        self.clear_selection_btn = self.entityListPanel.clear_selection_btn
        self.sync_selection_btn = self.entityListPanel.sync_selection_btn
        self._entity_lists = self.entityListPanel.entity_lists

        self.previewPanel = OverviewPreviewPanel(
            audio_summary_placeholder=self._audio_preview_placeholder,
            parent=self.splitter,
        )
        self.preview_path_edit = self.previewPanel.preview_path_edit
        self.reveal_file_btn = self.previewPanel.reveal_file_btn
        self.preview_mode_pivot = self.previewPanel.preview_mode_pivot
        self.preview_stack = self.previewPanel.preview_stack
        self.text_preview = self.previewPanel.text_preview
        self.audioPreviewPanel = self.previewPanel.audio_preview_panel
        self.audio_preview_summary_card = self.audioPreviewPanel.summary_card
        self.audio_preview_summary_label = self.audioPreviewPanel.summary_label
        self.audio_preview_tree = self.audioPreviewPanel.audio_preview_tree
        self.audio_list = self.audioPreviewPanel.audio_list
        QWidget.setTabOrder(self.nav_pivot, self.search_input)
        QWidget.setTabOrder(self.search_input, self.preview_mode_pivot)
        QWidget.setTabOrder(self.preview_mode_pivot, self.previewPanel.preview_search_input)

        self.splitter.addWidget(self.entityListPanel)
        self.splitter.addWidget(self.previewPanel)
        splitter_handle = self.splitter.handle(1)
        splitter_handle.setEnabled(False)
        splitter_handle.hide()
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([1, 1])

        root_layout.addWidget(self.splitter, 1)
        self._update_selection_summary()
        self.set_smooth_scroll_enabled(False)
        self._refresh_panel_shell_theme()

    def _rebuild_entity_list(self, entity_type: str) -> None:
        """刷新指定实体类型的 source model，并恢复选择状态。"""
        self.entityListPanel.set_rows(entity_type, self._entity_data_store.rows_for(entity_type))
        self._prune_entity_state(entity_type)
        self.entityListPanel.apply_keyword_and_restore(
            entity_type=entity_type,
            keyword=self.search_input.text(),
            selected_ids=self._selected_entity_ids.get(entity_type, set()),
            current_entity_id=self._current_preview_ids.get(entity_type),
        )

    def _apply_filter_to_current_list(self) -> int:
        """对当前列表应用代理过滤，并返回过滤后的可见行数。"""
        entity_type = self._current_entity_type()
        return self.entityListPanel.apply_keyword_and_restore(
            entity_type=entity_type,
            keyword=self.search_input.text(),
            selected_ids=self._selected_entity_ids.get(entity_type, set()),
            current_entity_id=self._current_preview_ids.get(entity_type),
        )

    def _prune_entity_state(self, entity_type: str) -> None:
        """移除已经不在当前 source model 中的选择与预览状态。"""
        available_ids = self._entity_lists[entity_type].entity_ids()
        selectable_ids = {
            str(row.get("key", row.get("id", "")))
            for row in self._entity_data_store.rows_for(entity_type)
            if row.get("selectable", True)
        }
        removed = self._selected_entity_ids[entity_type] - selectable_ids
        self._selected_entity_ids[entity_type] &= selectable_ids
        if removed:
            InfoBar.warning(
                "部分目标已移出选择",
                f"{len(removed)} 个目标已不可用，请查看文件缺失提示。",
                parent=self.window(),
                position=InfoBarPosition.TOP,
            )
        current_preview_id = self._current_preview_ids.get(entity_type)
        if current_preview_id is not None and current_preview_id not in available_ids:
            self._current_preview_ids[entity_type] = None

    def _sync_current_list_view(self) -> None:  # noqa: PLR0911
        """同步当前 tab 的列表显示状态，不重建已有缓存。"""
        entity_type = self._current_entity_type()
        source_rows = self._entity_data_store.rows_for(entity_type)
        visible_count = self._apply_filter_to_current_list()
        current_preview_id = self._current_preview_ids.get(entity_type)
        self.entityListPanel.set_current_entity_type(entity_type)
        list_widget = self._current_entity_list()

        if entity_type == "special" and self._app_context is None:
            self.entityListPanel.set_special_catalog_notice(None)
            self._set_splitter_sizes_evenly()
            self._show_placeholder("当前特殊内容数据尚未加载完成。")
            self._update_selection_summary()
            return

        if not source_rows:
            if entity_type == "special":
                self.entityListPanel.set_special_catalog_notice(None)
            self._set_splitter_sizes_evenly()
            if self._shared_data_state.phase is not SharedDataPhase.READY:
                display = describe_shared_data_state(self._shared_data_state)
                self._show_placeholder(display.status_text)
                self._update_selection_summary()
                return
            placeholders = {
                "champions": "当前英雄数据尚未加载完成。",
                "maps": "当前地图数据尚未加载完成。",
                "special": "当前版本未发现特殊内容",
            }
            placeholder = placeholders.get(entity_type, "当前实体数据尚未加载完成。")
            self._show_placeholder(placeholder)
            self._update_selection_summary()
            return

        if entity_type == "special":
            self.entityListPanel.set_special_catalog_notice(
                "特殊内容资源尚未准备。"
                if all(
                    str(row.get("audio", "")) == "未准备" and str(row.get("mapping", "")) == "未准备"
                    for row in source_rows
                )
                else None
            )

        if visible_count == 0:
            self._set_splitter_sizes_evenly()
            self._show_placeholder("未找到匹配的实体，请调整筛选条件。")
            self._update_selection_summary()
            return

        if current_preview_id is None:
            list_widget.setCurrentIndex(QModelIndex())
            self._set_splitter_sizes_evenly()
            self._show_placeholder(DEFAULT_PREVIEW_PLACEHOLDER_TEXT)
            self._update_selection_summary()
            return

        target_index = self.entityListPanel.find_index_by_entity_id(entity_type, current_preview_id)
        if not target_index.isValid():
            self._show_placeholder("当前筛选结果中不包含已选实体。")
            self._update_selection_summary()
            return

        if list_widget.currentIndex() != target_index:
            list_widget.setCurrentIndex(target_index)
        else:
            self._load_preview_for_item(entity_type, target_index)
        self._update_selection_summary()

    def _update_selection_summary(self) -> None:
        champion_count = len(self._selected_entity_ids["champions"])
        map_count = len(self._selected_entity_ids["maps"])
        special_count = len(self._selected_entity_ids["special"])
        self.entityListPanel.set_selection_counts(
            champion_count=champion_count,
            map_count=map_count,
            special_count=special_count,
        )
        if self._shared_data_state.blocks_new_tasks:
            display = describe_shared_data_state(self._shared_data_state)
            self.sync_selection_btn.setEnabled(False)
            self.sync_selection_btn.setToolTip(display.task_block_reason)
        else:
            self.sync_selection_btn.setToolTip("")

    def _sync_selected_entities(self) -> None:
        if self._shared_data_state.blocks_new_tasks:
            display = describe_shared_data_state(self._shared_data_state)
            InfoBar.warning(
                "共享数据尚未就绪",
                display.task_block_reason,
                parent=self.window(),
                position=InfoBarPosition.TOP,
            )
            return
        payload = self.entityListPanel.build_selection_sync_request(
            selected_champion_ids=self._selected_entity_ids["champions"],
            selected_map_ids=self._selected_entity_ids["maps"],
            selected_special_targets=self._selected_entity_ids["special"],
            special_target_names={
                str(row.get("key", "")): str(row.get("display_name", ""))
                for row in self._entity_data_store.rows_for("special")
            },
            resource_pack_wads={
                str(row.get("key", "")): ref
                for row in self._entity_data_store.rows_for("special")
                if isinstance(ref := row.get("resource_pack_wad"), ResourcePackWadRef)
            },
        )
        total_count = len(payload.champion_ids) + len(payload.map_ids) + len(payload.special_targets)
        if total_count == 0:
            InfoBar.warning(
                "没有可同步的选择",
                "请先在左侧列表中选择至少一个实体。",
                parent=self.window(),
                position=InfoBarPosition.TOP,
            )
            return

        self.selection_sync_requested.emit(payload)

    def _clear_selected_entities(self) -> None:
        for entity_type in self._selected_entity_ids:
            self._selected_entity_ids[entity_type] = set()
            self._current_preview_ids[entity_type] = None
            self.entityListPanel.clear_selection(entity_type)
        self._sync_current_list_view()

    def _select_resource_pack_wads(self) -> None:
        """选择 FINAL 内 WAD 并在后台启动显式资源包扫描。"""
        if self._app_context is None:
            self.entityListPanel.set_special_catalog_notice("游戏目录尚未就绪，暂时无法扫描资源包。")
            return

        game_root = Path(self._app_context.config.game_path)
        final_root = game_root / FINAL_DIR
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择本地 WAD，发现额外音频内容（经典英雄无需扫描）",
            str(final_root if final_root.is_dir() else game_root),
            "WAD 文件 (*.wad.client)",
        )
        if not paths:
            self.entityListPanel.set_special_catalog_notice("尚未选择本地资源包。")
            return

        refs: list[ResourcePackWadRef] = []
        errors: list[str] = []
        for path in paths:
            try:
                refs.append(ResourcePackWadRef.from_path(game_root, Path(path)))
            except ResourcePackSelectionError as exc:
                errors.append(str(exc))
        refs = list(dict.fromkeys(refs))
        if not refs:
            self.entityListPanel.set_special_catalog_notice(errors[0] if errors else "未选择可扫描的本地资源包。")
            return

        if errors:
            InfoBar.warning(
                "部分 WAD 未加入扫描",
                errors[0],
                parent=self.window(),
                position=InfoBarPosition.TOP,
            )
        self._start_resource_pack_scan(tuple(refs))

    def _start_resource_pack_scan(self, refs: tuple[ResourcePackWadRef, ...]) -> None:
        """通过线程池运行 selected-WAD discovery，避免阻塞 UI 线程。"""
        if (
            self._app_context is None
            or self._resource_pack_scan_worker is not None
            or self._task_busy
            or self._shared_data_state.active
        ):
            return

        context = self._app_context
        self._resource_pack_scan_worker = TaskWorker(
            lambda: LolAudioUnpackApp(context).discover_resource_packs(OperationOptions(resource_pack_wads=refs))
        )
        self.background_work_changed.emit(True)
        self.export_controller.set_busy(True)
        worker = self._resource_pack_scan_worker
        worker.signals.started.connect(self._on_resource_pack_scan_started)
        worker.signals.finished.connect(self._on_resource_pack_scan_finished)
        worker.signals.failed.connect(self._on_resource_pack_scan_failed)
        QThreadPool.globalInstance().start(worker)

    def _on_resource_pack_scan_started(self) -> None:
        """显示资源包扫描中的可观察状态。"""
        self.entityListPanel.set_resource_pack_scan_enabled(False)
        self.entityListPanel.set_special_catalog_notice("正在从所选 WAD 发现音频内容，完成后可选择并创建解包任务…")

    def _on_resource_pack_scan_finished(self, result: object) -> None:
        """合并最新已持久化 resource-pack 行，并展示聚合成本与状态。"""
        self._resource_pack_scan_worker = None
        self.background_work_changed.emit(False)
        self.export_controller.set_busy(self._task_busy)
        self.entityListPanel.set_resource_pack_scan_enabled(self._app_context is not None)
        loader = self._ensure_loader()
        if loader is not None:
            resource_rows = loader.load_resource_pack_rows()
            structured_rows = [
                row for row in self._entity_data_store.rows_for("special") if row.get("entity_type") != "resource_packs"
            ]
            self.set_entity_data("special", [*structured_rows, *resource_rows])
        self.entityListPanel.set_special_catalog_notice(self._resource_pack_scan_message(result))

    def _on_resource_pack_scan_failed(self, error: str) -> None:
        """恢复扫描入口，并保留后台 discovery 的失败说明。"""
        self._resource_pack_scan_worker = None
        self.background_work_changed.emit(False)
        self.export_controller.set_busy(self._task_busy)
        self.entityListPanel.set_resource_pack_scan_enabled(self._app_context is not None)
        self.entityListPanel.set_special_catalog_notice(f"本地资源包扫描失败: {error}")

    @staticmethod
    def _resource_pack_scan_message(result: object) -> str:
        """将 discovery 汇总投影为总览页可读状态。"""
        scans = tuple(getattr(result, "scans", ()))
        packs = tuple(getattr(result, "packs", ()))
        status = str(getattr(result, "status", "failed"))
        cost = getattr(result, "cost", {})
        if not isinstance(cost, dict):
            cost = {}
        cost_text = (
            f"candidate {cost.get('candidateEntries', 0)}，"
            f"payload {cost.get('payloadReads', 0)}，"
            f"耗时 {float(cost.get('elapsedSeconds', 0) or 0):.1f}s"
        )
        reasons = [str(getattr(scan, "reason", "") or "") for scan in scans]
        if any("Map 22 declaration" in reason for reason in reasons):
            return f"所选 WAD 的可解析声明属于 Map 22，已保持为地图实体（{cost_text}）。"
        if any("未找到可解析的 PROP/BIN candidate" in reason for reason in reasons):
            return f"未发现可解析 BIN（{cost_text}）。"
        if any(str(getattr(pack, "status", "")) == "conflict" for pack in packs):
            return f"扫描发现冲突，未覆盖已有资源包 artifact（{cost_text}）。"
        if status == "complete" and packs:
            return f"本地资源包扫描完成，发现 {len(packs)} 个资源包（{cost_text}）。"
        if status == "partial":
            return f"本地资源包扫描部分完成，发现 {len(packs)} 个资源包（{cost_text}）。"
        reason = next((reason for reason in reasons if reason), "未发现可用资源包")
        return f"本地资源包扫描失败: {reason}（{cost_text}）。"

    def _on_nav_changed(self, _key: str) -> None:
        if self._current_preview_key is not None and self._current_preview_key[0] != _key:
            if not self.export_controller.confirm_change():
                blocker = QSignalBlocker(self.nav_pivot)
                self.nav_pivot.setCurrentItem(self._current_preview_key[0])
                del blocker
                return
            self.export_controller.reset()
        self._update_catalog_subtitle()
        self._update_catalog_search_placeholder()
        self._sync_current_list_view()

    def _on_search_text_changed(self, _text: str) -> None:
        self._sync_current_list_view()

    def _on_preview_search_text_changed(self, _text: str) -> None:
        mode_key = self.preview_mode_pivot.currentRouteKey() or EVENT_PREVIEW_MODE
        if mode_key not in self._preview_search_keywords:
            return
        self._preview_search_keywords[mode_key] = self.previewPanel.preview_search_input.text()
        if mode_key == EVENT_PREVIEW_MODE:
            # 连续输入只更新关键词，停顿后再重建大事件树。
            self._event_search_pending = True
            self._event_search_timer.start()
            return
        self._refresh_current_preview_mode()

    def _apply_event_search(self) -> None:
        """等待结束或按下回车时，应用当前事件视图的待搜索关键词。"""
        if self._event_search_pending and self._active_preview_mode == EVENT_PREVIEW_MODE and not self._preview_loading:
            self._refresh_audio_preview_tree()

    def _on_current_item_changed(self, entity_type: str, current, _previous) -> None:
        if entity_type != self._current_entity_type():
            return
        if self._load_preview_for_item(entity_type, current) is False:
            selection_model = self._entity_lists[entity_type].selectionModel()
            blocker = QSignalBlocker(selection_model)
            selection_model.setCurrentIndex(_previous, QItemSelectionModel.SelectionFlag.NoUpdate)
            del blocker

    def _on_entity_selection_changed(self, entity_type: str) -> None:
        self._selected_entity_ids[entity_type] = self.entityListPanel.selected_entity_ids(entity_type)
        self._update_selection_summary()

    def _load_preview_for_item(self, entity_type: str, item) -> None:
        row = self.entityListPanel.resolve_row_payload(item)
        if not row:
            self._current_preview_ids[entity_type] = None
            self._show_placeholder(DEFAULT_PREVIEW_PLACEHOLDER_TEXT)
            return

        preview_state_id = str(row.get("key", row["id"]) if entity_type == "special" else row["id"])
        preview_entity_type = str(row.get("entity_type", entity_type))
        preview_entity_id = str(row["id"])
        preview_key = (entity_type, preview_state_id, preview_entity_type, preview_entity_id)
        no_artifacts = all(row.get(key) in {"未存在", "未准备"} for key in ("audio", "mapping"))
        if preview_key == self._current_preview_key and not no_artifacts:
            if self._preview_loading:
                return
            self.previewPanel.show_current_preview()
            self._apply_preview_mode(self.preview_mode_pivot.currentRouteKey() or self._active_preview_mode)
            return

        previous = self.export_controller.request
        if (
            not self._preview_loading
            and previous is not None
            and (previous.entity_type, previous.entity_id)
            != (
                preview_entity_type,
                preview_entity_id,
            )
        ):
            if not self.export_controller.confirm_change():
                return False
        self._current_preview_ids[entity_type] = preview_state_id
        if no_artifacts:
            # 普通与特殊目录都复用已确认的无产物状态；清理同时使旧实体的异步结果失效。
            self._show_placeholder("尚未解包")
            return
        self.previewPanel.clear_resource_info()

        self._event_search_timer.stop()
        self._event_search_pending = False
        self._current_preview_entity_type = preview_entity_type
        self._current_preview_entity_id = preview_entity_id
        self._current_preview_entity_name = str(row.get("display_name", row["name"]))
        self._audio_refs_token += 1
        loader = self._ensure_loader()
        token = self._audio_refs_token
        self._current_preview_key = preview_key
        self._clear_audio_preview_request()
        self._preview_loading = True
        self._preview_placeholder = None
        self.previewPanel.show_loading("正在读取实体映射…")
        started = monotonic()
        logger.debug("实体预览加载启动：{} {}", preview_entity_type, preview_entity_id)
        worker = TaskWorker(
            lambda: (
                None
                if token != self._audio_refs_token
                else self._preview_controller.load_preview(
                    entity_type=preview_entity_type,
                    entity_id=preview_entity_id,
                    entity_name=str(row.get("display_name", row["name"])),
                    loader=loader,
                )
            )
        )
        self._preview_workers[token] = worker
        worker.signals.finished.connect(lambda result: self._finish_preview(token, preview_key, row, result, started))
        worker.signals.failed.connect(lambda error: self._preview_failed(token, error))
        self._preview_pool.start(worker)

    def _preview_failed(self, token, error):
        """只显示当前选择的错误，过期请求不得清空新实体。"""
        self._preview_workers.pop(token, None)
        logger.warning("实体预览加载失败：{}", error)
        if token == self._audio_refs_token:
            self._show_placeholder(f"实体预览加载失败：{error}")

    def _finish_preview(self, token, preview_key, row, preview_result, started):
        """在 GUI 线程应用后台结果，快速切换时丢弃旧实体。"""
        self._preview_workers.pop(token, None)
        if token != self._audio_refs_token:
            return
        self._preview_loading = False
        self.export_controller.reset()
        entity_type, _, preview_entity_type, preview_entity_id = preview_key
        logger.debug("实体预览加载完成：{} {}，{:.3f}s", preview_entity_type, preview_entity_id, monotonic() - started)
        self._resolve_event_ref = preview_result.resolve_ref
        self._raw_loaded = False
        self._resource_stats = None
        if preview_result.placeholder_message is not None:
            self._show_placeholder(preview_result.placeholder_message)
            return

        self._current_preview_key = preview_key
        self._current_mapping_path = preview_result.mapping_path
        self.text_preview.setPlainText(preview_result.preview_content)
        self._clear_audio_preview_request()
        self._current_preview_mapping_data = preview_result.mapping_data
        self._current_preview_group_label_map = dict(preview_result.group_label_map)
        self._current_audio_refs = preview_result.audio_refs
        self._current_event_audio_refs = preview_result.event_audio_refs or preview_result.audio_refs
        self._current_audio_roots = preview_result.audio_roots
        self._audio_refs_loaded = preview_result.audio_refs_loaded
        cache_key = (preview_entity_type, str(row["id"]))
        if not self._audio_refs_loaded and cache_key in self._audio_refs_cache:
            self._current_audio_refs = self._audio_refs_cache[cache_key]
            self._audio_refs_loaded = True
        self._audio_list_ready = False
        self._audio_refs_error = None
        self._audio_refs_progress = None
        if self._audio_refs_loaded:
            self._cache_audio_refs(cache_key, self._current_audio_refs)
        self._selected_audio_refs = {
            EVENT_PREVIEW_MODE: None,
            ALL_AUDIO_PREVIEW_MODE: None,
        }
        self._current_mapping_notice = preview_result.mapping_notice
        self._configure_audio_export(self._current_preview_entity_name, preview_result.version)
        self._refresh_resource_info()
        self._preview_search_keywords = {
            EVENT_PREVIEW_MODE: "",
            ALL_AUDIO_PREVIEW_MODE: "",
        }
        mode = preview_result.default_preview_mode
        if self._pending_event_search is not None and self._pending_event_search[0] == token:
            self._preview_search_keywords[EVENT_PREVIEW_MODE] = self._pending_event_search[1]
            mode = EVENT_PREVIEW_MODE
        self._pending_event_search = None
        self._refresh_audio_preview_tree()
        if self._audio_refs_loaded and mode == ALL_AUDIO_PREVIEW_MODE:
            self._populate_audio_list()
        self._refresh_all_audio_preview()
        self.previewPanel.show_current_preview()
        self._sync_audio_preview_playback_state()
        pivot_blocker = QSignalBlocker(self.preview_mode_pivot)
        self.preview_mode_pivot.setCurrentItem(mode)
        del pivot_blocker
        self._active_preview_mode = mode
        self._apply_preview_mode(mode)

    def _on_audio_preview_toggle_requested(self, audio_ref: AudioRef) -> None:
        """响应路径级试听项点击并触发精确 WEM 播放控制。"""
        self._set_selected_audio_ref(audio_ref)
        self._sync_preview_path()
        result = self._preview_controller.resolve_audio_preview_toggle(
            requested_audio=audio_ref,
            current_audio_path=self._current_audio_preview_path,
        )
        if result is None:
            return
        if result.warning_message is not None:
            InfoBar.warning(
                "找不到试听音频",
                result.warning_message,
                parent=self.window(),
                position=InfoBarPosition.TOP,
            )
            return

        if result.audio_id is None or result.audio_path is None:
            self._clear_audio_preview_request()
            return

        self._preview_playback_controller.play(
            audio_id=result.audio_id,
            audio_path=result.audio_path,
        )

    def _on_audio_ref_selected(self, audio_ref: AudioRef) -> None:
        """让全部音频模式路径栏跟随用户实际选中的 WEM。"""
        self._set_selected_audio_ref(audio_ref)
        self._sync_preview_path()

    def _set_selected_audio_ref(self, audio_ref: AudioRef) -> None:
        """按当前试听模式保存用户选中的精确路径引用。"""
        mode_key = self.preview_mode_pivot.currentRouteKey() or self._active_preview_mode
        if mode_key in self._selected_audio_refs:
            self._selected_audio_refs[mode_key] = audio_ref

    @staticmethod
    def _audio_menu_wem_path(audio_ref: AudioRef) -> Path:
        """返回右键菜单目标的已验证精确 WEM 路径。"""
        return audio_ref.path

    def _show_audio_menu(self, audio_ref: AudioRef, global_pos: QPoint) -> None:
        """显示事件树与全部音频共用的路径级操作菜单。"""
        self._set_selected_audio_ref(audio_ref)
        self._sync_preview_path()
        wem_path = self._audio_menu_wem_path(audio_ref)
        menu = RoundMenu(parent=self)

        save_action = Action("另存为 WAV...", menu)
        save_action.setEnabled(not self.export_controller.busy)
        save_action.triggered.connect(lambda: self._save_audio_wav(audio_ref.wem_id, wem_path))
        menu.addAction(save_action)

        reveal_wem_action = Action("打开 WEM 所在位置", menu)
        reveal_wem_action.triggered.connect(lambda: self._reveal_wem(wem_path))
        menu.addAction(reveal_wem_action)

        reveal_wav_action = Action("转码并打开 WAV 所在位置", menu)
        reveal_wav_action.setEnabled(not self.export_controller.busy)
        reveal_wav_action.triggered.connect(lambda: self._reveal_wav(wem_path))
        menu.addAction(reveal_wav_action)

        menu.exec(global_pos, aniType=MenuAnimationType.DROP_DOWN)

    def _wav_format(self) -> str:
        """返回右键单文件转码使用的采样格式。"""
        return str(getattr(self.gui_config, "wav_format", "pcm16") or "pcm16")

    def _configure_audio_export(self, name: str, version: str | None) -> None:
        """从现有领域目录与配置建立当前实体的导出基础快照。"""
        if self._app_context is None or version is None or not self._current_audio_roots:
            self.export_controller.reset()
            return
        # 其他目录刷新可清空 loader；版本必须与本次已通过 token 校验的预览结果一致。
        region_root = self._app_context.version_path("audio", version).resolve()
        self.export_controller.configure(
            AudioExportRequest(
                entity_type=self._current_preview_entity_type or "",
                entity_id=self._current_preview_entity_id or "",
                entity_name=name,
                version=version,
                version_root=region_root,
                targets=tuple(
                    ExportTarget(
                        AudioScope(root),
                        self._app_context.version_path("wav", version),
                    )
                    for root in self._current_audio_roots
                ),
                report_root=self._app_context.version_path("report", version),
                region=region_root.name,
                library_root=Path(self._app_context.config.output_path),
                options=WavOutputOptions(
                    enabled=True,
                    worker_count=int(getattr(self.gui_config, "wav_workers", 2)),
                    timeout_seconds=int(getattr(self.gui_config, "wav_timeout", 5)),
                    max_retries=int(getattr(self.gui_config, "wav_retries", 3)),
                    backend_path=str(self.gui_config.resolve_vgmstream_path() or "") if self.gui_config else None,
                    format=self._wav_format(),
                ),
            ),
            unavailable_count=0,
            mapping_path=self._current_mapping_path,
        )
        self.export_controller.set_available(
            self._current_audio_refs if self._audio_refs_loaded else self._current_event_audio_refs,
            complete=self._audio_refs_loaded,
        )

    def set_task_busy(self, busy: bool) -> None:
        """用全局任务忙碌状态阻止重叠导出和资源包扫描。"""
        self._task_busy = busy
        self.export_controller.set_busy(busy or self._resource_pack_scan_worker is not None)
        self.entityListPanel.set_resource_pack_scan_enabled(
            not busy and self._app_context is not None and self._resource_pack_scan_worker is None
        )

    def _save_audio_wav(self, audio_id: str, wem_path: Path) -> None:
        """把当前试听音频另存为用户选择的 WAV 文件。"""
        output_text, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "另存为 WAV",
            f"{audio_id}.wav",
            "WAV 音频 (*.wav);;所有文件 (*)",
        )
        if not output_text:
            return

        output = Path(output_text)
        if output.suffix.lower() != ".wav":
            output = output.with_suffix(".wav")

        self.export_controller.export_file(wem_path, output, overwrite=True)

    def _reveal_wem(self, wem_path: Path) -> None:
        """打开当前试听 WEM 所在位置。"""
        if self._reveal_file_path(wem_path):
            return
        InfoBar.warning(
            "打开目录失败",
            f"无法打开目录：{wem_path.parent}",
            parent=self.window(),
            position=InfoBarPosition.TOP,
        )

    def _reveal_wav(self, wem_path: Path) -> None:
        """转码当前试听音频到默认 WAV 镜像路径并定位文件。"""
        loader = self._ensure_loader()
        if self._app_context is None or loader is None:
            return

        version = loader.data_reader.version
        audio_root = self._app_context.version_path("audio", version).resolve()
        wav_root = self._app_context.version_path("wav", version)
        try:
            wav_path = resolve_wav_path(wem_path, audio_root=audio_root, wav_root=wav_root)
            self.export_controller.export_file(wem_path, wav_path, overwrite=True, reveal=True)
        except Exception as exc:  # noqa: BLE001
            InfoBar.warning(
                "转码 WAV 失败",
                f"{type(exc).__name__}: {exc}",
                parent=self.window(),
                position=InfoBarPosition.TOP,
            )
            return

    def _apply_audio_preview_playback_state(self, state: PreviewPlaybackState) -> None:
        """同步播放控制器发出的最新试听状态。"""
        self._current_audio_preview_audio_id = state.audio_id
        self._current_audio_preview_path = state.audio_path
        self._current_audio_preview_progress = state.progress
        self._current_audio_preview_is_playing = state.is_playing
        self._current_audio_preview_is_paused = state.is_paused
        self._sync_audio_preview_playback_state()

    def _show_audio_preview_playback_error(self, message: str) -> None:
        """显示试听播放链路的用户可见错误。"""
        InfoBar.warning(
            "试听播放失败",
            message,
            parent=self.window(),
            position=InfoBarPosition.TOP,
        )

    def _sync_audio_preview_playback_state(self) -> None:
        """把当前缓存的试听状态同步到两种路径级试听视图。"""
        self.audioPreviewPanel.set_playback_state(
            self._current_audio_preview_path,
            progress=self._current_audio_preview_progress,
            is_playing=self._current_audio_preview_is_playing,
            is_paused=self._current_audio_preview_is_paused,
        )

    def _clear_audio_preview_request(self) -> None:
        """清空当前试听请求并停止活跃中的播放器。"""
        self._preview_playback_controller.stop()
        self._apply_audio_preview_playback_state(
            PreviewPlaybackState(
                audio_id=None,
                audio_path=None,
                progress=0.0,
                is_playing=False,
                is_paused=False,
            )
        )

    def _show_placeholder(self, message: str) -> None:
        self._event_search_timer.stop()
        self._event_search_pending = False
        # 空实体共享同一空态；只在进入空态时清理，避免反复重置右侧模型与布局。
        if self._preview_placeholder == message:
            return
        self._preview_placeholder = message
        self._preview_loading = False
        self._current_mapping_path = None
        self._current_preview_key = None
        self._current_preview_entity_type = None
        self._current_preview_entity_id = None
        self._current_preview_entity_name = ""
        self._current_preview_mapping_data = None
        self._current_preview_group_label_map = {}
        self._current_event_audio_refs = ()
        self._current_audio_refs = ()
        self._current_audio_roots = ()
        self._audio_refs_loaded = False
        self._audio_list_ready = False
        self._audio_refs_error = None
        self._audio_refs_progress = None
        self._audio_refs_token += 1
        self.audioPreviewPanel.clear_load_progress()
        self._selected_audio_refs = {
            EVENT_PREVIEW_MODE: None,
            ALL_AUDIO_PREVIEW_MODE: None,
        }
        self._current_mapping_notice = None
        self._event_preview_summary = self._audio_preview_placeholder
        self._all_audio_preview_summary = self._audio_preview_placeholder
        self.export_controller.reset()
        self.previewPanel.show_placeholder(message)
        self._clear_audio_preview_request()
        self._sync_audio_preview_playback_state()

    def _refresh_audio_preview_tree(self) -> None:
        """根据当前搜索状态刷新右侧事件树。"""
        self._event_search_timer.stop()
        self._event_search_pending = False
        keyword = self._preview_search_keywords[EVENT_PREVIEW_MODE]
        filter_result = filter_preview_mapping_data(self._current_preview_mapping_data, keyword)
        summary_text = (
            f"分组 {len(extract_tree_groups(filter_result.mapping_data))} · 展开事件查看音频；播放时检查对应文件"
        )
        if self._current_mapping_notice:
            summary_text = f"{self._current_mapping_notice} · {summary_text}"
        if filter_result.is_active:
            summary_text = (
                f"{summary_text} · 匹配事件 {filter_result.matched_event_count} · "
                f"匹配 ID {filter_result.matched_audio_id_count}"
            )
        self._event_preview_summary = summary_text

        self.audioPreviewPanel.set_preview_data(
            mapping_data=filter_result.mapping_data,
            audio_refs=self._current_event_audio_refs,
            group_label_map=self._current_preview_group_label_map,
            summary_text=summary_text,
            selection_mapping=self._current_preview_mapping_data,
            resolve_ref=self._resolve_event_ref,
        )
        self.export_controller.refresh()
        if filter_result.is_active:
            self.audio_preview_tree.expandAll()

    def _refresh_all_audio_preview(self) -> None:
        """根据当前搜索状态刷新全部音频模型与摘要。"""
        keyword = self._preview_search_keywords[ALL_AUDIO_PREVIEW_MODE]
        self.audioPreviewPanel.set_audio_keyword(keyword)
        if not self._audio_refs_loaded:
            if self._audio_refs_error:
                self.audioPreviewPanel.clear_load_progress()
                summary_text = self._audio_refs_error
            else:
                progress = self._audio_refs_progress or AudioIndexProgress(current=0, total=0)
                self.audioPreviewPanel.set_load_progress(progress.current, progress.total)
                if progress.total > 0:
                    percent = progress.current * 100 // progress.total
                    summary_text = f"正在后台加载全部音频… {progress.current:,} / {progress.total:,}（{percent}%）"
                else:
                    summary_text = "正在发现全部音频…"
            if self._current_mapping_notice:
                summary_text = f"{self._current_mapping_notice} · {summary_text}"
            self._all_audio_preview_summary = summary_text
            self.audioPreviewPanel.set_summary_text(summary_text)
            return

        self.audioPreviewPanel.clear_load_progress()
        matched_count = self.audio_list.model().rowCount()
        total_count = len(self._current_audio_refs)
        summary_text = f"全部音频 {total_count:,} 个 WEM 文件"
        if keyword:
            summary_text = f"{summary_text} · 匹配 {matched_count} 个"
        if total_count == 0:
            summary_text = "暂无已解包音频"
        if self._current_mapping_notice:
            summary_text = f"{self._current_mapping_notice} · {summary_text}"
        self._all_audio_preview_summary = summary_text
        self.audioPreviewPanel.set_summary_text(summary_text)

    def _ensure_raw_preview(self) -> None:
        """首次进入原始数据视图才在后台读文件并生成文本。"""
        if self._raw_loaded or self._current_mapping_path is None:
            return
        self._raw_loaded = True
        token = self._audio_refs_token
        path = self._current_mapping_path
        started = monotonic()
        logger.info("原始映射文本加载启动：{}", path)
        worker = TaskWorker(lambda: json.dumps(read_data(path), ensure_ascii=False, indent=2))
        self._preview_workers[("raw", token)] = worker

        def finish(text):
            self._preview_workers.pop(("raw", token), None)
            logger.info("原始映射文本加载完成：{}，{:.3f}s", path, monotonic() - started)
            if token == self._audio_refs_token:
                self.text_preview.setPlainText(text)

        def fail(error):
            self._preview_workers.pop(("raw", token), None)
            logger.warning("原始映射文本加载失败：{}：{}", path, error)
            if token == self._audio_refs_token:
                self._raw_loaded = False
                self.text_preview.setPlainText(f"原始数据读取失败：{error}")

        worker.signals.finished.connect(finish)
        worker.signals.failed.connect(fail)
        self._preview_pool.start(worker)

    def _load_resource_stats(self) -> None:
        """用户打开详情后才统计映射引用，不检验文件内容或存在性。"""
        if self._resource_stats is None and self._current_preview_mapping_data is not None:
            paths = set()
            for group in extract_tree_groups(self._current_preview_mapping_data).values():
                for events in group.get("audioPaths", {}).values():
                    for values in events.values():
                        paths.update(values)
            refs = (
                tuple(filter(None, (self._resolve_event_ref(path) for path in paths)))
                if self._resolve_event_ref
                else self._current_event_audio_refs
            )
            self._resource_stats = collect_tree_stats(self._current_preview_mapping_data, refs)
            self._refresh_resource_info()

    def _refresh_resource_info(self) -> None:
        """按当前实体刷新资源信息快照，明确文件与事件引用的统计单位。"""
        if self._current_preview_entity_type is None or self._current_preview_entity_id is None:
            self.previewPanel.clear_resource_info()
            return

        known_count = len({ref.path for ref in (*self._current_audio_refs, *self._current_event_audio_refs)})
        if self._audio_refs_loaded:
            local_files = f"{len({ref.path for ref in self._current_audio_refs}):,} 个文件（目录索引已完成）"
        elif self._audio_refs_error:
            local_files = f"目录索引失败，当前已确认 {known_count:,} 个文件"
        else:
            local_files = f"目录索引未完成，当前已确认 {known_count:,} 个文件"

        if self._current_preview_mapping_data is None:
            mapping_files = "暂无事件映射"
            references = "暂无事件映射"
            unavailable = "暂无事件映射"
            structure = "暂无事件映射"
        else:
            mapping_files = "按展开的事件读取路径"
            references = "按需加载"
            unavailable = "播放或导出时检查"
            structure = "按需展开"

        details = {
            "当前对象": self._current_preview_entity_name,
            "本地音频文件": local_files,
            "映射可用文件": mapping_files,
            "事件中的可用引用": references,
            "不可用映射项": unavailable,
            "映射结构": structure,
        }
        shared = (self._current_preview_mapping_data or {}).get("sharedAudio", {})
        if shared:
            category_count = sum(len(categories) for categories in shared.values())
            details["皮肤共享来源"] = f"{len(shared)} 个皮肤、{category_count} 个分类复用资源，完整来源见原始数据。"
            details["差异规则"] = "纯继承事件不重复展开；变化事件保留完整音频引用，共享文件只输出一份。"
        diagnostics = (self._current_preview_mapping_data or {}).get("mappingDiagnostics", {})
        copies = diagnostics.get("sharedCopyPaths", [])
        unverified = diagnostics.get("unverifiedSharedPaths", [])
        if copies or unverified:
            details["旧目录核对"] = (
                f"上次映射检查：{len(copies)} 个共享同内容副本，{len(unverified)} 个待核对文件。"
                "文件均保留在全部音频中，具体路径见原始数据；不计为皮肤新增内容。"
            )
        if self._resource_stats is not None:
            stats = self._resource_stats
            details["映射结构"] = f"{stats.skin_count} 分组 · {stats.audio_type_count} 类型 · {stats.event_count} 事件"
            details["映射路径文件"] = str(stats.available_file_count)
            details["映射路径引用"] = str(stats.available_audio_id_count)
        self.previewPanel.set_resource_info(
            details,
            self._current_mapping_path,
            self._current_audio_roots,
        )

    def _ensure_audio_refs(self) -> None:
        """在首次进入全部音频时请求后台枚举，后续切换复用缓存。"""
        if self._preview_loading:
            return
        if self._audio_refs_loaded:
            self._populate_audio_list()
            return
        if self._current_preview_entity_type is None or self._current_preview_entity_id is None:
            return

        request = _AudioRefsRequest(
            token=self._audio_refs_token,
            entity_type=self._current_preview_entity_type,
            entity_id=self._current_preview_entity_id,
        )
        if request.key in self._audio_refs_cache:
            self._audio_refs_cache.move_to_end(request.key)
            self._current_audio_refs = self._audio_refs_cache[request.key]
            self._audio_refs_loaded = True
            self._audio_refs_error = None
            self._audio_refs_progress = None
            self.export_controller.set_available(self._current_audio_refs, complete=True)
            self._refresh_resource_info()
            self._populate_audio_list()
            return

        # 事件页切换实体时保留隐藏模型，避免同步拆除数万行；只有用户真正进入
        # “全部音频”且新实体没有缓存时，才清掉上一实体的可见列表。
        if not self._audio_list_ready and self.audio_list.source_model.rowCount() > 0:
            self.audioPreviewPanel.set_audio_refs((), summary_text="")
        if self._audio_refs_request == request:
            return
        if self._audio_refs_worker is not None:
            self._pending_audio_refs_request = request
            return
        self._start_audio_refs_load(request)

    def _start_audio_refs_load(self, request: _AudioRefsRequest) -> None:
        """把一次全量 WEM 枚举提交到线程池。"""
        if self._app_context is None:
            return

        context = self._app_context
        logger.debug("全部音频索引启动：{} {}", request.entity_type, request.entity_id)
        self._audio_refs_request = request
        self._audio_refs_error = None
        self._audio_refs_progress = AudioIndexProgress(current=0, total=0)
        self.export_controller.set_index_error(False)
        self._audio_refs_worker = TaskWorker(
            lambda signals: EntityDataLoader(context).load_audio_refs(
                request.entity_type,
                request.entity_id,
                progress=signals.progress.emit,
            ),
            pass_signals=True,
        )
        worker = self._audio_refs_worker
        worker.signals.progress.connect(self._on_audio_refs_progress)
        worker.signals.finished.connect(self._on_audio_refs_loaded)
        worker.signals.failed.connect(self._on_audio_refs_failed)
        self._audio_refs_pool.start(worker)

    def _on_audio_refs_progress(self, result: object) -> None:
        """更新当前实体的后台索引进度，隐藏页面只保留状态。"""
        request = self._audio_refs_request
        if not isinstance(result, AudioIndexProgress):
            return
        if request is None or not self._audio_refs_request_is_current(request):
            return
        self._audio_refs_progress = result
        if (
            self.isVisible()
            and (self.preview_mode_pivot.currentRouteKey() or self._active_preview_mode) == ALL_AUDIO_PREVIEW_MODE
        ):
            self._refresh_all_audio_preview()

    def _on_audio_refs_loaded(self, result: object) -> None:
        """接收后台枚举结果，并拒绝覆盖已切换的实体。"""
        request = self._audio_refs_request
        if not isinstance(result, list | tuple) or any(not isinstance(ref, AudioRef) for ref in result):
            self._on_audio_refs_failed("后台索引返回了无效结果，请重新选择实体后重试")
            return
        refs = tuple(result)
        self._audio_refs_worker = None
        self._audio_refs_request = None
        logger.debug("全部音频索引完成：{}，{} 个文件", request, len(refs))
        if request is not None and self._audio_refs_request_is_current(request):
            self._cache_audio_refs(request.key, refs)
            self._current_audio_refs = refs
            self._audio_refs_loaded = True
            self.export_controller.set_available(refs, complete=True)
            self._audio_refs_error = None
            self._audio_refs_progress = None
            self._refresh_resource_info()
            if (
                self.isVisible()
                and (self.preview_mode_pivot.currentRouteKey() or self._active_preview_mode) == ALL_AUDIO_PREVIEW_MODE
            ):
                self._populate_audio_list()
                self._refresh_all_audio_preview()
                self._sync_preview_path()
        self._start_pending_audio_refs_load()

    def _cache_audio_refs(self, key: tuple[str, str], refs: tuple[AudioRef, ...]) -> None:
        """保留最近实体的索引，并限制条目总量，避免大型地图无限占用内存。"""
        self._audio_refs_cache[key] = refs
        self._audio_refs_cache.move_to_end(key)
        while len(self._audio_refs_cache) > 8 or sum(map(len, self._audio_refs_cache.values())) > 100_000:  # noqa: PLR2004
            self._audio_refs_cache.popitem(last=False)

    def _on_audio_refs_failed(self, error: str) -> None:
        """记录当前实体的后台枚举失败，并继续处理最新待加载实体。"""
        request = self._audio_refs_request
        logger.warning("全部音频索引失败：{}，{}", request, error)
        self._audio_refs_worker = None
        self._audio_refs_request = None
        if request is not None and self._audio_refs_request_is_current(request):
            self._audio_refs_error = f"全部音频加载失败：{error}"
            self._audio_refs_progress = None
            self.export_controller.set_index_error(True)
            if self.isVisible():
                self._refresh_all_audio_preview()
            self._refresh_resource_info()
        self._start_pending_audio_refs_load()

    def _start_pending_audio_refs_load(self) -> None:
        """当前 worker 结束后只继续仍为当前实体的最后一次请求。"""
        request = self._pending_audio_refs_request
        self._pending_audio_refs_request = None
        if request is not None and self._audio_refs_request_is_current(request) and not self._audio_refs_loaded:
            self._start_audio_refs_load(request)

    def _audio_refs_request_is_current(self, request: _AudioRefsRequest) -> bool:
        """判断后台结果是否仍属于当前预览实体。"""
        return (
            request.token == self._audio_refs_token
            and request.entity_type == self._current_preview_entity_type
            and request.entity_id == self._current_preview_entity_id
        )

    def _populate_audio_list(self) -> None:
        """只把当前实体的全量 refs 同步到平铺模型一次。"""
        if self._audio_list_ready:
            return
        self.audioPreviewPanel.set_audio_refs(self._current_audio_refs, summary_text="")
        self._audio_list_ready = True

    def _refresh_current_preview_mode(self) -> None:
        """只刷新当前模式所需的筛选模型，避免切换时重置另一视图。"""
        mode_key = self.preview_mode_pivot.currentRouteKey() or EVENT_PREVIEW_MODE
        if mode_key == EVENT_PREVIEW_MODE:
            self._refresh_audio_preview_tree()
        elif mode_key == ALL_AUDIO_PREVIEW_MODE:
            self._refresh_all_audio_preview()

    def _set_splitter_sizes_evenly(self) -> None:
        """在页面宽度已知时将左右面板收敛到更适合缩放的宽度比例。"""
        total_width = self.splitter.width()
        if total_width <= 0:
            return

        left_min_width = 280
        right_min_width = 190
        left_preferred_width = min(max(total_width // 2, left_min_width), 580)
        max_left_width = max(total_width - right_min_width, left_min_width)
        left_width = min(left_preferred_width, max_left_width)
        left_width = max(left_width, min(left_min_width, total_width))
        right_width = max(total_width - left_width, 0)
        self.splitter.setSizes([left_width, right_width])

    def _update_catalog_subtitle(self) -> None:
        """按当前一级目录更新总览说明，避免隐藏特殊内容可用性边界。"""
        entity_type = self._current_entity_type()
        if entity_type == "special" and self._app_context is None:
            self.subtitle_label.setText("正在准备特殊内容数据。")
            return
        labels = {
            "champions": "查看英雄状态，选好后可直接发送到执行中心。",
            "maps": "查看地图状态，选好后可直接发送到执行中心。",
            "special": "经典召唤师峡谷等模式使用当前客户端资源，选择英雄后可直接发送到执行中心。",
        }
        self.subtitle_label.setText(labels.get(entity_type, "查看实体状态，选好后可直接发送到执行中心。"))

    def _update_catalog_search_placeholder(self) -> None:
        """按当前目录收窄搜索提示，说明各目录实际可检索字段。"""
        placeholders = {
            "champions": "搜索英雄、别名或 ID",
            "maps": "搜索地图、别名或 ID",
            "special": "搜索模式、英雄、别名或资源包",
        }
        self.search_input.setPlaceholderText(placeholders.get(self._current_entity_type(), "搜索实体名称或 ID"))

    def _resolve_preview_target(self) -> Path | None:
        """按当前预览方式返回可安全打开的单一资源目标。"""
        mode_key = self.preview_mode_pivot.currentRouteKey() or EVENT_PREVIEW_MODE
        if mode_key in {EVENT_PREVIEW_MODE, RAW_PREVIEW_MODE}:
            return self._current_mapping_path
        selected_ref = self._selected_audio_refs[ALL_AUDIO_PREVIEW_MODE]
        if selected_ref is not None:
            return selected_ref.path
        if len(self._current_audio_roots) == 1:
            return self._current_audio_roots[0]
        return None

    def _sync_preview_path(self) -> None:
        """按当前模式更新路径栏、打开目标与工具提示。"""
        mode_key = self.preview_mode_pivot.currentRouteKey() or EVENT_PREVIEW_MODE
        target_path = self._resolve_preview_target()
        if target_path is not None:
            self.previewPanel.set_preview_path(build_preview_path_text(target_path))
            self.preview_path_edit.setCursorPosition(0)
            if mode_key == ALL_AUDIO_PREVIEW_MODE:
                self.reveal_file_btn.setToolTip("打开当前 WEM 所在位置" if target_path.is_file() else "打开音频目录")
            else:
                self.reveal_file_btn.setToolTip("打开映射文件位置")
            self.reveal_file_btn.setEnabled(True)
            return

        if mode_key == ALL_AUDIO_PREVIEW_MODE and self._current_audio_roots:
            paths_text = "；".join(str(path) for path in self._current_audio_roots)
            self.previewPanel.set_preview_path(f"多个音频目录：{paths_text}")
            self.reveal_file_btn.setToolTip("当前实体包含多个音频目录，请先选择一条音频。")
        else:
            self.previewPanel.set_preview_path("")
            self.reveal_file_btn.setToolTip("当前模式没有可打开的资源。")
        self.reveal_file_btn.setEnabled(False)

    def _reveal_current_preview_target(self) -> None:
        """打开当前预览模式对应的精确文件或音频目录。"""
        target_path = self._resolve_preview_target()
        if target_path is None:
            return
        if self._reveal_file_path(target_path):
            return

        directory = target_path if target_path.is_dir() else target_path.parent
        InfoBar.warning(
            "打开目录失败",
            f"无法打开目录：{directory}",
            parent=self.window(),
            position=InfoBarPosition.TOP,
        )

    def _open_resource_info_source(self, source_path: object) -> None:
        """复用现有路径边界打开资源信息中的映射来源。"""
        if not isinstance(source_path, Path):
            return
        if self._reveal_file_path(source_path):
            return
        InfoBar.warning(
            "打开映射来源失败",
            f"无法打开目录：{source_path.parent}",
            parent=self.window(),
            position=InfoBarPosition.TOP,
        )

    def _reveal_file_path(self, target_path: Path) -> bool:
        """在系统文件管理器中定位指定文件。"""
        if target_path.is_dir():
            return QDesktopServices.openUrl(QUrl.fromLocalFile(str(target_path)))

        directory = target_path.parent
        try:
            if os.name == "nt" and target_path.exists():
                subprocess.Popen(["explorer.exe", "/select,", str(target_path)])
                return True
        except OSError:
            pass

        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def set_smooth_scroll_enabled(self, enabled: bool) -> None:
        """根据设置应用总览页的滚动模式。"""
        for list_widget in self._entity_lists.values():
            apply_smooth_scroll_enabled(list_widget, enabled)
        apply_smooth_scroll_enabled(self.text_preview, enabled)
        apply_smooth_scroll_enabled(self.audio_preview_tree, enabled)
        apply_smooth_scroll_enabled(self.audio_list, enabled)
