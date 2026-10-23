from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn, TypedDict

from cli.cli_interactive import print_encode_results, print_plan
from core.config import (
    config_dir as app_config_dir,
    delete_preset,
    encode_options_to_preset_data,
    ensure_runtime_layout,
    list_presets,
    load_app_config,
    load_preset,
    parse_quality_unreachable_policy,
    parse_size_blocked_policy,
    parse_skipped_output_policy,
    save_preset,
    workdir_dir,
)
from core.encoding import (
    build_encode_plan,
    execute_plan,
    execute_plan_concurrent,
)
from core.ffmpeg import discover_ffmpeg_tools, list_available_encoders, preset_choices_for_encoder, resolve_encoder
from core.i18n import LANGUAGES, Text, Translator
from core.media import publish_skipped_sources
from core.models import (
    AnalysisProfileName,
    AudioMode,
    BackendChoice,
    CodecChoice,
    CompressionMode,
    SmartAlgorithm,
    ConstraintPolicy,
    QualityUnreachablePolicy,
    SizeBlockedPolicy,
    SkippedOutputPolicy,
    ContainerChoice,
    DecodeAcceleration,
    EncodeOptions,
    VmafViewingContext,
)
from core.smart import (
    bind_analysis_profile,
    constraint_policy_from_size_blocked,
    parse_analysis_profile_name,
    size_blocked_from_constraint_policy,
)


class _BoolActionKwargs(TypedDict):
    action: type[argparse.Action]
    default: None


def _bool_action_kwargs() -> _BoolActionKwargs:
    return {"action": argparse.BooleanOptionalAction, "default": None}


def _config_dir() -> Path:
    return app_config_dir()


def _default_workdir() -> Path:
    return workdir_dir()


def _load_base_options(args: argparse.Namespace, config_dir: Path) -> EncodeOptions:
    preset_name = getattr(args, "preset", None)
    if preset_name:
        return load_preset(preset_name, config_dir)

    app_config = load_app_config()
    default_preset_name = app_config.get("default_preset_name")
    if default_preset_name:
        try:
            return load_preset(default_preset_name, config_dir)
        except FileNotFoundError:
            pass
    return EncodeOptions()


def _merge_options(base: EncodeOptions, args: argparse.Namespace) -> EncodeOptions:
    updates: dict[str, object] = {}
    scalar_map = {
        "codec": lambda value: CodecChoice(value),
        "compression_mode": lambda value: CompressionMode(value),
        "smart_algorithm": lambda value: SmartAlgorithm(value),
        "backend": lambda value: BackendChoice(value),
        "decode_acceleration": lambda value: DecodeAcceleration(value),
        "ratio": float,
        "min_vmaf": float,
        "viewing_context": lambda value: VmafViewingContext(value),
        "max_output_ratio": float,
        "min_video_kbps": int,
        "max_video_kbps": int,
        "container": lambda value: ContainerChoice(value),
        "audio_mode": lambda value: AudioMode(value),
        "audio_bitrate": str,
        "encoder_preset": str,
        "pix_fmt": str,
        "maxrate_factor": float,
        "bufsize_factor": float,
    }
    for name, caster in scalar_map.items():
        value = getattr(args, name, None)
        if value is not None:
            updates[name] = caster(value)

    bool_names = ["copy_subtitles", "copy_external_subtitles", "two_pass", "overwrite", "recursive", "dry_run"]
    for name in bool_names:
        value = getattr(args, name, None)
        if value is not None:
            updates[name] = bool(value)

    size_blocked = getattr(args, "size_blocked_policy", None)
    constraint_policy = getattr(args, "constraint_policy", None)
    if size_blocked is not None:
        updates["size_blocked_policy"] = parse_size_blocked_policy(size_blocked)
    elif constraint_policy is not None:
        updates["size_blocked_policy"] = size_blocked_from_constraint_policy(ConstraintPolicy(constraint_policy))
    unreachable = getattr(args, "quality_unreachable_policy", None)
    if unreachable is not None:
        updates["quality_unreachable_policy"] = parse_quality_unreachable_policy(unreachable)
    skipped_output = getattr(args, "skipped_output_policy", None)
    if skipped_output is not None:
        updates["skipped_output_policy"] = parse_skipped_output_policy(skipped_output)

    return replace(base, **updates)


def _normalize_auto_backend_preset(options: EncodeOptions, args: argparse.Namespace) -> EncodeOptions:
    if options.backend != BackendChoice.AUTO or options.encoder_preset is None:
        return options
    tr = _translator_for_args(args)
    if getattr(args, "encoder_preset", None) is not None:
        raise ValueError(tr.t(Text(
            en='--encoder-preset cannot be used with --backend auto. Choose a concrete backend, or omit --encoder-preset.',
            zh_cn='--encoder-preset 不能与 --backend auto 并用。请选择具体后端，或省略 --encoder-preset。',
            ja='--encoder-preset は --backend auto と併用できません。具体的なバックエンドを選択するか、--encoder-preset を省略してください。',
        )))
    print(tr.t(Text(
        en='Warning: inherited encoder preset ignored because backend is auto.',
        zh_cn='警告: 后端为 auto，已忽略继承的编码器预设。',
        ja='警告: バックエンドがautoのため、継承されたエンコーダープリセットを無視しました。',
    )), file=sys.stderr)
    return replace(options, encoder_preset=None)


def _apply_analysis_profile(options: EncodeOptions, args: argparse.Namespace, config_dir: Path) -> EncodeOptions:
    app_config = load_app_config()
    selected = getattr(args, "analysis_profile", None)
    if selected is None:
        selected = app_config.get("analysis_profile")
    return bind_analysis_profile(
        options,
        name=parse_analysis_profile_name(selected),
        stored_profiles=app_config.get("analysis_profiles"),
    )


def _options_from_args(args: argparse.Namespace, config_dir: Path) -> EncodeOptions:
    options = _normalize_auto_backend_preset(_merge_options(_load_base_options(args, config_dir), args), args)
    return _apply_analysis_profile(options, args, config_dir)


class _LocalizedArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args: Any, translator: Translator, **kwargs: Any) -> None:
        self.translator = translator
        super().__init__(*args, add_help=False, **kwargs)
        self._positionals.title = translator.t(Text(en='positional arguments', zh_cn='位置参数', ja='位置引数'))
        self._optionals.title = translator.t(Text(en='options', zh_cn='选项', ja='オプション'))
        self.add_argument('-h', '--help', action='help', help=translator.t(Text(
            en='show this help message and exit', zh_cn='显示帮助并退出', ja='ヘルプを表示して終了',
        )))

    def _localized_usage(self, rendered: str) -> str:
        return rendered.replace('usage:', self.translator.t(Text(en='usage:', zh_cn='用法:', ja='使い方:')), 1)

    def format_help(self) -> str:
        return self._localized_usage(super().format_help())

    def format_usage(self) -> str:
        return self._localized_usage(super().format_usage())

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        # argparse and external diagnostic payloads keep their original details.
        detail = self.translator.t(Text(en='error: {message}', zh_cn='错误: {message}', ja='エラー: {message}'), message=message)
        self.exit(2, f'{self.prog}: {detail}\n')


def _add_language_flag(parser: argparse.ArgumentParser, translator: Translator, *, default: str | None = argparse.SUPPRESS) -> None:
    parser.add_argument('--lang', choices=[info.code for info in LANGUAGES], default=default,
                        help=translator.t(Text(en='Display language', zh_cn='显示语言', ja='表示言語')))


def _language_from_argv(argv: list[str]) -> str | None:
    # Select the display language before argparse processes an eager --help.
    selected = None
    for index, argument in enumerate(argv):
        if argument == '--':
            break
        option, separator, value = argument.partition('=')
        # --lang is the only option with this prefix in every parser scope.
        if option.startswith('--') and '--lang'.startswith(option):
            if separator:
                selected = value
            elif index + 1 < len(argv):
                selected = argv[index + 1]
    return selected


def _add_runtime_flags(
    parser: argparse.ArgumentParser,
    translator: Translator,
    include_input: bool = True,
) -> None:
    if include_input:
        parser.add_argument("input", help=translator.t(Text(en='Input file or directory', zh_cn='输入文件或目录', ja='入力ファイルまたはフォルダー')))
    parser.add_argument("-o", "--output", help=translator.t(Text(en='Output directory', zh_cn='输出目录', ja='出力フォルダー')))
    parser.add_argument("--workdir", help=translator.t(Text(en='Working directory', zh_cn='工作目录', ja='作業フォルダー')))
    parser.add_argument("--ffmpeg", help=translator.t(Text(en='Path to ffmpeg', zh_cn='ffmpeg 路径', ja='ffmpeg のパス')))
    parser.add_argument("--ffprobe", help=translator.t(Text(en='Path to ffprobe', zh_cn='ffprobe 路径', ja='ffprobe のパス')))
    parser.add_argument("--preset", help=translator.t(Text(en='Saved preset name', zh_cn='已保存的预设名称', ja='保存されたプリセット名')))
    _add_language_flag(parser, translator)
    parser.add_argument("--recursive", **_bool_action_kwargs(), help=translator.t(Text(en='Recursively scan subdirectories', zh_cn='递归扫描子目录', ja='サブフォルダーを再帰的にスキャン')))


def _add_encode_flags(parser: argparse.ArgumentParser, translator: Translator) -> None:
    parser.add_argument("--codec", choices=["hevc", "av1"], help=translator.t(Text(en='Target codec', zh_cn='目标编码格式', ja='出力コーデック')))
    parser.add_argument(
        "--compression-mode",
        dest="compression_mode",
        choices=[mode.value for mode in CompressionMode],
        help=translator.t(Text(en='Compression policy', zh_cn='压缩策略', ja='圧縮ポリシー')),
    )
    parser.add_argument(
        "--backend",
        choices=[backend.value for backend in BackendChoice],
        help=translator.t(Text(en='Encoder backend', zh_cn='编码器后端', ja='エンコーダーバックエンド')),
    )
    parser.add_argument("--smart-algorithm", choices=[algorithm.value for algorithm in SmartAlgorithm],
                        help=translator.t(Text(en='Smart algorithm (default: v1; v2_experimental uses per-shot average quality)', zh_cn='Smart 算法（默认 v1；v2_experimental 使用逐镜头平均画质）', ja='Smartアルゴリズム（デフォルト: v1。v2_experimental はショットごとの平均品質を使用）')))
    parser.add_argument(
        "--decode-acceleration",
        dest="decode_acceleration",
        choices=[acceleration.value for acceleration in DecodeAcceleration],
        help=translator.t(Text(en='Video decoding acceleration method', zh_cn='视频解码加速方式', ja='動画デコードのアクセラレーション方式')),
    )
    parser.add_argument("--ratio", type=float, help=translator.t(Text(en='Target video bitrate ratio', zh_cn='目标视频码率比例', ja='目標動画ビットレート比率')))
    parser.add_argument(
        "--min-vmaf",
        dest="min_vmaf",
        type=float,
        help=translator.t(Text(en='VMAF target (default: 90): v1 sampled-window gate; experimental v2 whole-video mean', zh_cn='VMAF 目标（默认 90）：v1 采样窗口门槛；实验性 v2 整片平均值', ja='VMAF目標（デフォルト: 90）: v1 はサンプル区間の基準、実験的v2 は動画全体の平均値')),
    )
    parser.add_argument(
        "--analysis-profile",
        dest="analysis_profile",
        choices=[name.value for name in AnalysisProfileName],
        help=translator.t(Text(en='Smart VMAF scan profile: fast, balance, or precise', zh_cn='Smart VMAF 分析强度：fast、balance 或 precise', ja='Smart VMAF分析強度: fast、balance、precise')),
    )
    parser.add_argument(
        "--viewing-context",
        dest="viewing_context",
        choices=[context.value for context in VmafViewingContext],
        help=translator.t(Text(en='Smart viewing scenario: high_fidelity (default) or standard_display', zh_cn='Smart 观看场景：high_fidelity（默认）或 standard_display', ja='Smart視聴環境: high_fidelity（デフォルト）または standard_display')),
    )
    parser.add_argument(
        "--max-output-ratio",
        dest="max_output_ratio",
        type=float,
        help=translator.t(Text(en='Maximum final file size ratio for smart mode', zh_cn='Smart 模式最终文件的最大体积比例', ja='Smartモードの最終ファイルサイズ比率の上限')),
    )
    parser.add_argument("--min-video-kbps", dest="min_video_kbps", type=int, help=translator.t(Text(en='Minimum target bitrate', zh_cn='最低目标码率', ja='目標ビットレートの下限')))
    parser.add_argument("--max-video-kbps", dest="max_video_kbps", type=int, help=translator.t(Text(en='Maximum target bitrate', zh_cn='最高目标码率', ja='目標ビットレートの上限')))
    parser.add_argument("--container", choices=["mkv", "mp4"], help=translator.t(Text(en='Output container', zh_cn='输出容器', ja='出力コンテナー')))
    parser.add_argument("--audio-mode", dest="audio_mode", choices=["copy", "aac"], help=translator.t(Text(en='Audio handling mode', zh_cn='音频处理方式', ja='音声の処理方式')))
    parser.add_argument("--audio-bitrate", dest="audio_bitrate", help=translator.t(Text(en='Audio bitrate when re-encoding to AAC', zh_cn='重新编码为 AAC 时的音频码率', ja='AACへ再エンコードする際の音声ビットレート')))
    parser.add_argument("--copy-subtitles", **_bool_action_kwargs(), help=translator.t(Text(en='Copy subtitle streams', zh_cn='复制字幕流', ja='字幕ストリームをコピー')))
    parser.add_argument(
        "--copy-external-subtitles",
        dest="copy_external_subtitles",
        **_bool_action_kwargs(),
        help=translator.t(Text(en='Copy matching external subtitle files next to the encoded output', zh_cn='将匹配的外挂字幕复制到编码输出旁', ja='一致する外部字幕をエンコード出力と同じ場所にコピー')),
    )
    parser.add_argument("--two-pass", dest="two_pass", **_bool_action_kwargs(), help=translator.t(Text(en='Enable two-pass mode', zh_cn='启用双遍编码', ja='2パスモードを有効化')))
    parser.add_argument("--overwrite", **_bool_action_kwargs(), help=translator.t(Text(en='Overwrite existing output', zh_cn='覆盖现有输出', ja='既存の出力を上書き')))
    parser.add_argument("--encoder-preset", dest="encoder_preset", help=translator.t(Text(en='Concrete ffmpeg encoder preset', zh_cn='具体 FFmpeg 编码器预设', ja='使用するFFmpegエンコーダープリセット')))
    parser.add_argument("--pix-fmt", dest="pix_fmt", help=translator.t(Text(en='Output pixel format', zh_cn='输出像素格式', ja='出力ピクセルフォーマット')))
    parser.add_argument("--maxrate-factor", dest="maxrate_factor", type=float, help=translator.t(Text(en='maxrate factor', zh_cn='maxrate 系数', ja='maxrate係数')))
    parser.add_argument("--bufsize-factor", dest="bufsize_factor", type=float, help=translator.t(Text(en='bufsize factor', zh_cn='bufsize 系数', ja='bufsize係数')))
    parser.add_argument("--dry-run", action="store_true", help=translator.t(Text(en='Build the plan and stop', zh_cn='生成计划后停止', ja='計画を作成して終了')))


def _validate_compression_options(options: EncodeOptions, args: argparse.Namespace) -> None:
    tr = _translator_for_args(args)
    if options.compression_mode != CompressionMode.SMART and options.smart_algorithm != SmartAlgorithm.V1:
        raise ValueError(tr.t(Text(en='--smart-algorithm v2_experimental requires --compression-mode smart', zh_cn='--smart-algorithm v2_experimental 需要 --compression-mode smart', ja='--smart-algorithm v2_experimental には --compression-mode smart が必要です。')))
    if not 0 < float(options.min_vmaf) <= 100:
        raise ValueError(tr.t(Text(en='--min-vmaf must be greater than 0 and at most 100', zh_cn='--min-vmaf 必须大于 0 且不超过 100', ja='--min-vmaf は0より大きく100以下である必要があります。')))
    if options.max_output_ratio is not None and not 0 < float(options.max_output_ratio) <= 1:
        raise ValueError(tr.t(Text(en='--max-output-ratio must be greater than 0 and at most 1', zh_cn='--max-output-ratio 必须大于 0 且不超过 1', ja='--max-output-ratio は0より大きく1以下である必要があります。')))
    if options.compression_mode == CompressionMode.SMART and getattr(args, "ratio", None) is not None:
        raise ValueError(tr.t(Text(en='--ratio cannot be used with --compression-mode smart', zh_cn='--ratio 不能与 --compression-mode smart 并用', ja='--ratio は --compression-mode smart と併用できません。')))
    if options.compression_mode == CompressionMode.FIXED_BITRATE and (
        getattr(args, "min_vmaf", None) is not None
        or getattr(args, "max_output_ratio", None) is not None
        or getattr(args, "viewing_context", None) is not None
    ):
        raise ValueError(tr.t(Text(
            en='--min-vmaf, --max-output-ratio, and --viewing-context require --compression-mode smart',
            zh_cn='--min-vmaf、--max-output-ratio 和 --viewing-context 需要 --compression-mode smart',
            ja='--min-vmaf、--max-output-ratio、--viewing-context には --compression-mode smart が必要です。',
        )))


def _resolve_encoder_info(options: EncodeOptions, args: argparse.Namespace):
    ffmpeg_path, _ffprobe_path = discover_ffmpeg_tools(args.ffmpeg, args.ffprobe)
    available_encoders = list_available_encoders(ffmpeg_path)
    encoder_info = resolve_encoder(options.codec, options.backend, available_encoders, ffmpeg_path)
    return ffmpeg_path, encoder_info


def _invalid_preset_message(preset: str, encoder_name: str, choices: list[str], translator: Translator) -> str:
    return translator.t(Text(
        en="Invalid encoder preset '{preset}' for {encoder}. Valid presets: {choices}",
        zh_cn="'{preset}' 不是 {encoder} 的有效编码器预设。有效预设: {choices}",
        ja="{encoder} に無効なエンコーダープリセット '{preset}' が指定されました。有効なプリセット: {choices}",
    ), preset=preset, encoder=encoder_name, choices=', '.join(choices))


def _unverifiable_preset_message(preset: str, encoder_name: str, translator: Translator) -> str:
    return translator.t(Text(
        en="Cannot reliably validate encoder preset '{preset}' for {encoder}. Use the encoder default preset instead.",
        zh_cn="无法可靠验证 {encoder} 的编码器预设 '{preset}'。请改用编码器默认预设。",
        ja="{encoder} のエンコーダープリセット '{preset}' を確実に検証できません。エンコーダーのデフォルトプリセットを使用してください。",
    ), preset=preset, encoder=encoder_name)


def _validate_encoder_preset(options: EncodeOptions, args: argparse.Namespace) -> None:
    preset = options.encoder_preset
    if preset is None:
        return
    ffmpeg_path, encoder_info = _resolve_encoder_info(options, args)
    choices = preset_choices_for_encoder(ffmpeg_path, encoder_info.encoder_name)
    if not choices:
        raise ValueError(_unverifiable_preset_message(preset, encoder_info.encoder_name, _translator_for_args(args)))
    if preset not in choices:
        raise ValueError(_invalid_preset_message(preset, encoder_info.encoder_name, choices, _translator_for_args(args)))


def _build_parser(language: str = 'en') -> argparse.ArgumentParser:
    translator = Translator(language)

    class LocalizedParser(_LocalizedArgumentParser):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, translator=translator, **kwargs)

    parser = LocalizedParser(description="QualityBound CLI")
    _add_language_flag(parser, translator, default=None)
    subparsers = parser.add_subparsers(dest="command", required=True, parser_class=LocalizedParser)

    plan_parser = subparsers.add_parser("plan", help=translator.t(Text(en='Inspect the encode plan', zh_cn='检查编码计划', ja='エンコード計画を確認')))
    _add_runtime_flags(plan_parser, translator)
    _add_encode_flags(plan_parser, translator)

    encode_parser = subparsers.add_parser("encode", help=translator.t(Text(en='Execute the encode plan', zh_cn='执行编码计划', ja='エンコード計画を実行')))
    _add_runtime_flags(encode_parser, translator)
    _add_encode_flags(encode_parser, translator)
    encode_parser.add_argument(
        "--jobs",
        type=int,
        choices=range(1, 9),
        default=1,
        help=translator.t(Text(en='Maximum concurrent full encodes (1-8, default: 1)', zh_cn='最大并发完整编码数（1–8，默认 1）', ja='最大同時エンコード数（1–8、デフォルト: 1）')),
    )
    encode_parser.add_argument(
        "--size-blocked-policy",
        dest="size_blocked_policy",
        choices=[policy.value for policy in SizeBlockedPolicy],
        help=translator.t(Text(en='When VMAF is reachable only above the size limit (default: relax_size)', zh_cn='仅超过体积上限才能达到 VMAF 时的操作（默认 relax_size）', ja='サイズ上限を超えた場合のみVMAFを達成できる時の動作（デフォルト: relax_size）')),
    )
    encode_parser.add_argument(
        "--quality-unreachable-policy",
        dest="quality_unreachable_policy",
        choices=[policy.value for policy in QualityUnreachablePolicy],
        help=translator.t(Text(en='When the encoder cannot reach the VMAF target (default: skip)', zh_cn='编码器无法达到 VMAF 目标时的操作（默认 skip）', ja='エンコーダーがVMAF目標を達成できない時の動作（デフォルト: skip）')),
    )
    encode_parser.add_argument(
        "--constraint-policy",
        choices=[policy.value for policy in ConstraintPolicy],
        help=translator.t(Text(en='Deprecated alias of --size-blocked-policy (fail means ask)', zh_cn='--size-blocked-policy 的旧别名（fail 表示 ask）', ja='--size-blocked-policy の非推奨エイリアス（fail は ask を意味）')),
    )
    encode_parser.add_argument(
        "--skipped-output-policy",
        dest="skipped_output_policy",
        choices=[policy.value for policy in SkippedOutputPolicy],
        help=translator.t(Text(en='What to do with sources skipped by Smart analysis (default: copy)', zh_cn='Smart 分析跳过片源时的操作（默认 copy）', ja='Smart分析でスキップされたソースの処理（デフォルト: copy）')),
    )

    preset_parser = subparsers.add_parser("preset", help=translator.t(Text(en='Manage presets', zh_cn='管理预设', ja='プリセットを管理')))
    preset_sub = preset_parser.add_subparsers(dest="preset_command", required=True, parser_class=LocalizedParser)
    _add_language_flag(preset_parser, translator)

    preset_list = preset_sub.add_parser("list", help=translator.t(Text(en='List available presets', zh_cn='列出可用预设', ja='利用可能なプリセットを一覧表示')))
    _add_language_flag(preset_list, translator)

    preset_load = preset_sub.add_parser("load", help=translator.t(Text(en='Print a preset', zh_cn='打印预设', ja='プリセットを表示')))
    preset_load.add_argument("name")
    _add_language_flag(preset_load, translator)

    preset_delete = preset_sub.add_parser("delete", help=translator.t(Text(en='Delete a preset', zh_cn='删除预设', ja='プリセットを削除')))
    preset_delete.add_argument("name")
    _add_language_flag(preset_delete, translator)

    preset_save = preset_sub.add_parser("save", help=translator.t(Text(en='Save a preset from current options', zh_cn='将当前选项保存为预设', ja='現在のオプションからプリセットを保存')))
    preset_save.add_argument("name")
    preset_save.add_argument("--preset", help=translator.t(Text(en='Base preset name', zh_cn='基础预设名称', ja='ベースプリセット名')))
    _add_language_flag(preset_save, translator)
    _add_encode_flags(preset_save, translator)

    return parser


def _translator_for_args(args: argparse.Namespace) -> Translator:
    app_config = load_app_config()
    language = getattr(args, "lang", None) or app_config.get("language", "en")
    return Translator(language)


def _run_plan(args: argparse.Namespace, config_dir: Path) -> int:
    tr = _translator_for_args(args)
    options = _options_from_args(args, config_dir)
    _validate_compression_options(options, args)
    _validate_encoder_preset(options, args)
    plan = build_encode_plan(
        input_path=Path(args.input),
        options=options,
        output_dir=Path(args.output).expanduser().resolve() if args.output else None,
        workdir=Path(args.workdir).expanduser().resolve() if args.workdir else _default_workdir(),
        ffmpeg_path=args.ffmpeg,
        ffprobe_path=args.ffprobe,
    )
    print_plan(plan, tr)
    return 0


def _run_encode(args: argparse.Namespace, config_dir: Path) -> int:
    tr = _translator_for_args(args)
    options = _options_from_args(args, config_dir)
    _validate_compression_options(options, args)
    _validate_encoder_preset(options, args)
    plan = build_encode_plan(
        input_path=Path(args.input),
        options=options,
        output_dir=Path(args.output).expanduser().resolve() if args.output else None,
        workdir=Path(args.workdir).expanduser().resolve() if args.workdir else _default_workdir(),
        ffmpeg_path=args.ffmpeg,
        ffprobe_path=args.ffprobe,
    )
    print_plan(plan, tr)
    if options.dry_run:
        return 0
    workdir = Path(args.workdir).expanduser().resolve() if args.workdir else _default_workdir()
    if args.jobs > 1:
        results = execute_plan_concurrent(
            plan,
            workdir,
            max_workers=args.jobs,
            log_callback=print if options.smart_algorithm == SmartAlgorithm.V2_EXPERIMENTAL else None,
            constraint_policy=constraint_policy_from_size_blocked(options.size_blocked_policy),
        )
    else:
        results = execute_plan(
            plan,
            workdir,
            log_callback=print if options.smart_algorithm == SmartAlgorithm.V2_EXPERIMENTAL else None,
            constraint_policy=constraint_policy_from_size_blocked(options.size_blocked_policy),
        )
    if options.skipped_output_policy == SkippedOutputPolicy.COPY:
        by_source = {result.source_path: result for result in results}
        published = publish_skipped_sources(
            [(item, by_source[item.source_path]) for item in plan.items if item.source_path in by_source]
        )
        for item in published:
            if item.copied:
                print(tr.t(Text(
                    en='Copied Smart-skipped source {file} -> {path}',
                    zh_cn='已复制 Smart 跳过的片源 {file} -> {path}',
                    ja='Smartでスキップされたソースをコピーしました {file} -> {path}',
                ), file=item.source_path.name, path=item.output_path))
            elif item.reason:
                print(tr.t(Text(
                    en='Did not copy Smart-skipped source {file}: {reason}',
                    zh_cn='未复制 Smart 跳过的片源 {file}: {reason}',
                    ja='Smartでスキップされたソースをコピーしませんでした {file}: {reason}',
                ), file=item.source_path.name, reason=item.reason))
    elif options.skipped_output_policy == SkippedOutputPolicy.ASK:
        print(tr.t(Text(
            en='Skipped-output policy is ask; CLI leaves Smart-skipped sources in place.',
            zh_cn='跳过文件策略为 ask；CLI 将 Smart 跳过的片源保留在原处。',
            ja='スキップ出力ポリシーがaskのため、CLIはSmartでスキップされたソースを元の場所に保持します。',
        )))
    print_encode_results(results, tr)
    if any(result.needs_decision for result in results):
        return 3
    return 0 if all(result.success or result.skipped for result in results) else 2


def _run_preset(args: argparse.Namespace, config_dir: Path) -> int:
    tr = _translator_for_args(args)
    if args.preset_command == "list":
        names = list_presets(config_dir)
        if not names:
            print(tr.t(Text(en='No presets found.', zh_cn='没有可用预设。', ja='プリセットが見つかりません。')))
            return 0
        for name in names:
            print(name)
        return 0

    if args.preset_command == "load":
        options = load_preset(args.name, config_dir)
        print(json.dumps(encode_options_to_preset_data(options), indent=2, ensure_ascii=False))
        return 0

    if args.preset_command == "delete":
        delete_preset(args.name, config_dir)
        print(tr.t(Text(en="Preset '{name}' deleted", zh_cn="预设 '{name}' 已删除", ja="プリセット '{name}' を削除しました"), name=args.name))
        return 0

    if args.preset_command == "save":
        options = _options_from_args(args, config_dir)
        _validate_compression_options(options, args)
        _validate_encoder_preset(options, args)
        path = save_preset(args.name, options, config_dir)
        print(tr.t(Text(en="Preset '{name}' saved to {path}", zh_cn="预设 '{name}' 已保存到 {path}", ja="プリセット '{name}' を {path} に保存しました"), name=args.name, path=path))
        return 0

    raise ValueError(tr.t(Text(
        en='Unsupported preset command: {command}', zh_cn='不支持的预设命令: {command}', ja='未対応のプリセットコマンド: {command}',
    ), command=args.preset_command))


def run_cli(argv: list[str] | None = None) -> int:
    ensure_runtime_layout()
    arguments = list(sys.argv[1:] if argv is None else argv)
    translator = _translator_for_args(argparse.Namespace(lang=_language_from_argv(arguments)))
    parser = _build_parser(translator.language)
    args = parser.parse_args(arguments)
    config_dir = _config_dir()

    try:
        if args.command == "plan":
            return _run_plan(args, config_dir)
        if args.command == "encode":
            return _run_encode(args, config_dir)
        if args.command == "preset":
            return _run_preset(args, config_dir)
    except Exception as exc:
        tr = _translator_for_args(args)
        print(f"{tr.t(Text(en='Error', zh_cn='错误', ja='エラー'))}: {exc}", file=sys.stderr)
        return 2

    parser.print_help()
    return 1
