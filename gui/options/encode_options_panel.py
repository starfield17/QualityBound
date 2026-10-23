from __future__ import annotations

from collections.abc import Callable
from enum import Enum
import math
import re
from typing import Final

from PySide6.QtCore import QEvent, QSignalBlocker, Signal
from PySide6.QtGui import QResizeEvent, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QScrollArea,
    QStyle,
    QStyleOptionTabWidgetFrame,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QWidgetItem,
)

from core.config import smart_policies_from_config
from core.ffmpeg import (
    ENCODER_CANDIDATES,
    available_backends_for_codec,
    preset_choices_from_capabilities,
)
from core.i18n import Text, Translator
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


LOG_DECODE_ACCELERATION_RESET = Text(
    en='VideoToolbox decoding is unavailable in the selected FFmpeg build; reset to software decoding.',
    zh_cn='当前 FFmpeg 不支持 VideoToolbox 解码，已回退到软件解码。',
    ja='選択されたFFmpegビルドはVideoToolboxデコードをサポートしていません。ソフトウェアデコードに戻しました。',
)

TOOLTIP_DECODE_ACCELERATION_UNAVAILABLE = Text(
    en='VideoToolbox decoding is unavailable in the selected FFmpeg build; software decoding is used.',
    zh_cn='当前 FFmpeg 不支持 VideoToolbox 解码，将使用软件解码。',
    ja='選択されたFFmpegビルドはVideoToolboxデコードをサポートしていません。ソフトウェアデコードが使用されます。',
)


LABEL_MIN_VMAF = Text(
    en='VMAF v1 target',
    zh_cn='VMAF v1 目标值',
    ja='VMAF v1 目標値',
)

LABEL_RATIO = Text(
    en='Size ratio',
    zh_cn='体积比例',
    ja='サイズ比率',
)

MESSAGE_ENCODER_SELECTION_PENDING = Text(
    en='The selected encoder or preset has not been verified. Wait for detection or choose an available encoder.',
    zh_cn='所选编码器或预设尚未验证。请等待检测完成或选择可用编码器。',
    ja='選択されたエンコーダーまたはプリセットが検証されていません。検出の完了を待つか、利用可能なエンコーダーを選択してください。',
)

MESSAGE_INVALID_AUDIO_BITRATE = Text(
    en='Enter a positive audio bitrate, for example 128k or 0.192M.',
    zh_cn='请输入正数音频码率，例如 128k 或 0.192M。',
    ja='128k や 0.192M などの正の音声ビットレートを入力してください。',
)

MESSAGE_INVALID_RATIO = Text(
    en='Enter a finite number greater than 0, or leave blank to use the default ratio.',
    zh_cn='请输入大于 0 的有限数值，或留空使用默认比例。',
    ja='0より大きい有限数を入力するか、空欄にしてデフォルト比率を使用してください。',
)

TOOLTIP_ANALYSIS_PROFILE = Text(
    en='Controls Smart analysis confidence through timeline scouting, difficult-content coverage, and independent verification.',
    zh_cn='通过全片侦察、困难内容覆盖与独立验证控制 Smart 分析置信度。',
    ja='タイムラインのスカウト、高難度コンテンツのカバー、独立検証によりSmart分析の信頼性を制御します。',
)

TOOLTIP_ENCODER_PRESET_UNAVAILABLE = Text(
    en='Preset choices are unavailable for the current encoder; using the encoder default preset.',
    zh_cn='当前编码器无法提供可靠的预设列表，将使用编码器默认预设。',
    ja='現在のエンコーダーのプリセット候補が利用できません。エンコーダーのデフォルトプリセットを使用します。',
)

TOOLTIP_MEAN_VMAF_V2 = Text(
    en='Time-weighted mean target; each shot mean must be at least target minus 4, and the worst one-second score at least target minus 8.',
    zh_cn='按时长加权的整片平均目标；每镜头平均不低于目标减4，最差连续一秒不低于目标减8。',
    ja='時間加重平均目標。各ショットの平均は目標値-4以上、最悪1秒間のスコアは目標値-8以上である必要があります。',
)

TOOLTIP_MIN_VMAF = Text(
    en="Minimum SMART quality score. Recommended: 90. It combines each sample's mean VMAF with its worst one-second period; higher values usually increase file size.",
    zh_cn='最低 SMART 画质分数，推荐 90。它综合每个样本的平均 VMAF 与最差一秒画质；数值越高通常文件越大。',
    ja='最小SMART品質スコア。推奨値: 90。各サンプルの平均VMAFと最悪1秒間のスコアを組み合わせた指標で、値が高いほど通常ファイルサイズが大きくなります。',
)

TOOLTIP_RATIO = Text(
    en='Output size as a fraction of the source. Used only in fixed-bitrate mode.',
    zh_cn='输出体积占输入源的比例，仅在固定码率模式使用。',
    ja='ソースに対する出力サイズの比率。固定ビットレートモードでのみ使用されます。',
)

TOOLTIP_SMART_UNAVAILABLE = Text(
    en='Smart mode is unavailable because the selected FFmpeg cannot run libvmaf.',
    zh_cn='所选 FFmpeg 无法运行 libvmaf，智能模式不可用。',
    ja='選択されたFFmpegがlibvmafを実行できないため、Smartモードは利用できません。',
)

TOOLTIP_SMART_V2 = Text(
    en='Per-shot bitrate allocation. Candidate search uses samples; the final output is fully scored. May take substantially longer.',
    zh_cn='逐镜头分配码率，候选搜索使用样本，最终输出完整评分。运行时间可能显著增加。',
    ja='ショット単位のビットレート配分。候補検索はサンプルを使用し、最終出力は完全スコアリングされます。処理時間が大幅に長くなる可能性があります。',
)

TOOLTIP_VIEWING_CONTEXT = Text(
    en='The default uses the stricter high-fidelity quality assumption, including demanding large-display viewing for 4K sources. Choose Standard display only when that viewing condition matches your intended use.',
    zh_cn='默认采用更严格的高保真画质假设，对 4K 片源也按高要求的大屏观看条件评估。仅当标准显示设备符合预期用途时才选择该选项。',
    ja='デフォルトでは、4Kソースの高要求な大画面視聴を含む、より厳格な高忠実度品質の前提が使用されます。この視聴環境が想定用途に一致する場合にのみ、標準ディスプレイを選択してください。',
)

VALUE_ANALYSIS_BALANCE = Text(
    en='Balance',
    zh_cn='均衡',
    ja='バランス',
)

VALUE_ANALYSIS_FAST = Text(
    en='Fast',
    zh_cn='快速',
    ja='高速',
)

VALUE_ANALYSIS_PRECISE = Text(
    en='Precise',
    zh_cn='精细',
    ja='精密',
)


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

        self._tab_layout_signatures: dict[QWidget, tuple[object, ...]] = {}
        self.options_tabs = QTabWidget()
        self._build_basic_tab()
        self._build_video_tab()
        self._build_audio_tab()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.setFocusProxy(self.options_tabs)
        layout.addWidget(self.options_tabs)

        self.options_tabs.currentChanged.connect(self._reflow_tabs)
        self._connect_signals()
        self.ratio_edit.textChanged.connect(self._validate_ratio)
        self.audio_bitrate_edit.textChanged.connect(self._validate_audio_bitrate)

    def _connect_signals(self) -> None:
        self.smart_algorithm_combo.currentIndexChanged.connect(self._on_smart_algorithm_changed)
        self.compression_mode_combo.currentIndexChanged.connect(self._on_compression_mode_changed)
        self.audio_mode_combo.currentIndexChanged.connect(self.sync_dependent_controls)
        self.codec_combo.currentIndexChanged.connect(self._on_codec_changed)
        self.backend_combo.currentIndexChanged.connect(self._on_backend_changed)
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
        self.encoder_preset_combo.currentIndexChanged.connect(self._on_encoder_preset_changed)

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
            return self.translator.t(Text(en='HEVC', zh_cn='HEVC', ja='HEVC'))
        return self.translator.t(Text(en='AV1', zh_cn='AV1', ja='AV1'))

    def _container_label(self, container: ContainerChoice) -> str:
        if container == ContainerChoice.MKV:
            return self.translator.t(Text(en='MKV', zh_cn='MKV', ja='MKV'))
        return self.translator.t(Text(en='MP4', zh_cn='MP4', ja='MP4'))

    def _backend_label(self, backend: BackendChoice) -> str:
        if backend == BackendChoice.AUTO:
            return self.translator.t(Text(en='Auto', zh_cn='自动', ja='自動'))
        if backend == BackendChoice.CPU:
            return self.translator.t(Text(en='CPU', zh_cn='CPU', ja='CPU'))
        if backend == BackendChoice.NVENC:
            return self.translator.t(Text(en='NVIDIA NVENC', zh_cn='NVIDIA NVENC', ja='NVIDIA NVENC'))
        if backend == BackendChoice.QSV:
            return self.translator.t(Text(en='Intel QSV', zh_cn='Intel QSV', ja='Intel QSV'))
        if backend == BackendChoice.AMF:
            return self.translator.t(Text(en='AMD AMF', zh_cn='AMD AMF', ja='AMD AMF'))
        return self.translator.t(Text(en='VideoToolbox', zh_cn='VideoToolbox', ja='VideoToolbox'))

    def _audio_mode_label(self, mode: AudioMode) -> str:
        if mode == AudioMode.COPY:
            return self.translator.t(Text(en='Copy (no re-encode)', zh_cn='复制（不重新编码）', ja='コピー (再エンコードなし)'))
        return self.translator.t(Text(en='AAC', zh_cn='AAC', ja='AAC'))

    def summary_text(self) -> str:
        codec = self._current_codec()
        mode_data = self.compression_mode_combo.currentData() or CompressionMode.SMART.value
        smart = mode_data == CompressionMode.SMART.value
        if smart:
            quality = self.translator.t(
                Text(en='VMAF ≥ {value}', zh_cn='VMAF ≥ {value}', ja='VMAF ≥ {value}'), value=f"{self.min_vmaf_spin.value():.1f}"
            )
            size = self.translator.t(
                Text(en='≤ {value}% of source', zh_cn='≤ 源体积的 {value}%', ja='≤ 元の {value}%'), value=f"{self.max_output_ratio_spin.value():.0f}"
            )
        else:
            ratio = self.ratio_edit.text().strip() or "—"
            quality = f"{self.translator.t(LABEL_RATIO)} {ratio}"
            size = "—"
        container = self._container_label(ContainerChoice(str(self.container_combo.currentData())))
        mode = self.compression_mode_combo.currentText()
        return self.translator.t(
            Text(en='{codec} · {mode} · {quality} · {size} · {container}', zh_cn='{codec} · {mode} · {quality} · {size} · {container}', ja='{codec} · {mode} · {quality} · {size} · {container}'),
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
        for combo in (
            self.codec_combo,
            self.compression_mode_combo,
            self.backend_combo,
            self.container_combo,
            self.analysis_profile_combo,
            self.viewing_context_combo,
            self.smart_algorithm_combo,
        ):
            combo.setMinimumWidth(80)

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
        self.min_bitrate_spin.setSuffix(" kbps")

        self.max_bitrate_label = QLabel()
        self.max_bitrate_spin = QSpinBox()
        self.max_bitrate_spin.setRange(0, 500000)
        self.max_bitrate_spin.setValue(0)
        self.max_bitrate_spin.setSuffix(" kbps")

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
        for combo in (self.encoder_preset_combo, self.decode_acceleration_combo):
            combo.setMinimumWidth(80)

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
        self.audio_mode_combo.setMinimumWidth(80)

        self.options_tabs.addTab(page, "")
        self.audio_tab = page

    def _fill_analysis_profile_combo(self) -> None:
        self.analysis_profile_combo.clear()
        for name in AnalysisProfileName:
            self.analysis_profile_combo.addItem("", name.value)

    # ---------------------------------------------------------------- profiles

    def apply_analysis_profile_settings(self) -> None:
        with QSignalBlocker(self):
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
            self.translator.t(TOOLTIP_RATIO) if valid else self.translator.t(MESSAGE_INVALID_RATIO)
        )
        return valid

    def _validate_audio_bitrate(self) -> bool:
        text = self.audio_bitrate_edit.text().strip()
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kKmM]?)", text or "128k")
        valid = self.audio_mode_combo.currentData() != AudioMode.AAC.value or bool(match and math.isfinite(float(match.group(1))) and float(match.group(1)) > 0)
        self.audio_bitrate_edit.setProperty("invalid", not valid)
        self.audio_bitrate_edit.setToolTip("" if valid else self.translator.t(MESSAGE_INVALID_AUDIO_BITRATE))
        self.audio_bitrate_edit.style().unpolish(self.audio_bitrate_edit)
        self.audio_bitrate_edit.style().polish(self.audio_bitrate_edit)
        return valid

    def read_options(self) -> EncodeOptions:
        if self._pending_backend is not None or self._pending_encoder_preset is not None:
            raise ValueError(self.translator.t(MESSAGE_ENCODER_SELECTION_PENDING))
        if not self._validate_audio_bitrate():
            self.audio_bitrate_edit.setFocus()
            raise ValueError(self.translator.t(MESSAGE_INVALID_AUDIO_BITRATE))
        if self.max_bitrate_spin.value() and self.max_bitrate_spin.value() < self.min_bitrate_spin.value():
            raise ValueError(self.translator.t(Text(en='Maximum video bitrate must be at least the minimum, or unlimited.', zh_cn='最高视频码率不能小于最低码率，或请选择不限。', ja='最大動画ビットレートは最小ビットレート以上か、無制限にする必要があります。')))
        ratio_text = self.ratio_edit.text().strip()
        if not self._validate_ratio():
            self.ratio_edit.setFocus()
            raise ValueError(self.translator.t(MESSAGE_INVALID_RATIO))
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
        with QSignalBlocker(self):
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
        with QSignalBlocker(self):
            self._encoder_capabilities_ready = False
            self._runtime_capabilities_snapshot = None
            self._rebuild_backend_controls()
            # Do not show stale choices while the worker refreshes the snapshot.
            # ``refresh_encoder_preset_choices`` retains a selected value so it
            # can be restored after detection completes.
            self.refresh_encoder_preset_choices()

    def set_runtime_capabilities(self, capabilities: dict) -> None:
        with QSignalBlocker(self):
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
        if self.compression_mode_combo.toolTip():
            self.smart_unavailable.emit(self.compression_mode_combo.toolTip())

    def notify_capability_detection_failed(self) -> None:
        with QSignalBlocker(self):
            self._encoder_capabilities_ready = False
            self._runtime_capabilities_snapshot = None
            self._pending_backend = None
            self._pending_encoder_preset = None
            self._rebuild_backend_controls()
            self.refresh_encoder_preset_choices()
            self._refresh_decode_acceleration_choices(log_reset=True, force_unavailable=True)
            self._refresh_smart_mode_availability(force_unavailable=True)
        self.smart_unavailable.emit(self.compression_mode_combo.toolTip())

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
        with QSignalBlocker(self):
            self.translator = translator
            self.codec_label.setText(self.translator.t(Text(en='Codec', zh_cn='编码格式', ja='コーデック')))
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
            self.compression_mode_label.setText(self.translator.t(Text(en='Compression mode', zh_cn='压缩模式', ja='圧縮モード')))
            self.smart_algorithm_label.setText(self.translator.t(Text(en='Smart algorithm', zh_cn='Smart 算法', ja='Smartアルゴリズム')))
            self.smart_algorithm_combo.setItemText(0, self.translator.t(Text(en='Smart v1 (default)', zh_cn='Smart v1（默认）', ja='Smart v1 (デフォルト)')))
            self.smart_algorithm_combo.setItemText(1, self.translator.t(Text(en='Smart v2 (experimental)', zh_cn='Smart v2（实验性）', ja='Smart v2 (実験的)')))
            self.smart_algorithm_combo.setToolTip(self.translator.t(TOOLTIP_SMART_V2))
            smart_mode_index = self.compression_mode_combo.findData(CompressionMode.SMART.value)
            fixed_mode_index = self.compression_mode_combo.findData(CompressionMode.FIXED_BITRATE.value)
            if smart_mode_index >= 0:
                self.compression_mode_combo.setItemText(
                    smart_mode_index,
                    self.translator.t(Text(en='Smart (VMAF)', zh_cn='智能（VMAF）', ja='Smart (VMAF)')),
                )
            if fixed_mode_index >= 0:
                self.compression_mode_combo.setItemText(
                    fixed_mode_index,
                    self.translator.t(Text(en='Fixed bitrate', zh_cn='固定码率', ja='固定ビットレート')),
                )
            self.backend_label.setText(self.translator.t(Text(en='Backend', zh_cn='后端', ja='バックエンド')))
            self.container_label.setText(self.translator.t(Text(en='Container', zh_cn='容器', ja='コンテナ')))
            self.ratio_label.setText(self.translator.t(LABEL_RATIO))
            ratio_tooltip = self.translator.t(TOOLTIP_RATIO)
            self.ratio_label.setToolTip(ratio_tooltip)
            self.ratio_edit.setToolTip(ratio_tooltip)
            self.min_vmaf_label.setText(self.translator.t(LABEL_MIN_VMAF))
            self.min_vmaf_label.setToolTip(self.translator.t(TOOLTIP_MIN_VMAF))
            self.min_vmaf_spin.setToolTip(self.translator.t(TOOLTIP_MIN_VMAF))
            self.max_output_ratio_label.setText(self.translator.t(Text(en='Max output size', zh_cn='最大输出体积', ja='最大出力サイズ')))
            max_ratio_tooltip = self.translator.t(Text(en='Maximum output size as a percentage of the source. Smart mode also uses this as the size ceiling.', zh_cn='输出体积占输入源的最大百分比。智能模式也把它作为体积上限。', ja='ソースに対する最大出力サイズの割合(%)。Smartモードでもサイズ上限として使用されます。'))
            self.max_output_ratio_label.setToolTip(max_ratio_tooltip)
            self.max_output_ratio_spin.setToolTip(max_ratio_tooltip)
            self.analysis_profile_label.setText(self.translator.t(Text(en='Analysis effort', zh_cn='分析强度', ja='分析強度')))
            self.analysis_profile_combo.setItemText(
                self.analysis_profile_combo.findData(AnalysisProfileName.FAST.value),
                self.translator.t(VALUE_ANALYSIS_FAST),
            )
            self.analysis_profile_combo.setItemText(
                self.analysis_profile_combo.findData(AnalysisProfileName.BALANCE.value),
                self.translator.t(VALUE_ANALYSIS_BALANCE),
            )
            self.analysis_profile_combo.setItemText(
                self.analysis_profile_combo.findData(AnalysisProfileName.PRECISE.value),
                self.translator.t(VALUE_ANALYSIS_PRECISE),
            )
            self.analysis_profile_combo.setToolTip(self.translator.t(TOOLTIP_ANALYSIS_PROFILE))
            self.viewing_context_label.setText(self.translator.t(Text(en='Viewing scenario', zh_cn='观看场景', ja='視聴環境')))
            self.viewing_context_combo.setItemText(
                self.viewing_context_combo.findData(VmafViewingContext.HIGH_FIDELITY.value),
                self.translator.t(Text(en='High-fidelity / Large display', zh_cn='高保真 / 大屏幕', ja='高忠実度 / 大画面')),
            )
            self.viewing_context_combo.setItemText(
                self.viewing_context_combo.findData(VmafViewingContext.STANDARD_DISPLAY.value),
                self.translator.t(Text(en='Standard display', zh_cn='标准显示设备', ja='標準ディスプレイ')),
            )
            viewing_tooltip = self.translator.t(TOOLTIP_VIEWING_CONTEXT)
            self.viewing_context_label.setToolTip(viewing_tooltip)
            self.viewing_context_combo.setToolTip(viewing_tooltip)
            self.overwrite_check.setText(self.translator.t(Text(en='Overwrite', zh_cn='允许覆盖', ja='上書きを許可')))
            self.recursive_check.setText(self.translator.t(Text(en='Recursive', zh_cn='递归扫描', ja='再帰スキャン')))
            self.encoder_preset_label.setText(self.translator.t(Text(en='Encoder preset', zh_cn='编码器预设', ja='エンコーダープリセット')))
            self.decode_acceleration_label.setText(self.translator.t(Text(en='Decode acceleration', zh_cn='解码加速', ja='デコードアクセラレーション')))
            self.decode_acceleration_combo.setItemText(
                self.decode_acceleration_combo.findData(DecodeAcceleration.SOFTWARE.value),
                self.translator.t(Text(en='Software', zh_cn='软件', ja='ソフトウェア')),
            )
            self.decode_acceleration_combo.setItemText(
                self.decode_acceleration_combo.findData(DecodeAcceleration.VIDEOTOOLBOX.value),
                self.translator.t(Text(en='VideoToolbox', zh_cn='VideoToolbox', ja='VideoToolbox')),
            )
            self.pix_fmt_label.setText(self.translator.t(Text(en='Pixel format', zh_cn='像素格式', ja='ピクセルフォーマット')))
            self.min_bitrate_label.setText(self.translator.t(Text(en='Min video kbps', zh_cn='最低视频码率', ja='最小動画ビットレート')))
            self.max_bitrate_label.setText(self.translator.t(Text(en='Max video kbps', zh_cn='最高视频码率', ja='最大動画ビットレート')))
            self.max_bitrate_spin.setSpecialValueText(self.translator.t(Text(en='Unlimited', zh_cn='不限', ja='無制限')))
            self.maxrate_factor_label.setText(self.translator.t(Text(en='Maxrate factor', zh_cn='Maxrate 系数', ja='最大レート係数')))
            self.bufsize_factor_label.setText(self.translator.t(Text(en='Bufsize factor', zh_cn='Bufsize 系数', ja='バッファサイズ係数')))
            self.two_pass_check.setText(self.translator.t(Text(en='Two-pass', zh_cn='双遍编码', ja='2パス')))
            self.audio_mode_label.setText(self.translator.t(Text(en='Audio mode', zh_cn='音频模式', ja='音声モード')))
            self.audio_bitrate_label.setText(self.translator.t(Text(en='Audio bitrate', zh_cn='音频码率', ja='音声ビットレート')))
            self.copy_subtitles_check.setText(self.translator.t(Text(en='Copy subtitles', zh_cn='复制字幕', ja='字幕をコピー')))
            self.copy_external_subtitles_check.setText(self.translator.t(Text(en='Copy external subtitles', zh_cn='复制外挂字幕', ja='外部字幕をコピー')))
            self.ratio_edit.setPlaceholderText(self.translator.t(Text(en='Leave blank to use codec default ratio', zh_cn='留空则使用编码格式默认比例', ja='空欄でコーデックのデフォルト比率を使用')))
            self.pix_fmt_edit.setPlaceholderText("yuv420p")
            self.audio_bitrate_edit.setPlaceholderText("128k")

            self.options_tabs.setTabText(self.options_tabs.indexOf(self.basic_tab), self.translator.t(Text(en='Basic', zh_cn='基础', ja='基本')))
            self.options_tabs.setTabText(self.options_tabs.indexOf(self.video_tab), self.translator.t(Text(en='Video', zh_cn='视频', ja='映像')))
            self.options_tabs.setTabText(self.options_tabs.indexOf(self.audio_tab), self.translator.t(Text(en='Audio / Subtitles', zh_cn='音频 / 字幕', ja='音声 / 字幕')))

            self._bind_labels()
            self.apply_tab_order()
            self._rebuild_backend_controls()
            self.refresh_encoder_preset_choices()

    # ---------------------------------------------------- backend/preset logic

    def _on_encoder_preset_changed(self, *_args: object) -> None:
        self._pending_encoder_preset = None
        self.options_changed.emit()

    def _on_backend_changed(self, *_args: object) -> None:
        self._pending_backend = None
        self._pending_encoder_preset = None
        with QSignalBlocker(self.encoder_preset_combo):
            self.encoder_preset_combo.setCurrentIndex(0)
        self.refresh_encoder_preset_choices()

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
                self.translator.t(TOOLTIP_DECODE_ACCELERATION_UNAVAILABLE)
            )
        else:
            self.decode_acceleration_combo.setToolTip("")

        if desired == DecodeAcceleration.VIDEOTOOLBOX and runtime_known and not videotoolbox_available:
            desired = DecodeAcceleration.SOFTWARE
            if log_reset:
                self._append_log(self.translator.t(LOG_DECODE_ACCELERATION_RESET))

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
        desired_backend = preferred_backend or self._pending_backend or current_backend

        choices = [BackendChoice.AUTO, *self._available_explicit_backends_for_current_codec()]
        if self._runtime_capabilities() is None and BackendChoice.CPU not in choices:
            choices.append(BackendChoice.CPU)

        if desired_backend not in choices:
            self._pending_backend = desired_backend
            choices.append(desired_backend)
            if log_reset and self._runtime_capabilities() is not None:
                self._append_log(self.translator.t(MESSAGE_ENCODER_SELECTION_PENDING))
        else:
            self._pending_backend = None
        selected_backend = desired_backend

        self.backend_combo.blockSignals(True)
        self.backend_combo.clear()
        for backend in choices:
            self.backend_combo.addItem(self._backend_label(backend), backend.value)
        selected_index = self.backend_combo.findData(selected_backend.value)
        if selected_index >= 0:
            self.backend_combo.setCurrentIndex(selected_index)
        self.backend_combo.blockSignals(False)
        if self._pending_backend is not None:
            model = self.backend_combo.model()
            if isinstance(model, QStandardItemModel):
                item = maybe_none(model.item(selected_index))
                if item is not None:
                    item.setEnabled(False)
        tooltip_text = Text(en='Only backends that passed hardware encoder detection are shown.', zh_cn='仅显示已通过硬件编码器检测的后端。', ja='ハードウェアエンコーダー検出に合格したバックエンドのみ表示されます。') if self._runtime_capabilities() is not None else Text(en='Hardware encoder detection is running; only safe backend choices are shown.', zh_cn='正在检测硬件编码器；当前仅显示安全后端选项。', ja='ハードウェアエンコーダーを検出中。安全なバックエンド候補のみ表示されます。')
        self.backend_combo.setToolTip(self.translator.t(MESSAGE_ENCODER_SELECTION_PENDING) if self._pending_backend is not None else self.translator.t(tooltip_text))

        self.sync_dependent_controls()

    def sync_dependent_controls(self) -> None:
        self._validate_ratio()
        self._validate_audio_bitrate()
        mode_value = self.compression_mode_combo.currentData() or self.compression_mode_combo.currentText()
        smart_mode = mode_value == CompressionMode.SMART.value
        self.smart_algorithm_label.setVisible(smart_mode)
        self.smart_algorithm_combo.setVisible(smart_mode)
        v2 = smart_mode and self.smart_algorithm_combo.currentData() == SmartAlgorithm.V2_EXPERIMENTAL.value
        if v2:
            self.min_vmaf_label.setText(self.translator.t(Text(en='Whole-video mean VMAF target', zh_cn='整片平均 VMAF 目标', ja='動画全体の平均VMAF目標')))
            self.min_vmaf_spin.setToolTip(self.translator.t(TOOLTIP_MEAN_VMAF_V2))
        else:
            self.min_vmaf_label.setText(self.translator.t(LABEL_MIN_VMAF))
            self.min_vmaf_spin.setToolTip(self.translator.t(TOOLTIP_MIN_VMAF))
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
        self._reflow_tabs()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._reflow_tabs()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._reflow_tabs()

    def _reflow_tabs(self, *_args: object) -> None:
        if not hasattr(self, "audio_tab"):
            return
        available = self.contentsRect().width()
        ancestor = self.parentWidget()
        while ancestor is not None:
            if isinstance(ancestor, QScrollArea) and ancestor.isVisible():
                available = min(available, ancestor.viewport().contentsRect().width())
                break
            ancestor = ancestor.parentWidget()
        option = QStyleOptionTabWidgetFrame()
        self.options_tabs.initStyleOption(option)
        contents = self.options_tabs.style().subElementRect(
            QStyle.SubElement.SE_TabWidgetTabContents, option, self.options_tabs
        )
        available = max(0, available - (self.options_tabs.width() - contents.width()))
        smart = self.compression_mode_combo.currentData() == CompressionMode.SMART.value
        basic_pairs: list[tuple[QLabel, QWidget]] = [
            (self.codec_label, self.codec_combo),
            (self.compression_mode_label, self.compression_mode_combo),
            (self.backend_label, self.backend_combo),
            (self.container_label, self.container_combo),
        ]
        basic_pairs += [
            (self.min_vmaf_label, self.min_vmaf_spin),
            (self.max_output_ratio_label, self.max_output_ratio_spin),
            (self.analysis_profile_label, self.analysis_profile_combo),
            (self.viewing_context_label, self.viewing_context_combo),
            (self.smart_algorithm_label, self.smart_algorithm_combo),
        ] if smart else [(self.ratio_label, self.ratio_edit)]
        self._reflow_tab(
            self.basic_tab, basic_pairs, [self.overwrite_check, self.recursive_check], 3, available
        )
        self._reflow_tab(self.video_tab, [
            (self.encoder_preset_label, self.encoder_preset_combo),
            (self.decode_acceleration_label, self.decode_acceleration_combo),
            (self.pix_fmt_label, self.pix_fmt_edit),
            (self.min_bitrate_label, self.min_bitrate_spin),
            (self.max_bitrate_label, self.max_bitrate_spin),
            (self.maxrate_factor_label, self.maxrate_factor_spin),
            (self.bufsize_factor_label, self.bufsize_factor_spin),
        ], [self.two_pass_check], 2, available)
        self._reflow_tab(self.audio_tab, [
            (self.audio_mode_label, self.audio_mode_combo),
            (self.audio_bitrate_label, self.audio_bitrate_edit),
        ], [self.copy_subtitles_check, self.copy_external_subtitles_check], 2, available)

    @staticmethod
    def _control_minimum_width(widget: QWidget) -> int:
        # Qt also applies size policies here: spin boxes can require their
        # full value width even when minimumSizeHint() reports less.
        return QWidgetItem(widget).minimumSize().width()

    def _reflow_tab(
        self,
        page: QWidget,
        pairs: list[tuple[QLabel, QWidget]],
        checks: list[QCheckBox],
        maximum_columns: int,
        available: int,
    ) -> None:
        layout = page.layout()
        if not isinstance(layout, QGridLayout):
            return
        for label, _control in pairs:
            label.setWordWrap(True)
        margins = layout.contentsMargins()
        spacing = layout.horizontalSpacing()
        columns = 1
        for candidate in range(maximum_columns, 0, -1):
            widths = [0] * (candidate * 2)
            for index, (label, control) in enumerate(pairs):
                column = index % candidate * 2
                widths[column] = max(widths[column], label.minimumSizeHint().width())
                widths[column + 1] = max(widths[column + 1], self._control_minimum_width(control))
            for index, check in enumerate(checks):
                column = index % candidate * 2
                pair_width = widths[column] + spacing + widths[column + 1]
                widths[column + 1] += max(0, check.minimumSizeHint().width() - pair_width)
            required = sum(widths) + spacing * (candidate * 2 - 1) + margins.left() + margins.right()
            if required <= available:
                columns = candidate
                break
        signature = (columns, tuple(label for label, _control in pairs))
        if self._tab_layout_signatures.get(page) == signature:
            return
        self._tab_layout_signatures[page] = signature
        while layout.count():
            layout.takeAt(0)
        for index, (label, control) in enumerate(pairs):
            row, column = divmod(index, columns)
            layout.addWidget(label, row, column * 2)
            layout.addWidget(control, row, column * 2 + 1)
        first_check_row = (len(pairs) + columns - 1) // columns
        for index, check in enumerate(checks):
            row, column = divmod(index, columns)
            layout.addWidget(check, first_check_row + row, column * 2, 1, 2)
        for column in range(maximum_columns * 2 + 1):
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
            reason = message or self.translator.t(TOOLTIP_SMART_UNAVAILABLE)
            self.compression_mode_combo.setToolTip(reason)
            self.smart_unavailable.emit(reason)
        else:
            self.compression_mode_combo.setToolTip("")
        self.sync_dependent_controls()

    def _default_preset_text(self) -> str:
        return self.translator.t(Text(en='(default)', zh_cn='（默认）', ja='(デフォルト)'))

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
        with QSignalBlocker(self.encoder_preset_combo):
            if preset is None:
                self.encoder_preset_combo.setCurrentIndex(0)
                return
            index = self.encoder_preset_combo.findData(preset)
            if index < 0:
                self._pending_encoder_preset = preset
                self.encoder_preset_combo.setEnabled(True)
                self.encoder_preset_combo.addItem(self.translator.t(Text(en='{value} (unavailable)', zh_cn='{value}（不可用）', ja='{value} (利用不可)'), value=preset), preset)
                index = self.encoder_preset_combo.count() - 1
                model = self.encoder_preset_combo.model()
                if isinstance(model, QStandardItemModel):
                    item = maybe_none(model.item(index))
                    if item is not None:
                        item.setEnabled(False)
                if log_invalid:
                    self._append_log(self.translator.t(MESSAGE_ENCODER_SELECTION_PENDING))
            self.encoder_preset_combo.setCurrentIndex(index)

    def refresh_encoder_preset_choices(
        self,
        *_args: object,
        preset: str | None | _PresetUnset = _PRESET_UNSET,
        log_invalid: bool = False,
    ) -> None:
        with QSignalBlocker(self):
            desired_preset = (self._current_encoder_preset() or self._pending_encoder_preset) if preset is _PRESET_UNSET else preset
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
                self.encoder_preset_combo.setToolTip(self.translator.t(Text(en='Auto backend uses resolved encoder default preset.', zh_cn='自动后端会使用实际解析到的编码器默认预设。', ja='自動バックエンドは解決されたエンコーダーのデフォルトプリセットを使用します。')))
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
                self.encoder_preset_combo.setToolTip(self.translator.t(TOOLTIP_ENCODER_PRESET_UNAVAILABLE))
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
            tooltip_text = TOOLTIP_ENCODER_PRESET_UNAVAILABLE if not choices else None
            self.encoder_preset_combo.setToolTip(self.translator.t(tooltip_text) if tooltip_text else "")
            self._select_encoder_preset(desired_preset, log_invalid=log_invalid)
