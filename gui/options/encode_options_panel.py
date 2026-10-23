from __future__ import annotations

from collections.abc import Callable
from enum import Enum
import math
from typing import Final

from PySide6.QtCore import Signal
from PySide6.QtGui import QResizeEvent, QStandardItemModel
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
    SmartAlgorithm,
    ContainerChoice,
    DecodeAcceleration,
    EncodeOptions,
    VmafViewingContext,
)
from core.smart import analysis_profiles_from_config, parse_analysis_profile_name, resolve_max_output_ratio
from gui.qt_optionals import maybe_none


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
    smart_unavailable = Signal(str)

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
        self.ratio_edit.textChanged.connect(self._validate_ratio)

    def _connect_signals(self) -> None:
        self.smart_algorithm_combo.currentIndexChanged.connect(self._on_smart_algorithm_changed)
        self.compression_mode_combo.currentIndexChanged.connect(self._on_compression_mode_changed)
        self.audio_mode_combo.currentIndexChanged.connect(self.sync_dependent_controls)
        self.codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        self.backend_combo.currentIndexChanged.connect(self.refresh_encoder_preset_choices)
        self.decode_acceleration_combo.currentIndexChanged.connect(self.sync_dependent_controls)
        self.analysis_profile_combo.currentIndexChanged.connect(self._on_analysis_profile_changed)
        for combo in (
            self.codec_combo,
            self.compression_mode_combo,
            self.smart_algorithm_combo,
            self.backend_combo,
            self.container_combo,
            self.audio_mode_combo,
            self.viewing_context_combo,
            self.analysis_profile_combo,
        ):
            combo.currentIndexChanged.connect(self._emit_options_changed)
        for spin in (self.min_vmaf_spin, self.max_output_ratio_spin, self.min_bitrate_spin,
                     self.max_bitrate_spin, self.maxrate_factor_spin, self.bufsize_factor_spin):
            spin.valueChanged.connect(self._emit_options_changed)
        for edit in (self.ratio_edit, self.audio_bitrate_edit, self.pix_fmt_edit):
            edit.textChanged.connect(self._emit_options_changed)
        for check in (self.overwrite_check, self.recursive_check, self.copy_subtitles_check,
                      self.copy_external_subtitles_check, self.two_pass_check):
            check.toggled.connect(self._emit_options_changed)
        self.decode_acceleration_combo.currentIndexChanged.connect(self._emit_options_changed)
        self.encoder_preset_combo.currentIndexChanged.connect(self._emit_options_changed)

    def _on_compression_mode_changed(self, *_args: object) -> None:
        self.sync_dependent_controls()
        value = self.compression_mode_combo.currentData() or self.compression_mode_combo.currentText()
        self.compression_mode_changed.emit(CompressionMode(value))

    def _on_analysis_profile_changed(self, *_args: object) -> None:
        self.analysis_profile_changed.emit(self.current_analysis_profile_name())

    def _on_smart_algorithm_changed(self, *_args: object) -> None:
        self.sync_dependent_controls()
        self.options_changed.emit()

    def _emit_options_changed(self, *_args: object) -> None:
        self.options_changed.emit()

    def _codec_label(self, codec: CodecChoice) -> str:
        if codec == CodecChoice.HEVC:
            return self.translator.t("gui.value.codec_hevc")
        return self.translator.t("gui.value.codec_av1")

    def _container_label(self, container: ContainerChoice) -> str:
        if container == ContainerChoice.MKV:
            return self.translator.t("gui.value.container_mkv")
        return self.translator.t("gui.value.container_mp4")

    def _backend_label(self, backend: BackendChoice) -> str:
        if backend == BackendChoice.AUTO:
            return self.translator.t("gui.value.backend_auto")
        if backend == BackendChoice.CPU:
            return self.translator.t("gui.value.backend_cpu")
        if backend == BackendChoice.NVENC:
            return self.translator.t("gui.value.backend_nvenc")
        if backend == BackendChoice.QSV:
            return self.translator.t("gui.value.backend_qsv")
        if backend == BackendChoice.AMF:
            return self.translator.t("gui.value.backend_amf")
        return self.translator.t("gui.value.backend_videotoolbox")

    def _audio_mode_label(self, mode: AudioMode) -> str:
        if mode == AudioMode.COPY:
            return self.translator.t("gui.value.audio_copy")
        return self.translator.t("gui.value.audio_aac")

    def summary_text(self) -> str:
        codec = self._current_codec()
        mode_data = self.compression_mode_combo.currentData() or CompressionMode.SMART.value
        smart = mode_data == CompressionMode.SMART.value
        if smart:
            quality = self.translator.t(
                "gui.summary.quality_floor", value=f"{self.min_vmaf_spin.value():.1f}"
            )
            size = self.translator.t(
                "gui.summary.size_ceiling", value=f"{self.max_output_ratio_spin.value():.0f}"
            )
        else:
            ratio = self.ratio_edit.text().strip() or "—"
            quality = f"{self.translator.t('gui.label.ratio')} {ratio}"
            size = "—"
        container = self._container_label(ContainerChoice(str(self.container_combo.currentData())))
        mode = self.compression_mode_combo.currentText()
        return self.translator.t(
            "gui.summary.encoding_compact",
            codec=self._codec_label(codec),
            mode=mode,
            quality=quality,
            size=size,
            container=container,
        )

    # ------------------------------------------------------------------ building

    def _build_basic_tab(self) -> None:
        page = QWidget()
        layout = QGridLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(10)

        self.codec_label = QLabel()
        self.codec_combo = QComboBox()
        self.codec_combo.addItem("", CodecChoice.HEVC.value)
        self.codec_combo.addItem("", CodecChoice.AV1.value)

        self.compression_mode_label = QLabel()
        self.compression_mode_combo = QComboBox()
        self.compression_mode_combo.addItem("smart", CompressionMode.SMART.value)
        self.compression_mode_combo.addItem("fixed_bitrate", CompressionMode.FIXED_BITRATE.value)
        self.smart_algorithm_label = QLabel()
        self.smart_algorithm_combo = QComboBox()
        self.smart_algorithm_combo.addItem("Smart v1", SmartAlgorithm.V1.value)
        self.smart_algorithm_combo.addItem("Smart v2 (experimental)", SmartAlgorithm.V2_EXPERIMENTAL.value)

        self.backend_label = QLabel()
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("", BackendChoice.AUTO.value)
        self.backend_combo.addItem("", BackendChoice.CPU.value)

        self.container_label = QLabel()
        self.container_combo = QComboBox()
        self.container_combo.addItem("", ContainerChoice.MKV.value)
        self.container_combo.addItem("", ContainerChoice.MP4.value)

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
        layout.addWidget(self.smart_algorithm_label, 4, 0)
        layout.addWidget(self.smart_algorithm_combo, 4, 1, 1, 3)

        self.viewing_context_label = QLabel()
        self.viewing_context_combo = QComboBox()
        for context in VmafViewingContext:
            self.viewing_context_combo.addItem("", context.value)
        layout.addWidget(self.viewing_context_label, 3, 2)
        layout.addWidget(self.viewing_context_combo, 3, 3)
        layout.setColumnStretch(6, 1)

        self.codec_combo.setMaximumWidth(140)
        self.compression_mode_combo.setMaximumWidth(200)
        self.backend_combo.setMaximumWidth(170)
        self.container_combo.setMaximumWidth(120)
        self.ratio_edit.setMaximumWidth(160)
        self.min_vmaf_spin.setMaximumWidth(120)
        self.max_output_ratio_spin.setMaximumWidth(120)
        self.analysis_profile_combo.setMaximumWidth(180)
        self.viewing_context_combo.setMaximumWidth(250)
        self.smart_algorithm_combo.setMaximumWidth(260)

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
        layout.setColumnStretch(4, 1)
        self.encoder_preset_combo.setMaximumWidth(220)
        self.decode_acceleration_combo.setMaximumWidth(180)
        self.pix_fmt_edit.setMaximumWidth(160)
        self.min_bitrate_spin.setMaximumWidth(140)
        self.max_bitrate_spin.setMaximumWidth(140)
        self.maxrate_factor_spin.setMaximumWidth(120)
        self.bufsize_factor_spin.setMaximumWidth(120)

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
        self.audio_mode_combo.addItem("", AudioMode.COPY.value)
        self.audio_mode_combo.addItem("", AudioMode.AAC.value)

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
        layout.setColumnStretch(4, 1)
        self.audio_mode_combo.setMaximumWidth(220)
        self.audio_bitrate_edit.setMaximumWidth(140)

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

    def recursive_scan_enabled(self) -> bool:
        return self.recursive_check.isChecked()

    def _validate_ratio(self) -> bool:
        text = self.ratio_edit.text().strip()
        smart = self.compression_mode_combo.currentData() == CompressionMode.SMART.value
        try:
            valid = smart or not text or (math.isfinite(float(text)) and float(text) > 0)
        except ValueError:
            valid = False
        if self.ratio_edit.property("invalid") != (not valid):
            self.ratio_edit.setProperty("invalid", not valid)
            self.ratio_edit.style().unpolish(self.ratio_edit)
            self.ratio_edit.style().polish(self.ratio_edit)
        self.ratio_edit.setToolTip(
            self.translator.t("gui.tooltip.ratio") if valid else self.translator.t("gui.message.invalid_ratio")
        )
        return valid

    def read_options(self) -> EncodeOptions:
        ratio_text = self.ratio_edit.text().strip()
        if not self._validate_ratio():
            self.ratio_edit.setFocus()
            raise ValueError(self.translator.t("gui.message.invalid_ratio"))
        fixed = self.compression_mode_combo.currentData() == CompressionMode.FIXED_BITRATE.value
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
            codec=CodecChoice(str(self.codec_combo.currentData())),
            compression_mode=CompressionMode(
                self.compression_mode_combo.currentData()
                or self.compression_mode_combo.currentText()
            ),
            backend=BackendChoice(str(self.backend_combo.currentData())),
            ratio=float(ratio_text) if fixed and ratio_text else None,
            min_vmaf=float(self.min_vmaf_spin.value()),
            viewing_context=VmafViewingContext(self.viewing_context_combo.currentData()),
            max_output_ratio=float(self.max_output_ratio_spin.value()) / 100.0,
            min_video_kbps=int(self.min_bitrate_spin.value()),
            max_video_kbps=int(self.max_bitrate_spin.value()),
            container=ContainerChoice(str(self.container_combo.currentData())),
            audio_mode=AudioMode(str(self.audio_mode_combo.currentData())),
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
            recursive=self.recursive_scan_enabled(),
            size_blocked_policy=size_policy,
            quality_unreachable_policy=unreachable_policy,
            skipped_output_policy=skipped_policy,
            analysis_profile=profile_name,
            analysis_settings=profile_settings,
            smart_algorithm=SmartAlgorithm(self.smart_algorithm_combo.currentData())
            if self.compression_mode_combo.currentData() == CompressionMode.SMART.value else SmartAlgorithm.V1,
        )

    def apply_options(self, options: EncodeOptions) -> None:
        self.smart_algorithm_combo.setCurrentIndex(self.smart_algorithm_combo.findData(options.smart_algorithm.value))
        codec_index = self.codec_combo.findData(options.codec.value)
        if codec_index >= 0:
            self.codec_combo.setCurrentIndex(codec_index)
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
        container_index = self.container_combo.findData(options.container.value)
        if container_index >= 0:
            self.container_combo.setCurrentIndex(container_index)
        audio_index = self.audio_mode_combo.findData(options.audio_mode.value)
        if audio_index >= 0:
            self.audio_mode_combo.setCurrentIndex(audio_index)
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

    def set_visible(self, visible: bool) -> None:
        self.setVisible(visible)

    def _bind_labels(self) -> None:
        pairs = (
            (self.codec_label, self.codec_combo),
            (self.compression_mode_label, self.compression_mode_combo),
            (self.backend_label, self.backend_combo),
            (self.container_label, self.container_combo),
            (self.ratio_label, self.ratio_edit),
            (self.min_vmaf_label, self.min_vmaf_spin),
            (self.max_output_ratio_label, self.max_output_ratio_spin),
            (self.analysis_profile_label, self.analysis_profile_combo),
            (self.viewing_context_label, self.viewing_context_combo),
            (self.smart_algorithm_label, self.smart_algorithm_combo),
            (self.encoder_preset_label, self.encoder_preset_combo),
            (self.decode_acceleration_label, self.decode_acceleration_combo),
            (self.pix_fmt_label, self.pix_fmt_edit),
            (self.min_bitrate_label, self.min_bitrate_spin),
            (self.max_bitrate_label, self.max_bitrate_spin),
            (self.maxrate_factor_label, self.maxrate_factor_spin),
            (self.bufsize_factor_label, self.bufsize_factor_spin),
            (self.audio_mode_label, self.audio_mode_combo),
            (self.audio_bitrate_label, self.audio_bitrate_edit),
        )
        for label, widget in pairs:
            label.setBuddy(widget)
            widget.setAccessibleName(label.text())

    def apply_tab_order(self) -> None:
        chain = (
            self.codec_combo,
            self.compression_mode_combo,
            self.backend_combo,
            self.container_combo,
            self.ratio_edit,
            self.min_vmaf_spin,
            self.max_output_ratio_spin,
            self.analysis_profile_combo,
            self.viewing_context_combo,
            self.smart_algorithm_combo,
            self.overwrite_check,
            self.recursive_check,
        )
        for first, second in zip(chain, chain[1:]):
            self.setTabOrder(first, second)

    def set_translator(self, translator: Translator) -> None:
        self.translator = translator
        self.codec_label.setText(self.translator.t("gui.label.codec"))
        for codec in (CodecChoice.HEVC, CodecChoice.AV1):
            index = self.codec_combo.findData(codec.value)
            if index >= 0:
                self.codec_combo.setItemText(index, self._codec_label(codec))
        for container in (ContainerChoice.MKV, ContainerChoice.MP4):
            index = self.container_combo.findData(container.value)
            if index >= 0:
                self.container_combo.setItemText(index, self._container_label(container))
        for audio_mode in (AudioMode.COPY, AudioMode.AAC):
            index = self.audio_mode_combo.findData(audio_mode.value)
            if index >= 0:
                self.audio_mode_combo.setItemText(index, self._audio_mode_label(audio_mode))
        self.compression_mode_label.setText(self.translator.t("gui.label.compression_mode"))
        self.smart_algorithm_label.setText(self.translator.t("gui.label.smart_algorithm"))
        self.smart_algorithm_combo.setItemText(0, self.translator.t("gui.value.smart_v1"))
        self.smart_algorithm_combo.setItemText(1, self.translator.t("gui.value.smart_v2"))
        self.smart_algorithm_combo.setToolTip(self.translator.t("gui.tooltip.smart_v2"))
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
        ratio_tooltip = self.translator.t("gui.tooltip.ratio")
        self.ratio_label.setToolTip(ratio_tooltip)
        self.ratio_edit.setToolTip(ratio_tooltip)
        self.min_vmaf_label.setText(self.translator.t("gui.label.min_vmaf"))
        self.min_vmaf_label.setToolTip(self.translator.t("gui.tooltip.min_vmaf"))
        self.min_vmaf_spin.setToolTip(self.translator.t("gui.tooltip.min_vmaf"))
        self.max_output_ratio_label.setText(self.translator.t("gui.label.max_output_ratio"))
        max_ratio_tooltip = self.translator.t("gui.tooltip.max_output_ratio")
        self.max_output_ratio_label.setToolTip(max_ratio_tooltip)
        self.max_output_ratio_spin.setToolTip(max_ratio_tooltip)
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

        self._bind_labels()
        self.apply_tab_order()
        self._rebuild_backend_controls()
        self.refresh_encoder_preset_choices()

    # ---------------------------------------------------- backend/preset logic

    def _on_codec_changed(self, *_args: object) -> None:
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
        return CodecChoice(str(self.codec_combo.currentData()))

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
            # QStandardItemModel.item() is declared as non-null but returns None for a
            # row the model does not hold yet.
            item = maybe_none(model.item(videotoolbox_index))
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
        current_value = self.backend_combo.currentData()
        try:
            current_backend = BackendChoice(str(current_value)) if current_value else BackendChoice.AUTO
        except ValueError:
            current_backend = BackendChoice.AUTO
        desired_backend = preferred_backend or current_backend

        choices = [BackendChoice.AUTO, *self._available_explicit_backends_for_current_codec()]
        if self._runtime_capabilities() is None and BackendChoice.CPU not in choices:
            choices.append(BackendChoice.CPU)

        if desired_backend in choices:
            selected_backend = desired_backend
        else:
            # ``choices`` always contains AUTO, so a desired backend that reaches this
            # branch is never AUTO; the redundant identity checks were dropped.
            selected_backend = BackendChoice.AUTO
            if self._runtime_capabilities() is None:
                self._pending_backend = desired_backend
            if log_reset and self._runtime_capabilities() is not None:
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
            self.backend_combo.addItem(self._backend_label(backend), backend.value)
        selected_index = self.backend_combo.findData(selected_backend.value)
        if selected_index >= 0:
            self.backend_combo.setCurrentIndex(selected_index)
        self.backend_combo.blockSignals(False)
        tooltip_key = "gui.tooltip.backend_filtered" if self._runtime_capabilities() is not None else "gui.tooltip.backend_detecting"
        self.backend_combo.setToolTip(self.translator.t(tooltip_key))

        self.sync_dependent_controls()

    def sync_dependent_controls(self) -> None:
        self._validate_ratio()
        mode_value = self.compression_mode_combo.currentData() or self.compression_mode_combo.currentText()
        smart_mode = mode_value == CompressionMode.SMART.value
        self.smart_algorithm_label.setVisible(smart_mode)
        self.smart_algorithm_combo.setVisible(smart_mode)
        v2 = smart_mode and self.smart_algorithm_combo.currentData() == SmartAlgorithm.V2_EXPERIMENTAL.value
        if v2:
            self.min_vmaf_label.setText(self.translator.t("gui.label.mean_vmaf_v2"))
            self.min_vmaf_spin.setToolTip(self.translator.t("gui.tooltip.mean_vmaf_v2"))
        else:
            self.min_vmaf_label.setText(self.translator.t("gui.label.min_vmaf"))
            self.min_vmaf_spin.setToolTip(self.translator.t("gui.tooltip.min_vmaf"))
        self.ratio_label.setVisible(not smart_mode)
        self.ratio_edit.setVisible(not smart_mode)
        self.min_vmaf_label.setVisible(smart_mode)
        self.min_vmaf_spin.setVisible(smart_mode)
        self.max_output_ratio_label.setVisible(smart_mode)
        self.max_output_ratio_spin.setVisible(smart_mode)
        self.analysis_profile_label.setVisible(smart_mode)
        self.analysis_profile_combo.setVisible(smart_mode)
        self.viewing_context_label.setVisible(smart_mode)
        self.viewing_context_combo.setVisible(smart_mode)
        self.min_vmaf_spin.setEnabled(smart_mode)
        self.max_output_ratio_spin.setEnabled(smart_mode)
        self.analysis_profile_combo.setEnabled(smart_mode)
        self.viewing_context_combo.setEnabled(smart_mode)
        self.audio_bitrate_edit.setEnabled(self.audio_mode_combo.currentData() == AudioMode.AAC.value)
        self._reflow_basic_tab()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reflow_basic_tab()

    def _reflow_basic_tab(self) -> None:
        layout = self.basic_tab.layout()
        if not isinstance(layout, QGridLayout):
            return
        smart = self.compression_mode_combo.currentData() == CompressionMode.SMART.value
        columns = 2 if self.width() < 900 else 3
        signature = (smart, columns)
        if getattr(self, "_basic_layout_signature", None) == signature:
            return
        self._basic_layout_signature = signature
        while layout.count():
            layout.takeAt(0)
        pairs = [
            (self.codec_label, self.codec_combo),
            (self.compression_mode_label, self.compression_mode_combo),
            (self.backend_label, self.backend_combo),
            (self.container_label, self.container_combo),
        ]
        pairs += [
            (self.min_vmaf_label, self.min_vmaf_spin),
            (self.max_output_ratio_label, self.max_output_ratio_spin),
            (self.analysis_profile_label, self.analysis_profile_combo),
            (self.viewing_context_label, self.viewing_context_combo),
            (self.smart_algorithm_label, self.smart_algorithm_combo),
        ] if smart else [(self.ratio_label, self.ratio_edit)]
        for index, (label, control) in enumerate(pairs):
            row, column = divmod(index, columns)
            label.setWordWrap(True)
            layout.addWidget(label, row, column * 2)
            layout.addWidget(control, row, column * 2 + 1)
        row = (len(pairs) + columns - 1) // columns
        layout.addWidget(self.overwrite_check, row, 0, 1, 2)
        layout.addWidget(self.recursive_check, row, 2, 1, 2)
        for column in range(7):
            layout.setColumnStretch(column, 1 if column < columns * 2 and column % 2 else 0)

    def _refresh_smart_mode_availability(self, *, force_unavailable: bool = False) -> None:
        capabilities = self._runtime_capabilities()
        vmaf = capabilities.get("vmaf") if capabilities else None
        known = force_unavailable or isinstance(vmaf, dict)
        available = bool(isinstance(vmaf, dict) and vmaf.get("runnable"))
        index = self.compression_mode_combo.findData(CompressionMode.SMART.value)
        model = self.compression_mode_combo.model()
        if index >= 0 and isinstance(model, QStandardItemModel):
            item = maybe_none(model.item(index))
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
            reason = message or self.translator.t("gui.tooltip.smart_unavailable")
            self.compression_mode_combo.setToolTip(reason)
            self.smart_unavailable.emit(reason)
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
        *_args: object,
        preset: str | None | _PresetUnset = _PRESET_UNSET,
        log_invalid: bool = False,
    ) -> None:
        desired_preset = self._current_encoder_preset() if preset is _PRESET_UNSET else preset
        if self.backend_combo.currentData() == BackendChoice.AUTO.value:
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
            BackendChoice(str(self.backend_combo.currentData())),
        )
        self._set_encoder_preset_items(choices)
        self.encoder_preset_combo.setEnabled(bool(choices))
        tooltip_key = "gui.tooltip.encoder_preset_unavailable" if not choices else ""
        self.encoder_preset_combo.setToolTip(self.translator.t(tooltip_key) if tooltip_key else "")
        self._select_encoder_preset(desired_preset, log_invalid=log_invalid)
