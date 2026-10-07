from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Final

from PySide6.QtCore import Signal
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.config import smart_policies_from_config
from core.ffmpeg import (
    ENCODER_CANDIDATES,
    available_backends_for_codec,
    preset_choices_from_capabilities,
)
from core.i18n import Translator
from core.models import (
    AnalysisProfileName,
    AudioMode,
    BackendChoice,
    CodecChoice,
    CompressionMode,
    ContainerChoice,
    DecodeAcceleration,
    EncodeOptions,
    VmafViewingContext,
)
from core.smart import analysis_profiles_from_config, parse_analysis_profile_name, resolve_max_output_ratio


EXPLICIT_BACKEND_ORDER: tuple[BackendChoice, ...] = (
    BackendChoice.NVENC,
    BackendChoice.QSV,
    BackendChoice.AMF,
    BackendChoice.VIDEOTOOLBOX,
    BackendChoice.CPU,
)


class _PresetUnset(Enum):
    """Sentinel distinguishing an unsupplied preset from an explicit ``None``."""

    UNSET = "unset"


_PRESET_UNSET: Final = _PresetUnset.UNSET


class EncodeOptionsPanel(QWidget):
    """The Basic/Video/Audio-Subtitles options region.

    Owns all option widgets and their internal wiring. MainWindow coordinates
    through the public read/apply/set methods and the semantic signals below; it
    must not reach into the raw option widgets.
    """

    codec_changed = Signal(object)
    compression_mode_changed = Signal(object)
    analysis_profile_changed = Signal(object)
    options_changed = Signal()

    def __init__(
        self,
        tr: Translator,
        app_config: dict,
        parent: QWidget | None = None,
        append_log: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.translator = tr
        self.app_config = app_config
        self._append_log = append_log if append_log is not None else (lambda message: None)
        self._encoder_capabilities_ready = False
        self._runtime_capabilities_snapshot: dict | None = None
        self._pending_backend: BackendChoice | None = None
        self._pending_encoder_preset: str | None = None
        self._last_codec_for_ratio = CodecChoice.HEVC

        self.options_tabs = QTabWidget()
        self._build_basic_tab()
        self._build_video_tab()
        self._build_audio_tab()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.options_tabs)

        self._connect_signals()

    def _connect_signals(self) -> None:
        self.compression_mode_combo.currentIndexChanged.connect(self._on_compression_mode_changed)
        self.audio_mode_combo.currentIndexChanged.connect(self.sync_dependent_controls)
        self.codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        self.backend_combo.currentIndexChanged.connect(self.refresh_encoder_preset_choices)
        self.decode_acceleration_combo.currentIndexChanged.connect(self.sync_dependent_controls)
        self.analysis_profile_combo.currentIndexChanged.connect(self._on_analysis_profile_changed)

    def _on_compression_mode_changed(self, *_args) -> None:
        self.sync_dependent_controls()
        value = self.compression_mode_combo.currentData() or self.compression_mode_combo.currentText()
        self.compression_mode_changed.emit(CompressionMode(value))

    def _on_analysis_profile_changed(self, *_args) -> None:
        self.analysis_profile_changed.emit(self.current_analysis_profile_name())

    # ------------------------------------------------------------------ building

    def _build_basic_tab(self) -> None:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(10)

        self.codec_label = QLabel()
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["hevc", "av1"])

        self.compression_mode_label = QLabel()
        self.compression_mode_combo = QComboBox()
        self.compression_mode_combo.addItem("smart", CompressionMode.SMART.value)
        self.compression_mode_combo.addItem("fixed_bitrate", CompressionMode.FIXED_BITRATE.value)

        self.backend_label = QLabel()
        self.backend_combo = QComboBox()
        self.backend_combo.addItems(["auto", "cpu"])

        self.container_label = QLabel()
        self.container_combo = QComboBox()
        self.container_combo.addItems(["mkv", "mp4"])

        self.ratio_label = QLabel()
        self.ratio_edit = QLineEdit()

        self.min_vmaf_label = QLabel()
        self.min_vmaf_spin = QDoubleSpinBox()
        self.min_vmaf_spin.setRange(1.0, 100.0)
        self.min_vmaf_spin.setDecimals(1)
        self.min_vmaf_spin.setValue(90.0)

        self.max_output_ratio_label = QLabel()
        self.max_output_ratio_spin = QDoubleSpinBox()
        self.max_output_ratio_spin.setRange(1.0, 100.0)
        self.max_output_ratio_spin.setDecimals(1)
        self.max_output_ratio_spin.setSuffix("%")
        self.max_output_ratio_spin.setValue(70.0)

        self.overwrite_check = QCheckBox()
        self.recursive_check = QCheckBox()

        layout.addWidget(self.codec_label, 0, 0)
        layout.addWidget(self.codec_combo, 0, 1)
        layout.addWidget(self.compression_mode_label, 0, 2)
        layout.addWidget(self.compression_mode_combo, 0, 3)
        layout.addWidget(self.backend_label, 0, 4)
        layout.addWidget(self.backend_combo, 0, 5)
        layout.addWidget(self.container_label, 1, 0)
        layout.addWidget(self.container_combo, 1, 1)
        layout.addWidget(self.ratio_label, 1, 2)
        layout.addWidget(self.ratio_edit, 1, 3)
        layout.addWidget(self.min_vmaf_label, 1, 4)
        layout.addWidget(self.min_vmaf_spin, 1, 5)
        layout.addWidget(self.max_output_ratio_label, 2, 0)
        layout.addWidget(self.max_output_ratio_spin, 2, 1)
        layout.addWidget(self.overwrite_check, 2, 2)
        layout.addWidget(self.recursive_check, 2, 3)

        self.analysis_profile_label = QLabel()
        self.analysis_profile_combo = QComboBox()
        self._fill_analysis_profile_combo()
        layout.addWidget(self.analysis_profile_label, 3, 0)
        layout.addWidget(self.analysis_profile_combo, 3, 1)

        self.viewing_context_label = QLabel()
        self.viewing_context_combo = QComboBox()
        for context in VmafViewingContext:
            self.viewing_context_combo.addItem("", context.value)
        layout.addWidget(self.viewing_context_label, 3, 2)
        layout.addWidget(self.viewing_context_combo, 3, 3)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)
        layout.setColumnStretch(5, 1)

        self.options_tabs.addTab(page, "")
        self.basic_tab = page

    def _build_video_tab(self) -> None:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(10)

        self.encoder_preset_label = QLabel()
        self.encoder_preset_combo = QComboBox()

        self.decode_acceleration_label = QLabel()
        self.decode_acceleration_combo = QComboBox()
        self.decode_acceleration_combo.addItem("", DecodeAcceleration.SOFTWARE.value)
        self.decode_acceleration_combo.addItem("", DecodeAcceleration.VIDEOTOOLBOX.value)

        self.pix_fmt_label = QLabel()
        self.pix_fmt_edit = QLineEdit()

        self.min_bitrate_label = QLabel()
        self.min_bitrate_spin = QSpinBox()
        self.min_bitrate_spin.setRange(0, 500000)
        self.min_bitrate_spin.setValue(250)

        self.max_bitrate_label = QLabel()
        self.max_bitrate_spin = QSpinBox()
        self.max_bitrate_spin.setRange(0, 500000)
        self.max_bitrate_spin.setValue(0)

        self.maxrate_factor_label = QLabel()
        self.maxrate_factor_spin = QDoubleSpinBox()
        self.maxrate_factor_spin.setRange(0.1, 20.0)
        self.maxrate_factor_spin.setDecimals(2)
        self.maxrate_factor_spin.setSingleStep(0.05)
        self.maxrate_factor_spin.setValue(1.08)

        self.bufsize_factor_label = QLabel()
        self.bufsize_factor_spin = QDoubleSpinBox()
        self.bufsize_factor_spin.setRange(0.1, 20.0)
        self.bufsize_factor_spin.setDecimals(2)
        self.bufsize_factor_spin.setSingleStep(0.10)
        self.bufsize_factor_spin.setValue(2.0)

        self.two_pass_check = QCheckBox()

        layout.addWidget(self.encoder_preset_label, 0, 0)
        layout.addWidget(self.encoder_preset_combo, 0, 1)
        layout.addWidget(self.decode_acceleration_label, 0, 2)
        layout.addWidget(self.decode_acceleration_combo, 0, 3)
        layout.addWidget(self.pix_fmt_label, 1, 0)
        layout.addWidget(self.pix_fmt_edit, 1, 1)
        layout.addWidget(self.min_bitrate_label, 1, 2)
        layout.addWidget(self.min_bitrate_spin, 1, 3)
        layout.addWidget(self.max_bitrate_label, 2, 0)
        layout.addWidget(self.max_bitrate_spin, 2, 1)
        layout.addWidget(self.maxrate_factor_label, 2, 2)
        layout.addWidget(self.maxrate_factor_spin, 2, 3)
        layout.addWidget(self.bufsize_factor_label, 3, 0)
        layout.addWidget(self.bufsize_factor_spin, 3, 1)
        layout.addWidget(self.two_pass_check, 3, 2, 1, 2)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)

        self.options_tabs.addTab(page, "")
        self.video_tab = page

    def _build_audio_tab(self) -> None:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(10)

        self.audio_mode_label = QLabel()
        self.audio_mode_combo = QComboBox()
        self.audio_mode_combo.addItems(["copy", "aac"])

        self.audio_bitrate_label = QLabel()
        self.audio_bitrate_edit = QLineEdit()

        self.copy_subtitles_check = QCheckBox()
        self.copy_external_subtitles_check = QCheckBox()

        layout.addWidget(self.audio_mode_label, 0, 0)
        layout.addWidget(self.audio_mode_combo, 0, 1)
        layout.addWidget(self.audio_bitrate_label, 0, 2)
        layout.addWidget(self.audio_bitrate_edit, 0, 3)
        layout.addWidget(self.copy_subtitles_check, 1, 0, 1, 2)
        layout.addWidget(self.copy_external_subtitles_check, 1, 2, 1, 2)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(3, 1)

        self.options_tabs.addTab(page, "")
        self.audio_tab = page

    def _fill_analysis_profile_combo(self) -> None:
        self.analysis_profile_combo.clear()
        for name in AnalysisProfileName:
            self.analysis_profile_combo.addItem("", name.value)

    # ---------------------------------------------------------------- profiles

    def apply_analysis_profile_settings(self) -> None:
        name, _settings = analysis_profiles_from_config(self.app_config)
        index = self.analysis_profile_combo.findData(name.value)
        if index >= 0:
            self.analysis_profile_combo.setCurrentIndex(index)

    def current_analysis_profile_name(self) -> AnalysisProfileName:
        return parse_analysis_profile_name(self.analysis_profile_combo.currentData())

    # ------------------------------------------------------- encode options io

    def read_options(self) -> EncodeOptions:
        ratio_text = self.ratio_edit.text().strip()
        preset_value = self.encoder_preset_combo.currentData()
        encoder_preset = str(preset_value) if isinstance(preset_value, str) and preset_value else None
        pix_fmt = self.pix_fmt_edit.text().strip() or "yuv420p"
        size_policy, unreachable_policy, skipped_policy = smart_policies_from_config(self.app_config)
        profile_name, profile_settings = analysis_profiles_from_config(
            {
                **self.app_config,
                "analysis_profile": self.current_analysis_profile_name().value,
            }
        )
        return EncodeOptions(
            codec=CodecChoice(self.codec_combo.currentText()),
            compression_mode=CompressionMode(
                self.compression_mode_combo.currentData()
                or self.compression_mode_combo.currentText()
            ),
            backend=BackendChoice(self.backend_combo.currentText()),
            ratio=float(ratio_text) if ratio_text else None,
            min_vmaf=float(self.min_vmaf_spin.value()),
            viewing_context=VmafViewingContext(self.viewing_context_combo.currentData()),
            max_output_ratio=float(self.max_output_ratio_spin.value()) / 100.0,
            min_video_kbps=int(self.min_bitrate_spin.value()),
            max_video_kbps=int(self.max_bitrate_spin.value()),
            container=ContainerChoice(self.container_combo.currentText()),
            audio_mode=AudioMode(self.audio_mode_combo.currentText()),
            audio_bitrate=self.audio_bitrate_edit.text().strip() or "128k",
            copy_subtitles=self.copy_subtitles_check.isChecked(),
            copy_external_subtitles=self.copy_external_subtitles_check.isChecked(),
            two_pass=self.two_pass_check.isChecked(),
            decode_acceleration=DecodeAcceleration(
                self.decode_acceleration_combo.currentData()
                or self.decode_acceleration_combo.currentText()
            ),
            encoder_preset=encoder_preset,
            pix_fmt=pix_fmt,
            maxrate_factor=float(self.maxrate_factor_spin.value()),
            bufsize_factor=float(self.bufsize_factor_spin.value()),
            overwrite=self.overwrite_check.isChecked(),
            recursive=self.recursive_check.isChecked(),
            size_blocked_policy=size_policy,
            quality_unreachable_policy=unreachable_policy,
            skipped_output_policy=skipped_policy,
            analysis_profile=profile_name,
            analysis_settings=profile_settings,
        )

    def apply_options(self, options: EncodeOptions) -> None:
        self.codec_combo.setCurrentText(options.codec.value)
        mode_index = self.compression_mode_combo.findData(options.compression_mode.value)
        if mode_index >= 0:
            self.compression_mode_combo.setCurrentIndex(mode_index)
        self._rebuild_backend_controls(preferred_backend=options.backend, log_reset=options.backend != BackendChoice.AUTO)
        self._refresh_decode_acceleration_choices(
            preferred=options.decode_acceleration,
            log_reset=options.decode_acceleration != DecodeAcceleration.SOFTWARE,
        )
        self.ratio_edit.setText("" if options.ratio is None else str(options.ratio))
        self.min_vmaf_spin.setValue(options.min_vmaf)
        viewing_index = self.viewing_context_combo.findData(options.viewing_context.value)
        if viewing_index >= 0:
            self.viewing_context_combo.setCurrentIndex(viewing_index)
        self.max_output_ratio_spin.setValue(
            resolve_max_output_ratio(options.codec, options.max_output_ratio) * 100.0
        )
        self.container_combo.setCurrentText(options.container.value)
        self.audio_mode_combo.setCurrentText(options.audio_mode.value)
        self.audio_bitrate_edit.setText(options.audio_bitrate)
        self.pix_fmt_edit.setText(options.pix_fmt)
        self.min_bitrate_spin.setValue(options.min_video_kbps)
        self.max_bitrate_spin.setValue(options.max_video_kbps)
        self.maxrate_factor_spin.setValue(options.maxrate_factor)
        self.bufsize_factor_spin.setValue(options.bufsize_factor)
        self.copy_subtitles_check.setChecked(options.copy_subtitles)
        self.copy_external_subtitles_check.setChecked(options.copy_external_subtitles)
        self.two_pass_check.setChecked(options.two_pass)
        self.overwrite_check.setChecked(options.overwrite)
        self.recursive_check.setChecked(options.recursive)
        self.refresh_encoder_preset_choices(preset=options.encoder_preset, log_invalid=options.encoder_preset is not None)
        self.sync_dependent_controls()

    # ------------------------------------------------------ capabilities logic

    def begin_capability_detection(self) -> None:
        self._encoder_capabilities_ready = False
        self._runtime_capabilities_snapshot = None
        self._rebuild_backend_controls()
        # Do not show stale choices while the worker refreshes the snapshot.
        # ``refresh_encoder_preset_choices`` retains a selected value so it
        # can be restored after detection completes.
        self.refresh_encoder_preset_choices()

    def set_runtime_capabilities(self, capabilities: dict) -> None:
        self._encoder_capabilities_ready = True
        self._runtime_capabilities_snapshot = dict(capabilities)
        pending_backend = self._pending_backend
        self._pending_backend = None
        pending_preset = self._pending_encoder_preset
        self._pending_encoder_preset = None
        self._rebuild_backend_controls(
            preferred_backend=pending_backend,
            log_reset=pending_backend is not None,
        )
        self._refresh_decode_acceleration_choices(log_reset=True)
        self._refresh_smart_mode_availability()
        self.refresh_encoder_preset_choices(preset=pending_preset)

    def notify_capability_detection_failed(self) -> None:
        self._encoder_capabilities_ready = False
        self._runtime_capabilities_snapshot = None
        self._pending_backend = None
        self._pending_encoder_preset = None
        self._rebuild_backend_controls()
        self.refresh_encoder_preset_choices()
        self._refresh_decode_acceleration_choices(log_reset=True, force_unavailable=True)
        self._refresh_smart_mode_availability(force_unavailable=True)

    def set_busy(self, busy: bool) -> None:
        self.options_tabs.setEnabled(not busy)

    def set_translator(self, translator: Translator) -> None:
        self.translator = translator
        self.codec_label.setText(self.translator.t("gui.label.codec"))
        self.compression_mode_label.setText(self.translator.t("gui.label.compression_mode"))
        smart_mode_index = self.compression_mode_combo.findData(CompressionMode.SMART.value)
        fixed_mode_index = self.compression_mode_combo.findData(CompressionMode.FIXED_BITRATE.value)
        if smart_mode_index >= 0:
            self.compression_mode_combo.setItemText(
                smart_mode_index,
                self.translator.t("gui.value.compression_smart"),
            )
        if fixed_mode_index >= 0:
            self.compression_mode_combo.setItemText(
                fixed_mode_index,
                self.translator.t("gui.value.compression_fixed"),
            )
        self.backend_label.setText(self.translator.t("gui.label.backend"))
        self.container_label.setText(self.translator.t("gui.label.container"))
        self.ratio_label.setText(self.translator.t("gui.label.ratio"))
        self.min_vmaf_label.setText(self.translator.t("gui.label.min_vmaf"))
        self.min_vmaf_label.setToolTip(self.translator.t("gui.tooltip.min_vmaf"))
        self.min_vmaf_spin.setToolTip(self.translator.t("gui.tooltip.min_vmaf"))
        self.max_output_ratio_label.setText(self.translator.t("gui.label.max_output_ratio"))
        self.analysis_profile_label.setText(self.translator.t("gui.label.analysis_profile"))
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
        self.analysis_profile_combo.setToolTip(self.translator.t("gui.tooltip.analysis_profile"))
        self.viewing_context_label.setText(self.translator.t("gui.label.viewing_context"))
        self.viewing_context_combo.setItemText(
            self.viewing_context_combo.findData(VmafViewingContext.HIGH_FIDELITY.value),
            self.translator.t("gui.value.viewing_high_fidelity"),
        )
        self.viewing_context_combo.setItemText(
            self.viewing_context_combo.findData(VmafViewingContext.STANDARD_DISPLAY.value),
            self.translator.t("gui.value.viewing_standard_display"),
        )
        viewing_tooltip = self.translator.t("gui.tooltip.viewing_context")
        self.viewing_context_label.setToolTip(viewing_tooltip)
        self.viewing_context_combo.setToolTip(viewing_tooltip)
        self.overwrite_check.setText(self.translator.t("gui.checkbox.overwrite"))
        self.recursive_check.setText(self.translator.t("gui.checkbox.recursive"))
        self.encoder_preset_label.setText(self.translator.t("gui.label.encoder_preset"))
        self.decode_acceleration_label.setText(self.translator.t("gui.label.decode_acceleration"))
        self.decode_acceleration_combo.setItemText(
            self.decode_acceleration_combo.findData(DecodeAcceleration.SOFTWARE.value),
            self.translator.t("gui.value.decode_software"),
        )
        self.decode_acceleration_combo.setItemText(
            self.decode_acceleration_combo.findData(DecodeAcceleration.VIDEOTOOLBOX.value),
            self.translator.t("gui.value.decode_videotoolbox"),
        )
        self.pix_fmt_label.setText(self.translator.t("gui.label.pix_fmt"))
        self.min_bitrate_label.setText(self.translator.t("gui.label.min_video_kbps"))
        self.max_bitrate_label.setText(self.translator.t("gui.label.max_video_kbps"))
        self.maxrate_factor_label.setText(self.translator.t("gui.label.maxrate_factor"))
        self.bufsize_factor_label.setText(self.translator.t("gui.label.bufsize_factor"))
        self.two_pass_check.setText(self.translator.t("gui.checkbox.two_pass"))
        self.audio_mode_label.setText(self.translator.t("gui.label.audio_mode"))
        self.audio_bitrate_label.setText(self.translator.t("gui.label.audio_bitrate"))
        self.copy_subtitles_check.setText(self.translator.t("gui.checkbox.copy_subtitles"))
        self.copy_external_subtitles_check.setText(self.translator.t("gui.checkbox.copy_external_subtitles"))
        self.ratio_edit.setPlaceholderText(self.translator.t("gui.placeholder.auto_ratio"))
        self.pix_fmt_edit.setPlaceholderText("yuv420p")
        self.audio_bitrate_edit.setPlaceholderText("128k")

        self.options_tabs.setTabText(self.options_tabs.indexOf(self.basic_tab), self.translator.t("gui.tab.basic"))
        self.options_tabs.setTabText(self.options_tabs.indexOf(self.video_tab), self.translator.t("gui.tab.video"))
        self.options_tabs.setTabText(self.options_tabs.indexOf(self.audio_tab), self.translator.t("gui.tab.audio_subtitles"))

        self._rebuild_backend_controls()
        self.refresh_encoder_preset_choices()

    # ---------------------------------------------------- backend/preset logic

    def _on_codec_changed(self, *_args) -> None:
        current_codec = self._current_codec()
        previous_default = resolve_max_output_ratio(self._last_codec_for_ratio, None) * 100.0
        if abs(self.max_output_ratio_spin.value() - previous_default) < 0.05:
            self.max_output_ratio_spin.setValue(
                resolve_max_output_ratio(current_codec, None) * 100.0
            )
        self._last_codec_for_ratio = current_codec
        self._rebuild_backend_controls()
        self.refresh_encoder_preset_choices()
        self.codec_changed.emit(current_codec)

    def _current_codec(self) -> CodecChoice:
        return CodecChoice(self.codec_combo.currentText())

    def _runtime_capabilities(self) -> dict | None:
        return (
            self._runtime_capabilities_snapshot
            if self._encoder_capabilities_ready
            else None
        )

    def _refresh_decode_acceleration_choices(
        self,
        *,
        preferred: object = _PRESET_UNSET,
        log_reset: bool = False,
        force_unavailable: bool = False,
    ) -> None:
        current_value = self.decode_acceleration_combo.currentData()
        desired_value = current_value if preferred is _PRESET_UNSET else preferred
        try:
            desired = DecodeAcceleration(desired_value)
        except ValueError:
            desired = DecodeAcceleration.SOFTWARE

        capabilities = self._runtime_capabilities()
        runtime_known = capabilities is not None or force_unavailable
        hwaccels = capabilities.get("hwaccels", []) if capabilities is not None else []
        videotoolbox_available = (
            not force_unavailable
            and isinstance(hwaccels, list)
            and "videotoolbox" in {str(item).strip().lower() for item in hwaccels}
        )

        videotoolbox_index = self.decode_acceleration_combo.findData(DecodeAcceleration.VIDEOTOOLBOX.value)
        model = self.decode_acceleration_combo.model()
        if videotoolbox_index >= 0 and isinstance(model, QStandardItemModel):
            item = model.item(videotoolbox_index)
            if item is not None:
                item.setEnabled(videotoolbox_available)
        if runtime_known and not videotoolbox_available:
            self.decode_acceleration_combo.setToolTip(
                self.translator.t("gui.tooltip.decode_acceleration_unavailable")
            )
        else:
            self.decode_acceleration_combo.setToolTip("")

        if desired == DecodeAcceleration.VIDEOTOOLBOX and runtime_known and not videotoolbox_available:
            desired = DecodeAcceleration.SOFTWARE
            if log_reset:
                self._append_log(self.translator.t("gui.log.decode_acceleration_reset"))

        selected_index = self.decode_acceleration_combo.findData(desired.value)
        if selected_index < 0:
            selected_index = self.decode_acceleration_combo.findData(DecodeAcceleration.SOFTWARE.value)
        if selected_index >= 0:
            self.decode_acceleration_combo.blockSignals(True)
            self.decode_acceleration_combo.setCurrentIndex(selected_index)
            self.decode_acceleration_combo.blockSignals(False)

    def _capability_has_backend(self, codec: CodecChoice, backend: BackendChoice) -> bool:
        if backend not in ENCODER_CANDIDATES[codec]:
            return False
        capabilities = self._runtime_capabilities()
        if capabilities is None:
            return backend == BackendChoice.CPU
        return backend in available_backends_for_codec(capabilities, codec)

    def _available_explicit_backends_for_current_codec(self) -> list[BackendChoice]:
        codec = self._current_codec()
        return [backend for backend in EXPLICIT_BACKEND_ORDER if self._capability_has_backend(codec, backend)]

    def _rebuild_backend_controls(
        self,
        *,
        preferred_backend: BackendChoice | None = None,
        log_reset: bool = False,
    ) -> None:
        current_text = self.backend_combo.currentText().strip()
        try:
            current_backend = BackendChoice(current_text) if current_text else BackendChoice.AUTO
        except ValueError:
            current_backend = BackendChoice.AUTO
        desired_backend = preferred_backend or current_backend

        choices = [BackendChoice.AUTO, *self._available_explicit_backends_for_current_codec()]
        if self._runtime_capabilities() is None and BackendChoice.CPU not in choices:
            choices.append(BackendChoice.CPU)

        if desired_backend in choices:
            selected_backend = desired_backend
        else:
            selected_backend = BackendChoice.AUTO
            if self._runtime_capabilities() is None and desired_backend != BackendChoice.AUTO:
                self._pending_backend = desired_backend
            if log_reset and desired_backend != BackendChoice.AUTO:
                if not (
                    self._runtime_capabilities() is None
                    and desired_backend != BackendChoice.AUTO
                ):
                    self._append_log(
                        self.translator.t(
                            "gui.log.backend_reset",
                            backend=desired_backend.value,
                            fallback=selected_backend.value,
                        )
                    )

        self.backend_combo.blockSignals(True)
        self.backend_combo.clear()
        for backend in choices:
            self.backend_combo.addItem(backend.value, backend.value)
        self.backend_combo.setCurrentText(selected_backend.value)
        self.backend_combo.blockSignals(False)
        tooltip_key = "gui.tooltip.backend_filtered" if self._runtime_capabilities() is not None else "gui.tooltip.backend_detecting"
        self.backend_combo.setToolTip(self.translator.t(tooltip_key))

        self.sync_dependent_controls()

    def sync_dependent_controls(self) -> None:
        mode_value = self.compression_mode_combo.currentData() or self.compression_mode_combo.currentText()
        smart_mode = mode_value == CompressionMode.SMART.value
        self.ratio_edit.setEnabled(not smart_mode)
        self.min_vmaf_spin.setEnabled(smart_mode)
        self.max_output_ratio_spin.setEnabled(smart_mode)
        self.analysis_profile_combo.setEnabled(smart_mode)
        self.viewing_context_label.setVisible(smart_mode)
        self.viewing_context_combo.setVisible(smart_mode)
        self.viewing_context_combo.setEnabled(smart_mode)
        self.audio_bitrate_edit.setEnabled(self.audio_mode_combo.currentText() == AudioMode.AAC.value)

    def _refresh_smart_mode_availability(self, *, force_unavailable: bool = False) -> None:
        capabilities = self._runtime_capabilities()
        vmaf = capabilities.get("vmaf") if capabilities else None
        known = force_unavailable or isinstance(vmaf, dict)
        available = bool(isinstance(vmaf, dict) and vmaf.get("runnable"))
        index = self.compression_mode_combo.findData(CompressionMode.SMART.value)
        model = self.compression_mode_combo.model()
        if index >= 0 and isinstance(model, QStandardItemModel):
            item = model.item(index)
            if item is not None:
                item.setEnabled(not known or available)
        if known and not available:
            if self.compression_mode_combo.currentData() == CompressionMode.SMART.value:
                fixed_index = self.compression_mode_combo.findData(CompressionMode.FIXED_BITRATE.value)
                if fixed_index >= 0:
                    self.compression_mode_combo.setCurrentIndex(fixed_index)
            message = ""
            if isinstance(vmaf, dict):
                message = str(vmaf.get("error_message") or "")
            self.compression_mode_combo.setToolTip(
                message or self.translator.t("gui.tooltip.smart_unavailable")
            )
        else:
            self.compression_mode_combo.setToolTip("")
        self.sync_dependent_controls()

    def _default_preset_text(self) -> str:
        return self.translator.t("gui.value.encoder_preset_default")

    def _current_encoder_preset(self) -> str | None:
        value = self.encoder_preset_combo.currentData()
        return value if isinstance(value, str) and value else None

    def _set_encoder_preset_items(self, choices: list[str]) -> None:
        self.encoder_preset_combo.blockSignals(True)
        self.encoder_preset_combo.clear()
        self.encoder_preset_combo.addItem(self._default_preset_text(), None)
        for choice in choices:
            self.encoder_preset_combo.addItem(choice, choice)
        self.encoder_preset_combo.blockSignals(False)

    def _select_encoder_preset(self, preset: str | None, *, log_invalid: bool) -> None:
        if preset is None:
            self.encoder_preset_combo.setCurrentIndex(0)
            return
        index = self.encoder_preset_combo.findData(preset)
        if index >= 0:
            self.encoder_preset_combo.setCurrentIndex(index)
            return
        self.encoder_preset_combo.setCurrentIndex(0)
        if log_invalid:
            self._append_log(self.translator.t("gui.log.encoder_preset_reset"))

    def refresh_encoder_preset_choices(
        self,
        *_args,
        preset: str | None | _PresetUnset = _PRESET_UNSET,
        log_invalid: bool = False,
    ) -> None:
        desired_preset = self._current_encoder_preset() if preset is _PRESET_UNSET else preset
        if self.backend_combo.currentText() == BackendChoice.AUTO.value:
            # ``apply_options`` may request an explicit backend before the
            # worker has produced a snapshot.  Keep its preset alongside the
            # pending backend; a true AUTO selection must discard it.
            if desired_preset is not None and self._pending_backend is not None:
                self._pending_encoder_preset = desired_preset
            else:
                self._pending_encoder_preset = None
            self._set_encoder_preset_items([])
            self.encoder_preset_combo.setEnabled(False)
            self.encoder_preset_combo.setToolTip(self.translator.t("gui.tooltip.encoder_preset_auto"))
            self._select_encoder_preset(None, log_invalid=False)
            return
        capabilities = self._runtime_capabilities()
        if capabilities is None:
            if desired_preset is not None:
                self._pending_encoder_preset = desired_preset
            else:
                self._pending_encoder_preset = None
            self._set_encoder_preset_items([])
            self.encoder_preset_combo.setEnabled(False)
            self.encoder_preset_combo.setToolTip(self.translator.t("gui.tooltip.encoder_preset_unavailable"))
            self._select_encoder_preset(None, log_invalid=log_invalid and desired_preset is not None)
            return
        self._pending_encoder_preset = None
        choices = preset_choices_from_capabilities(
            capabilities,
            self._current_codec(),
            BackendChoice(self.backend_combo.currentText()),
        )
        self._set_encoder_preset_items(choices)
        self.encoder_preset_combo.setEnabled(bool(choices))
        tooltip_key = "gui.tooltip.encoder_preset_unavailable" if not choices else ""
        self.encoder_preset_combo.setToolTip(self.translator.t(tooltip_key) if tooltip_key else "")
        self._select_encoder_preset(desired_preset, log_invalid=log_invalid)
