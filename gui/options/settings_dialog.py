from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.config import parse_encode_workers, smart_policies_from_config
from core.i18n import LanguageInfo, Translator
from core.models import (
    AnalysisProfileName,
    QualityUnreachablePolicy,
    SizeBlockedPolicy,
    SkippedOutputPolicy,
)
from core.smart import parse_analysis_profile_name
from core.media import (
    PostEncodeAction,
    parse_post_encode_action,
    post_encode_action_key,
)
from gui.qt_optionals import maybe_none
from gui.icons import set_button_icon
from gui.window_geometry import clamped_window_size

class SettingsDialog(QDialog):
    def __init__(
        self,
        tr: Translator,
        settings: dict[str, object],
        parent: QWidget | None = None,
        languages: list[LanguageInfo] | None = None,
    ) -> None:
        super().__init__(parent)
        self.translator = tr
        self.redetect_requested = False
        self._languages = languages or [
            LanguageInfo("en", "English"),
            LanguageInfo("zh_cn", "简体中文"),
        ]
        self._build_ui()
        self._load_settings(settings)
        self.apply_translations(tr)

    def _build_ui(self) -> None:
        self.resize(clamped_window_size(780, 760, minimum_width=640, minimum_height=480))
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        self.queue_group = QGroupBox()
        general = QGridLayout(self.queue_group)
        general.setHorizontalSpacing(10)
        general.setVerticalSpacing(8)

        self.language_label = QLabel()
        self.language_combo = QComboBox()
        for info in self._languages:
            self.language_combo.addItem(info.name, info.code)

        self.workdir_label = QLabel()
        self.workdir_edit = QLineEdit()
        self.workdir_button = QPushButton()

        self.log_level_label = QLabel()
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItem("", "info")
        self.log_level_combo.addItem("", "debug")

        self.encode_workers_label = QLabel()
        self.encode_workers_spin = QSpinBox()
        self.encode_workers_spin.setRange(1, 8)

        self.post_encode_label = QLabel()
        self.post_encode_combo = QComboBox()
        for action in PostEncodeAction:
            self.post_encode_combo.addItem("", action.value)

        self.desktop_notifications_check = QCheckBox()

        general.addWidget(self.language_label, 0, 0)
        general.addWidget(self.language_combo, 0, 1, 1, 2)
        general.addWidget(self.workdir_label, 1, 0)
        general.addWidget(self.workdir_edit, 1, 1)
        general.addWidget(self.workdir_button, 1, 2)
        general.addWidget(self.log_level_label, 2, 0)
        general.addWidget(self.log_level_combo, 2, 1, 1, 2)
        general.addWidget(self.encode_workers_label, 3, 0)
        general.addWidget(self.encode_workers_spin, 3, 1, 1, 2)
        general.addWidget(self.post_encode_label, 4, 0)
        general.addWidget(self.post_encode_combo, 4, 1, 1, 2)
        general.addWidget(self.desktop_notifications_check, 5, 0, 1, 3)
        general.setColumnStretch(1, 1)
        for combo in (self.language_combo, self.log_level_combo, self.post_encode_combo):
            combo.setMaximumWidth(320)
        self.encode_workers_spin.setMaximumWidth(120)
        layout.addWidget(self.queue_group)

        self.toolchain_group = QGroupBox()
        toolchain = QGridLayout(self.toolchain_group)
        toolchain.setHorizontalSpacing(10)
        toolchain.setVerticalSpacing(8)

        self.ffmpeg_label = QLabel()
        self.ffmpeg_edit = QLineEdit()
        self.ffmpeg_button = QPushButton()

        self.ffprobe_label = QLabel()
        self.ffprobe_edit = QLineEdit()
        self.ffprobe_button = QPushButton()

        self.check_paths_button = QPushButton()
        self.path_status_label = QLabel()
        self.path_status_label.setObjectName("setupSummary")
        self.path_status_label.setWordWrap(True)
        self.redetect_button = QPushButton()
        set_button_icon(self.redetect_button, "refresh")
        self.detection_hint_label = QLabel()
        self.detection_hint_label.setObjectName("setupSummary")
        self.detection_hint_label.setWordWrap(True)
        self.detection_hint_label.setVisible(False)

        toolchain.addWidget(self.ffmpeg_label, 0, 0)
        toolchain.addWidget(self.ffmpeg_edit, 0, 1)
        toolchain.addWidget(self.ffmpeg_button, 0, 2)
        toolchain.addWidget(self.ffprobe_label, 1, 0)
        toolchain.addWidget(self.ffprobe_edit, 1, 1)
        toolchain.addWidget(self.ffprobe_button, 1, 2)
        toolchain.addWidget(self.check_paths_button, 2, 1)
        toolchain.addWidget(self.redetect_button, 2, 2)
        toolchain.addWidget(self.path_status_label, 3, 0, 1, 3)
        toolchain.addWidget(self.detection_hint_label, 4, 0, 1, 3)
        toolchain.setColumnStretch(1, 1)
        for combo in (self.ffmpeg_edit, self.ffprobe_edit):
            combo.setMinimumWidth(260)
        layout.addWidget(self.toolchain_group)

        self.policy_group = QGroupBox()
        policy_layout = QGridLayout(self.policy_group)
        policy_layout.setHorizontalSpacing(10)
        policy_layout.setVerticalSpacing(8)
        self.size_blocked_policy_label = QLabel()
        self.size_blocked_policy_combo = QComboBox()
        self.quality_unreachable_policy_label = QLabel()
        self.quality_unreachable_policy_combo = QComboBox()
        self.skipped_output_policy_label = QLabel()
        self.skipped_output_policy_combo = QComboBox()
        policy_layout.addWidget(self.size_blocked_policy_label, 0, 0)
        policy_layout.addWidget(self.size_blocked_policy_combo, 0, 1)
        policy_layout.addWidget(self.quality_unreachable_policy_label, 1, 0)
        policy_layout.addWidget(self.quality_unreachable_policy_combo, 1, 1)
        policy_layout.addWidget(self.skipped_output_policy_label, 2, 0)
        policy_layout.addWidget(self.skipped_output_policy_combo, 2, 1)
        policy_layout.setColumnStretch(1, 1)
        for combo in (self.size_blocked_policy_combo, self.quality_unreachable_policy_combo, self.skipped_output_policy_combo):
            combo.setMinimumWidth(0)
            combo.setMinimumContentsLength(18)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        for label in (self.size_blocked_policy_label, self.quality_unreachable_policy_label, self.skipped_output_policy_label):
            label.setWordWrap(True)
        layout.addWidget(self.policy_group)

        self.analysis_group = QGroupBox()
        analysis_layout = QVBoxLayout(self.analysis_group)
        active_row = QHBoxLayout()
        self.analysis_profile_label = QLabel()
        self.analysis_profile_combo = QComboBox()
        for name in AnalysisProfileName:
            self.analysis_profile_combo.addItem("", name.value)
        active_row.addWidget(self.analysis_profile_label)
        active_row.addWidget(self.analysis_profile_combo, 1)
        self.analysis_profile_combo.setMaximumWidth(280)
        active_row.addStretch(1)
        analysis_layout.addLayout(active_row)
        layout.addWidget(self.analysis_group)

        layout.addStretch(1)

        scroll.setWidget(body)
        root.addWidget(scroll)

        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(12, 0, 12, 12)
        buttons.addWidget(self.button_box)
        root.addLayout(buttons)

        self.workdir_button.clicked.connect(self._browse_workdir)
        self.ffmpeg_button.clicked.connect(self._browse_ffmpeg)
        self.ffprobe_button.clicked.connect(self._browse_ffprobe)
        self.check_paths_button.clicked.connect(self._check_paths)
        self.redetect_button.clicked.connect(self._request_redetect)
        self.ffmpeg_edit.editingFinished.connect(self._check_paths)
        self.ffprobe_edit.editingFinished.connect(self._check_paths)
        self.workdir_edit.editingFinished.connect(self._check_paths)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

    def _path_problem(self, value: str, *, expect_directory: bool) -> str | None:
        if not value.strip():
            return None
        path = Path(value).expanduser()
        if not path.exists():
            return None if expect_directory else self.translator.t("gui.message.path_missing")
        if expect_directory and not path.is_dir():
            return self.translator.t("gui.message.path_not_directory")
        if not expect_directory and not path.is_file():
            return self.translator.t("gui.message.path_missing")
        return None

    def _check_paths(self) -> None:
        problems: list[str] = []
        workdir_problem = self._path_problem(self.workdir_edit.text(), expect_directory=True)
        if workdir_problem:
            problems.append(f"{self.translator.t('gui.label.workdir')}: {workdir_problem}")
        ffmpeg_text = self.ffmpeg_edit.text().strip()
        if ffmpeg_text:
            ffmpeg_problem = self._path_problem(ffmpeg_text, expect_directory=False)
            if ffmpeg_problem:
                problems.append(
                    f"{self.translator.t('gui.label.ffmpeg')}: "
                    f"{self.translator.t('gui.message.ffmpeg_not_file')}"
                )
        ffprobe_text = self.ffprobe_edit.text().strip()
        if ffprobe_text:
            ffprobe_problem = self._path_problem(ffprobe_text, expect_directory=False)
            if ffprobe_problem:
                problems.append(
                    f"{self.translator.t('gui.label.ffprobe')}: "
                    f"{self.translator.t('gui.message.ffprobe_not_file')}"
                )
        self.path_status_label.setText(
            "\n".join(problems) if problems else self.translator.t("gui.status.paths_ok")
        )
        self.path_status_label.setProperty("invalid", bool(problems))
        self.path_status_label.style().unpolish(self.path_status_label)
        self.path_status_label.style().polish(self.path_status_label)

    def _load_settings(self, settings: dict[str, object]) -> None:
        language = str(settings.get("language", "en"))
        index = self.language_combo.findData(language)
        if index >= 0:
            self.language_combo.setCurrentIndex(index)
        self.workdir_edit.setText(str(settings.get("workdir_path", "")))
        self.ffmpeg_edit.setText(str(settings.get("ffmpeg_path", "")))
        self.ffprobe_edit.setText(str(settings.get("ffprobe_path", "")))
        log_index = self.log_level_combo.findData(str(settings.get("log_level", "info")))
        if log_index >= 0:
            self.log_level_combo.setCurrentIndex(log_index)
        self.encode_workers_spin.setValue(parse_encode_workers(settings.get("encode_workers", 1)))
        self.size_blocked_policy_combo.clear()
        self.size_blocked_policy_combo.addItem("", SizeBlockedPolicy.RELAX_SIZE.value)
        self.size_blocked_policy_combo.addItem("", SizeBlockedPolicy.RELAX_QUALITY.value)
        self.size_blocked_policy_combo.addItem("", SizeBlockedPolicy.ASK.value)
        self.quality_unreachable_policy_combo.clear()
        self.quality_unreachable_policy_combo.addItem("", QualityUnreachablePolicy.SKIP.value)
        self.quality_unreachable_policy_combo.addItem("", QualityUnreachablePolicy.ASK.value)
        self.skipped_output_policy_combo.clear()
        self.skipped_output_policy_combo.addItem("", SkippedOutputPolicy.COPY.value)
        self.skipped_output_policy_combo.addItem("", SkippedOutputPolicy.ASK.value)
        self.skipped_output_policy_combo.addItem("", SkippedOutputPolicy.IGNORE.value)
        size_policy, unreachable_policy, skipped_policy = smart_policies_from_config(settings)
        size_index = self.size_blocked_policy_combo.findData(size_policy.value)
        if size_index >= 0:
            self.size_blocked_policy_combo.setCurrentIndex(size_index)
        unreachable_index = self.quality_unreachable_policy_combo.findData(unreachable_policy.value)
        if unreachable_index >= 0:
            self.quality_unreachable_policy_combo.setCurrentIndex(unreachable_index)
        skipped_index = self.skipped_output_policy_combo.findData(skipped_policy.value)
        if skipped_index >= 0:
            self.skipped_output_policy_combo.setCurrentIndex(skipped_index)

        profile_name = parse_analysis_profile_name(settings.get("analysis_profile"))
        profile_index = self.analysis_profile_combo.findData(profile_name.value)
        if profile_index >= 0:
            self.analysis_profile_combo.setCurrentIndex(profile_index)

        post_action = parse_post_encode_action(settings.get("post_encode_action", PostEncodeAction.DO_NOTHING.value))
        post_index = self.post_encode_combo.findData(post_action.value)
        if post_index >= 0:
            self.post_encode_combo.setCurrentIndex(post_index)
        self.desktop_notifications_check.setChecked(bool(settings.get("desktop_notifications", True)))

    def apply_translations(self, tr: Translator) -> None:
        self.translator = tr
        self.setWindowTitle(self.translator.t("gui.window.settings"))
        self.queue_group.setTitle(self.translator.t("gui.group.queue_settings"))
        self.toolchain_group.setTitle(self.translator.t("gui.group.toolchain"))
        self.language_label.setText(self.translator.t("gui.label.language"))
        self.workdir_label.setText(self.translator.t("gui.label.workdir"))
        self.workdir_label.setToolTip(self.translator.t("gui.tooltip.workdir"))
        self.workdir_edit.setToolTip(self.translator.t("gui.tooltip.workdir"))
        self.ffmpeg_label.setText(self.translator.t("gui.label.ffmpeg"))
        self.ffprobe_label.setText(self.translator.t("gui.label.ffprobe"))
        self.log_level_label.setText(self.translator.t("gui.label.log_level"))
        self.log_level_combo.setItemText(
            self.log_level_combo.findData("info"),
            self.translator.t("gui.value.log_info"),
        )
        self.log_level_combo.setItemText(
            self.log_level_combo.findData("debug"),
            self.translator.t("gui.value.log_debug"),
        )
        self.encode_workers_label.setText(self.translator.t("gui.label.encode_workers"))
        encode_workers_tooltip = self.translator.t("gui.tooltip.encode_workers")
        self.encode_workers_label.setToolTip(encode_workers_tooltip)
        self.encode_workers_spin.setToolTip(encode_workers_tooltip)
        self.post_encode_label.setText(self.translator.t("gui.label.post_encode_action"))
        self.desktop_notifications_check.setText(self.translator.t("gui.label.desktop_notifications"))
        for action in PostEncodeAction:
            idx = self.post_encode_combo.findData(action.value)
            if idx >= 0:
                self.post_encode_combo.setItemText(idx, self.translator.t(post_encode_action_key(action)))
        self.policy_group.setTitle(self.translator.t("gui.group.smart_policies"))
        self.size_blocked_policy_label.setText(self.translator.t("gui.label.size_blocked_policy"))
        self.quality_unreachable_policy_label.setText(self.translator.t("gui.label.quality_unreachable_policy"))
        self.size_blocked_policy_combo.setItemText(
            self.size_blocked_policy_combo.findData(SizeBlockedPolicy.RELAX_SIZE.value),
            self.translator.t("gui.value.size_blocked_relax_size"),
        )
        self.size_blocked_policy_combo.setItemText(
            self.size_blocked_policy_combo.findData(SizeBlockedPolicy.RELAX_QUALITY.value),
            self.translator.t("gui.value.size_blocked_relax_quality"),
        )
        self.size_blocked_policy_combo.setItemText(
            self.size_blocked_policy_combo.findData(SizeBlockedPolicy.ASK.value),
            self.translator.t("gui.value.size_blocked_ask"),
        )
        self.quality_unreachable_policy_combo.setItemText(
            self.quality_unreachable_policy_combo.findData(QualityUnreachablePolicy.SKIP.value),
            self.translator.t("gui.value.quality_unreachable_skip"),
        )
        self.quality_unreachable_policy_combo.setItemText(
            self.quality_unreachable_policy_combo.findData(QualityUnreachablePolicy.ASK.value),
            self.translator.t("gui.value.quality_unreachable_ask"),
        )
        self.skipped_output_policy_label.setText(self.translator.t("gui.label.skipped_output_policy"))
        self.skipped_output_policy_combo.setItemText(
            self.skipped_output_policy_combo.findData(SkippedOutputPolicy.COPY.value),
            self.translator.t("gui.value.skipped_output_copy"),
        )
        self.skipped_output_policy_combo.setItemText(
            self.skipped_output_policy_combo.findData(SkippedOutputPolicy.ASK.value),
            self.translator.t("gui.value.skipped_output_ask"),
        )
        self.skipped_output_policy_combo.setItemText(
            self.skipped_output_policy_combo.findData(SkippedOutputPolicy.IGNORE.value),
            self.translator.t("gui.value.skipped_output_ignore"),
        )
        self.analysis_group.setTitle(self.translator.t("gui.group.analysis_scan"))
        self.analysis_profile_label.setText(self.translator.t("gui.label.analysis_active_profile"))
        self.analysis_profile_combo.setItemText(
            self.analysis_profile_combo.findData(AnalysisProfileName.FAST.value),
            self.translator.t("gui.value.analysis_fast"),
        )
        self.analysis_profile_combo.setItemText(
            self.analysis_profile_combo.findData(AnalysisProfileName.BALANCE.value),
            self.translator.t("gui.value.analysis_balance"),
        )
        self.analysis_profile_combo.setItemText(
            self.analysis_profile_combo.findData(AnalysisProfileName.PRECISE.value),
            self.translator.t("gui.value.analysis_precise"),
        )
        self.workdir_button.setText(self.translator.t("gui.button.browse_dir"))
        self.ffmpeg_button.setText(self.translator.t("gui.button.browse_exe"))
        self.ffprobe_button.setText(self.translator.t("gui.button.browse_exe"))
        self.redetect_button.setText(self.translator.t("gui.button.redetect_encoders"))
        self.check_paths_button.setText(self.translator.t("gui.button.check_paths"))
        if self.redetect_requested:
            self.detection_hint_label.setText(self.translator.t("gui.status.re_detect_pending"))
        self.ffmpeg_edit.setPlaceholderText(self.translator.t("gui.placeholder.ffmpeg"))
        self.ffprobe_edit.setPlaceholderText(self.translator.t("gui.placeholder.ffprobe"))
        self.ffmpeg_edit.setToolTip(self.translator.t("gui.placeholder.ffmpeg"))
        self.ffprobe_edit.setToolTip(self.translator.t("gui.placeholder.ffprobe"))
        size_policy_tooltip = self.translator.t("gui.tooltip.size_blocked_policy")
        self.size_blocked_policy_label.setToolTip(size_policy_tooltip)
        self.size_blocked_policy_combo.setToolTip(size_policy_tooltip)
        skipped_policy_tooltip = self.translator.t("gui.tooltip.skipped_output_policy")
        self.skipped_output_policy_label.setToolTip(skipped_policy_tooltip)
        self.skipped_output_policy_combo.setToolTip(skipped_policy_tooltip)
        self.analysis_profile_combo.setToolTip(self.translator.t("gui.tooltip.analysis_profile"))
        ok_button = maybe_none(self.button_box.button(QDialogButtonBox.StandardButton.Ok))
        cancel_button = maybe_none(self.button_box.button(QDialogButtonBox.StandardButton.Cancel))
        if ok_button is not None:
            ok_button.setText(self.translator.t("gui.button.ok"))
        if cancel_button is not None:
            cancel_button.setText(self.translator.t("gui.button.cancel"))
        if self.detection_hint_label.isVisible():
            self.detection_hint_label.setText(self.translator.t("gui.status.re_detect_pending"))
        pairs = (
            (self.language_label, self.language_combo),
            (self.workdir_label, self.workdir_edit),
            (self.log_level_label, self.log_level_combo),
            (self.encode_workers_label, self.encode_workers_spin),
            (self.post_encode_label, self.post_encode_combo),
            (self.ffmpeg_label, self.ffmpeg_edit),
            (self.ffprobe_label, self.ffprobe_edit),
            (self.size_blocked_policy_label, self.size_blocked_policy_combo),
            (self.quality_unreachable_policy_label, self.quality_unreachable_policy_combo),
            (self.skipped_output_policy_label, self.skipped_output_policy_combo),
            (self.analysis_profile_label, self.analysis_profile_combo),
        )
        for label, widget in pairs:
            label.setBuddy(widget)
            widget.setAccessibleName(label.text())

    def values(self) -> dict[str, object]:
        return {
            "language": self.language_combo.currentData(),
            "workdir_path": self.workdir_edit.text().strip(),
            "ffmpeg_path": self.ffmpeg_edit.text().strip(),
            "ffprobe_path": self.ffprobe_edit.text().strip(),
            "log_level": self.log_level_combo.currentData() or "info",
            "encode_workers": self.encode_workers_spin.value(),
            "post_encode_action": self.post_encode_combo.currentData() or PostEncodeAction.DO_NOTHING.value,
            "desktop_notifications": self.desktop_notifications_check.isChecked(),
            "size_blocked_policy": self.size_blocked_policy_combo.currentData()
            or SizeBlockedPolicy.RELAX_SIZE.value,
            "quality_unreachable_policy": self.quality_unreachable_policy_combo.currentData()
            or QualityUnreachablePolicy.SKIP.value,
            "skipped_output_policy": self.skipped_output_policy_combo.currentData()
            or SkippedOutputPolicy.COPY.value,
            "analysis_profile": self.analysis_profile_combo.currentData()
            or AnalysisProfileName.BALANCE.value,
        }

    def _browse_workdir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, self.translator.t("gui.dialog.select_workdir"))
        if path:
            self.workdir_edit.setText(path)
            self._check_paths()

    def _browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.translator.t("gui.dialog.select_ffmpeg"))
        if path:
            self.ffmpeg_edit.setText(path)
            self._check_paths()

    def _browse_ffprobe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.translator.t("gui.dialog.select_ffprobe"))
        if path:
            self.ffprobe_edit.setText(path)
            self._check_paths()

    def _request_redetect(self) -> None:
        self.redetect_requested = True
        self.detection_hint_label.setText(self.translator.t("gui.status.re_detect_pending"))
        self.detection_hint_label.setVisible(True)
