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
from core.i18n import LANGUAGES, Text, Translator
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
)
from gui.qt_optionals import maybe_none
from gui.icons import set_button_icon
from gui.window_geometry import clamped_window_size

from gui.options.encode_options_panel import TOOLTIP_ANALYSIS_PROFILE, VALUE_ANALYSIS_BALANCE, VALUE_ANALYSIS_FAST, VALUE_ANALYSIS_PRECISE
from gui.power_action_dialog import post_encode_action_text, LABEL_POST_ENCODE_ACTION

BUTTON_BROWSE_EXE = Text(
    en='Pick File',
    zh_cn='选择文件',
    ja='ファイルを選択',
)

LABEL_FFMPEG = Text(
    en='FFmpeg path',
    zh_cn='FFmpeg 路径',
    ja='FFmpegのパス',
)

LABEL_FFPROBE = Text(
    en='FFprobe path',
    zh_cn='FFprobe 路径',
    ja='FFprobeのパス',
)

LABEL_WORKDIR = Text(
    en='Temporary work folder',
    zh_cn='临时工作目录',
    ja='一時作業フォルダー',
)

MESSAGE_PATH_MISSING = Text(
    en='Path does not exist.',
    zh_cn='路径不存在。',
    ja='パスが存在しません。',
)

MESSAGE_PATH_NOT_DIRECTORY = Text(
    en='Path is not a directory.',
    zh_cn='路径不是目录。',
    ja='パスがディレクトリではありません。',
)

PLACEHOLDER_FFMPEG = Text(
    en='Optional; blank uses project FFmpeg/ first.',
    zh_cn='可选；留空优先使用项目 FFmpeg/。',
    ja='省略可能。空欄の場合はプロジェクトの FFmpeg/ を優先します。',
)

PLACEHOLDER_FFPROBE = Text(
    en='Optional; blank uses project FFprobe/ first.',
    zh_cn='可选；留空优先使用项目 FFprobe/。',
    ja='省略可能。空欄の場合はプロジェクトの FFprobe/ を優先します。',
)

STATUS_RE_DETECT_PENDING = Text(
    en='Re-detection starts after you press OK.',
    zh_cn='按“确定”后开始重新检测。',
    ja='「OK」をクリックすると再検出が開始されます。',
)

TOOLTIP_ENCODE_WORKERS = Text(
    en='Maximum full-file encodes running at once (1-8). Smart analysis still finishes before full encoding. Higher values may contend for CPU, GPU memory, and hardware encoder sessions.',
    zh_cn='最多同时运行 1–8 个完整文件编码。Smart 分析仍会在完整编码前全部完成；较高数值可能争用 CPU、GPU 显存和硬件编码会话。',
    ja='同時に実行するフルエンコードの最大数 (1〜8)。Smart分析はフルエンコードの前に完了します。値を大きくするとCPU、GPUメモリ、ハードウェアエンコーダーセッションが競合する可能性があります。',
)

TOOLTIP_SIZE_BLOCKED_POLICY = Text(
    en='Applies to candidate selection before the full encode. If the chosen policy cannot resolve a conflict and the required output is larger than the source, Smart skips the file. Actual full-encode size misses are preserved for a separate decision.',
    zh_cn='用于完整编码前的候选选择。所选策略仍不能解决冲突且预测成片比片源大时，Smart 会跳过该文件。完整成片实际超限则保留文件并单独要求决定。',
    ja='フルエンコード前の候補選択に適用されます。選択したポリシーで競合を解決できず、必要な出力がソースより大きい場合、Smartはそのファイルをスキップします。実際のフルエンコードでサイズ超過したファイルは保持され、個別に判断を求めます。',
)

TOOLTIP_SKIPPED_OUTPUT_POLICY = Text(
    en='Applies to Smart-analysis skips, including predicted oversize and manual Skip choices. Runs after executable files finish even when other items still need a decision. planning skips and discarded actual-size misses are excluded.',
    zh_cn='适用于 Smart 分析跳过，包括预测过大及人工选择跳过。可编码文件完成后处理，不受其他待决定项阻塞；不适用于 planning 跳过或丢弃实际体积超限成片。',
    ja='予測超過や手動スキップを含むSmart分析のスキップに適用されます。他の項目が決定待ちの場合でも、エンコード可能なファイルが完了した後に実行されます。計画スキップや破棄された実サイズ超過は対象外です。',
)

TOOLTIP_WORKDIR = Text(
    en='Temporary files and logs are written here. Published videos go to the output folder.',
    zh_cn='临时文件和日志写入此处；发布视频写入输出目录。',
    ja='一時ファイルとログがここに書き込まれます。公開された動画は出力フォルダーに保存されます。',
)


class SettingsDialog(QDialog):
    def __init__(
        self,
        tr: Translator,
        settings: dict[str, object],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.translator = tr
        self.redetect_requested = False
        self._languages = LANGUAGES
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
            return None if expect_directory else self.translator.t(MESSAGE_PATH_MISSING)
        if expect_directory and not path.is_dir():
            return self.translator.t(MESSAGE_PATH_NOT_DIRECTORY)
        if not expect_directory and not path.is_file():
            return self.translator.t(MESSAGE_PATH_MISSING)
        return None

    def _check_paths(self) -> None:
        problems: list[str] = []
        workdir_problem = self._path_problem(self.workdir_edit.text(), expect_directory=True)
        if workdir_problem:
            problems.append(f"{self.translator.t(LABEL_WORKDIR)}: {workdir_problem}")
        ffmpeg_text = self.ffmpeg_edit.text().strip()
        if ffmpeg_text:
            ffmpeg_problem = self._path_problem(ffmpeg_text, expect_directory=False)
            if ffmpeg_problem:
                problems.append(
                    f"{self.translator.t(LABEL_FFMPEG)}: "
                    f"{self.translator.t(Text(en='FFmpeg path is not a file.', zh_cn='FFmpeg 路径不是文件。', ja='FFmpegのパスがファイルではありません。'))}"
                )
        ffprobe_text = self.ffprobe_edit.text().strip()
        if ffprobe_text:
            ffprobe_problem = self._path_problem(ffprobe_text, expect_directory=False)
            if ffprobe_problem:
                problems.append(
                    f"{self.translator.t(LABEL_FFPROBE)}: "
                    f"{self.translator.t(Text(en='FFprobe path is not a file.', zh_cn='FFprobe 路径不是文件。', ja='FFprobeのパスがファイルではありません。'))}"
                )
        self.path_status_label.setText(
            "\n".join(problems) if problems else self.translator.t(Text(en='Paths look usable.', zh_cn='路径可用。', ja='パスは有効です。'))
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
        self.setWindowTitle(self.translator.t(Text(en='Settings', zh_cn='设置', ja='設定')))
        self.queue_group.setTitle(self.translator.t(Text(en='Queue and output', zh_cn='队列与输出', ja='キューと出力')))
        self.toolchain_group.setTitle(self.translator.t(Text(en='FFmpeg toolchain', zh_cn='FFmpeg 工具链', ja='FFmpeg ツールチェーン')))
        self.language_label.setText(self.translator.t(Text(en='Language', zh_cn='语言', ja='言語')))
        self.workdir_label.setText(self.translator.t(LABEL_WORKDIR))
        self.workdir_label.setToolTip(self.translator.t(TOOLTIP_WORKDIR))
        self.workdir_edit.setToolTip(self.translator.t(TOOLTIP_WORKDIR))
        self.ffmpeg_label.setText(self.translator.t(LABEL_FFMPEG))
        self.ffprobe_label.setText(self.translator.t(LABEL_FFPROBE))
        self.log_level_label.setText(self.translator.t(Text(en='Log level', zh_cn='日志级别', ja='ログレベル')))
        self.log_level_combo.setItemText(
            self.log_level_combo.findData("info"),
            self.translator.t(Text(en='Info', zh_cn='信息', ja='情報')),
        )
        self.log_level_combo.setItemText(
            self.log_level_combo.findData("debug"),
            self.translator.t(Text(en='Debug', zh_cn='调试', ja='デバッグ')),
        )
        self.encode_workers_label.setText(self.translator.t(Text(en='Concurrent full encodes', zh_cn='同时进行的完整编码数', ja='同時フルエンコード数')))
        encode_workers_tooltip = self.translator.t(TOOLTIP_ENCODE_WORKERS)
        self.encode_workers_label.setToolTip(encode_workers_tooltip)
        self.encode_workers_spin.setToolTip(encode_workers_tooltip)
        self.post_encode_label.setText(self.translator.t(LABEL_POST_ENCODE_ACTION))
        self.desktop_notifications_check.setText(self.translator.t(Text(en='Send system notification when queue finishes', zh_cn='队列全部完成后发送桌面通知', ja='キュー完了時にシステム通知を送信')))
        for action in PostEncodeAction:
            idx = self.post_encode_combo.findData(action.value)
            if idx >= 0:
                self.post_encode_combo.setItemText(idx, self.translator.t(post_encode_action_text(action)))
        self.policy_group.setTitle(self.translator.t(Text(en='When Smart analysis cannot meet both targets', zh_cn='Smart 分析无法同时满足时', ja='Smart分析で両方の目標を満たせない場合')))
        self.size_blocked_policy_label.setText(self.translator.t(Text(en='If Smart analysis predicts VMAF needs a larger file', zh_cn='Smart 分析预测达到 VMAF 需要更大文件时', ja='VMAF達成により大きなファイルが必要と予測される場合')))
        self.quality_unreachable_policy_label.setText(self.translator.t(Text(en='If tested candidates miss the VMAF target', zh_cn='已测试候选未达到 VMAF 目标时', ja='テスト候補がVMAF目標を満たさない場合')))
        self.size_blocked_policy_combo.setItemText(
            self.size_blocked_policy_combo.findData(SizeBlockedPolicy.RELAX_SIZE.value),
            self.translator.t(Text(en='Allow a larger file, up to source size', zh_cn='放宽体积上限，最多到片源大小', ja='ソースサイズを上限としてより大きなファイルを許可')),
        )
        self.size_blocked_policy_combo.setItemText(
            self.size_blocked_policy_combo.findData(SizeBlockedPolicy.RELAX_QUALITY.value),
            self.translator.t(Text(en='Lower the VMAF target', zh_cn='降低 VMAF 门槛', ja='VMAF目標を引き下げる')),
        )
        self.size_blocked_policy_combo.setItemText(
            self.size_blocked_policy_combo.findData(SizeBlockedPolicy.ASK.value),
            self.translator.t(Text(en='Ask every time', zh_cn='每次询问', ja='毎回確認する')),
        )
        self.quality_unreachable_policy_combo.setItemText(
            self.quality_unreachable_policy_combo.findData(QualityUnreachablePolicy.SKIP.value),
            self.translator.t(Text(en='Skip the file', zh_cn='跳过该文件', ja='ファイルをスキップ')),
        )
        self.quality_unreachable_policy_combo.setItemText(
            self.quality_unreachable_policy_combo.findData(QualityUnreachablePolicy.ASK.value),
            self.translator.t(Text(en='Ask every time', zh_cn='每次询问', ja='毎回確認する')),
        )
        self.skipped_output_policy_label.setText(self.translator.t(Text(en='When Smart analysis skips a file', zh_cn='Smart 分析跳过文件时', ja='Smart分析がファイルをスキップした場合')))
        self.skipped_output_policy_combo.setItemText(
            self.skipped_output_policy_combo.findData(SkippedOutputPolicy.COPY.value),
            self.translator.t(Text(en='Copy the source to the planned output path', zh_cn='把片源复制到预定输出位置', ja='ソースを計画された出力パスにコピー')),
        )
        self.skipped_output_policy_combo.setItemText(
            self.skipped_output_policy_combo.findData(SkippedOutputPolicy.ASK.value),
            self.translator.t(Text(en='Ask after executable files finish', zh_cn='可编码文件完成后询问', ja='エンコード完了後に確認する')),
        )
        self.skipped_output_policy_combo.setItemText(
            self.skipped_output_policy_combo.findData(SkippedOutputPolicy.IGNORE.value),
            self.translator.t(Text(en='Leave Smart-skipped files unchanged', zh_cn='不处理 Smart 分析跳过的文件', ja='Smartでスキップされたファイルをそのままにする')),
        )
        self.analysis_group.setTitle(self.translator.t(Text(en='Analysis effort', zh_cn='分析强度', ja='分析強度')))
        self.analysis_profile_label.setText(self.translator.t(Text(en='Active effort', zh_cn='当前强度', ja='現在の強度')))
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
        self.workdir_button.setText(self.translator.t(Text(en='Pick Dir', zh_cn='选择目录', ja='フォルダーを選択')))
        self.ffmpeg_button.setText(self.translator.t(BUTTON_BROWSE_EXE))
        self.ffprobe_button.setText(self.translator.t(BUTTON_BROWSE_EXE))
        self.redetect_button.setText(self.translator.t(Text(en='Re-detect encoders', zh_cn='重新检测编码器', ja='エンコーダーを再検出')))
        self.check_paths_button.setText(self.translator.t(Text(en='Check paths', zh_cn='检查路径', ja='パスを確認')))
        if self.redetect_requested:
            self.detection_hint_label.setText(self.translator.t(STATUS_RE_DETECT_PENDING))
        self.ffmpeg_edit.setPlaceholderText(self.translator.t(PLACEHOLDER_FFMPEG))
        self.ffprobe_edit.setPlaceholderText(self.translator.t(PLACEHOLDER_FFPROBE))
        self.ffmpeg_edit.setToolTip(self.translator.t(PLACEHOLDER_FFMPEG))
        self.ffprobe_edit.setToolTip(self.translator.t(PLACEHOLDER_FFPROBE))
        size_policy_tooltip = self.translator.t(TOOLTIP_SIZE_BLOCKED_POLICY)
        self.size_blocked_policy_label.setToolTip(size_policy_tooltip)
        self.size_blocked_policy_combo.setToolTip(size_policy_tooltip)
        skipped_policy_tooltip = self.translator.t(TOOLTIP_SKIPPED_OUTPUT_POLICY)
        self.skipped_output_policy_label.setToolTip(skipped_policy_tooltip)
        self.skipped_output_policy_combo.setToolTip(skipped_policy_tooltip)
        self.analysis_profile_combo.setToolTip(self.translator.t(TOOLTIP_ANALYSIS_PROFILE))
        ok_button = maybe_none(self.button_box.button(QDialogButtonBox.StandardButton.Ok))
        cancel_button = maybe_none(self.button_box.button(QDialogButtonBox.StandardButton.Cancel))
        if ok_button is not None:
            ok_button.setText(self.translator.t(Text(en='OK', zh_cn='确定', ja='OK')))
        if cancel_button is not None:
            cancel_button.setText(self.translator.t(Text(en='Cancel', zh_cn='取消', ja='キャンセル')))
        if self.detection_hint_label.isVisible():
            self.detection_hint_label.setText(self.translator.t(STATUS_RE_DETECT_PENDING))
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
        path = QFileDialog.getExistingDirectory(self, self.translator.t(Text(en='Select workdir', zh_cn='选择工作目录', ja='作業フォルダーを選択')))
        if path:
            self.workdir_edit.setText(path)
            self._check_paths()

    def _browse_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.translator.t(Text(en='Select ffmpeg executable', zh_cn='选择 ffmpeg 可执行文件', ja='ffmpeg 実行ファイルを選択')))
        if path:
            self.ffmpeg_edit.setText(path)
            self._check_paths()

    def _browse_ffprobe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.translator.t(Text(en='Select ffprobe executable', zh_cn='选择 ffprobe 可执行文件', ja='ffprobe 実行ファイルを選択')))
        if path:
            self.ffprobe_edit.setText(path)
            self._check_paths()

    def _request_redetect(self) -> None:
        self.redetect_requested = True
        self.detection_hint_label.setText(self.translator.t(STATUS_RE_DETECT_PENDING))
        self.detection_hint_label.setVisible(True)
