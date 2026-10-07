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
        self.resize(760, 720)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        general = QGridLayout()
        general.setHorizontalSpacing(10)
        general.setVerticalSpacing(10)

        self.language_label = QLabel()
        self.language_combo = QComboBox()
        for info in self._languages:
            self.language_combo.addItem(info.name, info.code)

        self.workdir_label = QLabel()
        self.workdir_edit = QLineEdit()
        self.workdir_button = QPushButton()

        self.ffmpeg_label = QLabel()
        self.ffmpeg_edit = QLineEdit()
        self.ffmpeg_button = QPushButton()

        self.ffprobe_label = QLabel()
        self.ffprobe_edit = QLineEdit()
        self.ffprobe_button = QPushButton()

        self.log_level_label = QLabel()
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(["info", "debug"])

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
        general.addWidget(self.ffmpeg_label, 2, 0)
        general.addWidget(self.ffmpeg_edit, 2, 1)
        general.addWidget(self.ffmpeg_button, 2, 2)
        general.addWidget(self.ffprobe_label, 3, 0)
        general.addWidget(self.ffprobe_edit, 3, 1)
        general.addWidget(self.ffprobe_button, 3, 2)
        general.addWidget(self.log_level_label, 4, 0)
        general.addWidget(self.log_level_combo, 4, 1, 1, 2)
        general.addWidget(self.encode_workers_label, 5, 0)
        general.addWidget(self.encode_workers_spin, 5, 1, 1, 2)
        general.addWidget(self.post_encode_label, 6, 0)
        general.addWidget(self.post_encode_combo, 6, 1, 1, 2)
        general.addWidget(self.desktop_notifications_check, 7, 0, 1, 3)
        layout.addLayout(general)

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
        analysis_layout.addLayout(active_row)

        layout.addWidget(self.analysis_group)

        self.redetect_button = QPushButton()
        layout.addWidget(self.redetect_button)
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
        self.redetect_button.clicked.connect(self._request_redetect)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)

    def _load_settings(self, settings: dict[str, object]) -> None:
        language = str(settings.get("language", "en"))
        index = self.language_combo.findData(language)
        if index >= 0:
            self.language_combo.setCurrentIndex(index)
        self.workdir_edit.setText(str(settings.get("workdir_path", "")))
        self.ffmpeg_edit.setText(str(settings.get("ffmpeg_path", "")))
        self.ffprobe_edit.setText(str(settings.get("ffprobe_path", "")))
        self.log_level_combo.setCurrentText(str(settings.get("log_level", "info")))
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
        self.language_label.setText(self.translator.t("gui.label.language"))
        self.workdir_label.setText(self.translator.t("gui.label.workdir"))
        self.ffmpeg_label.setText(self.translator.t("gui.label.ffmpeg"))
        self.ffprobe_label.setText(self.translator.t("gui.label.ffprobe"))
        self.log_level_label.setText(self.translator.t("gui.label.log_level"))
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

    def values(self) -> dict[str, object]:
        return {
            "language": self.language_combo.currentData(),
            "workdir_path": self.workdir_edit.text().strip(),
            "ffmpeg_path": self.ffmpeg_edit.text().strip(),
            "ffprobe_path": self.ffprobe_edit.text().strip(),
            "log_level": self.log_level_combo.currentText(),
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
            "analysis_profiles": {},
        }

    def _browse_workdir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, self.translator.t("gui.dialog.select_workdir"))
        if path:
            self.workdir_edit.setText(path)

    def _browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.translator.t("gui.dialog.select_ffmpeg"))
        if path:
            self.ffmpeg_edit.setText(path)

    def _browse_ffprobe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.translator.t("gui.dialog.select_ffprobe"))
        if path:
            self.ffprobe_edit.setText(path)

    def _request_redetect(self) -> None:
        self.redetect_requested = True
        self.accept()
