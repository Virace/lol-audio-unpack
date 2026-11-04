"""执行中心使用的单卡片任务创建表单。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CardGroupWidget,
    CheckBox,
    ComboBox,
    HeaderCardWidget,
    IconWidget,
    InfoBarIcon,
    PrimaryPushButton,
    SegmentedWidget,
    SimpleCardWidget,
    StrongBodyLabel,
    TransparentToolButton,
    isDarkTheme,
    qconfig,
)
from qfluentwidgets import (
    FluentIcon as FIF,
)

from lol_audio_unpack.app.resource_pack import ResourcePackWadRef
from lol_audio_unpack.app.targets import split_ids
from lol_audio_unpack.gui.common.font_compat import apply_tool_button_safe_font
from lol_audio_unpack.gui.common.styles import (
    build_fluent_panel_frame_theme_pair,
    get_fluent_frame_stroke_pair,
)
from lol_audio_unpack.gui.task_models import (
    AppContextInputSnapshot,
    ExecutionTaskDraft,
    ExecutionTaskParamsSnapshot,
)
from lol_audio_unpack.gui.theme import get_accent_text_color_pair, get_semantic_text_color_pair

from .scope_input import ScopeInput

_WIDE_FORM_LAYOUT_MIN_WIDTH = 980
_FORM_ROW_HEIGHT = 78


def _parse_csv_ids(text: str) -> tuple[str, ...]:
    """将逗号分隔的 ID 输入解析为字符串元组。"""
    return split_ids(text)


def _parse_csv_int_ids(text: str, *, label: str) -> tuple[int, ...] | None:
    """将逗号分隔的数字 ID 输入解析为整数元组。"""
    raw_ids = _parse_csv_ids(text)
    if not raw_ids:
        return None

    try:
        if any(not entity_id.isdecimal() for entity_id in raw_ids):
            raise ValueError("ID 必须是非负整数")
        return tuple(dict.fromkeys(int(entity_id) for entity_id in raw_ids))
    except ValueError as exc:
        raise ValueError(f"{label} 仅支持用中英文逗号分隔的非负整数。") from exc


def _build_target_summary(
    champion_ids: tuple[str, ...],
    map_ids: tuple[str, ...],
    special_targets: tuple[str, ...] = (),
    special_target_names: tuple[str, ...] = (),
    modes: tuple[str, str] = ("none", "none"),
) -> str:
    """构造当前目标范围摘要。"""
    parts = []
    for label, ids, mode in zip(("英雄", "地图"), (champion_ids, map_ids), modes, strict=True):
        if mode == "none":
            continue
        scope = {"all": "全部", "ids": f"指定 {len(ids)} 个"}[mode]
        parts.append(f"{label}：{scope}")
    if special_targets:
        parts.append(f"特殊内容 {len(special_targets)} 个")
    summary = "，".join(parts) or "尚未选择处理对象"
    return f"{summary}（{'、'.join(special_target_names)}）" if special_target_names else summary


def _build_task_scope_summary(
    *,
    include_preflight_update: bool,
    include_extract: bool,
    include_wav: bool,
    include_mapping: bool,
) -> str:
    """构造当前任务包含的执行步骤摘要。"""
    parts: list[str] = []
    if include_extract:
        parts.append("音频解包")
    if include_wav:
        parts.append("音频转码")
    if include_mapping:
        parts.append("事件映射")
    if not parts:
        return "未选择执行内容"
    if include_preflight_update:
        parts.insert(0, "前置强制更新")
    return " + ".join(parts)


@dataclass(slots=True, frozen=True)
class _ExecutionTaskFormDefaults:
    """任务表单默认值。"""

    vo_filter_key: str = "VO"
    max_workers_text: str = "4"
    lobby_audio: bool = True
    force_update: bool = False
    integrate_data: bool = True
    wav_enabled: bool = False
    wav_format: str = "pcm16"


@dataclass(slots=True, frozen=True)
class _ExecutionTaskFormState:
    """任务表单状态快照。"""

    champion_ids: tuple[str, ...] = ()
    map_ids: tuple[str, ...] = ()
    modes: tuple[str, str] = ("none", "none")
    special_targets: tuple[str, ...] = ()
    special_target_names: tuple[str, ...] = ()
    include_extract: bool = True
    include_mapping: bool = True
    vo_filter_key: str = "VO"
    max_workers_text: str = "4"
    lobby_audio: bool = True
    force_update: bool = False
    integrate_data: bool = True
    wav_enabled: bool = False
    wav_format: str = "pcm16"
    resource_pack_wads: tuple[ResourcePackWadRef, ...] = ()

    def target_summary(self) -> str:
        """返回当前目标范围摘要。"""
        return _build_target_summary(
            self.champion_ids,
            self.map_ids,
            self.special_targets,
            self.special_target_names,
            self.modes,
        )

    def task_scope_summary(self) -> str:
        """返回当前勾选的任务步骤摘要。"""
        return _build_task_scope_summary(
            include_preflight_update=self.force_update,
            include_extract=self.include_extract,
            include_wav=self.wav_enabled,
            include_mapping=self.include_mapping,
        )


class TaskCreationCard(HeaderCardWidget):
    """承载执行中心自定义参数与任务创建按钮。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        """初始化任务创建卡片。"""
        super().__init__(parent)
        self.setTitle("自定义参数")
        self.setBorderRadius(8)
        self._defaults = _ExecutionTaskFormDefaults()
        self._state = _ExecutionTaskFormState()
        self._synced_selection: dict[str, Any] = self._build_empty_synced_selection()
        self._wide_layout_enabled: bool | None = None
        self._build_layout_shell()
        self._build_form_controls()
        self._build_action_controls()
        self.apply_defaults()
        self.refresh_summary()
        qconfig.themeChanged.connect(self._refresh_theme_styles)
        qconfig.themeColorChanged.connect(self._refresh_theme_styles)
        self.destroyed.connect(self._disconnect_theme_signals)
        self._refresh_theme_styles()

    def _build_layout_shell(self) -> None:
        """创建执行范围、参数与底部操作区的布局容器。"""
        self.viewLayout.setContentsMargins(0, 0, 0, 0)
        self.viewLayout.setSpacing(0)
        self.form_layout = QVBoxLayout()
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        self.form_layout.setSpacing(0)
        self.viewLayout.addLayout(self.form_layout)

        self.scope_section_label = StrongBodyLabel("执行范围", self)
        self.scope_section_label.setContentsMargins(24, 14, 24, 4)
        self.form_layout.addWidget(self.scope_section_label)

        self.scope_grid = QGridLayout()
        self.scope_grid.setContentsMargins(0, 0, 0, 0)
        self.scope_grid.setHorizontalSpacing(0)
        self.scope_grid.setVerticalSpacing(0)
        self.form_layout.addLayout(self.scope_grid)

        self.scope_hint_widget = QFrame(self)
        self.scope_hint_widget.setObjectName("ExecutionScopeHint")
        self.scope_hint_widget.setMinimumHeight(48)
        scope_hint_layout = QHBoxLayout(self.scope_hint_widget)
        scope_hint_layout.setContentsMargins(14, 12, 14, 12)
        scope_hint_layout.setSpacing(10)
        self.scope_hint_icon = IconWidget(InfoBarIcon.INFORMATION, self.scope_hint_widget)
        self.scope_hint_icon.setFixedSize(16, 16)
        self.scope_hint_label = BodyLabel(
            "每类可独立选择不处理、全部或指定 ID；支持中英文逗号分隔。",
            self.scope_hint_widget,
        )
        self.scope_hint_label.setWordWrap(True)
        scope_hint_layout.addWidget(self.scope_hint_icon, 0, Qt.AlignmentFlag.AlignVCenter)
        scope_hint_layout.addWidget(self.scope_hint_label, 1)
        scope_hint_shell = QWidget(self)
        scope_hint_shell_layout = QHBoxLayout(scope_hint_shell)
        scope_hint_shell_layout.setContentsMargins(24, 8, 24, 14)
        scope_hint_shell_layout.addWidget(self.scope_hint_widget)
        self.form_layout.addWidget(scope_hint_shell)

        self.parameter_section_label = StrongBodyLabel("处理参数", self)
        self.parameter_section_label.setContentsMargins(24, 10, 24, 4)
        self.form_layout.addWidget(self.parameter_section_label)

        self.parameter_grid = QGridLayout()
        self.parameter_grid.setContentsMargins(0, 0, 0, 0)
        self.parameter_grid.setHorizontalSpacing(0)
        self.parameter_grid.setVerticalSpacing(0)
        self.form_layout.addLayout(self.parameter_grid)

    def _build_form_controls(self) -> None:
        """创建全部参数输入控件与分组。"""
        self.champion_scope = ScopeInput("英雄", self)
        self.map_scope = ScopeInput("地图", self)
        self.champion_ids_input = self.champion_scope.edit
        self.map_ids_input = self.map_scope.edit

        self.vo_filter = SegmentedWidget(self)
        self.vo_filter.addItem("VO", "仅 VO")
        self.vo_filter.addItem("ALL", "全部类型")
        self.vo_filter.setCurrentItem("VO")
        self.vo_filter.setFixedWidth(180)
        self.vo_filter.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.max_workers_combo = ComboBox(self)
        self.max_workers_combo.addItems(["1", "2", "4", "8", "16", "32", "64"])
        self.max_workers_combo.setCurrentText("4")
        self.max_workers_combo.setFixedWidth(120)
        self.max_workers_combo.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self.lobby_audioice_cb = CheckBox("启用", self)
        self.force_update_cb = CheckBox("启用", self)
        self.integrate_data_cb = CheckBox("启用", self)
        self.wav_format_combo = ComboBox(self)
        self.wav_format_combo.addItems(["auto", "pcm16", "pcm24", "pcm32", "float"])
        self.wav_format_combo.setCurrentText("pcm16")
        self.wav_format_combo.setMinimumWidth(140)
        self.wav_format_combo.setSizePolicy(QSizePolicy.Policy.MinimumExpanding, QSizePolicy.Policy.Fixed)
        self.wav_format_combo.setVisible(True)

        self.wav_format_row = QWidget(self)
        self.wav_format_row.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        wav_format_layout = QHBoxLayout(self.wav_format_row)
        wav_format_layout.setContentsMargins(0, 0, 0, 0)
        wav_format_layout.setSpacing(8)
        wav_format_layout.addWidget(self.wav_format_combo)
        wav_format_layout.addStretch(1)

        self.scope_groups = (
            self._create_option_group(
                FIF.PEOPLE,
                "英雄",
                "选择全部或指定英雄 ID",
                self.champion_scope,
                stretch=1,
            ),
            self._create_option_group(
                FIF.GLOBE,
                "地图",
                "选择全部或指定地图 ID",
                self.map_scope,
                stretch=1,
            ),
        )
        self.parameter_groups = (
            self._create_option_group(
                FIF.FILTER,
                "音频范围",
                "默认只处理 VO，需要时可切换为全部类型",
                self.vo_filter,
                stretch=1,
            ),
            self._create_option_group(
                FIF.SPEED_HIGH,
                "并发数",
                "一般不建议超过 CPU 线程数",
                self.max_workers_combo,
            ),
            self._create_option_group(FIF.MUSIC, "附加大厅音频", "默认同时处理大厅音频", self.lobby_audioice_cb),
            self._create_option_group(FIF.ALBUM, "采样格式", "仅在启用音频转码时生效", self.wav_format_row),
            self._create_option_group(
                FIF.SYNC,
                "前置强制更新",
                "执行前强制刷新基础数据",
                self.force_update_cb,
            ),
            self._create_option_group(
                FIF.INFO,
                "整合数据文件",
                "映射时额外生成整合数据",
                self.integrate_data_cb,
            ),
        )
        self._update_responsive_layout(force=True)

    def _create_option_group(
        self,
        icon,
        title: str,
        content: str,
        widget: QWidget,
        *,
        stretch: int = 0,
    ) -> CardGroupWidget:
        """创建一个可放入响应式网格的参数行。"""
        group = CardGroupWidget(icon, title, content, self)
        group.addWidget(widget, stretch=stretch)
        group.setFixedHeight(_FORM_ROW_HEIGHT)
        group.setSeparatorVisible(False)
        return group

    def _build_action_controls(self) -> None:
        """创建任务范围摘要与底部操作控件。"""
        self.hintIcon = IconWidget(InfoBarIcon.INFORMATION, self)
        self.target_summary_value = BodyLabel("尚未选择处理对象", self)
        self.target_summary_value.setWordWrap(True)
        self.extract_task_cb = CheckBox("音频解包", self)
        self.extract_task_cb.setChecked(True)
        self.wav_task_cb = CheckBox("音频转码", self)
        self.mapping_task_cb = CheckBox("事件映射", self)
        self.mapping_task_cb.setChecked(True)
        self.restore_defaults_btn = TransparentToolButton(FIF.SYNC, self)
        self.restore_defaults_btn.setToolTip("恢复默认值")
        self.restore_defaults_btn.setFixedSize(32, 32)
        apply_tool_button_safe_font(self.restore_defaults_btn)
        self.create_task_btn = PrimaryPushButton("创建任务", self)
        self.restore_defaults_btn.clicked.connect(self.reset_custom_inputs_to_defaults)

    def create_action_card(self, parent: QWidget | None = None) -> SimpleCardWidget:
        """创建独立于参数卡片的底部任务操作卡。"""
        action_card = SimpleCardWidget(parent)
        action_card.setBorderRadius(8)

        self.bottom_toolbar_widget = action_card
        self.bottom_toolbar_layout = QHBoxLayout(action_card)
        self.bottom_toolbar_layout.setSpacing(10)
        self.bottom_toolbar_layout.setContentsMargins(24, 15, 24, 20)
        self.bottom_toolbar_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.hintIcon.setFixedSize(16, 16)
        self.bottom_toolbar_layout.addWidget(self.hintIcon, 0, Qt.AlignmentFlag.AlignLeft)
        self.bottom_toolbar_layout.addWidget(self.target_summary_value, 1)
        self.bottom_toolbar_layout.addWidget(self.extract_task_cb, 0, Qt.AlignmentFlag.AlignRight)
        self.bottom_toolbar_layout.addWidget(self.wav_task_cb, 0, Qt.AlignmentFlag.AlignRight)
        self.bottom_toolbar_layout.addWidget(self.mapping_task_cb, 0, Qt.AlignmentFlag.AlignRight)
        self.bottom_toolbar_layout.addWidget(self.restore_defaults_btn, 0, Qt.AlignmentFlag.AlignRight)
        self.bottom_toolbar_layout.addWidget(self.create_task_btn, 0, Qt.AlignmentFlag.AlignRight)
        return action_card

    def resizeEvent(self, event) -> None:
        """根据卡片宽度在两列与单列参数布局间切换。"""
        super().resizeEvent(event)
        self._update_responsive_layout()

    def _update_responsive_layout(self, *, force: bool = False) -> None:
        """在宽屏两列与窄屏单列之间重排参数控件。"""
        if not hasattr(self, "scope_groups"):
            return
        use_wide_layout = self.width() >= _WIDE_FORM_LAYOUT_MIN_WIDTH
        if not force and self._wide_layout_enabled == use_wide_layout:
            return
        self._wide_layout_enabled = use_wide_layout
        self._clear_row_separators()

        if use_wide_layout:
            for column, group in enumerate(self.scope_groups):
                self.scope_grid.addWidget(group, 0, column)
            self.scope_grid.addWidget(self._create_row_separator(), 1, 0, 1, 2)
            for index, group in enumerate(self.parameter_groups):
                logical_row = index // 2
                grid_row = logical_row * 2
                self.parameter_grid.addWidget(group, grid_row, index % 2)
            for logical_row in range(2):
                self.parameter_grid.addWidget(
                    self._create_row_separator(),
                    logical_row * 2 + 1,
                    0,
                    1,
                    2,
                )
            self.scope_grid.setColumnStretch(0, 1)
            self.scope_grid.setColumnStretch(1, 1)
            self.parameter_grid.setColumnStretch(0, 1)
            self.parameter_grid.setColumnStretch(1, 1)
            self._refresh_row_separator_styles()
            return

        for logical_row, group in enumerate(self.scope_groups):
            grid_row = logical_row * 2
            self.scope_grid.addWidget(group, grid_row, 0)
            if logical_row < len(self.scope_groups) - 1:
                self.scope_grid.addWidget(self._create_row_separator(), grid_row + 1, 0)
        for logical_row, group in enumerate(self.parameter_groups):
            grid_row = logical_row * 2
            self.parameter_grid.addWidget(group, grid_row, 0)
            if logical_row < len(self.parameter_groups) - 1:
                self.parameter_grid.addWidget(self._create_row_separator(), grid_row + 1, 0)
        self.scope_grid.setColumnStretch(0, 1)
        self.scope_grid.setColumnStretch(1, 0)
        self.parameter_grid.setColumnStretch(0, 1)
        self.parameter_grid.setColumnStretch(1, 0)
        self._refresh_row_separator_styles()

    def _clear_row_separators(self) -> None:
        """删除响应式重排前已创建的网格行级分割线。"""
        for shell in getattr(self, "_row_separator_shells", []):
            shell.deleteLater()
        self._row_separator_shells: list[QWidget] = []
        self._row_separators: list[QFrame] = []

    def _create_row_separator(self) -> QWidget:
        """创建与参数内容左右边界对齐的独立分割线。"""
        shell = QWidget(self)
        shell.setFixedHeight(1)
        shell_layout = QHBoxLayout(shell)
        shell_layout.setContentsMargins(24, 0, 24, 0)
        shell_layout.setSpacing(0)

        separator = QFrame(shell)
        separator.setObjectName("ExecutionFormRowSeparator")
        separator.setFixedHeight(1)
        shell_layout.addWidget(separator)
        self._row_separator_shells.append(shell)
        self._row_separators.append(separator)
        return shell

    def _refresh_theme_styles(self, *_args: object) -> None:
        """刷新分区标题与范围提示的强调色。"""
        accent_light, accent_dark = get_accent_text_color_pair()
        info_light, info_dark = get_semantic_text_color_pair("info")
        for label in (self.scope_section_label, self.parameter_section_label):
            label.setTextColor(accent_light, accent_dark)
        self.scope_hint_label.setTextColor(info_light, info_dark)
        self.scope_hint_icon.setIcon(FIF.INFO.icon(color=info_dark if isDarkTheme() else info_light))
        hint_light_qss, hint_dark_qss = build_fluent_panel_frame_theme_pair(
            "QFrame#ExecutionScopeHint",
            background_kind="subtle_idle",
            border_radius=8,
        )
        self.scope_hint_widget.setStyleSheet(hint_dark_qss if isDarkTheme() else hint_light_qss)
        self._refresh_row_separator_styles()

    def _refresh_row_separator_styles(self) -> None:
        """在响应式重排后立即应用当前主题的分割线颜色。"""
        light_stroke, dark_stroke = get_fluent_frame_stroke_pair()
        row_stroke = dark_stroke if isDarkTheme() else light_stroke
        for separator in self._row_separators:
            separator.setStyleSheet(
                f"QFrame#ExecutionFormRowSeparator {{ background-color: {row_stroke}; border: none; }}"
            )

    def _disconnect_theme_signals(self, *_args: object) -> None:
        """释放任务表单持有的全局主题信号连接。"""
        for signal in (qconfig.themeChanged, qconfig.themeColorChanged):
            try:
                signal.disconnect(self._refresh_theme_styles)
            except (RuntimeError, TypeError):
                pass

    def _build_empty_synced_selection(self) -> dict[str, Any]:
        """返回空的总览同步状态。"""
        return {
            "source": "未同步",
            "champion_ids": (),
            "map_ids": (),
            "special_targets": (),
            "special_target_names": (),
            "resource_pack_wads": (),
            "summary": "尚未从实体总览同步选择。",
        }

    def connect_form_signals(self, callback) -> None:
        """连接任务表单相关控件信号。"""
        self.extract_task_cb.stateChanged.connect(callback)
        self.wav_task_cb.stateChanged.connect(callback)
        self.mapping_task_cb.stateChanged.connect(callback)
        self.champion_scope.changed.connect(callback)
        self.map_scope.changed.connect(callback)
        self.vo_filter.currentItemChanged.connect(callback)
        self.max_workers_combo.currentTextChanged.connect(callback)
        self.lobby_audioice_cb.stateChanged.connect(callback)
        self.wav_format_combo.currentTextChanged.connect(callback)
        self.force_update_cb.stateChanged.connect(callback)
        self.integrate_data_cb.stateChanged.connect(callback)

    def apply_defaults(self) -> None:
        """将默认值应用到任务表单控件。"""
        defaults = self._defaults
        self.extract_task_cb.setChecked(True)
        self.wav_task_cb.setChecked(defaults.wav_enabled)
        self.mapping_task_cb.setChecked(True)
        self.vo_filter.setCurrentItem(defaults.vo_filter_key)
        self.max_workers_combo.setCurrentText(defaults.max_workers_text)
        self.lobby_audioice_cb.setChecked(defaults.lobby_audio)
        self.wav_format_combo.setCurrentText(defaults.wav_format)
        self.force_update_cb.setChecked(defaults.force_update)
        self.integrate_data_cb.setChecked(defaults.integrate_data)
        self._sync_wav_control_state()

    def apply_gui_config_defaults(self, gui_config) -> None:
        """根据当前 GUI 配置更新任务表单默认值。"""
        self._defaults = _ExecutionTaskFormDefaults(
            vo_filter_key=self._defaults.vo_filter_key,
            max_workers_text=self._defaults.max_workers_text,
            lobby_audio=self._defaults.lobby_audio,
            force_update=self._defaults.force_update,
            integrate_data=self._defaults.integrate_data,
            wav_enabled=False,
            wav_format=str(getattr(gui_config, "wav_format", "pcm16") or "pcm16"),
        )

    def _sync_wav_control_state(self) -> bool:
        """同步 WAV 控件状态，并返回当前是否启用 WAV。"""
        wav_enabled = bool(self.wav_task_cb.isChecked())
        self.set_wav_control_state(wav_enabled=wav_enabled)
        return wav_enabled

    def set_wav_control_state(self, *, wav_enabled: bool) -> None:
        """同步 WAV 开关与采样格式控件的可见性。"""
        self.wav_format_combo.setVisible(True)
        self.wav_format_combo.setEnabled(wav_enabled)

    def sync_state_from_widgets(self) -> None:
        """从当前控件值同步内部任务表单状态。"""
        include_extract = self.extract_task_cb.isChecked()
        wav_enabled = self._sync_wav_control_state()
        champion_ids = self.champion_scope.tokens()
        map_ids = self.map_scope.tokens()
        self._state = _ExecutionTaskFormState(
            champion_ids=champion_ids,
            map_ids=map_ids,
            modes=(self.champion_scope.mode, self.map_scope.mode),
            special_targets=tuple(self._synced_selection["special_targets"]),
            special_target_names=tuple(self._synced_selection["special_target_names"]),
            resource_pack_wads=tuple(self._synced_selection["resource_pack_wads"]),
            include_extract=include_extract,
            include_mapping=self.mapping_task_cb.isChecked(),
            vo_filter_key=self.vo_filter.currentRouteKey() or self._defaults.vo_filter_key,
            max_workers_text=self.max_workers_combo.currentText(),
            lobby_audio=self.lobby_audioice_cb.isChecked(),
            force_update=self.force_update_cb.isChecked(),
            integrate_data=self.integrate_data_cb.isChecked(),
            wav_enabled=wav_enabled,
            wav_format=self.wav_format_combo.currentText() or self._defaults.wav_format,
        )
        self.refresh_summary()

    def refresh_summary(self) -> None:
        """刷新底部执行范围摘要文案。"""
        self.target_summary_value.setText(f"执行范围：{self._state.target_summary()}")

    def current_target_ids(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """返回当前任务目标中的英雄和地图 ID。"""
        return self._state.champion_ids, self._state.map_ids

    def current_modes(self) -> tuple[str, str]:
        """返回两个类别的独立范围模式。"""
        return self._state.modes

    def current_special_targets(self) -> tuple[str, ...]:
        """返回当前同步的特殊内容稳定 key。"""
        return self._state.special_targets

    def current_special_target_names(self) -> tuple[str, ...]:
        """返回当前特殊内容的本地化展示名称。"""
        return self._state.special_target_names

    def current_resource_pack_wads(self) -> tuple[ResourcePackWadRef, ...]:
        """返回当前特殊内容携带的来源 WAD 快照。"""
        return self._state.resource_pack_wads

    def current_selection_source(self) -> str:
        """返回当前任务输入的来源标识。"""
        champion_ids, map_ids = self.current_target_ids()
        if (
            self._synced_selection["source"] != "未同步"
            and champion_ids == tuple(self._synced_selection["champion_ids"])
            and map_ids == tuple(self._synced_selection["map_ids"])
            and self._state.special_targets == tuple(self._synced_selection["special_targets"])
            and self._state.modes == tuple(self._synced_selection.get("modes", ("none", "none")))
        ):
            return str(self._synced_selection["source"])
        if self._state.modes != ("none", "none"):
            return "manual_input"
        return "default_scope"

    def selected_task_scope_summary(self) -> str:
        """返回当前选中的执行步骤摘要。"""
        return self._state.task_scope_summary()

    def current_task_config_summary(self) -> str:
        """构造当前任务配置摘要。"""
        state = self._state
        return (
            f"{state.task_scope_summary()} · 范围={state.target_summary()} · "
            f"VO={state.vo_filter_key} · "
            f"大厅音频={state.lobby_audio} · "
            f"采样格式={state.wav_format if state.wav_enabled else '未启用'} · "
            f"前置强制更新={state.force_update} · "
            f"整合={state.integrate_data} · "
            f"并发={state.max_workers_text}"
        )

    def build_task_draft(self, *, gui_config) -> ExecutionTaskDraft:
        """根据当前表单状态构造任务草稿。"""
        state = self._state
        ids = []
        for mode, tokens, label in zip(state.modes, (state.champion_ids, state.map_ids), ("英雄", "地图"), strict=True):
            parsed = _parse_csv_int_ids(",".join(tokens), label=f"{label} ID") if mode == "ids" else None
            if mode == "ids" and not parsed:
                raise ValueError(f"请填写指定的{label} ID，或切换范围模式。")
            ids.append(() if mode == "none" else parsed)
        champion_ids, map_ids = ids
        special_targets = state.special_targets
        if state.modes == ("none", "none") and not special_targets:
            raise ValueError("请至少选择英雄、地图或特殊内容中的一个范围。")
        exclude_types = ("SFX", "MUSIC") if state.vo_filter_key == "VO" else ()
        wav_workers = int(getattr(gui_config, "wav_workers", 2) if gui_config else 2)
        wav_timeout = int(getattr(gui_config, "wav_timeout", 5) if gui_config else 5)
        wav_retries = int(getattr(gui_config, "wav_retries", 3) if gui_config else 3)
        return ExecutionTaskDraft(
            source=self.current_selection_source(),
            source_summary=state.target_summary(),
            context_input=(gui_config.to_app_context_input_snapshot() if gui_config else AppContextInputSnapshot()),
            task_params=ExecutionTaskParamsSnapshot(
                champion_ids=champion_ids,
                map_ids=map_ids,
                special_targets=special_targets,
                resource_pack_wads=state.resource_pack_wads,
                run_update=state.force_update,
                run_extract=state.include_extract,
                run_mapping=state.include_mapping,
                max_workers=int(state.max_workers_text),
                lobby_audio=state.lobby_audio,
                exclude_types=exclude_types,
                integrate_data=state.integrate_data,
                wav_enabled=state.wav_enabled,
                wav_workers=wav_workers,
                wav_timeout=wav_timeout,
                wav_retries=wav_retries,
                wav_format=state.wav_format,
                wav_group_by_event=bool(getattr(gui_config, "wav_group_by_event", False)),
            ),
        )

    def reset_custom_inputs_to_defaults(self) -> None:
        """将自定义输入恢复到默认状态，同时保留执行步骤选择。"""
        current_state = self._state
        defaults = self._defaults
        self._synced_selection = self._build_empty_synced_selection()
        self._state = _ExecutionTaskFormState(
            champion_ids=(),
            map_ids=(),
            special_targets=(),
            special_target_names=(),
            resource_pack_wads=(),
            include_extract=current_state.include_extract,
            include_mapping=current_state.include_mapping,
            vo_filter_key=defaults.vo_filter_key,
            max_workers_text=defaults.max_workers_text,
            lobby_audio=defaults.lobby_audio,
            force_update=defaults.force_update,
            integrate_data=defaults.integrate_data,
            wav_enabled=defaults.wav_enabled,
            wav_format=defaults.wav_format,
        )
        # 批量重置期间不允许控件信号把尚未重置的旧值写回表单状态。
        blockers = [
            QSignalBlocker(widget)
            for widget in (
                self.champion_scope,
                self.map_scope,
                self.extract_task_cb,
                self.wav_task_cb,
                self.mapping_task_cb,
                self.vo_filter,
                self.max_workers_combo,
                self.lobby_audioice_cb,
                self.wav_format_combo,
                self.force_update_cb,
                self.integrate_data_cb,
            )
        ]
        self.champion_scope.reset()
        self.map_scope.reset()
        self.extract_task_cb.setChecked(self._state.include_extract)
        self.wav_task_cb.setChecked(self._state.wav_enabled)
        self.mapping_task_cb.setChecked(self._state.include_mapping)
        self.vo_filter.setCurrentItem(self._state.vo_filter_key)
        self.max_workers_combo.setCurrentText(self._state.max_workers_text)
        self.lobby_audioice_cb.setChecked(self._state.lobby_audio)
        self.wav_format_combo.setCurrentText(self._state.wav_format)
        self.force_update_cb.setChecked(self._state.force_update)
        self.integrate_data_cb.setChecked(self._state.integrate_data)
        del blockers
        self._sync_wav_control_state()
        self.refresh_summary()

    def apply_selected_entities(  # noqa: PLR0913
        self,
        *,
        champion_ids: tuple[str, ...],
        map_ids: tuple[str, ...],
        source: str,
        summary: str,
        special_targets: tuple[str, ...] = (),
        special_target_names: tuple[str, ...] = (),
        resource_pack_wads: tuple[ResourcePackWadRef, ...] = (),
        modes: tuple[str, str] | None = None,
    ) -> None:
        """将实体总览选择应用到任务表单。"""
        modes = modes or ("ids" if champion_ids else "none", "ids" if map_ids else "none")
        if any(
            mode not in ScopeInput.MODES or (mode != "ids" and ids)
            for mode, ids in zip(modes, (champion_ids, map_ids), strict=True)
        ):
            raise ValueError("同步范围与 ID 不一致。")
        self._synced_selection = {
            "source": source,
            "champion_ids": champion_ids,
            "map_ids": map_ids,
            "special_targets": special_targets,
            "special_target_names": special_target_names,
            "resource_pack_wads": tuple(dict.fromkeys(resource_pack_wads)),
            "summary": summary,
            "modes": modes,
        }
        blockers = [QSignalBlocker(self.champion_scope), QSignalBlocker(self.map_scope)]
        self.champion_scope.set_scope(modes[0], ",".join(champion_ids))
        self.map_scope.set_scope(modes[1], ",".join(map_ids))
        del blockers
        self.sync_state_from_widgets()
