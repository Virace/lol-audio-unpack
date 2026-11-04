"""关于页面展示品牌信息、版本信息与相关链接。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger
from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, QSize, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    IconWidget,
    ImageLabel,
    MessageBoxBase,
    SimpleCardWidget,
    SmoothScrollArea,
    TitleLabel,
    isDarkTheme,
    qconfig,
)
from qfluentwidgets import FluentIcon as FIF
from qfluentwidgets.common.icon import drawIcon

from lol_audio_unpack import __version__
from lol_audio_unpack.gui.common.page_style import (
    PAGE_CONTENT_MARGIN,
    apply_page_content_margins,
    configure_transparent_scroll_page,
)
from lol_audio_unpack.gui.common.styles import (
    get_fluent_frame_stroke_pair,
    get_fluent_neutral_surface_pair,
)
from lol_audio_unpack.gui.resources import ImageAsset, SvgAsset, assets

AUTHOR_URL = "https://x-item.com"
REPOSITORY_URL = "https://github.com/Virace/lol_audio_unpack"
BILIBILI_URL = "https://space.bilibili.com/12353537"
TECH_STACK = ("Python", "PySide6", "QFluentWidgets")
ABOUT_HERO_CARD_HEIGHT = 326
ABOUT_ACTION_CARD_SIZE = QSize(188, 224)
ABOUT_ACTION_CARD_SPACING = 16
ABOUT_TAG_ROW_HEIGHT = 40
ABOUT_SECTION_SPACING = 24


def _rgba_text(rgba: tuple[int, int, int, int]) -> str:
    """将 RGBA 元组转换为 QSS 可用的 `rgba(...)` 字符串。"""
    red, green, blue, alpha = rgba
    return f"rgba({red}, {green}, {blue}, {alpha})"


def _configure_label_font(
    label: QWidget,
    *,
    pixel_size: int,
    weight: QFont.Weight = QFont.Weight.Normal,
) -> None:
    """配置单个标签的字号与字重。"""
    font = label.font()
    font.setPixelSize(pixel_size)
    font.setWeight(weight)
    label.setFont(font)


def get_minimum_shell_size() -> QSize:
    """返回 About 页固定构图要求的最小窗口尺寸。"""
    cards_width = ABOUT_ACTION_CARD_SIZE.width() * 4 + ABOUT_ACTION_CARD_SPACING * 3
    content_width = cards_width + PAGE_CONTENT_MARGIN * 2
    content_height = (
        PAGE_CONTENT_MARGIN * 2
        + ABOUT_HERO_CARD_HEIGHT
        + ABOUT_SECTION_SPACING
        + ABOUT_TAG_ROW_HEIGHT
        + ABOUT_SECTION_SPACING
        + ABOUT_ACTION_CARD_SIZE.height()
    )
    return QSize(content_width, content_height)


@dataclass(frozen=True, slots=True)
class AboutActionSpec:
    """关于页底部动作卡片定义。"""

    object_name: str
    icon: object
    title: str
    value: str
    helper: str
    url: str | None = None
    on_click: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class SponsorQrSpec:
    """赞助弹窗中的二维码卡片定义。"""

    object_name: str
    title: str
    image: ImageAsset


SPONSOR_QR_SPECS = (
    SponsorQrSpec(
        object_name="SponsorDialogWechatCard",
        title="微信支付",
        image=assets.images.wechat_qr(),
    ),
    SponsorQrSpec(
        object_name="SponsorDialogAlipayCard",
        title="支付宝",
        image=assets.images.alipay_qr(),
    ),
)


class AboutRotatingLogoWidget(QWidget):
    """支持轻微旋转动画的关于页 Logo。"""

    def __init__(self, parent=None) -> None:
        """初始化 Logo 控件。"""
        super().__init__(parent)
        self.setObjectName("AboutPageLogo")
        self._rotation_angle = 0.0
        self._renderer = QSvgRenderer(self)

    def load_logo(self, logo: SvgAsset) -> None:
        """加载 SVG Logo 资源。

        Args:
            logo: 应用 logo 资源对象。
        """

        logo.load_into(self._renderer)
        self.update()

    def is_logo_ready(self) -> bool:
        """返回当前 Logo 是否加载成功。"""
        return self._renderer.isValid()

    def get_rotation_angle(self) -> float:
        """返回当前旋转角度。"""
        return self._rotation_angle

    def set_rotation_angle(self, value: float) -> None:
        """更新当前旋转角度。"""
        self._rotation_angle = float(value)
        self.update()

    def paintEvent(self, event) -> None:
        """按当前角度绘制 SVG Logo。"""
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        center = self.rect().center()
        painter.translate(center)
        painter.rotate(self._rotation_angle)
        painter.translate(-center)
        self._renderer.render(painter, QRectF(self.rect()))

    rotationAngle = Property(float, get_rotation_angle, set_rotation_angle)


class HoverLogoShell(QFrame):
    """带轻微旋转 hover 动画的 Logo 容器。"""

    def __init__(self, parent=None) -> None:
        """初始化 Logo 容器。"""
        super().__init__(parent)
        self.setObjectName("AboutLogoShell")
        self.setFixedSize(176, 176)
        self._base_size = 132

        self.logo_widget = AboutRotatingLogoWidget(self)
        self._update_logo_geometry()

        self._rotation_animation = QPropertyAnimation(self.logo_widget, b"rotationAngle", self)
        self._rotation_animation.setDuration(220)
        self._rotation_animation.setEasingCurve(QEasingCurve.OutCubic)

    def set_logo(self, logo: SvgAsset) -> None:
        """设置 SVG logo 资源。

        Args:
            logo: 应用 logo 资源对象。
        """

        self.logo_widget.load_logo(logo)

    def enterEvent(self, event) -> None:
        """鼠标进入时轻微旋转 logo。"""
        super().enterEvent(event)
        self._animate_rotation(4.0)

    def leaveEvent(self, event) -> None:
        """鼠标离开时恢复 logo 角度。"""
        super().leaveEvent(event)
        self._animate_rotation(0.0)

    def _animate_rotation(self, target: float) -> None:
        """启动中心旋转动画。"""
        self._rotation_animation.stop()
        self._rotation_animation.setStartValue(self.logo_widget.rotationAngle)
        self._rotation_animation.setEndValue(target)
        self._rotation_animation.start()

    def resizeEvent(self, event) -> None:
        """容器尺寸变化时保持 logo 居中。"""
        super().resizeEvent(event)
        self._update_logo_geometry()

    def _update_logo_geometry(self) -> None:
        """保持 logo 居中。"""
        size = self._base_size
        x = (self.width() - size) // 2
        y = (self.height() - size) // 2
        self.logo_widget.setGeometry(x, y, size, size)


class FaShakeIconWidget(IconWidget):
    """复刻 Font Awesome `fa-shake` 的图标控件。"""

    def __init__(self, icon, parent=None) -> None:
        """初始化图标控件。"""
        super().__init__(parent)
        self.setIcon(icon)
        self._rotation_angle = 0.0
        self._shake_animation = QPropertyAnimation(self, b"rotationAngle", self)
        self._shake_animation.setDuration(1000)
        self._shake_animation.setEasingCurve(QEasingCurve.Linear)
        self._configure_fa_shake_keyframes()

    def _configure_fa_shake_keyframes(self) -> None:
        """配置与 Font Awesome 对齐的 `fa-shake` 关键帧。"""
        for progress, angle in (
            (0.00, -15.0),
            (0.04, 15.0),
            (0.08, -18.0),
            (0.12, 18.0),
            (0.16, -22.0),
            (0.20, 22.0),
            (0.24, -18.0),
            (0.28, 18.0),
            (0.32, -12.0),
            (0.36, 12.0),
            (0.40, 0.0),
            (1.00, 0.0),
        ):
            self._shake_animation.setKeyValueAt(progress, angle)

    def play_shake(self) -> None:
        """重新播放一次 shake 动画。"""
        if self._shake_animation.state() == QPropertyAnimation.Running:
            return

        self._shake_animation.stop()
        self._shake_animation.start()

    def get_rotation_angle(self) -> float:
        """返回当前旋转角度。"""
        return self._rotation_angle

    def set_rotation_angle(self, angle: float) -> None:
        """更新当前旋转角度并重绘。"""
        self._rotation_angle = float(angle)
        self.update()

    def paintEvent(self, event) -> None:
        """在绘制图标时应用旋转。"""
        painter = QPainter(self)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        center = self.rect().center()
        painter.translate(center)
        painter.rotate(self._rotation_angle)
        painter.translate(-center)
        drawIcon(self._icon, painter, self.rect())

    rotationAngle = Property(float, get_rotation_angle, set_rotation_angle)


class AboutActionIconShell(QFrame):
    """底部卡片使用的静态图标容器。"""

    def __init__(self, *, icon, animated: bool = True, parent=None) -> None:
        """初始化图标容器。"""
        super().__init__(parent)
        self.setFixedSize(76, 76)
        self._base_size = 24

        icon_widget_class = FaShakeIconWidget if animated else IconWidget
        self.icon_widget = icon_widget_class(icon, self)
        self.icon_widget.setStyleSheet("background: transparent;")
        self._update_icon_geometry()

    def resizeEvent(self, event) -> None:
        """尺寸变化时保持图标居中。"""
        super().resizeEvent(event)
        self._update_icon_geometry()

    def _update_icon_geometry(self) -> None:
        """更新图标几何位置。"""
        size = self._base_size
        x = int((self.width() - size) / 2)
        y = int((self.height() - size) / 2)
        self.icon_widget.setGeometry(x, y, size, size)


class SponsorDialog(MessageBoxBase):
    """展示赞助二维码的模态框。"""

    def __init__(self, parent=None) -> None:
        """初始化赞助弹窗。

        Args:
            parent: 父级窗口或容器。
        """
        super().__init__(parent=parent)
        self.setObjectName("SponsorDialog")
        self.widget.setObjectName("SponsorDialogContent")
        self.widget.setAttribute(Qt.WA_StyledBackground, True)
        self.buttonGroup.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle("赞助支持")

        self.viewLayout.setSpacing(20)
        self._build_ui()
        self._apply_theme_styles()

        self.cancelButton.hide()
        self.buttonLayout.removeWidget(self.cancelButton)
        self.buttonLayout.insertStretch(0, 1)
        self.buttonLayout.addStretch(1)
        self.yesButton.setText("关闭")
        self.yesButton.setFixedWidth(132)
        self.buttonGroup.setMinimumWidth(180)

        size_hint = self.widget.sizeHint()
        self.widget.setFixedSize(max(560, size_hint.width()), size_hint.height())

    def _build_ui(self) -> None:
        """构建赞助弹窗的主体内容。"""
        hero_icon_shell = AboutActionIconShell(icon=FIF.HEART, animated=False, parent=self.widget)
        hero_icon_shell.setObjectName("SponsorDialogHeroIconShell")

        title_label = TitleLabel("赞助支持", self.widget)
        title_label.setObjectName("SponsorDialogTitle")
        title_label.setAlignment(Qt.AlignHCenter)

        description_label = BodyLabel(
            "如果您觉得本工具对您有帮助，欢迎扫码请作者喝杯咖啡！您的支持是我持续维护项目的最大动力。",
            self.widget,
        )
        description_label.setObjectName("SponsorDialogDescription")
        description_label.setWordWrap(False)
        description_label.setAlignment(Qt.AlignHCenter)

        qr_row = QWidget(self.widget)
        qr_row.setObjectName("SponsorDialogQrRow")
        qr_layout = QHBoxLayout(qr_row)
        qr_layout.setContentsMargins(0, 0, 0, 0)
        qr_layout.setSpacing(16)
        qr_layout.addStretch(1)

        for spec in SPONSOR_QR_SPECS:
            qr_layout.addWidget(self._build_qr_card(spec))

        qr_layout.addStretch(1)

        self.viewLayout.addWidget(hero_icon_shell, 0, Qt.AlignHCenter)
        self.viewLayout.addWidget(title_label, 0, Qt.AlignHCenter)
        self.viewLayout.addWidget(description_label, 0, Qt.AlignHCenter)
        self.viewLayout.addWidget(qr_row)

    def _build_qr_card(self, spec: SponsorQrSpec) -> SimpleCardWidget:
        """构建单个二维码卡片。"""
        card = SimpleCardWidget(self.widget)
        card.setObjectName(spec.object_name)
        card.setProperty("sponsorRole", "qrCard")
        card.setBorderRadius(18)
        card.setFixedWidth(196)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        image_label = ImageLabel(card)
        image_label.setObjectName(f"{spec.object_name}Image")
        spec.image.apply_to(image_label)
        image_label.setScaledSize(QSize(160, 160))
        image_label.setFixedSize(160, 160)
        image_label.setBorderRadius(14, 14, 14, 14)

        title_label = BodyLabel(spec.title, card)
        title_label.setObjectName(f"{spec.object_name}Title")
        title_label.setAlignment(Qt.AlignHCenter)
        _configure_label_font(title_label, pixel_size=15, weight=QFont.Weight.DemiBold)

        layout.addWidget(image_label, 0, Qt.AlignHCenter)
        layout.addWidget(title_label)

        return card

    def _apply_theme_styles(self) -> None:
        """按当前主题应用赞助弹窗的局部样式。"""
        light_idle_surface, dark_idle_surface = get_fluent_neutral_surface_pair("subtle_idle")
        light_hover_surface, dark_hover_surface = get_fluent_neutral_surface_pair("subtle_hover")
        light_border, dark_border = get_fluent_frame_stroke_pair()

        if isDarkTheme():
            panel_background = "rgb(28, 31, 38)"
            card_background = _rgba_text(dark_idle_surface)
            card_hover_background = _rgba_text(dark_hover_surface)
            frame_border = dark_border
            description_color = "rgba(255, 255, 255, 168)"
        else:
            panel_background = "rgb(255, 255, 255)"
            card_background = _rgba_text(light_idle_surface)
            card_hover_background = _rgba_text(light_hover_surface)
            frame_border = light_border
            description_color = "rgba(36, 36, 36, 140)"

        self.widget.setStyleSheet(
            f"""
            QWidget#SponsorDialogContent {{
                background: {panel_background};
                border: 1px solid {frame_border};
                border-radius: 28px;
            }}
            QFrame#SponsorDialogHeroIconShell {{
                background: rgba(255, 79, 141, 0.12);
                border-radius: 16px;
            }}
            QFrame#buttonGroup {{
                background: transparent;
                border: none;
            }}
            BodyLabel#SponsorDialogDescription {{
                color: {description_color};
            }}
            SimpleCardWidget[sponsorRole="qrCard"] {{
                background: {card_background};
                border: 1px solid {frame_border};
                border-radius: 18px;
            }}
            SimpleCardWidget[sponsorRole="qrCard"]:hover {{
                background: {card_hover_background};
                border: 1px solid {frame_border};
            }}
            """
        )


class AboutActionCard(SimpleCardWidget):
    """关于页底部的入口卡片。"""

    def __init__(self, spec: AboutActionSpec, parent=None) -> None:
        """初始化入口卡片。"""
        super().__init__(parent)
        self._url = spec.url
        self._on_click = spec.on_click
        self.setObjectName(spec.object_name)
        self.setFixedSize(ABOUT_ACTION_CARD_SIZE)
        self.setBorderRadius(20)
        self.setCursor(Qt.PointingHandCursor if (spec.url or spec.on_click) else Qt.ArrowCursor)
        self.setProperty("aboutRole", "action")
        self._animated_icon_widget: FaShakeIconWidget | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 16)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignTop | Qt.AlignHCenter)

        icon_shell = AboutActionIconShell(
            icon=spec.icon,
            animated=True,
            parent=self,
        )
        icon_shell.setObjectName(f"{spec.object_name}IconShell")
        icon_shell.icon_widget.setObjectName(f"{spec.object_name}Icon")
        if isinstance(icon_shell.icon_widget, FaShakeIconWidget):
            self._animated_icon_widget = icon_shell.icon_widget

        title_label = CaptionLabel(spec.title, self)
        title_label.setObjectName(f"{spec.object_name}Title")
        title_label.setAlignment(Qt.AlignHCenter)
        value_label = BodyLabel(spec.value, self)
        value_label.setObjectName(f"{spec.object_name}Value")
        value_label.setAlignment(Qt.AlignHCenter)
        helper_label = CaptionLabel(spec.helper, self)
        helper_label.setObjectName(f"{spec.object_name}Helper")
        helper_label.setWordWrap(True)
        helper_label.setAlignment(Qt.AlignHCenter)

        title_label.setTextColor(QColor(36, 36, 36, 140), QColor(255, 255, 255, 170))
        helper_label.setTextColor(QColor(36, 36, 36, 112), QColor(255, 255, 255, 128))
        value_label.setTextColor(QColor(17, 17, 17), QColor(255, 255, 255))
        _configure_label_font(title_label, pixel_size=12)
        _configure_label_font(helper_label, pixel_size=12)
        _configure_label_font(value_label, pixel_size=20, weight=QFont.Weight.Bold)

        layout.addWidget(icon_shell, alignment=Qt.AlignHCenter)
        layout.addSpacing(22)
        layout.addWidget(title_label)
        layout.addSpacing(8)
        layout.addWidget(value_label)
        layout.addSpacing(10)
        layout.addWidget(helper_label)
        layout.addStretch(1)

    def enterEvent(self, event) -> None:
        """鼠标进入整张卡片时触发图标 shake。"""
        super().enterEvent(event)
        if self._animated_icon_widget is not None:
            self._animated_icon_widget.play_shake()

    def mouseReleaseEvent(self, event) -> None:
        """整卡点击时打开目标链接。"""
        super().mouseReleaseEvent(event)
        if event.button() != Qt.LeftButton:
            return

        if self._on_click is not None:
            self._on_click()
            return

        if self._url:
            QDesktopServices.openUrl(QUrl(self._url))


class AboutPage(SmoothScrollArea):
    """展示项目品牌信息、版本信息与外部链接。"""

    def __init__(self, parent=None):
        """初始化关于页面。

        Args:
            parent: 父级窗口或容器。
        """
        super().__init__(parent=parent)
        self.view = configure_transparent_scroll_page(
            self,
            page_object_name="AboutPage",
            view_object_name="AboutPageView",
        )
        qconfig.themeChanged.connect(self._refresh_theme_styles)
        self.destroyed.connect(self._disconnect_theme_refresh_listener)

        self._build_ui()

    def _refresh_theme_styles(self, *_args: object) -> None:
        """按当前主题刷新关于页的容器样式。"""
        self.view.setStyleSheet(self._build_view_stylesheet())

    def _disconnect_theme_refresh_listener(self, *_args: object) -> None:
        """断开关于页注册的全局主题监听。"""
        try:
            qconfig.themeChanged.disconnect(self._refresh_theme_styles)
        except (RuntimeError, TypeError):
            pass

    def _build_view_stylesheet(self) -> str:
        """根据当前主题构造关于页的样式表。"""
        light_idle_surface, dark_idle_surface = get_fluent_neutral_surface_pair("subtle_idle")
        light_hover_surface, dark_hover_surface = get_fluent_neutral_surface_pair("subtle_hover")
        light_border, dark_border = get_fluent_frame_stroke_pair()

        if isDarkTheme():
            action_background = _rgba_text(dark_idle_surface)
            action_hover_background = _rgba_text(dark_hover_surface)
            frame_border = dark_border
            accent_hover_border = "rgba(132, 168, 255, 68)"
        else:
            action_background = _rgba_text(light_idle_surface)
            action_hover_background = _rgba_text(light_hover_surface)
            frame_border = light_border
            accent_hover_border = "rgba(87, 132, 255, 48)"

        return f"""
        QWidget#AboutPageView {{
            background: transparent;
        }}
        QFrame#AboutHeroCard {{
            background: transparent;
            border: none;
        }}
        SimpleCardWidget[aboutRole="action"] {{
            background: {action_background};
            border: 1px solid {frame_border};
            border-radius: 20px;
        }}
        SimpleCardWidget[aboutRole="action"]:hover {{
            background: {action_hover_background};
            border: 1px solid {accent_hover_border};
        }}
        QFrame#AboutLogoShell {{
            background: transparent;
            border: none;
            border-radius: 34px;
        }}
        QFrame[aboutRole="pill"] {{
            background: {action_background};
            border: 1px solid {frame_border};
            border-radius: 14px;
        }}
        CaptionLabel#AboutPageSubtitle {{
            color: rgba(255, 255, 255, 0.72);
        }}
        QFrame#AboutActionCardAuthorIconShell {{
            background: rgba(70, 110, 255, 0.12);
            border-radius: 16px;
        }}
        QFrame#AboutActionCardRepositoryIconShell {{
            background: rgba(0, 0, 0, 0.08);
            border-radius: 16px;
        }}
        QFrame#AboutActionCardBilibiliIconShell {{
            background: rgba(0, 180, 255, 0.12);
            border-radius: 16px;
        }}
        QFrame#AboutActionCardSponsorIconShell {{
            background: rgba(255, 79, 141, 0.12);
            border-radius: 16px;
        }}
        """

    def _build_ui(self):
        """构建关于页面的布局内容。"""
        root_layout = QVBoxLayout(self.view)
        apply_page_content_margins(root_layout)
        root_layout.setSpacing(ABOUT_SECTION_SPACING)
        self._refresh_theme_styles()

        hero_card = QFrame(self.view)
        hero_card.setObjectName("AboutHeroCard")
        hero_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        hero_card.setFixedHeight(ABOUT_HERO_CARD_HEIGHT)
        hero_layout = QVBoxLayout(hero_card)
        hero_layout.setContentsMargins(32, 10, 32, 12)
        hero_layout.setSpacing(6)
        hero_layout.setAlignment(Qt.AlignHCenter)

        try:
            logo = assets.app.logo_svg()
        except FileNotFoundError as exc:
            logger.warning("未找到 About 页使用的 Logo 资源: {}", exc)
            logo = None

        if logo is not None:
            logo_shell = HoverLogoShell(hero_card)
            logo_shell.set_logo(logo)
            hero_layout.addWidget(logo_shell, alignment=Qt.AlignHCenter)
            hero_layout.addSpacing(34)

        hero_title = TitleLabel("Lol Audio Unpack", hero_card)
        hero_title.setObjectName("AboutHeroTitle")
        hero_title.setAlignment(Qt.AlignHCenter)
        hero_layout.addWidget(hero_title)

        hero_version = CaptionLabel(__version__, hero_card)
        hero_version.setAlignment(Qt.AlignHCenter)
        hero_layout.addWidget(hero_version)

        hero_description = BodyLabel("英雄联盟音频提取与事件映射工具", hero_card)
        hero_description.setAlignment(Qt.AlignHCenter)
        hero_description.setWordWrap(True)
        hero_layout.addWidget(hero_description)

        root_layout.addWidget(hero_card)

        tags_row = QWidget(self.view)
        tags_row.setFixedHeight(ABOUT_TAG_ROW_HEIGHT)
        tags_layout = QHBoxLayout(tags_row)
        tags_layout.setContentsMargins(0, 0, 0, 0)
        tags_layout.setSpacing(10)
        tags_layout.addStretch(1)
        for tech in TECH_STACK:
            pill = QFrame(self.view)
            pill.setProperty("aboutRole", "pill")
            pill_layout = QHBoxLayout(pill)
            pill_layout.setContentsMargins(14, 8, 14, 8)
            pill_layout.setSpacing(0)
            pill_layout.addWidget(CaptionLabel(tech, pill))
            tags_layout.addWidget(pill)
        tags_layout.addStretch(1)
        root_layout.addWidget(tags_row)

        cards_row = QWidget(self.view)
        cards_layout = QHBoxLayout(cards_row)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(ABOUT_ACTION_CARD_SPACING)
        cards_layout.addStretch(1)

        action_specs = (
            AboutActionSpec(
                object_name="AboutActionCardAuthor",
                icon=FIF.PEOPLE,
                title="作者",
                value="Virace",
                helper="个人主页",
                url=AUTHOR_URL,
            ),
            AboutActionSpec(
                object_name="AboutActionCardRepository",
                icon=FIF.GITHUB,
                title="仓库地址",
                value="GitHub",
                helper="查看代码",
                url=REPOSITORY_URL,
            ),
            AboutActionSpec(
                object_name="AboutActionCardBilibili",
                icon=assets.icons.BILIBILI.colored("#242424", "#F5F5F5"),
                title="B站",
                value="Virace",
                helper="频道入口",
                url=BILIBILI_URL,
            ),
            AboutActionSpec(
                object_name="AboutActionCardSponsor",
                icon=FIF.HEART,
                title="赞助支持",
                value="支持作者",
                helper="赞助入口",
                on_click=self._show_sponsor_dialog,
            ),
        )
        for spec in action_specs:
            cards_layout.addWidget(AboutActionCard(spec, cards_row))
        cards_layout.addStretch(1)
        root_layout.addWidget(cards_row)

        id_notice = BodyLabel(
            "跨版本音频说明：音频 ID 和事件名可能随游戏版本变化，不能作为跨版本的唯一对照。\n"
            "ID 相同不代表音频相同；ID 或文件数据变化，也不一定代表台词或听感变化。请结合实际试听判断。",
            self.view,
        )
        id_notice.setWordWrap(True)
        id_notice.setAlignment(Qt.AlignHCenter)
        root_layout.addWidget(id_notice)
        root_layout.addStretch(1)

    def _show_sponsor_dialog(self) -> None:
        """弹出赞助二维码模态框。"""
        SponsorDialog(self.window()).exec()
