from __future__ import annotations

from dataclasses import dataclass
from string import Template

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QWidget


@dataclass(frozen=True, slots=True)
class ThemeTokens:
    """Semantic colours shared by the stylesheet, palette and status cells."""

    name: str
    window: str
    surface: str
    surface_alt: str
    border: str
    border_strong: str
    text: str
    muted: str
    muted_strong: str
    accent: str
    accent_hover: str
    accent_soft: str
    accent_text: str
    danger: str
    danger_soft: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    info: str
    info_soft: str
    disabled_text: str
    disabled_bg: str
    header: str
    selection: str
    selection_text: str
    progress_bg: str
    icon: str
    icon_disabled: str
    status: dict[str, str]


LIGHT = ThemeTokens(
    name="light",
    window="#F6F8FB",
    surface="#FFFFFF",
    surface_alt="#F8FAFC",
    border="#D8E0EA",
    border_strong="#CBD5E1",
    text="#0F172A",
    muted="#64748B",
    muted_strong="#334155",
    accent="#2563EB",
    accent_hover="#1D4ED8",
    accent_soft="#EAF1FB",
    accent_text="#FFFFFF",
    danger="#B91C1C",
    danger_soft="#FEE2E2",
    success="#15803D",
    success_soft="#DCFCE7",
    warning="#B45309",
    warning_soft="#FEF3C7",
    info="#0E7490",
    info_soft="#CFFAFE",
    disabled_text="#94A3B8",
    disabled_bg="#F1F5F9",
    header="#EEF3F8",
    selection="#DCEBFF",
    selection_text="#0F172A",
    progress_bg="#E8EEF6",
    icon="#1E3A5F",
    icon_disabled="#94A3B8",
    status={
        "waiting_analysis": "#64748B",
        "analyzing": "#7C3AED",
        "encoding": "#2563EB",
        "validating": "#0E7490",
        "done": "#15803D",
        "failed": "#B91C1C",
        "needs_decision": "#B45309",
        "cancelled": "#7C6F00",
        "skipped": "#64748B",
        "awaiting_start": "#64748B",
        "queued": "#64748B",
    },
)

DARK = ThemeTokens(
    name="dark",
    window="#16181D",
    surface="#1E2128",
    surface_alt="#232730",
    border="#303642",
    border_strong="#3C4452",
    text="#E6EAF2",
    muted="#9AA4B2",
    muted_strong="#C3CAD6",
    accent="#5B9CFF",
    accent_hover="#7FB2FF",
    accent_soft="#1F2C44",
    accent_text="#08111F",
    danger="#FF6B6B",
    danger_soft="#3A1D22",
    success="#5BD68A",
    success_soft="#17301F",
    warning="#F2B35B",
    warning_soft="#3A2C15",
    info="#6BD5E8",
    info_soft="#14323A",
    disabled_text="#6B7482",
    disabled_bg="#232730",
    header="#232730",
    selection="#24406B",
    selection_text="#E6EAF2",
    progress_bg="#2A303B",
    icon="#C9D6EA",
    icon_disabled="#6B7482",
    status={
        "waiting_analysis": "#9AA4B2",
        "analyzing": "#B79CFF",
        "encoding": "#7FB2FF",
        "validating": "#6BD5E8",
        "done": "#5BD68A",
        "failed": "#FF8080",
        "needs_decision": "#F2B35B",
        "cancelled": "#D6C56A",
        "skipped": "#9AA4B2",
        "awaiting_start": "#9AA4B2",
        "queued": "#9AA4B2",
    },
)


_STYLESHEET = Template(
    """
    /* QualityBound theme: $name */
    QWidget {
        color: $text;
    }
    QMainWindow, QDialog {
        background: $window;
    }
    QWidget#centralContent {
        background: $window;
    }
    QWidget#comparisonWindow {
        background: $window;
    }
    QLabel#comparePlaceholder {
        background: #000000;
        color: #CBD5E1;
        font-size: 14px;
    }
    QScrollArea {
        background: $window;
        border: none;
    }
    QLabel {
        background: transparent;
        color: $text;
    }
    QLabel:disabled {
        color: $disabled_text;
    }
    QLabel#summaryTitle {
        color: $muted;
        font-size: 12px;
        font-weight: 500;
    }
    QLabel#summaryValue {
        color: $text;
        font-size: 13px;
        font-weight: 700;
    }
    QLabel#statusPill {
        border-radius: 10px;
        padding: 4px 10px;
        font-weight: 600;
    }
    QLabel#statusPill[state="idle"] {
        background: $surface_alt;
        color: $muted_strong;
        border: 1px solid $border;
    }
    QLabel#statusPill[state="running"] {
        background: $accent_soft;
        color: $accent;
        border: 1px solid $accent;
    }
    QLabel#statusPill[state="pause_after_current"],
    QLabel#statusPill[state="paused"] {
        background: $warning_soft;
        color: $warning;
        border: 1px solid $warning;
    }
    QLabel#statusPill[state="awaiting_decision"] {
        background: $warning_soft;
        color: $warning;
        border: 1px solid $warning;
    }
    QLabel#statusPill[state="failed"],
    QLabel#statusPill[state="cancelled"] {
        background: $danger_soft;
        color: $danger;
        border: 1px solid $danger;
    }
    QLabel#setupSummary {
        color: $muted;
    }
    QLabel[invalid="true"], QLabel#setupSummary[invalid="true"] {
        color: $danger;
    }
    QLabel#emptyTitle {
        color: $text;
        font-size: 17px;
        font-weight: 700;
    }
    QLabel#emptyBody {
        color: $muted;
    }
    QLabel#dropOverlay {
        background: $accent_soft;
        border: 2px dashed $accent;
        border-radius: 10px;
        color: $accent;
        font-size: 16px;
        font-weight: 600;
    }
    QLabel#decisionTitle {
        color: $text;
        font-size: 15px;
        font-weight: 700;
    }
    QLabel#decisionFile {
        color: $muted_strong;
        font-weight: 600;
    }
    QLabel#decisionDetail {
        color: $muted;
    }
    QGroupBox {
        background: $surface;
        border: 1px solid $border;
        border-radius: 8px;
        margin-top: 14px;
        padding: 12px 10px 10px 10px;
        font-weight: 600;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 12px;
        padding: 0 6px;
        color: $icon;
    }
    QFrame#statCard {
        background: $surface_alt;
        border: 1px solid $border;
        border-radius: 6px;
    }
    QFrame#detailsPanel {
        background: $surface;
        border: 1px solid $border;
        border-radius: 6px;
    }
    QFrame#emptyState {
        background: $surface;
        border: 1px dashed $border_strong;
        border-radius: 8px;
    }
    QFrame#banner {
        border-radius: 6px;
        border: 1px solid $border;
    }
    QFrame#banner[severity="info"] {
        background: $accent_soft;
        border-color: $accent;
    }
    QFrame#banner[severity="success"] {
        background: $success_soft;
        border-color: $success;
    }
    QFrame#banner[severity="warning"] {
        background: $warning_soft;
        border-color: $warning;
    }
    QFrame#banner[severity="error"] {
        background: $danger_soft;
        border-color: $danger;
    }
    QLabel#bannerText {
        color: $text;
        font-weight: 600;
    }
    QToolBar {
        background: $surface;
        border-bottom: 1px solid $border;
        spacing: 4px;
        padding: 6px;
    }
    QToolButton {
        border: 1px solid transparent;
        border-radius: 6px;
        padding: 6px 9px;
        color: $text;
        background: transparent;
    }
    QToolButton:hover {
        background: $accent_soft;
        border-color: $border_strong;
    }
    QToolButton:pressed {
        background: $selection;
    }
    QToolButton:focus {
        border: 1px solid $accent;
    }
    QToolButton:disabled {
        color: $disabled_text;
        background: transparent;
    }
    QToolButton#primaryAction {
        background: $accent;
        border-color: $accent;
        color: $accent_text;
        font-weight: 600;
    }
    QToolButton#primaryAction:hover {
        background: $accent_hover;
        border-color: $accent_hover;
    }
    QToolButton#primaryAction:disabled {
        background: $disabled_bg;
        border-color: $border;
        color: $disabled_text;
    }
    QToolButton#dangerAction {
        color: $danger;
        border-color: $border;
    }
    QToolButton#dangerAction:hover {
        background: $danger_soft;
        border-color: $danger;
    }
    QToolButton#dangerAction:disabled {
        color: $disabled_text;
        border-color: transparent;
    }
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
        background: $surface;
        border: 1px solid $border_strong;
        border-radius: 5px;
        min-height: 28px;
        padding: 3px 7px;
        selection-background-color: $accent;
        selection-color: $accent_text;
    }
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
        border: 1px solid $accent;
    }
    QLineEdit[invalid="true"], QComboBox[invalid="true"] {
        border: 1px solid $danger;
    }
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
        background: $disabled_bg;
        color: $disabled_text;
    }
    QComboBox QAbstractItemView {
        background: $surface;
        color: $text;
        border: 1px solid $border;
        selection-background-color: $accent_soft;
        selection-color: $text;
    }
    QPushButton {
        background: $surface_alt;
        border: 1px solid $border_strong;
        border-radius: 5px;
        min-height: 28px;
        padding: 4px 10px;
        color: $text;
    }
    QPushButton:hover {
        background: $accent_soft;
        border-color: $accent;
    }
    QPushButton:pressed {
        background: $selection;
    }
    QPushButton:focus {
        border: 1px solid $accent;
    }
    QPushButton:disabled {
        color: $disabled_text;
        background: $disabled_bg;
        border-color: $border;
    }
    QPushButton[role="primary"] {
        background: $accent;
        border-color: $accent;
        color: $accent_text;
        font-weight: 600;
    }
    QPushButton[role="primary"]:hover {
        background: $accent_hover;
        border-color: $accent_hover;
    }
    QPushButton[role="primary"]:disabled {
        background: $disabled_bg;
        color: $disabled_text;
        border-color: $border;
    }
    QPushButton[role="danger"] {
        color: $danger;
        border-color: $danger;
    }
    QPushButton[role="danger"]:hover {
        background: $danger_soft;
    }
    QPushButton[role="danger"]:disabled {
        color: $disabled_text;
        border-color: $border;
    }
    QCheckBox, QRadioButton {
        color: $text;
        spacing: 6px;
    }
    QCheckBox:disabled, QRadioButton:disabled {
        color: $disabled_text;
    }
    QCheckBox:focus, QRadioButton:focus {
        color: $accent;
    }
    QTabWidget::pane {
        border: 1px solid $border;
        border-radius: 8px;
        background: $surface;
        top: -1px;
    }
    QTabBar::tab {
        background: $header;
        border: 1px solid $border;
        border-bottom: none;
        border-top-left-radius: 6px;
        border-top-right-radius: 6px;
        padding: 7px 14px;
        margin-right: 2px;
        color: $muted_strong;
    }
    QTabBar::tab:selected {
        background: $surface;
        color: $text;
        font-weight: 600;
    }
    QTabBar::tab:focus {
        color: $accent;
    }
    QProgressBar {
        background: $progress_bg;
        border: 1px solid $border;
        border-radius: 5px;
        min-height: 14px;
        text-align: center;
        color: $text;
    }
    QProgressBar::chunk {
        background: $accent;
        border-radius: 4px;
    }
    QProgressBar[over="true"]::chunk {
        background: $danger;
    }
    QTableView {
        background: $surface;
        alternate-background-color: $surface_alt;
        border: 1px solid $border;
        border-radius: 6px;
        gridline-color: $border;
        selection-background-color: $selection;
        selection-color: $selection_text;
    }
    QTableView::item {
        padding: 2px 4px;
    }
    QTableView:focus {
        border: 1px solid $accent;
    }
    QHeaderView::section {
        background: $header;
        color: $muted_strong;
        border: none;
        border-right: 1px solid $border;
        border-bottom: 1px solid $border;
        padding: 6px 8px;
        font-weight: 600;
    }
    QTableCornerButton::section {
        background: $header;
        border: none;
    }
    QStatusBar {
        background: $surface;
        border-top: 1px solid $border;
        color: $muted_strong;
    }
    QMenu {
        background: $surface;
        color: $text;
        border: 1px solid $border;
        padding: 4px;
    }
    QMenu::item {
        padding: 5px 22px;
        border-radius: 4px;
    }
    QMenu::item:selected {
        background: $accent_soft;
        color: $text;
    }
    QMenu::item:disabled {
        color: $disabled_text;
    }
    QToolTip {
        background: $surface;
        color: $text;
        border: 1px solid $border;
        padding: 4px;
    }
    QPlainTextEdit, QTextEdit, QListWidget {
        background: $surface;
        color: $text;
        border: 1px solid $border;
        border-radius: 6px;
        selection-background-color: $accent;
        selection-color: $accent_text;
    }
    QScrollBar:vertical {
        background: $window;
        width: 12px;
        margin: 0;
    }
    QScrollBar::handle:vertical {
        background: $border_strong;
        border-radius: 5px;
        min-height: 24px;
    }
    QScrollBar::handle:vertical:hover {
        background: $muted;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
        background: transparent;
        height: 0;
    }
    QScrollBar:horizontal {
        background: $window;
        height: 12px;
        margin: 0;
    }
    QScrollBar::handle:horizontal {
        background: $border_strong;
        border-radius: 5px;
        min-width: 24px;
    }
    QScrollBar::handle:horizontal:hover {
        background: $muted;
    }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {
        background: transparent;
        width: 0;
    }
    QSplitter::handle {
        background: $border;
    }
    QSplitter#workspaceSplitter::handle:vertical {
        background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
            stop:0 $window, stop:0.4 $window, stop:0.5 $border,
            stop:0.6 $window, stop:1 $window);
    }
    QSplitter#workspaceSplitter::handle:vertical:hover {
        background: $accent_soft;
    }
    QDialogButtonBox QPushButton {
        min-width: 72px;
    }
    """
)


def _scheme_is_dark() -> bool:
    app = QApplication.instance()
    if app is None:
        return False
    hints = QGuiApplication.styleHints()
    scheme = hints.colorScheme()
    if scheme == Qt.ColorScheme.Dark:
        return True
    if scheme == Qt.ColorScheme.Light:
        return False
    # Older platforms can report Unknown; fall back to the palette luminance.
    window = QGuiApplication.palette().color(QPalette.ColorRole.Window)
    return window.lightness() < 128


def current_tokens() -> ThemeTokens:
    manager = _theme_manager()
    if manager is not None:
        return manager.tokens
    return DARK if _scheme_is_dark() else LIGHT


def status_color(status_value: str) -> QColor:
    return QColor(current_tokens().status.get(status_value, current_tokens().muted))


def _build_palette(tokens: ThemeTokens) -> QPalette:
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: tokens.window,
        QPalette.ColorRole.WindowText: tokens.text,
        QPalette.ColorRole.Base: tokens.surface,
        QPalette.ColorRole.AlternateBase: tokens.surface_alt,
        QPalette.ColorRole.Text: tokens.text,
        QPalette.ColorRole.Button: tokens.surface,
        QPalette.ColorRole.ButtonText: tokens.text,
        QPalette.ColorRole.Highlight: tokens.accent,
        QPalette.ColorRole.HighlightedText: tokens.accent_text,
        QPalette.ColorRole.ToolTipBase: tokens.surface,
        QPalette.ColorRole.ToolTipText: tokens.text,
        QPalette.ColorRole.PlaceholderText: tokens.muted,
        QPalette.ColorRole.Link: tokens.accent,
    }
    for role, value in roles.items():
        palette.setColor(role, QColor(value))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(tokens.disabled_text))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Button, QColor(tokens.disabled_bg))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor(tokens.disabled_bg))
    return palette


def build_stylesheet(tokens: ThemeTokens) -> str:
    values = {field: getattr(tokens, field) for field in tokens.__dataclass_fields__ if field != "status"}
    return _STYLESHEET.safe_substitute(values)


class ThemeManager(QObject):
    themeChanged = Signal()

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self._app = app
        self.tokens = LIGHT
        hints = QGuiApplication.styleHints()
        try:
            hints.colorSchemeChanged.connect(self._on_color_scheme_changed)
        except (AttributeError, TypeError):
            pass
        self.refresh()

    def _on_color_scheme_changed(self, *_args: object) -> None:
        self.refresh()

    def refresh(self) -> None:
        self.tokens = DARK if _scheme_is_dark() else LIGHT
        self._app.setPalette(_build_palette(self.tokens))
        self._app.setStyleSheet(build_stylesheet(self.tokens))
        self.themeChanged.emit()


_manager: ThemeManager | None = None


def _theme_manager() -> ThemeManager | None:
    return _manager


def theme_manager() -> ThemeManager | None:
    """Return the active application theme manager, if one has been applied."""

    return _manager


def apply_theme(widget: QWidget | None = None) -> None:
    """Apply the palette-aware theme to the whole application."""

    del widget  # kept for call-site compatibility; the theme is application-wide
    app = QApplication.instance()
    if not isinstance(app, QApplication):
        return
    global _manager
    if _manager is None or _manager.parent() is not app:
        _manager = ThemeManager(app)
    else:
        _manager.refresh()
