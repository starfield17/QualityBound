import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QByteArray, QModelIndex, QPoint, QSignalBlocker, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QKeySequence,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QAbstractScrollArea,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QStatusBar,
    QToolButton,
    QSystemTrayIcon,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core.i18n import TranslationCatalog
from core.models import (
    BackendChoice,
    CodecChoice,
    DecisionActionCode,
    EncodeOptions,
    EncodePlan,
    EncodeRequest,
    EncodeResult,
    VideoFileItem,
)
from core.config import (
    delete_preset,
    list_presets,
    load_app_config,
    load_preset,
    parse_encode_workers,
    save_preset,
    update_app_config,
)
from core.media import (
    PostEncodeAction,
    VIDEO_EXTENSIONS,
    collect_video_files,
    parse_post_encode_action,
    post_encode_action_key,
)
from gui.activity_log_window import ActivityLogWindow
from gui.comparison_window import ComparisonWindow
from gui.constraint_decision_dialog import (
    SizeMissDecision,
    choose_quality_decision,
    choose_size_miss_decision,
)
from gui.encode_options_panel import EncodeOptionsPanel
from gui.gui_workers import EncoderCapabilityDetectWorker, QueueIntakeWorker, QueuePrepareWorker
from gui.preset_manager_dialog import PresetManagerDialog
from gui.qt_optionals import maybe_none
from gui.queue_completion import QueueCompletionHandler
from gui.queue_manager import QueueManager, QueueRunCompletion
from gui.queue_state import (
    TERMINAL_ITEM_STATUSES,
    QueueItemRecord,
    compute_metrics,
    QueueItemStatus,
    QueueMetrics,
    progress_stage_key,
    progress_state_key,
)
from gui.queue_model import QueueTableModel, format_duration, format_size
from gui.queue_view import ResponsiveQueueTableView, create_queue_view
from gui.queue_window import QueueWindow
from gui.settings_dialog import SettingsDialog
from gui.icons import action_icon
from gui.item_details_panel import ItemDetailsPanel
from gui.notification_banner import NotificationBanner
from gui.theme import apply_theme, current_tokens, theme_manager
from gui.window_geometry import clamped_window_size


EXPLICIT_BACKEND_ORDER: tuple[BackendChoice, ...] = (
    BackendChoice.NVENC,
    BackendChoice.QSV,
    BackendChoice.AMF,
    BackendChoice.VIDEOTOOLBOX,
    BackendChoice.CPU,
)

RUNTIME_CONFIG_KEYS = frozenset(
    {
        "analysis_profile",
        "analysis_profiles",
        "default_preset_name",
        "desktop_notifications",
        "encode_workers",
        "ffmpeg_path",
        "ffprobe_path",
        "language",
        "last_output_dir",
        "last_source_path",
        "log_level",
        "post_encode_action",
        "quality_unreachable_policy",
        "queue_table_header_state",
        "recent_paths",
        "setup_collapsed",
        "main_window_geometry",
        "queue_window_geometry",
        "activity_log_geometry",
        "size_blocked_policy",
        "skipped_output_policy",
        "workdir_path",
    }
)


class MainWindow(QMainWindow):
    def __init__(
        self,
        repo_root: Path,
        language: str | None = None,
        catalog: TranslationCatalog | None = None,
    ) -> None:
        super().__init__()
        self.repo_root = repo_root
        self.config_dir = repo_root / "config"
        self.default_workdir = repo_root / "workdir"
        self.app_config = load_app_config()
        self.catalog = catalog or TranslationCatalog(
            bundle_dir=self.config_dir / "i18n",
            translations_dir=self.config_dir.parent / "translations",
        )
        self.language = language or self.app_config.get("language", "en")
        if not self.catalog.has_locale(self.language):
            self.language = "en"
        self.translator = self.catalog.translator(self.language)

        self.active_worker = None
        self.encoder_detection_worker = None
        self._encoder_capabilities_ready = False
        self._startup_encoder_detection_started = False
        self._pending_encoder_detection_force_refresh = False
        self.queue_busy = False
        self._postprocessing = False
        self._postprocessing_cancel_requested = False
        self._asked_analysis_decisions: dict[tuple[str, str], EncodeResult] = {}
        self._header_sync_guard = False
        self._queue_state = "idle"
        self._persisted_snapshot: tuple[object, ...] | None = None
        self._action_state: tuple[object, ...] | None = None
        self._status_stage = "-"
        self._status_file = "-"
        self._status_speed = "-"
        self._status_elapsed = "-"
        self._status_percent: float | None = None
        self._close_after_running_task_stops = False
        self._loading_initial_state = True
        self.comparison_windows: list[ComparisonWindow] = []
        self._preset_dirty = False
        self._applying_preset = False
        self._completion_banner: tuple[str, list[tuple[str, Callable[[], None]]]] | None = None
        self._runtime_banner: tuple[str, str, list[tuple[str, Callable[[], None]]]] | None = None
        self._banner_key: tuple[object, ...] | None = None
        self._dismissed_banner_key: tuple[object, ...] | None = None

        self.queue_model = QueueTableModel(self.translator, self)
        self.queue_manager = QueueManager(self.queue_model, self)
        self.activity_log_window = ActivityLogWindow(self.translator, self)
        self.queue_window = QueueWindow(self.translator, self.queue_model, self)
        self.queue_completion_handler = QueueCompletionHandler(
            parent=self,
            append_log=self._append_log,
            notify=self._send_desktop_notification,
            close=self.close,
        )

        self._build_ui()
        self._connect_signals()
        self._load_initial_state()
        self._loading_initial_state = False
        self._apply_translations()
        self.options_panel.sync_dependent_controls()
        self._update_queue_metrics(self.queue_model.metrics())
        self._refresh_action_state()
        self._log_catalog_diagnostics()

    def _only_encoder_detection_running(self) -> bool:
        return (
            self.encoder_detection_worker is not None
            and self.active_worker is None
            and not self.queue_busy
            and not self.queue_manager.is_busy()
            and not self._postprocessing
            and not self.queue_completion_handler.is_busy()
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._only_encoder_detection_running():
            self._close_after_running_task_stops = True
            detection_worker = self.encoder_detection_worker
            if detection_worker is not None:
                detection_worker.cancel()
            self._maybe_close_after_running_task()
            event.ignore()
            return
        if not self._has_running_task():
            if not self._close_after_running_task_stops and self._has_unsaved_queue_work():
                result = QMessageBox.question(
                    self,
                    self.translator.t("gui.message.close_pending_title"),
                    self.translator.t("gui.message.close_pending_text"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if result != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
            self._persist_runtime_state()
            self._save_window_geometry()
            for comparison in list(self.comparison_windows):
                comparison.close()
            event.accept()
            return
        if self._close_after_running_task_stops:
            event.ignore()
            return

        result = QMessageBox.question(
            self,
            self.translator.t("gui.message.close_busy_title"),
            self.translator.t("gui.message.close_busy_text"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if result != QMessageBox.StandardButton.Yes:
            event.ignore()
            return

        self._close_after_running_task_stops = True
        self._append_log(self.translator.t("gui.log.close_after_stop_requested"))
        self._stop_active_task()
        self._maybe_close_after_running_task()
        event.ignore()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if self._startup_encoder_detection_started:
            return
        self._startup_encoder_detection_started = True
        QTimer.singleShot(0, lambda: self._start_encoder_capability_detection(force_refresh=False))

    def _apply_action_icons(self) -> None:
        tokens = current_tokens()
        icon_names = {
            self.add_files_action: "add_files",
            self.add_folder_action: "add_folder",
            self.plan_action: "add_to_queue",
            self.start_queue_action: "start",
            self.pause_after_current_action: "pause",
            self.stop_action: "stop",
            self.queue_action: "queue",
            self.activity_log_action: "log",
            self.compare_action: "compare",
            self.presets_action: "presets",
            self.settings_action: "settings",
        }
        for action, icon_name in icon_names.items():
            action.setIcon(action_icon(icon_name, tokens.icon, tokens.icon_disabled))

    def _sync_action_tips(self) -> None:
        for action in [
            self.add_files_action,
            self.add_folder_action,
            self.plan_action,
            self.start_queue_action,
            self.pause_after_current_action,
            self.stop_action,
            self.queue_action,
            self.activity_log_action,
            self.compare_action,
            self.presets_action,
            self.settings_action,
            self.exit_action,
            self.remove_selected_action,
            self.clear_completed_action,
            self.about_action,
        ]:
            action.setToolTip(action.text())

    def _build_ui(self) -> None:
        apply_theme(self)
        manager = theme_manager()
        if manager is not None:
            manager.themeChanged.connect(self._on_theme_changed)
        self.setAcceptDrops(True)
        self._build_toolbar()
        self._build_menu_bar()
        root_layout = self._build_central_content()
        self.source_box = self._build_source_box()
        self.options_panel = EncodeOptionsPanel(
            self.translator,
            self.app_config,
            self,
            append_log=self._append_log,
        )
        self.jobs_box = self._build_jobs_box()
        self.setup_bar = self._build_setup_bar()
        root_layout.addWidget(self.setup_bar)
        root_layout.addWidget(self.source_box)
        root_layout.addWidget(self.options_panel)
        root_layout.addWidget(self.jobs_box, 1)
        self._build_status_bar()
        self._apply_initial_window_geometry()
        self._restore_window_geometry()

        self.tray_icon = QSystemTrayIcon(self)
        if self.tray_icon.isSystemTrayAvailable() and not self.windowIcon().isNull():
            self.tray_icon.setIcon(self.windowIcon())
            self.tray_icon.setToolTip(self.translator.t("app.title"))
            self.tray_show_action = QAction(self)
            self.tray_quit_action = QAction(self)
            self.tray_show_action.triggered.connect(self._show_main_window)
            self.tray_quit_action.triggered.connect(self.close)
            self.tray_menu = QMenu(self)
            self.tray_menu.addAction(self.tray_show_action)
            self.tray_menu.addSeparator()
            self.tray_menu.addAction(self.pause_after_current_action)
            self.tray_menu.addAction(self.stop_action)
            self.tray_menu.addSeparator()
            self.tray_menu.addAction(self.tray_quit_action)
            self.tray_icon.setContextMenu(self.tray_menu)
            self.tray_icon.activated.connect(self._on_tray_activated)
            self.tray_icon.show()
        self.queue_window.attach_actions(
            [self.start_queue_action, self.pause_after_current_action, self.stop_action]
        )

    def _build_toolbar(self) -> None:
        toolbar = QToolBar(self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        toolbar.setIconSize(QSize(18, 18))
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)
        self.toolbar = toolbar

        self.add_files_action = QAction(self)
        self.add_folder_action = QAction(self)
        self.plan_action = QAction(self)
        self.start_queue_action = QAction(self)
        self.pause_after_current_action = QAction(self)
        self.stop_action = QAction(self)
        self.queue_action = QAction(self)
        self.activity_log_action = QAction(self)
        self.compare_action = QAction(self)
        self.presets_action = QAction(self)
        self.settings_action = QAction(self)
        self.exit_action = QAction(self)
        self.about_action = QAction(self)
        self.remove_selected_action = QAction(self)
        self.clear_completed_action = QAction(self)
        self._apply_action_icons()
        self.add_files_action.setShortcut(QKeySequence.StandardKey.Open)
        self.add_folder_action.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self.plan_action.setShortcut(QKeySequence("Ctrl+Shift+Return"))
        self.start_queue_action.setShortcut(QKeySequence("Ctrl+Return"))
        self.stop_action.setShortcut(QKeySequence("Ctrl+."))
        self.activity_log_action.setShortcut(QKeySequence("Ctrl+L"))
        self.compare_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self.presets_action.setShortcut(QKeySequence("Ctrl+Shift+P"))
        self.settings_action.setShortcut(QKeySequence.StandardKey.Preferences)
        self.exit_action.setShortcut(QKeySequence.StandardKey.Quit)

        for action in [
            self.add_files_action,
            self.add_folder_action,
            self.plan_action,
            self.start_queue_action,
            self.pause_after_current_action,
            self.stop_action,
        ]:
            toolbar.addAction(action)
        toolbar.addSeparator()
        for action in [
            self.queue_action,
            self.activity_log_action,
            self.compare_action,
            self.presets_action,
            self.settings_action,
        ]:
            toolbar.addAction(action)
        self._mark_action_roles()

    def _mark_action_roles(self) -> None:
        for action, object_name in (
            (self.start_queue_action, "primaryAction"),
            (self.stop_action, "dangerAction"),
        ):
            widget = maybe_none(self.toolbar.widgetForAction(action))
            if widget is not None:
                widget.setObjectName(object_name)

    def _build_menu_bar(self) -> None:
        self.file_menu = self.menuBar().addMenu("")
        self.queue_menu = self.menuBar().addMenu("")
        self.view_menu = self.menuBar().addMenu("")
        self.tools_menu = self.menuBar().addMenu("")
        self.help_menu = self.menuBar().addMenu("")

        self.file_menu.addAction(self.add_files_action)
        self.file_menu.addAction(self.add_folder_action)
        self.file_menu.addSeparator()
        self.file_menu.addAction(self.exit_action)
        self.exit_action.triggered.connect(self.close)

        self.queue_menu.addAction(self.start_queue_action)
        self.queue_menu.addAction(self.pause_after_current_action)
        self.queue_menu.addAction(self.stop_action)
        self.queue_menu.addSeparator()
        self.queue_menu.addAction(self.remove_selected_action)
        self.queue_menu.addAction(self.clear_completed_action)
        self.remove_selected_action.triggered.connect(self._remove_selected_queue_rows)
        self.clear_completed_action.triggered.connect(self._clear_completed_queue_rows)

        self.view_menu.addAction(self.queue_action)
        self.view_menu.addAction(self.activity_log_action)
        self.view_menu.addAction(self.compare_action)

        self.tools_menu.addAction(self.presets_action)
        self.tools_menu.addAction(self.settings_action)

        self.help_menu.addAction(self.about_action)
        self.about_action.triggered.connect(self._show_about)

    def _on_theme_changed(self) -> None:
        self._apply_action_icons()
        self.queue_model.refresh_theme()
        self._render_queue_state()
        tokens = current_tokens()
        self.empty_icon_label.setPixmap(
            action_icon("queue", tokens.accent, tokens.icon_disabled).pixmap(44, 44)
        )
        self.activity_log_window.highlighter.rehighlight()

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            self.translator.t("gui.about.title"),
            self.translator.t("gui.about.text"),
        )

    def _remove_selected_queue_rows(self) -> None:
        rows = self._selected_rows_from_view(self._active_queue_view())
        if not rows:
            return
        removed = self.queue_manager.remove_rows(rows)
        if removed:
            self._append_log(self.translator.t("gui.log.removed_from_queue", count=removed))

    def _clear_completed_queue_rows(self) -> None:
        removed = self.queue_manager.clear_completed()
        if removed:
            self._append_log(self.translator.t("gui.log.cleared_completed", count=removed))

    def _active_queue_view(self) -> ResponsiveQueueTableView:
        focus = QApplication.focusWidget()
        queue_view = self.queue_window.table_view
        if focus is not None and (focus is queue_view or queue_view.isAncestorOf(focus)):
            return queue_view
        return self.table_view

    def _build_central_content(self) -> QVBoxLayout:
        central = QScrollArea(self)
        central.setWidgetResizable(True)
        central.setFrameShape(QFrame.Shape.NoFrame)
        central.setSizeAdjustPolicy(QAbstractScrollArea.SizeAdjustPolicy.AdjustIgnored)
        central.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        central.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        central.setMinimumSize(0, 0)
        self.setCentralWidget(central)

        content = QWidget(self)
        content.setObjectName("centralContent")
        content.setMinimumSize(0, 0)
        central.setWidget(content)
        root_layout = QVBoxLayout(content)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(10)
        self.main_scroll_area = central
        self.main_content_widget = content
        self.drop_overlay = QLabel(self)
        self.drop_overlay.setObjectName("dropOverlay")
        self.drop_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.drop_overlay.setVisible(False)
        return root_layout

    def _build_source_box(self) -> QGroupBox:
        source_box = QGroupBox()
        source_layout = QGridLayout(source_box)
        source_layout.setContentsMargins(10, 6, 10, 6)
        source_layout.setHorizontalSpacing(8)
        source_layout.setVerticalSpacing(6)

        self.source_label = QLabel()
        self.source_combo = QComboBox()
        self.source_combo.setEditable(True)
        self.source_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.source_file_button = QPushButton()
        self.source_dir_button = QPushButton()
        self.source_file_button.setMaximumWidth(110)
        self.source_dir_button.setMaximumWidth(110)

        self.preset_label = QLabel()
        self.preset_combo = QComboBox()
        self.manage_presets_button = QPushButton()
        self.manage_presets_button.setMaximumWidth(160)

        self.post_encode_label = QLabel()
        self.post_encode_combo = QComboBox()
        self.post_encode_combo.setMaximumWidth(300)
        for action in PostEncodeAction:
            self.post_encode_combo.addItem("", action.value)

        self.output_label = QLabel()
        self.output_edit = QLineEdit()
        self.output_button = QPushButton()
        self.output_button.setMaximumWidth(110)

        source_layout.addWidget(self.source_label, 0, 0)
        source_layout.addWidget(self.source_combo, 0, 1, 1, 3)
        source_layout.addWidget(self.source_file_button, 0, 4)
        source_layout.addWidget(self.source_dir_button, 0, 5)
        source_layout.addWidget(self.output_label, 1, 0)
        source_layout.addWidget(self.output_edit, 1, 1, 1, 4)
        source_layout.addWidget(self.output_button, 1, 5)
        source_layout.addWidget(self.preset_label, 2, 0)
        source_layout.addWidget(self.preset_combo, 2, 1)
        source_layout.addWidget(self.manage_presets_button, 2, 2)
        source_layout.addWidget(self.post_encode_label, 2, 3)
        source_layout.addWidget(self.post_encode_combo, 2, 4, 1, 2)
        source_layout.setColumnStretch(1, 1)
        source_layout.setColumnStretch(2, 1)
        source_layout.setColumnStretch(3, 1)
        return source_box

    def _build_setup_bar(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.setup_toggle = QToolButton()
        self.setup_toggle.setCheckable(True)
        self.setup_toggle.setChecked(True)
        self.setup_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setup_toggle.setArrowType(Qt.ArrowType.DownArrow)
        self.setup_toggle.toggled.connect(self._on_setup_toggled)
        self.setup_summary_label = QLabel()
        self.setup_summary_label.setObjectName("setupSummary")
        self.setup_summary_label.setVisible(False)
        layout.addWidget(self.setup_toggle)
        layout.addWidget(self.setup_summary_label, 1)
        layout.addStretch(1)
        return bar

    def _on_setup_toggled(self, expanded: bool) -> None:
        self._set_setup_collapsed(not expanded, persist=not self._loading_initial_state)

    def _set_setup_collapsed(self, collapsed: bool, *, persist: bool = False) -> None:
        self.setup_toggle.blockSignals(True)
        self.setup_toggle.setChecked(not collapsed)
        self.setup_toggle.blockSignals(False)
        self.setup_toggle.setArrowType(
            Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow
        )
        self.source_box.setVisible(not collapsed)
        self.options_panel.set_visible(not collapsed)
        self.setup_summary_label.setVisible(collapsed)
        self._update_setup_summary()
        self.app_config["setup_collapsed"] = collapsed
        if persist:
            self._persist_runtime_state()

    def _update_setup_summary(self) -> None:
        self.setup_summary_label.setText(self.options_panel.summary_text())

    def _on_options_changed(self) -> None:
        self._update_setup_summary()
        self._mark_preset_modified()

    def _build_empty_state(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("emptyState")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)
        layout.addStretch(1)
        self.empty_icon_label = QLabel()
        tokens = current_tokens()
        self.empty_icon_label.setPixmap(
            action_icon("queue", tokens.accent, tokens.icon_disabled).pixmap(44, 44)
        )
        self.empty_icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_icon_label)
        self.empty_title_label = QLabel()
        self.empty_title_label.setObjectName("emptyTitle")
        self.empty_title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_title_label)
        self.empty_body_label = QLabel()
        self.empty_body_label.setObjectName("emptyBody")
        self.empty_body_label.setWordWrap(True)
        self.empty_body_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_body_label)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.empty_add_files_button = QPushButton()
        self.empty_add_files_button.setProperty("role", "primary")
        self.empty_add_folder_button = QPushButton()
        self.empty_add_files_button.clicked.connect(self._trigger_add_files)
        self.empty_add_folder_button.clicked.connect(self._trigger_add_folder)
        buttons.addWidget(self.empty_add_files_button)
        buttons.addWidget(self.empty_add_folder_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.empty_drop_label = QLabel()
        self.empty_drop_label.setObjectName("setupSummary")
        self.empty_drop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_drop_label)
        layout.addStretch(1)
        return frame

    @staticmethod
    def _stat_card(title: QLabel, value: QLabel) -> QFrame:
        card = QFrame()
        card.setObjectName("statCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(1)
        layout.addWidget(title)
        layout.addWidget(value)
        return card

    def _build_jobs_box(self) -> QGroupBox:
        jobs_box = QGroupBox()
        jobs_layout = QVBoxLayout(jobs_box)
        jobs_layout.setContentsMargins(12, 10, 12, 10)
        jobs_layout.setSpacing(7)

        self.notification_banner = NotificationBanner(self.translator, self)
        jobs_layout.addWidget(self.notification_banner)

        self.total_items_title = QLabel()
        self.total_items_value = QLabel("0")
        self.total_duration_title = QLabel()
        self.total_duration_value = QLabel("—")
        self.states_title = QLabel()
        self.states_value = QLabel("—")
        self.saved_space_title = QLabel()
        self.saved_space_value = QLabel("—")
        for label in (
            self.total_items_title,
            self.total_duration_title,
            self.states_title,
            self.saved_space_title,
        ):
            label.setObjectName("summaryTitle")
        for label in (
            self.total_items_value,
            self.total_duration_value,
            self.states_value,
            self.saved_space_value,
        ):
            label.setObjectName("summaryValue")
        self.states_value.setWordWrap(True)

        summary_layout = QHBoxLayout()
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.setSpacing(8)
        summary_layout.addWidget(self._stat_card(self.total_items_title, self.total_items_value), 1)
        summary_layout.addWidget(self._stat_card(self.total_duration_title, self.total_duration_value), 1)
        summary_layout.addWidget(self._stat_card(self.states_title, self.states_value), 2)
        summary_layout.addWidget(self._stat_card(self.saved_space_title, self.saved_space_value), 1)
        jobs_layout.addLayout(summary_layout)

        self.queue_state_label = QLabel()
        self.queue_state_label.setObjectName("statusPill")
        self.queue_progress_text = QLabel()
        self.queue_progress_text.setObjectName("setupSummary")
        self.queue_progress_bar = QProgressBar()
        self.queue_progress_bar.setRange(0, 1000)
        progress_layout = QHBoxLayout()
        progress_layout.setContentsMargins(0, 0, 0, 0)
        progress_layout.setSpacing(8)
        progress_layout.addWidget(self.queue_state_label, 0)
        progress_layout.addWidget(self.queue_progress_text, 2)
        progress_layout.addWidget(self.queue_progress_bar, 3)
        jobs_layout.addLayout(progress_layout)

        self.table_view = create_queue_view(self)
        self.table_view.setModel(self.queue_model)
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.horizontalHeader().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        self.empty_state = self._build_empty_state()
        self.queue_stack = QStackedWidget()
        self.queue_stack.addWidget(self.table_view)
        self.queue_stack.addWidget(self.empty_state)
        jobs_layout.addWidget(self.queue_stack, 1)

        self.details_panel = ItemDetailsPanel(self.translator, self)
        jobs_layout.addWidget(self.details_panel)
        self.details_panel.openSource.connect(self._open_selected_source_folder)
        self.details_panel.openOutput.connect(self._open_selected_output_folder)
        self.details_panel.compare.connect(self._compare_selected_record)
        self.details_panel.resolve.connect(self._resolve_selected_decision)
        self.details_panel.closeRequested.connect(self._clear_details_panel)
        return jobs_box

    def _build_status_bar(self) -> None:
        status_bar = QStatusBar(self)
        self.setStatusBar(status_bar)
        self.status_stage_label = QLabel()
        self.status_file_label = QLabel()
        self.status_speed_label = QLabel()
        self.status_elapsed_label = QLabel()
        self.status_current_progress_label = QLabel()
        self.encoder_status_label = QLabel()
        self.encoder_status_label.setVisible(False)
        self.current_progress_bar = QProgressBar()
        self.current_progress_bar.setRange(0, 1000)
        self.current_progress_bar.setFixedWidth(220)
        self.current_progress_bar.setValue(0)

        status_bar.addWidget(self.status_stage_label, 0)
        status_bar.addWidget(self.status_file_label, 1)
        status_bar.addPermanentWidget(self.encoder_status_label)
        status_bar.addPermanentWidget(self.status_speed_label)
        status_bar.addPermanentWidget(self.status_elapsed_label)
        status_bar.addPermanentWidget(self.status_current_progress_label)
        status_bar.addPermanentWidget(self.current_progress_bar)

    def _restore_window_geometry(self) -> None:
        for widget, key in (
            (self, "main_window_geometry"),
            (self.queue_window, "queue_window_geometry"),
            (self.activity_log_window, "activity_log_geometry"),
        ):
            encoded = str(self.app_config.get(key, "")).strip()
            if not encoded:
                continue
            try:
                raw = QByteArray.fromBase64(encoded.encode("ascii"))
            except Exception:
                continue
            widget.restoreGeometry(raw)

    def _save_window_geometry(self) -> None:
        for widget, key in (
            (self, "main_window_geometry"),
            (self.queue_window, "queue_window_geometry"),
            (self.activity_log_window, "activity_log_geometry"),
        ):
            state = widget.saveGeometry()
            self.app_config[key] = bytes(state.toBase64().data()).decode("ascii")

    def _apply_initial_window_geometry(self) -> None:
        minimum = clamped_window_size(760, 520)
        self.setMinimumSize(minimum)
        initial = clamped_window_size(
            1520,
            960,
            minimum_width=minimum.width(),
            minimum_height=minimum.height(),
        )
        self.resize(initial)

    def _connect_signals(self) -> None:
        self.add_files_action.triggered.connect(self._add_files_dialog)
        self.add_folder_action.triggered.connect(self._add_folder_dialog)
        self.plan_action.triggered.connect(self._plan_current_source)
        self.start_queue_action.triggered.connect(self._start_queue)
        self.pause_after_current_action.triggered.connect(self._pause_after_current)
        self.stop_action.triggered.connect(self._stop_active_task)
        self.queue_action.triggered.connect(self._show_queue_window)
        self.activity_log_action.triggered.connect(self._show_activity_log)
        self.compare_action.triggered.connect(self._show_manual_comparison)
        self.presets_action.triggered.connect(self._open_preset_manager)
        self.settings_action.triggered.connect(self._open_settings_dialog)

        self.source_file_button.clicked.connect(self._browse_source_file)
        self.source_dir_button.clicked.connect(self._browse_source_dir)
        self.output_button.clicked.connect(self._browse_output)
        self.manage_presets_button.clicked.connect(self._open_preset_manager)
        self.post_encode_combo.currentIndexChanged.connect(self._post_encode_combo_changed)
        self.options_panel.analysis_profile_changed.connect(self._on_analysis_profile_changed)
        self.source_combo.activated.connect(self._on_source_activated)
        self.output_edit.editingFinished.connect(self._persist_runtime_state)
        self.source_combo.currentTextChanged.connect(self._refresh_path_validation)
        self.output_edit.textChanged.connect(self._refresh_path_validation)
        self.preset_combo.currentIndexChanged.connect(self._preset_combo_changed)
        self.options_panel.options_changed.connect(self._on_options_changed)
        self.options_panel.smart_unavailable.connect(self._on_smart_unavailable)
        self.notification_banner.dismissed.connect(self._on_banner_dismissed)

        self.table_view.filesDropped.connect(self._handle_dropped_paths)
        self.queue_window.table_view.filesDropped.connect(self._handle_dropped_paths)
        for view in [self.table_view, self.queue_window.table_view]:
            view.removeRequested.connect(self._remove_selected_queue_rows)
            view.dragActivityChanged.connect(self._set_drop_overlay_visible)
        self.table_view.doubleClicked.connect(self._on_main_row_activated)
        self.table_view.activateRequested.connect(self._on_main_row_activated)
        self.queue_window.table_view.doubleClicked.connect(self._on_queue_window_row_activated)
        self.queue_window.table_view.activateRequested.connect(self._on_queue_window_row_activated)
        selection_model = maybe_none(self.table_view.selectionModel())
        if selection_model is not None:
            selection_model.selectionChanged.connect(self._on_queue_selection_changed)

        self.queue_model.metricsChanged.connect(self._update_queue_metrics)
        self.queue_manager.log.connect(self._append_log)
        self.queue_manager.progress.connect(self._update_progress)
        self.queue_manager.busyChanged.connect(self._on_queue_busy_changed)
        self.queue_manager.stateChanged.connect(self._on_queue_state_changed)
        self.queue_manager.runCompleted.connect(self._on_queue_run_completed)
        self.queue_manager.executionStopped.connect(self._on_queue_execution_stopped)
        self.queue_manager.workerFinished.connect(self._maybe_close_after_running_task)
        self.queue_manager.error.connect(self._on_queue_error)

        self.table_view.customContextMenuRequested.connect(self._show_main_queue_context_menu)
        self.queue_window.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue_window.table_view.customContextMenuRequested.connect(
            self._show_queue_window_context_menu
        )
        self.table_view.horizontalHeader().customContextMenuRequested.connect(
            self._show_main_header_context_menu
        )
        self.queue_window.table_view.horizontalHeader().setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.queue_window.table_view.horizontalHeader().customContextMenuRequested.connect(
            self._show_queue_window_header_context_menu
        )
        self.table_view.horizontalHeader().sectionMoved.connect(self._on_main_header_moved)
        self.queue_window.table_view.horizontalHeader().sectionMoved.connect(
            self._on_queue_window_header_moved
        )

    def _trigger_add_files(self, _checked: bool = False) -> None:
        self.add_files_action.trigger()

    def _trigger_add_folder(self, _checked: bool = False) -> None:
        self.add_folder_action.trigger()

    def _compare_selected_record(self) -> None:
        self._show_comparison(self._selected_record())

    def _show_manual_comparison(self) -> None:
        self._show_comparison()

    def _on_analysis_profile_changed(self, _name: object) -> None:
        self._persist_runtime_state()

    def _on_source_activated(self, _index: int) -> None:
        self._persist_runtime_state()

    def _on_main_row_activated(self, index: QModelIndex) -> None:
        self._on_queue_row_activated(self.table_view, index)

    def _on_queue_window_row_activated(self, index: QModelIndex) -> None:
        self._on_queue_row_activated(self.queue_window.table_view, index)

    def _on_queue_selection_changed(self, *_args: object) -> None:
        self._update_details_panel()

    def _show_main_queue_context_menu(self, pos: QPoint) -> None:
        self._show_queue_context_menu(self.table_view, pos)

    def _show_queue_window_context_menu(self, pos: QPoint) -> None:
        self._show_queue_context_menu(self.queue_window.table_view, pos)

    def _show_main_header_context_menu(self, pos: QPoint) -> None:
        self._show_header_context_menu(self.table_view, pos)

    def _show_queue_window_header_context_menu(self, pos: QPoint) -> None:
        self._show_header_context_menu(self.queue_window.table_view, pos)

    def _on_main_header_moved(self, *_args: object) -> None:
        self._persist_header_state(self.table_view)

    def _on_queue_window_header_moved(self, *_args: object) -> None:
        self._persist_header_state(self.queue_window.table_view)

    def _on_worker_finished_busy(self) -> None:
        self._set_worker_busy(False)

    @staticmethod
    def _open_folder_action(path: Path) -> Callable[[], None]:
        def open_folder() -> None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        return open_folder

    def _load_initial_state(self) -> None:
        self._set_source_history(list(self.app_config.get("recent_paths", [])))
        self.source_combo.setEditText(self.app_config.get("last_source_path", ""))
        self.output_edit.setText(self.app_config.get("last_output_dir", ""))

        self._refresh_presets()
        default_preset = self.app_config.get("default_preset_name")
        if default_preset:
            preset_index = self.preset_combo.findText(default_preset)
            if preset_index >= 0:
                self.preset_combo.setCurrentIndex(preset_index)
                try:
                    self._applying_preset = True
                    self.options_panel.apply_options(load_preset(default_preset, self.config_dir))
                except FileNotFoundError:
                    self.options_panel.apply_options(EncodeOptions())
                finally:
                    self._applying_preset = False
            else:
                self.options_panel.apply_options(EncodeOptions())
        else:
            self.options_panel.apply_options(EncodeOptions())
        self.options_panel.apply_analysis_profile_settings()

        post_action = parse_post_encode_action(self.app_config.get("post_encode_action", PostEncodeAction.DO_NOTHING.value))
        post_index = self.post_encode_combo.findData(post_action.value)
        if post_index >= 0:
            self.post_encode_combo.setCurrentIndex(post_index)

        self._set_status_snapshot("-", "-", "-", "-", None)
        self._restore_header_state()
        default_collapsed = self.height() < 820
        self._set_setup_collapsed(
            bool(self.app_config.get("setup_collapsed", default_collapsed))
        )
        self._refresh_path_validation()
        self._update_setup_summary()

    def _apply_translations(self) -> None:
        self.translator = self.catalog.translator(self.language)
        self.setWindowTitle(self.translator.t("app.title"))
        self.source_box.setTitle(self.translator.t("gui.group.source"))
        self.jobs_box.setTitle(self.translator.t("gui.group.jobs"))

        self.add_files_action.setText(self.translator.t("gui.button.add_files"))
        self.add_folder_action.setText(self.translator.t("gui.button.add_folder"))
        self.plan_action.setText(self.translator.t("gui.button.add_to_queue"))
        self.start_queue_action.setText(self.translator.t("gui.button.start_queue"))
        self.pause_after_current_action.setText(self.translator.t("gui.button.pause_after_current"))
        self.stop_action.setText(self.translator.t("gui.button.stop"))
        self.queue_action.setText(self.translator.t("gui.button.queue"))
        self.activity_log_action.setText(self.translator.t("gui.button.activity_log"))
        self.compare_action.setText(self.translator.t("gui.compare.title"))
        self.presets_action.setText(self.translator.t("gui.button.presets"))
        self.settings_action.setText(self.translator.t("gui.button.settings"))
        self.exit_action.setText(self.translator.t("gui.button.exit"))
        self.about_action.setText(self.translator.t("gui.button.about"))
        self.remove_selected_action.setText(self.translator.t("gui.menu.remove_selected"))
        self.clear_completed_action.setText(self.translator.t("gui.menu.clear_completed"))
        self.file_menu.setTitle(self.translator.t("gui.menu.file"))
        self.queue_menu.setTitle(self.translator.t("gui.menu.queue"))
        self.view_menu.setTitle(self.translator.t("gui.menu.view"))
        self.tools_menu.setTitle(self.translator.t("gui.menu.tools"))
        self.help_menu.setTitle(self.translator.t("gui.menu.help"))
        self.setup_toggle.setText(self.translator.t("gui.button.setup"))
        self.empty_title_label.setText(self.translator.t("gui.empty.title"))
        self.empty_body_label.setText(self.translator.t("gui.empty.body"))
        self.empty_add_files_button.setText(self.translator.t("gui.button.add_files"))
        self.empty_add_folder_button.setText(self.translator.t("gui.button.add_folder"))
        self.empty_drop_label.setText(self.translator.t("gui.empty.drop"))
        self.notification_banner.apply_translations(self.translator)
        self.details_panel.apply_translations(self.translator)
        if hasattr(self, "tray_show_action"):
            self.tray_show_action.setText(self.translator.t("gui.tray.show"))
            self.tray_quit_action.setText(self.translator.t("gui.tray.quit"))
            self.tray_icon.setToolTip(self.translator.t("app.title"))
        self._sync_action_tips()

        self.source_label.setText(self.translator.t("gui.label.source"))
        self.preset_label.setText(self.translator.t("gui.label.preset"))
        self.post_encode_label.setText(self.translator.t("gui.label.post_encode_action"))
        for action in PostEncodeAction:
            idx = self.post_encode_combo.findData(action.value)
            if idx >= 0:
                self.post_encode_combo.setItemText(idx, self.translator.t(post_encode_action_key(action)))
        self.output_label.setText(self.translator.t("gui.label.output_folder"))
        self.source_file_button.setText(self.translator.t("gui.button.browse_file"))
        self.source_dir_button.setText(self.translator.t("gui.button.browse_dir"))
        self.output_button.setText(self.translator.t("gui.button.browse_dir"))
        self.manage_presets_button.setText(self.translator.t("gui.button.manage_presets"))

        self.options_panel.set_translator(self.translator)
        self._refresh_presets()

        self.total_items_title.setText(self.translator.t("gui.summary.total_items"))
        self.states_title.setText(self.translator.t("gui.summary.states"))
        self.total_duration_title.setText(self.translator.t("gui.summary.total_duration"))
        self.saved_space_title.setText(self.translator.t("gui.summary.estimated_saved"))

        editable = self.source_combo.lineEdit()
        if editable is not None:
            editable.setPlaceholderText(self.translator.t("gui.placeholder.source"))
        self.output_edit.setPlaceholderText(self.translator.t("gui.placeholder.default_output"))
        self._bind_accessibility()

        self.queue_model.set_translator(self.translator)
        for view in (self.table_view, self.queue_window.table_view):
            view.horizontalHeader().setToolTip(self.translator.t("gui.tooltip.table_header"))
        self.activity_log_window.apply_translations(self.translator)
        self.queue_window.apply_translations(self.translator)
        for comparison in self.comparison_windows:
            comparison.apply_translations(self.translator)
        self._render_queue_state()
        self._refresh_path_validation()
        self._update_setup_summary()
        self._update_details_panel()
        self._set_status_snapshot(
            self._status_stage,
            self._status_file,
            self._status_speed,
            self._status_elapsed,
            self._status_percent,
        )
        self._update_queue_metrics(self.queue_model.metrics())

    def _bind_accessibility(self) -> None:
        pairs = (
            (self.source_label, self.source_combo),
            (self.output_label, self.output_edit),
            (self.preset_label, self.preset_combo),
            (self.post_encode_label, self.post_encode_combo),
        )
        for label, widget in pairs:
            label.setBuddy(widget)
            widget.setAccessibleName(label.text())
        self.setup_toggle.setAccessibleName(self.translator.t("gui.button.setup"))
        self.queue_state_label.setAccessibleName(self.translator.t("gui.summary.states"))
        self.table_view.setAccessibleName(self.translator.t("gui.group.jobs"))
        self.queue_window.table_view.setAccessibleName(self.translator.t("gui.group.jobs"))
        self.setTabOrder(self.source_combo, self.output_edit)
        self.setTabOrder(self.output_edit, self.preset_combo)
        self.setTabOrder(self.preset_combo, self.post_encode_combo)
        self.setTabOrder(self.post_encode_combo, self.table_view)

    def _set_source_history(self, items: list[str]) -> None:
        current = self.source_combo.currentText().strip()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItems(items)
        self.source_combo.setEditText(current)
        self.source_combo.blockSignals(False)

    def _selected_input(self) -> Path | None:
        text = self.source_combo.currentText().strip()
        return Path(text).expanduser().resolve() if text else None

    def _selected_output(self) -> Path | None:
        text = self.output_edit.text().strip()
        return Path(text).expanduser().resolve() if text else None

    def _selected_workdir(self) -> Path:
        text = str(self.app_config.get("workdir_path", "")).strip()
        return Path(text).expanduser().resolve() if text else self.default_workdir.resolve()

    def _selected_ffmpeg(self) -> str | None:
        text = str(self.app_config.get("ffmpeg_path", "")).strip()
        return text or None

    def _selected_ffprobe(self) -> str | None:
        text = str(self.app_config.get("ffprobe_path", "")).strip()
        return text or None

    def _append_log(self, message: str) -> None:
        self.activity_log_window.append_message(message)

    def _log_catalog_diagnostics(self) -> None:
        for diagnostic in self.catalog.diagnostics():
            self._append_log(
                f"i18n warning ({diagnostic.locale}): {diagnostic.message}"
            )

    def _encoder_capability_summary(self, capabilities: dict) -> str:
        codecs = capabilities.get("codecs", {})
        if not isinstance(codecs, dict):
            return self.translator.t("gui.value.unknown")

        parts: list[str] = []
        for codec in (CodecChoice.HEVC, CodecChoice.AV1):
            items = codecs.get(codec.value, [])
            labels: list[str] = []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    backend = str(item.get("backend", "")).strip()
                    encoder = str(item.get("encoder", "")).strip()
                    if backend and encoder:
                        labels.append(f"{encoder} ({backend})")
            parts.append(f"{codec.value}: {', '.join(labels) if labels else self.translator.t('gui.label.none')}")
        hwaccels = capabilities.get("hwaccels", [])
        if isinstance(hwaccels, list) and hwaccels:
            parts.append(
                f"{self.translator.t('gui.label.decode_hwaccels')}: "
                f"{', '.join(str(item) for item in hwaccels)}"
            )
        return "; ".join(parts)

    def _start_encoder_capability_detection(self, *, force_refresh: bool) -> None:
        if self.encoder_detection_worker is not None:
            if force_refresh:
                self._pending_encoder_detection_force_refresh = True
            self._append_log(self.translator.t("gui.log.encoder_detection_running"))
            return

        self._encoder_capabilities_ready = False
        self._runtime_banner = None
        self.options_panel.begin_capability_detection()
        worker = EncoderCapabilityDetectWorker(
            self._selected_ffmpeg(),
            force_refresh=force_refresh,
        )
        self.encoder_detection_worker = worker
        self._set_encoder_status(
            self.translator.t("gui.status.detecting_encoders"),
            self.translator.t("gui.status.detecting_encoders"),
            visible=True,
        )
        worker.log.connect(self._append_log)
        worker.completed.connect(self._on_encoder_capability_detection_completed)
        worker.failed.connect(self._on_encoder_capability_detection_failed)
        worker.finished.connect(self._on_encoder_capability_detection_finished)
        worker.start()

    def _on_encoder_capability_detection_completed(self, capabilities: dict) -> None:
        self.app_config["encoder_capabilities"] = capabilities
        self._encoder_capabilities_ready = True
        self._runtime_banner = None
        self.options_panel.set_runtime_capabilities(capabilities)
        self._set_encoder_status("", "", visible=False)
        self._append_log(
            self.translator.t(
                "gui.log.encoder_detection_done",
                summary=self._encoder_capability_summary(capabilities),
            )
        )

    def _on_encoder_capability_detection_failed(self, message: str) -> None:
        self._encoder_capabilities_ready = False
        self.options_panel.notify_capability_detection_failed()
        self._set_encoder_status(
            self.translator.t("gui.status.detection_failed"),
            message,
            visible=True,
        )
        self._append_log(self.translator.t("gui.log.encoder_detection_failed", error=message))
        self._set_runtime_banner(
            self.translator.t("gui.banner.detection_failed"),
            "error",
            [(self.translator.t("gui.completion.view_log"), self._show_activity_log)],
        )

    def _set_encoder_status(self, text: str, tooltip: str, *, visible: bool) -> None:
        self.encoder_status_label.setText(text)
        self.encoder_status_label.setToolTip(tooltip)
        self.encoder_status_label.setVisible(visible)

    def _on_encoder_capability_detection_finished(self) -> None:
        self.encoder_detection_worker = None
        if self._close_after_running_task_stops:
            self._pending_encoder_detection_force_refresh = False
            self._maybe_close_after_running_task()
            return
        if self._pending_encoder_detection_force_refresh:
            self._pending_encoder_detection_force_refresh = False
            QTimer.singleShot(0, lambda: self._start_encoder_capability_detection(force_refresh=True))

    def _set_status_snapshot(
        self,
        stage: str,
        file_name: str,
        speed: str,
        elapsed: str,
        percent: float | None,
    ) -> None:
        self._status_stage = stage
        self._status_file = file_name
        self._status_speed = speed
        self._status_elapsed = elapsed
        self._status_percent = percent
        self.status_stage_label.setText(self.translator.t("gui.statusbar.stage", value=stage))
        self.status_file_label.setText(self.translator.t("gui.statusbar.file", value=file_name))
        self.status_speed_label.setText(self.translator.t("gui.statusbar.speed", value=speed))
        self.status_elapsed_label.setText(self.translator.t("gui.statusbar.elapsed", value=elapsed))
        if percent is None:
            self.status_current_progress_label.setText(self.translator.t("gui.statusbar.current_progress", value="-"))
            self.current_progress_bar.setValue(0)
        else:
            bounded = max(0.0, min(100.0, percent))
            self.status_current_progress_label.setText(self.translator.t("gui.statusbar.current_progress", value=f"{bounded:.1f}%"))
            self.current_progress_bar.setValue(int(round(bounded * 10)))

    def _update_queue_metrics(self, metrics: QueueMetrics) -> None:
        self.total_items_value.setText(str(metrics.total_items))
        self.states_value.setText(
            self.translator.t(
                "gui.summary.queue_states",
                ready=metrics.ready_items,
                running=metrics.running_items,
                failed=metrics.failed_items,
                decision=metrics.needs_decision_items,
            )
        )
        self.total_duration_value.setText(format_duration(metrics.total_duration_sec))
        measured = metrics.actual_saved_bytes
        if measured is not None:
            self.saved_space_title.setText(self.translator.t("gui.summary.measured_saved"))
            self.saved_space_value.setText(format_size(measured))
            self.saved_space_value.setToolTip(
                self.translator.t(
                    "gui.summary.measured_saved_tooltip",
                    count=metrics.measured_saved_items,
                )
            )
        else:
            self.saved_space_title.setText(self.translator.t("gui.summary.estimated_saved"))
            self.saved_space_value.setToolTip("")
            saved_bytes = metrics.estimated_saved_bytes
            if saved_bytes is None:
                self.saved_space_value.setText(self.translator.t("gui.value.unknown"))
            elif metrics.estimated_saved_is_floor:
                self.saved_space_value.setText(
                    self.translator.t(
                        "gui.summary.saved_at_least",
                        value=format_size(saved_bytes),
                    )
                )
            else:
                self.saved_space_value.setText(format_size(saved_bytes))
        eta_text = format_duration(metrics.eta_sec) if metrics.eta_sec else self.translator.t("gui.value.unknown")
        self.queue_progress_text.setText(
            self.translator.t(
                "gui.summary.queue_progress",
                percent=f"{metrics.queue_percent:.1f}",
                completed=metrics.completed_items,
                total=metrics.total_items,
                eta=eta_text,
            )
        )
        self.queue_progress_bar.setValue(int(round(metrics.queue_percent * 10)))
        self.queue_window.update_metrics(metrics)
        self._update_queue_empty_state(metrics)
        self._update_decision_banner(metrics)
        self._refresh_action_state()

    def _set_drop_overlay_visible(self, visible: bool) -> None:
        if not hasattr(self, "drop_overlay"):
            return
        if visible and not (
            self.active_worker is not None or self.queue_busy or self.queue_manager.is_busy()
        ):
            self.drop_overlay.setText(self.translator.t("gui.drop.overlay"))
            self._position_drop_overlay()
            self.drop_overlay.raise_()
            self.drop_overlay.setVisible(True)
        else:
            self.drop_overlay.setVisible(False)

    def _position_drop_overlay(self) -> None:
        if not hasattr(self, "drop_overlay"):
            return
        rect = self.main_scroll_area.geometry()
        self.drop_overlay.setGeometry(rect.adjusted(12, 12, -12, -12))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._position_drop_overlay()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drop_overlay_visible(False)
        super().dragLeaveEvent(event)

    def _on_smart_unavailable(self, reason: str) -> None:
        self._set_runtime_banner(
            self.translator.t("gui.banner.smart_unavailable", reason=reason),
            "warning",
            [(self.translator.t("gui.button.settings"), self._open_settings_dialog)],
        )

    def _set_runtime_banner(
        self,
        text: str,
        severity: str,
        actions: list[tuple[str, Callable[[], None]]] | None = None,
    ) -> None:
        self._runtime_banner = (text, severity, actions or [])
        self._dismissed_banner_key = None
        self._update_decision_banner(self.queue_model.metrics())

    def _refresh_path_validation(self) -> None:
        text = self.source_combo.currentText().strip()
        invalid_source = bool(text) and not Path(text).expanduser().exists()
        self.source_combo.setProperty("invalid", invalid_source)
        self.source_combo.setToolTip(
            self.translator.t("gui.message.path_missing") if invalid_source else ""
        )
        self.source_combo.style().unpolish(self.source_combo)
        self.source_combo.style().polish(self.source_combo)

        output_text = self.output_edit.text().strip()
        invalid_output = bool(output_text) and Path(output_text).expanduser().is_file()
        self.output_edit.setProperty("invalid", invalid_output)
        self.output_edit.setToolTip(
            self.translator.t("gui.message.path_missing") if invalid_output else ""
        )
        self.output_edit.style().unpolish(self.output_edit)
        self.output_edit.style().polish(self.output_edit)

    def _update_queue_empty_state(self, metrics: QueueMetrics) -> None:
        if metrics.total_items == 0:
            self.queue_stack.setCurrentWidget(self.empty_state)
            self.details_panel.set_record(None, self.translator)
        else:
            self.queue_stack.setCurrentWidget(self.table_view)

    def _update_details_panel(self) -> None:
        selection_model = maybe_none(self.table_view.selectionModel())
        rows = (
            sorted(index.row() for index in selection_model.selectedRows())
            if selection_model is not None
            else []
        )
        record = self.queue_model.record_for_row(rows[0]) if rows else None
        self.details_panel.set_record(record, self.translator)

    def _selected_record(self) -> QueueItemRecord | None:
        selection_model = maybe_none(self.table_view.selectionModel())
        rows = (
            sorted(index.row() for index in selection_model.selectedRows())
            if selection_model is not None
            else []
        )
        if not rows:
            rows = self._selected_rows_from_view(self.queue_window.table_view)
        return self.queue_model.record_for_row(rows[0]) if rows else None

    def _open_selected_source_folder(self) -> None:
        record = self._selected_record()
        if record is not None:
            self._open_folder(record.source_path.parent)

    def _open_selected_output_folder(self) -> None:
        record = self._selected_record()
        if record is not None and record.plan_item is not None:
            self._open_folder(record.output_path.parent)

    def _resolve_selected_decision(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        row, current = self.queue_model.record_for_id(record.item_id)
        if row is not None and current is not None:
            self._resolve_queue_decision(row, current)

    def _clear_details_panel(self) -> None:
        self.table_view.clearSelection()
        self.details_panel.set_record(None, self.translator)

    def _resolve_next_decision(self) -> None:
        for row, record in enumerate(self.queue_model.records()):
            if record.status == QueueItemStatus.NEEDS_DECISION:
                self._resolve_queue_decision(row, record)
                return

    def _on_banner_dismissed(self) -> None:
        self._dismissed_banner_key = self._banner_key

    def _update_decision_banner(self, metrics: QueueMetrics) -> None:
        if metrics.needs_decision_items > 0:
            key: tuple[object, ...] = ("decision", metrics.needs_decision_items)
            self._banner_key = key
            if key == self._dismissed_banner_key:
                self.notification_banner.hide_message()
                return
            self.notification_banner.show_message(
                self.translator.t("gui.banner.needs_decision", count=metrics.needs_decision_items),
                severity="warning",
                actions=[(self.translator.t("gui.banner.resolve"), self._resolve_next_decision)],
            )
            return
        if self._completion_banner is not None:
            text, actions = self._completion_banner
            key = ("completion", text)
            self._banner_key = key
            if key == self._dismissed_banner_key:
                self.notification_banner.hide_message()
                return
            self.notification_banner.show_message(text, severity="success", actions=actions)
            return
        if self._runtime_banner is not None:
            text, severity, actions = self._runtime_banner
            key = ("runtime", text)
            self._banner_key = key
            if key == self._dismissed_banner_key:
                self.notification_banner.hide_message()
                return
            self.notification_banner.show_message(text, severity=severity, actions=actions)
            return
        self._banner_key = None
        self.notification_banner.hide_message()

    def _show_completion_banner(self, text: str, actions: list[tuple[str, Callable[[], None]]]) -> None:
        self._completion_banner = (text, actions)
        self._dismissed_banner_key = None
        self._update_decision_banner(self.queue_model.metrics())

    def _language_changed(self, language: str) -> None:
        self.language = language
        self.app_config["language"] = self.language
        self._persist_runtime_state()
        self._apply_translations()
        self.options_panel.sync_dependent_controls()

    def _browse_source_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.translator.t("gui.dialog.select_source_file"))
        if path:
            self.source_combo.setEditText(path)
            self._persist_runtime_state()

    def _browse_source_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, self.translator.t("gui.dialog.select_source_dir"))
        if path:
            self.source_combo.setEditText(path)
            self._persist_runtime_state()

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(self, self.translator.t("gui.dialog.select_output_dir"))
        if path:
            self.output_edit.setText(path)
            self._persist_runtime_state()

    def _refresh_presets(self) -> None:
        current_name = self.preset_combo.currentData()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem(self.translator.t("gui.value.preset_custom"), "")
        for name in list_presets(self.config_dir):
            label = (
                self.translator.t("gui.label.preset_modified", name=name)
                if self._preset_dirty and name == current_name
                else name
            )
            self.preset_combo.addItem(label, name)
        index = self.preset_combo.findData(current_name)
        if index >= 0:
            self.preset_combo.setCurrentIndex(index)
        self.preset_combo.blockSignals(False)

    def _select_preset(self, name: str) -> None:
        index = self.preset_combo.findData(name)
        if index >= 0:
            self.preset_combo.setCurrentIndex(index)

    def _mark_preset_modified(self) -> None:
        if self._loading_initial_state or self._applying_preset:
            return
        name = self.preset_combo.currentData()
        if not isinstance(name, str) or not name:
            return
        self._preset_dirty = True
        index = self.preset_combo.findData(name)
        if index >= 0:
            self.preset_combo.blockSignals(True)
            self.preset_combo.setItemText(
                index,
                self.translator.t("gui.label.preset_modified", name=name),
            )
            self.preset_combo.blockSignals(False)

    def _preset_combo_changed(self) -> None:
        self._persist_runtime_state()
        name = self.preset_combo.currentData()
        if not isinstance(name, str) or not name:
            self._preset_dirty = False
            return
        try:
            self._applying_preset = True
            self.options_panel.apply_options(load_preset(name, self.config_dir))
            self._preset_dirty = False
            self._refresh_presets()
        except Exception as exc:
            QMessageBox.critical(self, self.translator.t("gui.message.error"), str(exc))
            return
        finally:
            self._applying_preset = False
        self._append_log(self.translator.t("gui.log.preset_loaded", name=name))

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self.translator, self.app_config, self, languages=self.catalog.languages())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        old_language = self.language
        old_ffmpeg_path = str(self.app_config.get("ffmpeg_path", "")).strip()
        self.app_config.update(values)
        post_action = str(
            values.get("post_encode_action", PostEncodeAction.DO_NOTHING.value)
        )
        with QSignalBlocker(self.post_encode_combo):
            index = self.post_encode_combo.findData(post_action)
            if index >= 0:
                self.post_encode_combo.setCurrentIndex(index)
        self.options_panel.apply_analysis_profile_settings()
        self._persist_runtime_state()
        if values["language"] != old_language:
            self._language_changed(str(values["language"]))
        new_ffmpeg_path = str(values.get("ffmpeg_path", "")).strip()
        if dialog.redetect_requested or new_ffmpeg_path != old_ffmpeg_path:
            self._start_encoder_capability_detection(force_refresh=True)

    def _open_preset_manager(self) -> None:
        dialog = PresetManagerDialog(
            self.translator,
            list_presets(self.config_dir),
            str(self.app_config.get("default_preset_name", "")),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.selected_action:
            return

        action = dialog.selected_action["action"]
        name = dialog.selected_action["name"]

        try:
            if action == "load":
                self._applying_preset = True
                try:
                    options = load_preset(name, self.config_dir)
                    self.options_panel.apply_options(options)
                finally:
                    self._applying_preset = False
                self._preset_dirty = False
                self._refresh_presets()
                self._select_preset(name)
                self._append_log(self.translator.t("gui.log.preset_loaded", name=name))
            elif action == "save":
                options = self.options_panel.read_options()
                path = save_preset(name, options, self.config_dir)
                self.app_config["default_preset_name"] = name
                self._preset_dirty = False
                self._refresh_presets()
                self._select_preset(name)
                self._append_log(self.translator.t("gui.log.preset_saved", name=name, path=path))
            elif action == "delete":
                delete_preset(name, self.config_dir)
                if self.app_config.get("default_preset_name") == name:
                    self.app_config["default_preset_name"] = ""
                if self.preset_combo.currentData() == name:
                    self._preset_dirty = False
                self._refresh_presets()
                self._append_log(self.translator.t("gui.log.preset_deleted", name=name))
            elif action == "set_default":
                self.app_config["default_preset_name"] = name
                self._select_preset(name)
                self._append_log(self.translator.t("gui.log.default_preset_set", name=name))
            self._persist_runtime_state()
        except Exception as exc:
            QMessageBox.critical(self, self.translator.t("gui.message.error"), str(exc))

    def _save_app_config_preserving_capabilities(self) -> None:
        patch = {
            key: value
            for key, value in self.app_config.items()
            if key in RUNTIME_CONFIG_KEYS
        }

        def merge(data: dict) -> dict:
            data.update(patch)
            return data

        update_app_config(merge)

    def _post_encode_combo_changed(self) -> None:
        action = self.post_encode_combo.currentData()
        if action:
            self.app_config["post_encode_action"] = action
            self._persist_runtime_state()

    def _persist_runtime_state(self) -> None:
        if self._loading_initial_state:
            return
        source_text = self.source_combo.currentText().strip()
        output_text = self.output_edit.text().strip()
        analysis_profile = self.options_panel.current_analysis_profile_name().value
        post_action = self.post_encode_combo.currentData() or PostEncodeAction.DO_NOTHING.value
        self.app_config["language"] = self.language
        self.app_config["last_source_path"] = source_text
        self.app_config["last_output_dir"] = output_text
        self.app_config.setdefault("workdir_path", str(self.default_workdir))
        self.app_config.setdefault("ffmpeg_path", "")
        self.app_config.setdefault("ffprobe_path", "")
        self.app_config.setdefault("encode_workers", 1)
        self.app_config.setdefault("log_level", "info")
        self.app_config["analysis_profile"] = analysis_profile
        self.app_config["post_encode_action"] = post_action

        # Only remember a source that resolves on disk.  Persisting every
        # keystroke used to fill the history with prefixes like "/U", "/Us".
        recent_paths = list(self.app_config.get("recent_paths", []))
        if source_text and Path(source_text).expanduser().exists():
            recent_paths = [item for item in recent_paths if item != source_text]
            recent_paths.insert(0, source_text)
            recent_paths = recent_paths[:10]
            self.app_config["recent_paths"] = recent_paths
            self._set_source_history(recent_paths)

        snapshot = (
            self.language,
            source_text,
            output_text,
            analysis_profile,
            post_action,
            tuple(recent_paths),
            bool(self.app_config.get("setup_collapsed", False)),
        )
        if snapshot == self._persisted_snapshot:
            return
        self._persisted_snapshot = snapshot
        self._save_app_config_preserving_capabilities()

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in [
            self.source_file_button,
            self.source_dir_button,
            self.output_button,
            self.manage_presets_button,
            self.source_combo,
            self.preset_combo,
            self.output_edit,
            self.post_encode_combo,
        ]:
            widget.setEnabled(enabled)
        self.options_panel.set_busy(not enabled)

    def _refresh_action_state(self) -> None:
        plan_busy = self.active_worker is not None
        queue_busy = self.queue_busy
        any_busy = plan_busy or queue_busy
        has_queued_items = bool(self.queue_model.draft_records() or self.queue_model.execution_records())
        has_pending_run = self.queue_manager.has_pending_run()
        has_completed = any(
            record.status in TERMINAL_ITEM_STATUSES for record in self.queue_model.records()
        )
        signature = (plan_busy, queue_busy, has_queued_items, has_pending_run, has_completed)
        if signature == self._action_state:
            return
        self._action_state = signature

        self.add_files_action.setEnabled(not any_busy)
        self.add_folder_action.setEnabled(not any_busy)
        self.plan_action.setEnabled(not any_busy)
        self.start_queue_action.setEnabled(not any_busy and has_queued_items)
        self.pause_after_current_action.setEnabled(queue_busy and not plan_busy)
        self.stop_action.setEnabled(any_busy or has_pending_run)
        self.presets_action.setEnabled(not any_busy)
        self.settings_action.setEnabled(not any_busy)
        self.remove_selected_action.setEnabled(not any_busy)
        self.clear_completed_action.setEnabled(not any_busy and has_completed)
        self._set_controls_enabled(not any_busy)
        drag_drop_mode = QAbstractItemView.DragDropMode.NoDragDrop if any_busy else QAbstractItemView.DragDropMode.InternalMove
        for view in [self.table_view, self.queue_window.table_view]:
            view.setDragDropMode(drag_drop_mode)
            view.setDragEnabled(not any_busy)
            view.setAcceptDrops(not any_busy)

    def _has_running_task(self) -> bool:
        queue_manager_busy = self.queue_manager.is_busy()
        return (
            self.active_worker is not None
            or self.encoder_detection_worker is not None
            or self.queue_busy
            or queue_manager_busy
            or self._postprocessing
            or self.queue_completion_handler.is_busy()
        )

    def _has_unsaved_queue_work(self) -> bool:
        if not self.queue_manager.has_pending_run():
            return False
        return any(
            record.status in {
                QueueItemStatus.NEEDS_DECISION,
                QueueItemStatus.AWAITING_START,
                QueueItemStatus.QUEUED,
                QueueItemStatus.WAITING_ANALYSIS,
            }
            for record in self.queue_model.records()
        )

    def _maybe_close_after_running_task(self) -> None:
        if not self._close_after_running_task_stops:
            return
        if self._has_running_task():
            return
        self._close_after_running_task_stops = False
        QTimer.singleShot(0, self.close)

    def _set_worker_busy(self, busy: bool) -> None:
        if not busy:
            self.active_worker = None
        self._refresh_action_state()
        self._maybe_close_after_running_task()

    def _start_worker(
        self,
        worker: QueueIntakeWorker | QueuePrepareWorker,
        completed_slot: Callable[..., object],
        finished_slot: Callable[[], object] | None = None,
    ) -> None:
        self.active_worker = worker
        self._refresh_action_state()
        worker.log.connect(self._append_log)
        worker.progress.connect(self._update_progress)
        if finished_slot is not None:
            worker.finished.connect(finished_slot)
        worker.finished.connect(self._on_worker_finished_busy)
        worker.failed.connect(self._on_worker_failed)
        worker.cancelled.connect(self._on_worker_cancelled)
        worker.completed.connect(completed_slot)
        worker.start()

    def _on_worker_failed(self, message: str) -> None:
        self._append_log(f"{self.translator.t('gui.message.error')}: {message}")
        self._set_status_snapshot(self.translator.t("gui.status.failed"), "-", "-", "-", None)
        QMessageBox.critical(self, self.translator.t("gui.message.error"), message)

    def _on_worker_cancelled(self, message: str) -> None:
        self._append_log(message)
        self._set_status_snapshot(self.translator.t("gui.status.cancelled"), "-", "-", "-", None)

    def _on_queue_busy_changed(self, busy: bool) -> None:
        self.queue_busy = busy
        if not busy and not self.queue_manager.has_pending_run():
            self._asked_analysis_decisions.clear()
        self._refresh_action_state()
        self._maybe_close_after_running_task()

    def _queue_state_text(self) -> str:
        if self._queue_state == "running":
            return self.translator.t("gui.queue_state.running")
        if self._queue_state == "pause_after_current":
            return self.translator.t("gui.queue_state.pause_after_current")
        if self._queue_state == "paused":
            return self.translator.t("gui.queue_state.paused")
        if self._queue_state == "awaiting_decision":
            return self.translator.t("gui.queue_state.awaiting_decision")
        if self._queue_state == "cancelled":
            return self.translator.t("gui.queue_state.cancelled")
        if self._queue_state == "failed":
            return self.translator.t("gui.queue_state.failed")
        return self.translator.t("gui.queue_state.idle")

    def _render_queue_state(self) -> None:
        self.queue_state_label.setText(self._queue_state_text())
        self.queue_state_label.setObjectName("statusPill")
        self.queue_state_label.setProperty("state", self._queue_state)
        self.queue_state_label.style().unpolish(self.queue_state_label)
        self.queue_state_label.style().polish(self.queue_state_label)

    def _on_queue_state_changed(self, state: str) -> None:
        self._queue_state = state
        self._render_queue_state()
        if state == "pause_after_current":
            self._append_log(self.translator.t("gui.log.pause_after_current_requested"))
        elif state == "paused":
            self._append_log(self.translator.t("gui.log.queue_paused"))
        elif state == "cancelled":
            self._append_log(self.translator.t("gui.log.queue_cancelled"))
        elif state == "awaiting_decision":
            self._append_log(self.translator.t("gui.log.queue_awaiting_decision"))
            if self.app_config.get("desktop_notifications", True):
                self._send_desktop_notification(
                    self.translator.t("app.title"),
                    self.translator.t("gui.notification.awaiting_decision"),
                )
        if state in {"paused", "awaiting_decision", "cancelled", "failed"}:
            self._set_status_snapshot(self._queue_state_text(), "-", "-", "-", None)
        if state in {"idle", "paused", "awaiting_decision", "cancelled", "failed"}:
            self._refresh_action_state()

    def _records_for_ids(self, item_ids: tuple[str, ...]) -> list[QueueItemRecord]:
        records: list[QueueItemRecord] = []
        for item_id in item_ids:
            _row, record = self.queue_model.record_for_id(item_id)
            if record is not None:
                records.append(record)
        return records

    def _runtime_encoder_capabilities(self) -> dict:
        capabilities = self.app_config.get("encoder_capabilities")
        if not self._encoder_capabilities_ready or not isinstance(capabilities, dict):
            raise RuntimeError(self.translator.t("gui.message.encoder_capabilities_not_ready"))
        return capabilities

    def _on_queue_error(self, message: str) -> None:
        self._append_log(f"{self.translator.t('gui.message.error')}: {message}")
        QMessageBox.critical(self, self.translator.t("gui.message.error"), message)

    def _stop_active_task(self) -> None:
        if self._postprocessing:
            self._postprocessing_cancel_requested = True
            self.queue_completion_handler.cancel()
            return
        if self.active_worker is not None and hasattr(self.active_worker, "cancel"):
            self._append_log(self.translator.t("gui.log.stop_requested"))
            self.active_worker.cancel()
            return
        if self.queue_busy:
            self._append_log(self.translator.t("gui.log.stop_requested"))
            self.queue_manager.stop()
            return
        if self.encoder_detection_worker is not None:
            self.encoder_detection_worker.cancel()
            self._append_log(self.translator.t("gui.log.stop_requested"))
            return
        if self.queue_manager.abandon_run():
            self._asked_analysis_decisions.clear()
            self._append_log(self.translator.t("gui.log.queue_abandoned"))

    def _update_progress(self, event: dict[str, object]) -> None:
        stage = str(event.get("stage") or event.get("phase") or "-")
        state = str(event.get("state") or "-")
        file_name = str(event.get("file_name") or event.get("file_path") or "-")
        percent = event.get("file_progress")
        if not isinstance(percent, (int, float)):
            percent = event.get("percent")
        speed = str(event.get("speed") or "-")
        elapsed_sec = event.get("elapsed_sec")
        duration_sec = event.get("duration_sec")

        if isinstance(elapsed_sec, (int, float)):
            elapsed_text = format_duration(float(elapsed_sec))
            if isinstance(duration_sec, (int, float)) and float(duration_sec) > 0:
                elapsed_text = f"{elapsed_text} / {format_duration(float(duration_sec))}"
        elif isinstance(duration_sec, (int, float)) and float(duration_sec) > 0:
            elapsed_text = format_duration(float(duration_sec))
        else:
            elapsed_text = "-"

        bounded_percent = None
        if isinstance(percent, (int, float)):
            bounded_percent = max(0.0, min(100.0, float(percent)))

        stage_text = (
            self.translator.t(progress_stage_key(stage))
            if stage not in {"", "-"}
            else "-"
        )
        state_text = (
            self.translator.t(progress_state_key(state))
            if state not in {"", "-"}
            else "-"
        )
        display = f"{stage_text} · {state_text}" if state_text != "-" else stage_text
        self._set_status_snapshot(display, file_name, speed if speed else "-", elapsed_text, bounded_percent)

    def _plan_current_source(self) -> None:
        input_path = self._selected_input()
        if input_path is None:
            self.notification_banner.show_message(
                self.translator.t("gui.message.select_source"),
                severity="warning",
            )
            return
        if not input_path.exists():
            self._refresh_path_validation()
            self.notification_banner.show_message(
                self.translator.t("gui.banner.path_invalid"),
                severity="error",
            )
            return
        self._persist_runtime_state()
        worker = QueueIntakeWorker(input_path, self.options_panel.read_options().recursive, self._selected_ffprobe(), ffmpeg_path=self._selected_ffmpeg())
        self._start_worker(worker, self._on_sources_ready)

    def _start_plan_for_files(self, files: Sequence[Path | VideoFileItem]) -> None:
        if not files:
            return
        self._persist_runtime_state()
        file_items = [item if isinstance(item, VideoFileItem) else VideoFileItem(item.resolve(), Path(item.name)) for item in files]
        worker = QueueIntakeWorker(None, False, self._selected_ffprobe(), file_items, ffmpeg_path=self._selected_ffmpeg())
        self._start_worker(worker, self._on_sources_ready)

    def _on_sources_ready(self, records: list[QueueItemRecord]) -> None:
        existing = {str(record.source_path).casefold() for record in self.queue_model.records()}
        unique = []
        for record in records:
            key = str(record.source_path).casefold()
            if key not in existing:
                existing.add(key)
                unique.append(record)
        self.queue_model.add_records(unique)
        self._completion_banner = None
        self._dismissed_banner_key = None
        self._append_log(self.translator.t("gui.log.sources_added", count=len(unique)))
        self._refresh_action_state()

    def _add_files_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, self.translator.t("gui.dialog.select_source_file"))
        if not paths:
            return
        self.source_combo.setEditText(paths[0])
        self._persist_runtime_state()
        self._start_plan_for_files([Path(path) for path in paths])

    def _add_folder_dialog(self) -> None:
        path = QFileDialog.getExistingDirectory(self, self.translator.t("gui.dialog.select_source_dir"))
        if not path:
            return
        self.source_combo.setEditText(path)
        self._persist_runtime_state()
        self._plan_current_source()

    def _start_queue(self) -> None:
        if self.active_worker is not None or self.queue_manager.is_busy():
            return
        self._completion_banner = None
        self._dismissed_banner_key = None
        max_workers = parse_encode_workers(self.app_config.get("encode_workers", 1))
        records = self.queue_model.draft_records()
        if not records:
            self._start_bound_queue(max_workers)
            return
        options = self.options_panel.read_options()
        output = self._selected_output()
        workdir = self._selected_workdir()
        self._persist_runtime_state()
        requests = []
        for record in records:
            draft = record.draft
            assert draft is not None
            requests.append(EncodeRequest(draft.file_item, draft.options_override or options,
                                          draft.output_dir_override or output, draft.input_root))
        worker = QueuePrepareWorker(requests, workdir, self._selected_ffmpeg(), self._selected_ffprobe())
        plans: list[EncodePlan] = []
        ids = [record.item_id for record in records]

        def finish_preparation() -> None:
            self.queue_manager.finish_preparation()
            if not plans or worker.cancel_requested() or self._close_after_running_task_stops:
                return
            try:
                self.queue_model.apply_prepared_plan(ids, plans[0], workdir)
            except (RuntimeError, OSError, ValueError) as exc:
                self._on_worker_failed(str(exc))
                return
            self.queue_manager.include_prepared_items(ids)
            self._start_bound_queue(max_workers)

        self._append_log(self.translator.t("gui.log.preparing_queue"))
        self._set_status_snapshot(self.translator.t("gui.status.preparing_queue"), "-", "-", "-", 0.0)
        self.queue_manager.begin_preparation()
        self._start_worker(worker, plans.append, finish_preparation)

    def _start_bound_queue(self, max_workers: int) -> None:
        if not self.queue_manager.start(max_workers=max_workers):
            self.queue_manager.reconcile_after_decision()
            if not self.queue_model.execution_records():
                QMessageBox.information(self, self.translator.t("gui.message.info"), self.translator.t("gui.message.no_queued_items"))
            return
        self._append_log(self.translator.t("gui.log.encoding"))
        self._set_status_snapshot(
            self.translator.t("gui.status.starting_queue"), "-", "-", "-", 0.0
        )

    def _pause_after_current(self) -> None:
        if not self.queue_busy:
            QMessageBox.information(self, self.translator.t("gui.message.info"), self.translator.t("gui.message.no_running_queue"))
            return
        self.queue_manager.pause_after_current()

    def _show_main_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in {
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        }:
            self._show_main_window()

    def _show_queue_window(self) -> None:
        self.queue_window.show()
        self.queue_window.raise_()
        self.queue_window.activateWindow()

    def _show_activity_log(self) -> None:
        self.activity_log_window.show()
        self.activity_log_window.raise_()
        self.activity_log_window.activateWindow()

    def _show_comparison(self, record: QueueItemRecord | None = None) -> None:
        ffmpeg, ffprobe = self._selected_ffmpeg(), self._selected_ffprobe()
        if record is not None and record.job_snapshot is not None:
            ffmpeg = str(record.job_snapshot.ffmpeg_path)
            ffprobe = str(record.job_snapshot.ffprobe_path)
        window = ComparisonWindow(self.translator, self, ffmpeg=ffmpeg, ffprobe=ffprobe)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.comparison_windows.append(window)
        window.destroyed.connect(lambda: self.comparison_windows.remove(window))
        if record is not None:
            comparison_output = record.output_path
            if (
                record.result is not None
                and record.result.rejected_output_path is not None
                and not comparison_output.exists()
            ):
                comparison_output = record.result.rejected_output_path
            window.load_paths(record.source_path, comparison_output)
        window.show()
        window.raise_()
        window.activateWindow()

    def _compare_preserved(self, record: QueueItemRecord, preserved_path: Path) -> None:
        ffmpeg, ffprobe = self._selected_ffmpeg(), self._selected_ffprobe()
        if record.job_snapshot is not None:
            ffmpeg = str(record.job_snapshot.ffmpeg_path)
            ffprobe = str(record.job_snapshot.ffprobe_path)
        window = ComparisonWindow(self.translator, self, ffmpeg=ffmpeg, ffprobe=ffprobe)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.comparison_windows.append(window)
        window.destroyed.connect(lambda: self.comparison_windows.remove(window))
        window.load_paths(record.source_path, preserved_path)
        window.show()
        window.raise_()
        window.activateWindow()

    def _selected_rows_from_view(self, view: ResponsiveQueueTableView) -> list[int]:
        return sorted(index.row() for index in view.selectionModel().selectedRows())

    def _show_queue_context_menu(self, view: ResponsiveQueueTableView, pos: QPoint) -> None:
        menu = QMenu(self)
        rows = self._selected_rows_from_view(view)
        selected_record = self.queue_model.record_for_row(rows[0]) if rows else None

        has_selection = bool(rows)
        busy = self.active_worker is not None or self.queue_manager.is_busy()
        can_edit = has_selection and self.queue_model.can_edit_rows(rows) and not busy
        can_reconfigure = can_edit

        open_source_action = menu.addAction(self.translator.t("gui.menu.open_source_folder"))
        open_output_action = menu.addAction(self.translator.t("gui.menu.open_output_folder"))
        copy_source_action = menu.addAction(self.translator.t("gui.menu.copy_source_path"))
        copy_output_action = menu.addAction(self.translator.t("gui.menu.copy_output_path"))
        compare_action = menu.addAction(self.translator.t("gui.compare.queue_action"))
        compare_action.setEnabled(
            len(rows) == 1
            and selected_record is not None
            and (
                selected_record.status == QueueItemStatus.DONE
                or (
                    selected_record.result is not None
                    and selected_record.result.rejected_output_path is not None
                )
            )
        )
        menu.addSeparator()

        apply_options_action = menu.addAction(self.translator.t("gui.menu.apply_current_options"))
        preset_sub_menu = menu.addMenu(self.translator.t("gui.menu.apply_preset"))
        follow_options_action = menu.addAction(self.translator.t("gui.menu.follow_global_options"))
        follow_output_action = menu.addAction(self.translator.t("gui.menu.follow_global_output"))
        change_output_action = menu.addAction(self.translator.t("gui.menu.change_output_dir"))
        menu.addSeparator()

        retry_action = menu.addAction(self.translator.t("gui.menu.retry_selected"))
        resolve_action = menu.addAction(self.translator.t("gui.menu.resolve_decision"))
        remove_action = menu.addAction(self.translator.t("gui.menu.remove_from_queue"))
        clear_completed_action = menu.addAction(self.translator.t("gui.menu.clear_completed"))

        open_source_action.setEnabled(has_selection)
        has_output = selected_record is not None and selected_record.plan_item is not None
        open_output_action.setEnabled(has_output)
        copy_source_action.setEnabled(has_selection)
        copy_output_action.setEnabled(has_output)
        apply_options_action.setEnabled(can_reconfigure)
        preset_sub_menu.setEnabled(can_reconfigure)
        change_output_action.setEnabled(can_edit)
        follow_options_action.setEnabled(can_edit)
        follow_output_action.setEnabled(can_edit)

        available_presets = list_presets(self.config_dir)
        preset_actions: dict[object, str] = {}
        for p_name in available_presets:
            p_action = preset_sub_menu.addAction(p_name)
            preset_actions[p_action] = p_name

        retry_action.setEnabled(has_selection and self.queue_model.can_retry_rows(rows) and not busy)
        resolve_action.setEnabled(
            len(rows) == 1 and self.queue_model.can_resolve_row(rows[0]) and not busy
        )
        remove_action.setEnabled(
            has_selection
            and self.queue_manager.can_remove_rows(rows)
            and not busy
        )
        clear_completed_action.setEnabled(
            not busy
            and any(record.status in TERMINAL_ITEM_STATUSES for record in self.queue_model.records())
        )

        action = maybe_none(menu.exec(view.viewport().mapToGlobal(pos)))
        if action is None:
            return
        if action == open_source_action and selected_record is not None:
            self._open_folder(selected_record.source_path.parent)
        elif action == open_output_action and selected_record is not None:
            self._open_folder(selected_record.output_path.parent)
        elif action == copy_source_action and selected_record is not None:
            self._copy_to_clipboard(str(selected_record.source_path))
        elif action == copy_output_action and selected_record is not None:
            self._copy_to_clipboard(str(selected_record.output_path))
        elif action == compare_action and selected_record is not None:
            self._show_comparison(selected_record)
        elif action == apply_options_action and can_reconfigure:
            opts = self.options_panel.read_options()
            try:
                updated = self.queue_model.apply_options_to_rows(
                    rows,
                    opts,
                    runtime_capabilities=self._runtime_encoder_capabilities(),
                )
            except Exception as exc:
                QMessageBox.critical(self, self.translator.t("gui.message.error"), str(exc))
            else:
                if updated:
                    self._append_log(self.translator.t("gui.log.batch_options_applied", count=updated))
        elif action in preset_actions and can_reconfigure:
            preset_name = preset_actions[action]
            try:
                opts = load_preset(preset_name, self.config_dir)
                updated = self.queue_model.apply_options_to_rows(
                    rows,
                    opts,
                    runtime_capabilities=self._runtime_encoder_capabilities(),
                )
                if updated:
                    self._append_log(self.translator.t("gui.log.batch_preset_applied", count=updated, name=preset_name))
            except Exception as exc:
                QMessageBox.critical(self, self.translator.t("gui.message.error"), str(exc))
        elif action == change_output_action and can_edit:
            path = QFileDialog.getExistingDirectory(self, self.translator.t("gui.dialog.select_output_dir"))
            if path:
                try:
                    updated = self.queue_model.apply_output_dir_to_rows(rows, Path(path))
                except Exception as exc:
                    QMessageBox.critical(self, self.translator.t("gui.message.error"), str(exc))
                else:
                    if updated:
                        self._append_log(self.translator.t("gui.log.batch_output_dir_applied", count=updated, dir=path))
        elif action == follow_options_action and can_edit:
            self.queue_model.restore_global_options(rows)
        elif action == follow_output_action and can_edit:
            self.queue_model.restore_global_output(rows)
        elif action == retry_action:
            retried = self.queue_manager.retry_rows(rows)
            if retried:
                self._append_log(self.translator.t("gui.log.retry_selected", count=retried))
        elif action == resolve_action and selected_record is not None:
            self._resolve_queue_decision(rows[0], selected_record)
        elif action == remove_action:
            removed = self.queue_manager.remove_rows(rows)
            if removed:
                self._append_log(self.translator.t("gui.log.removed_from_queue", count=removed))
        elif action == clear_completed_action:
            removed = self.queue_manager.clear_completed()
            if removed:
                self._append_log(self.translator.t("gui.log.cleared_completed", count=removed))

    def _resolve_queue_decision(self, row: int, record: QueueItemRecord) -> None:
        result = record.result
        if result is not None and result.rejected_output_path is not None:
            choice = choose_size_miss_decision(
                self,
                self.translator,
                record,
                compare=lambda path: self._compare_preserved(record, path),
            )
            if choice is None:
                return
            resolved = False
            if choice == SizeMissDecision.ACCEPT:
                resolved = self.queue_model.accept_size_miss(row)
            elif choice == SizeMissDecision.RETRY:
                resolved = self.queue_model.retry_size_miss(row)
            elif choice == SizeMissDecision.DISCARD:
                resolved = self.queue_model.discard_size_miss(row)
            if not resolved:
                self._report_decision_failure(record)
                return
            self._append_log(
                self.translator.t("gui.log.decision_resolved", file=record.source_path.name, action=choice.value)
            )
            if record.result is not None:
                for warning in record.result.external_subtitle_warnings:
                    self._append_log(warning)
            if self._postprocessing:
                return
            if choice == SizeMissDecision.RETRY:
                self.queue_manager.resume_after_decision(
                    parse_encode_workers(self.app_config.get("encode_workers", 1))
                )
            else:
                self.queue_manager.reconcile_after_decision()
            return

        options = self.queue_model.decision_options_for_row(row)
        if not options:
            return
        decision = choose_quality_decision(self, self.translator, record, options)
        if decision is None:
            return
        if not self.queue_model.apply_quality_decision(row, decision):
            self._report_decision_failure(record)
            return
        self._append_log(
            self.translator.t(
                "gui.log.decision_resolved",
                file=record.source_path.name,
                action=decision.action_code.value,
            )
        )
        if self._postprocessing:
            return
        if decision.action_code in {
            DecisionActionCode.RELAX_SIZE,
            DecisionActionCode.RELAX_QUALITY,
            DecisionActionCode.CHANGE_MEDIA_BUDGET,
            DecisionActionCode.REANALYZE,
        }:
            self.queue_manager.resume_after_decision(
                parse_encode_workers(self.app_config.get("encode_workers", 1))
            )
        else:
            self._process_stopped_records(QueueRunCompletion("local-decision", (record.item_id,)), ask_analysis=False)

    def _report_decision_failure(self, record: QueueItemRecord) -> None:
        detail = record.error_summary or self.translator.t("gui.message.decision_failed")
        self._append_log(f"{self.translator.t('gui.message.error')}: {detail}")
        QMessageBox.warning(self, self.translator.t("gui.message.warning"), detail)

    def _on_queue_row_activated(self, _view: ResponsiveQueueTableView, index: QModelIndex) -> None:
        if self.active_worker is not None or self.queue_manager.is_busy():
            return
        row = index.row()
        record = self.queue_model.record_for_row(row)
        if record is None or not self.queue_model.can_resolve_row(row):
            return
        self._resolve_queue_decision(row, record)

    def _show_header_context_menu(self, view: ResponsiveQueueTableView, pos: QPoint) -> None:
        menu = QMenu(self)
        header = view.horizontalHeader()
        for column in range(self.queue_model.columnCount()):
            label = self.queue_model.headerData(column, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
            action = menu.addAction(str(label))
            action.setCheckable(True)
            action.setChecked(not view.isColumnHidden(column))
            action.setData(column)
        menu.addSeparator()
        reset_action = menu.addAction(self.translator.t("gui.menu.reset_queue_order"))
        reset_action.setEnabled(self.queue_model.has_sort_order())
        chosen = maybe_none(menu.exec(header.mapToGlobal(pos)))
        if chosen is None:
            return
        if chosen == reset_action:
            self.queue_model.reset_order()
            self._persist_header_state(view)
            return
        column = int(chosen.data())
        view.setColumnHidden(column, not chosen.isChecked())
        self._persist_header_state(view)
        self._reflow_queue_views()

    def _reflow_queue_views(self) -> None:
        for view in [self.table_view, self.queue_window.table_view]:
            reflow = getattr(view, "schedule_reflow", None)
            if callable(reflow):
                reflow()

    def _persist_header_state(self, source_view: ResponsiveQueueTableView) -> None:
        if self._header_sync_guard:
            return
        state = source_view.horizontalHeader().saveState()
        self.app_config["queue_table_header_state"] = bytes(state.toBase64().data()).decode("ascii")
        self._save_app_config_preserving_capabilities()

        self._header_sync_guard = True
        try:
            for view in [self.table_view, self.queue_window.table_view]:
                if view is source_view:
                    continue
                view.horizontalHeader().restoreState(state)
            self._reflow_queue_views()
        finally:
            self._header_sync_guard = False

    def _restore_header_state(self) -> None:
        encoded = str(self.app_config.get("queue_table_header_state", "")).strip()
        if not encoded:
            self._reflow_queue_views()
            return
        try:
            raw = QByteArray.fromBase64(encoded.encode("ascii"))
        except Exception:
            self._reflow_queue_views()
            return
        self._header_sync_guard = True
        try:
            for view in [self.table_view, self.queue_window.table_view]:
                view.horizontalHeader().restoreState(raw)
            self._reflow_queue_views()
        finally:
            self._header_sync_guard = False

    def _copy_to_clipboard(self, text: str) -> None:
        QApplication.clipboard().setText(text)

    def _open_folder(self, path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self.active_worker is not None or self.queue_busy or self.queue_manager.is_busy():
            event.ignore()
            return
        if event.mimeData().hasUrls():
            urls = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            if urls:
                event.acceptProposedAction()
                self._set_drop_overlay_visible(True)
                return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self.active_worker is not None or self.queue_busy or self.queue_manager.is_busy():
            event.ignore()
            return
        if event.mimeData().hasUrls():
            urls = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            if urls:
                event.acceptProposedAction()
                self._set_drop_overlay_visible(True)
                return
        super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if self.active_worker is not None or self.queue_busy or self.queue_manager.is_busy():
            event.ignore()
            return
        if event.mimeData().hasUrls():
            urls = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
            if urls:
                event.acceptProposedAction()
                self._set_drop_overlay_visible(False)
                self._handle_dropped_paths(urls)
                return
        super().dropEvent(event)

    def _handle_dropped_paths(self, paths: list[Path]) -> None:
        if (
            not paths
            or self.active_worker is not None
            or self.queue_busy
            or self.queue_manager.is_busy()
        ):
            return
        existing_paths = [p.resolve() for p in paths if p.exists()]
        if not existing_paths:
            return
        if len(existing_paths) == 1 and existing_paths[0].is_dir():
            self.source_combo.setEditText(str(existing_paths[0]))
            self._persist_runtime_state()
            self._plan_current_source()
            return

        all_video_files: list[VideoFileItem] = []
        for p in existing_paths:
            if p.is_file():
                if p.suffix.lower() in VIDEO_EXTENSIONS:
                    all_video_files.append(
                        VideoFileItem(path=p.resolve(), relative_path=Path(p.name))
                    )
            elif p.is_dir():
                try:
                    discovered = collect_video_files(p, recursive=self.options_panel.read_options().recursive)
                    all_video_files.extend(discovered)
                except Exception:
                    pass
        if not all_video_files:
            self._append_log(self.translator.t("gui.log.no_video_files_dropped"))
            return
        if len(existing_paths) == 1 and existing_paths[0].is_file():
            self.source_combo.setEditText(str(existing_paths[0]))
            self._persist_runtime_state()
        self._start_plan_for_files(all_video_files)

    @staticmethod
    def _escape_applescript(text: str) -> str:
        return text.replace("\\", "\\\\").replace('"', '\\"')

    def _send_desktop_notification(self, title: str, message: str) -> None:
        if self.tray_icon.isSystemTrayAvailable() and not self.tray_icon.icon().isNull():
            if not self.tray_icon.isVisible():
                self.tray_icon.show()
            self.tray_icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)
        elif sys.platform == "darwin":
            try:
                import subprocess
                escaped_message = self._escape_applescript(message)
                escaped_title = self._escape_applescript(title)
                subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'display notification "{escaped_message}" with title "{escaped_title}"',
                    ],
                    check=False,
                    capture_output=True,
                )
            except Exception:
                pass

    def _on_queue_execution_stopped(self, completion: QueueRunCompletion) -> None:
        if self._close_after_running_task_stops:
            self.queue_manager.abandon_run()
            self._asked_analysis_decisions.clear()
            return
        self._process_stopped_records(completion, ask_analysis=True)

    def _process_stopped_records(self, completion: QueueRunCompletion, *, ask_analysis: bool) -> None:
        if self._postprocessing:
            return
        self._postprocessing = True
        self._postprocessing_cancel_requested = False
        self.queue_manager.begin_postprocessing()
        records = self._records_for_ids(completion.item_ids)
        if ask_analysis:
            for record in records:
                if self._postprocessing_cancel_requested or self._close_after_running_task_stops:
                    self._on_postprocessing_finished(True)
                    return
                if (record.status == QueueItemStatus.NEEDS_DECISION and record.result is not None
                        and record.result.rejected_output_path is None):
                    key = (completion.run_id, record.item_id)
                    if self._asked_analysis_decisions.get(key) is record.result:
                        continue
                    self._asked_analysis_decisions[key] = record.result
                    row, current = self.queue_model.record_for_id(record.item_id)
                    if row is not None and current is not None:
                        self._resolve_queue_decision(row, current)
        if self._postprocessing_cancel_requested or self._close_after_running_task_stops:
            self._on_postprocessing_finished(True)
            return
        self.queue_completion_handler.process_stopped(
            records, self.translator, self.queue_model.apply_result,
            lambda cancelled: self._on_postprocessing_finished(cancelled),
        )

    def _on_postprocessing_finished(self, cancelled: bool) -> None:
        self._postprocessing = False
        if cancelled:
            self.queue_manager.abandon_run()
            self._asked_analysis_decisions.clear()
        self.queue_manager.finish_postprocessing()
        if not cancelled and not self._close_after_running_task_stops:
            # The old worker's finished signal must return before another run starts.
            QTimer.singleShot(0, self._resume_after_postprocessing)
        self._refresh_action_state()
        self._maybe_close_after_running_task()

    def _resume_after_postprocessing(self) -> None:
        if not self._postprocessing and not self._close_after_running_task_stops:
            self.queue_manager.resume_after_decision(parse_encode_workers(self.app_config.get("encode_workers", 1)))

    def _on_queue_run_completed(self, completion: QueueRunCompletion) -> None:
        self._asked_analysis_decisions = {
            key: result for key, result in self._asked_analysis_decisions.items() if key[0] != completion.run_id
        }
        records = self._records_for_ids(completion.item_ids)
        self._register_completion_banner(records)
        self.queue_completion_handler.handle(records, self.translator, self.app_config)

    def _register_completion_banner(self, records: list[QueueItemRecord]) -> None:
        done = [record for record in records if record.status == QueueItemStatus.DONE]
        failed = [record for record in records if record.status == QueueItemStatus.FAILED]
        cancelled = [record for record in records if record.status == QueueItemStatus.CANCELLED]
        if not done and not failed and not cancelled:
            return
        actions: list[tuple[str, Callable[[], None]]] = []
        output_record = next(
            (record for record in done if record.plan_item is not None),
            None,
        )
        if output_record is not None:
            output_parent = output_record.output_path.parent
            actions.append(
                (
                    self.translator.t("gui.completion.open_output"),
                    self._open_folder_action(output_parent),
                )
            )
        actions.append(
            (
                self.translator.t("gui.completion.view_log"),
                self._show_activity_log,
            )
        )
        if done:
            measured = compute_metrics(records)
            saved = measured.actual_saved_bytes or 0
            source_bytes = measured.actual_source_bytes or 0
            ratio = (saved / source_bytes * 100.0) if source_bytes > 0 else 0.0
            text = self.translator.t(
                "gui.completion.title",
            ) + " · " + self.translator.t(
                "gui.completion.summary",
                count=len(done),
                saved=format_size(saved),
                ratio=f"{ratio:.1f}",
            )
            if failed or cancelled:
                text += "\n" + self.translator.t(
                    "gui.completion.incomplete",
                    failed=len(failed),
                    cancelled=len(cancelled),
                )
            self._show_completion_banner(text, actions)
        else:
            self._show_completion_banner(
                self.translator.t(
                    "gui.completion.incomplete",
                    failed=len(failed),
                    cancelled=len(cancelled),
                ),
                actions,
            )
