from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import TypedDict

from cli.cli_interactive import print_encode_results, print_plan
from core.config import (
    app_root,
    bundle_root,
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
from core.i18n import TranslationCatalog
from core.media import publish_skipped_sources
from core.models import (
    AnalysisProfileName,
    AudioMode,
    BackendChoice,
    CodecChoice,
    CompressionMode,
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _config_dir() -> Path:
    return app_config_dir()


def _default_workdir() -> Path:
    return workdir_dir()


def _load_base_options(args: argparse.Namespace, config_dir: Path) -> EncodeOptions:
    preset_name = getattr(args, "preset", None)
    if preset_name:
        return load_preset(preset_name, config_dir)

    app_config = load_app_config(config_dir)
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
    if getattr(args, "encoder_preset", None) is not None:
        raise ValueError(
            "--encoder-preset cannot be used with --backend auto. "
            "Choose a concrete backend, or omit --encoder-preset."
        )
    print(
        "Warning: inherited encoder preset ignored because backend is auto.",
        file=sys.stderr,
    )
    return replace(options, encoder_preset=None)


def _apply_analysis_profile(options: EncodeOptions, args: argparse.Namespace, config_dir: Path) -> EncodeOptions:
    app_config = load_app_config(config_dir)
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


def _default_catalog() -> TranslationCatalog:
    return TranslationCatalog(
        bundle_dir=bundle_root() / "config" / "i18n",
        translations_dir=app_root() / "translations",
    )


def _lang_choices(catalog: TranslationCatalog) -> list[str]:
    return [info.code for info in catalog.languages()]


def _add_runtime_flags(
    parser: argparse.ArgumentParser,
    catalog: TranslationCatalog,
    include_input: bool = True,
) -> None:
    if include_input:
        parser.add_argument("input", help="Input file or directory")
    parser.add_argument("-o", "--output", help="Output directory")
    parser.add_argument("--workdir", help="Working directory")
    parser.add_argument("--ffmpeg", help="Path to ffmpeg")
    parser.add_argument("--ffprobe", help="Path to ffprobe")
    parser.add_argument("--preset", help="Saved preset name")
    parser.add_argument("--lang", choices=_lang_choices(catalog), help="Language pack to load")
    parser.add_argument("--recursive", **_bool_action_kwargs(), help="Recursively scan subdirectories")


def _add_encode_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--codec", choices=["hevc", "av1"], help="Target codec")
    parser.add_argument(
        "--compression-mode",
        dest="compression_mode",
        choices=[mode.value for mode in CompressionMode],
        help="Compression policy",
    )
    parser.add_argument(
        "--backend",
        choices=[backend.value for backend in BackendChoice],
        help="Encoder backend",
    )
    parser.add_argument(
        "--decode-acceleration",
        dest="decode_acceleration",
        choices=[acceleration.value for acceleration in DecodeAcceleration],
        help="Video decoding acceleration method",
    )
    parser.add_argument("--ratio", type=float, help="Target video bitrate ratio")
    parser.add_argument(
        "--min-vmaf",
        dest="min_vmaf",
        type=float,
        help="Lowest sampled-window VMAF v1 target for Smart mode (default: 90)",
    )
    parser.add_argument(
        "--analysis-profile",
        dest="analysis_profile",
        choices=[name.value for name in AnalysisProfileName],
        help="Smart VMAF scan profile: fast, balance, or precise",
    )
    parser.add_argument(
        "--viewing-context",
        dest="viewing_context",
        choices=[context.value for context in VmafViewingContext],
        help="Smart viewing scenario: high_fidelity (default) or standard_display",
    )
    parser.add_argument(
        "--max-output-ratio",
        dest="max_output_ratio",
        type=float,
        help="Maximum final file size ratio for smart mode",
    )
    parser.add_argument("--min-video-kbps", dest="min_video_kbps", type=int, help="Minimum target bitrate")
    parser.add_argument("--max-video-kbps", dest="max_video_kbps", type=int, help="Maximum target bitrate")
    parser.add_argument("--container", choices=["mkv", "mp4"], help="Output container")
    parser.add_argument("--audio-mode", dest="audio_mode", choices=["copy", "aac"], help="Audio handling mode")
    parser.add_argument("--audio-bitrate", dest="audio_bitrate", help="Audio bitrate when re-encoding to AAC")
    parser.add_argument("--copy-subtitles", **_bool_action_kwargs(), help="Copy subtitle streams")
    parser.add_argument(
        "--copy-external-subtitles",
        dest="copy_external_subtitles",
        **_bool_action_kwargs(),
        help="Copy matching external subtitle files next to the encoded output",
    )
    parser.add_argument("--two-pass", dest="two_pass", **_bool_action_kwargs(), help="Enable two-pass mode")
    parser.add_argument("--overwrite", **_bool_action_kwargs(), help="Overwrite existing output")
    parser.add_argument("--encoder-preset", dest="encoder_preset", help="Concrete ffmpeg encoder preset")
    parser.add_argument("--pix-fmt", dest="pix_fmt", help="Output pixel format")
    parser.add_argument("--maxrate-factor", dest="maxrate_factor", type=float, help="maxrate factor")
    parser.add_argument("--bufsize-factor", dest="bufsize_factor", type=float, help="bufsize factor")
    parser.add_argument("--dry-run", action="store_true", help="Build the plan and stop")


def _validate_compression_options(options: EncodeOptions, args: argparse.Namespace) -> None:
    if not 0 < float(options.min_vmaf) <= 100:
        raise ValueError("--min-vmaf must be greater than 0 and at most 100")
    if options.max_output_ratio is not None and not 0 < float(options.max_output_ratio) <= 1:
        raise ValueError("--max-output-ratio must be greater than 0 and at most 1")
    if options.compression_mode == CompressionMode.SMART and getattr(args, "ratio", None) is not None:
        raise ValueError("--ratio cannot be used with --compression-mode smart")
    if options.compression_mode == CompressionMode.FIXED_BITRATE and (
        getattr(args, "min_vmaf", None) is not None
        or getattr(args, "max_output_ratio", None) is not None
        or getattr(args, "viewing_context", None) is not None
    ):
        raise ValueError(
            "--min-vmaf, --max-output-ratio, and --viewing-context require "
            "--compression-mode smart"
        )


def _resolve_encoder_info(options: EncodeOptions, args: argparse.Namespace):
    ffmpeg_path, _ffprobe_path = discover_ffmpeg_tools(args.ffmpeg, args.ffprobe)
    available_encoders = list_available_encoders(ffmpeg_path)
    encoder_info = resolve_encoder(options.codec, options.backend, available_encoders, ffmpeg_path)
    return ffmpeg_path, encoder_info


def _invalid_preset_message(preset: str, encoder_name: str, choices: list[str]) -> str:
    choice_text = ", ".join(choices)
    return f"Invalid encoder preset '{preset}' for {encoder_name}. Valid presets: {choice_text}"


def _unverifiable_preset_message(preset: str, encoder_name: str) -> str:
    return (
        f"Cannot reliably validate encoder preset '{preset}' for {encoder_name}. "
        "Use the encoder default preset instead."
    )


def _validate_encoder_preset(options: EncodeOptions, args: argparse.Namespace) -> None:
    preset = options.encoder_preset
    if preset is None:
        return
    ffmpeg_path, encoder_info = _resolve_encoder_info(options, args)
    choices = preset_choices_for_encoder(ffmpeg_path, encoder_info.encoder_name)
    if not choices:
        raise ValueError(_unverifiable_preset_message(preset, encoder_info.encoder_name))
    if preset not in choices:
        raise ValueError(_invalid_preset_message(preset, encoder_info.encoder_name, choices))


def _build_parser(catalog: TranslationCatalog | None = None) -> argparse.ArgumentParser:
    if catalog is None:
        catalog = _default_catalog()
    parser = argparse.ArgumentParser(description="QualityBound CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan_parser = subparsers.add_parser("plan", help="Inspect the encode plan")
    _add_runtime_flags(plan_parser, catalog)
    _add_encode_flags(plan_parser)

    encode_parser = subparsers.add_parser("encode", help="Execute the encode plan")
    _add_runtime_flags(encode_parser, catalog)
    _add_encode_flags(encode_parser)
    encode_parser.add_argument(
        "--jobs",
        type=int,
        choices=range(1, 9),
        default=1,
        help="Maximum concurrent full encodes (1-8, default: 1)",
    )
    encode_parser.add_argument(
        "--size-blocked-policy",
        dest="size_blocked_policy",
        choices=[policy.value for policy in SizeBlockedPolicy],
        help="When VMAF is reachable only above the size limit (default: relax_size)",
    )
    encode_parser.add_argument(
        "--quality-unreachable-policy",
        dest="quality_unreachable_policy",
        choices=[policy.value for policy in QualityUnreachablePolicy],
        help="When the encoder cannot reach the VMAF target (default: skip)",
    )
    encode_parser.add_argument(
        "--constraint-policy",
        choices=[policy.value for policy in ConstraintPolicy],
        help="Deprecated alias of --size-blocked-policy (fail means ask)",
    )
    encode_parser.add_argument(
        "--skipped-output-policy",
        dest="skipped_output_policy",
        choices=[policy.value for policy in SkippedOutputPolicy],
        help="What to do with sources skipped by Smart analysis (default: copy)",
    )

    preset_parser = subparsers.add_parser("preset", help="Manage presets")
    preset_sub = preset_parser.add_subparsers(dest="preset_command", required=True)

    preset_list = preset_sub.add_parser("list", help="List available presets")
    preset_list.add_argument("--lang", choices=_lang_choices(catalog), help="Language pack to load")

    preset_load = preset_sub.add_parser("load", help="Print a preset")
    preset_load.add_argument("name")
    preset_load.add_argument("--lang", choices=_lang_choices(catalog), help="Language pack to load")

    preset_delete = preset_sub.add_parser("delete", help="Delete a preset")
    preset_delete.add_argument("name")
    preset_delete.add_argument("--lang", choices=_lang_choices(catalog), help="Language pack to load")

    preset_save = preset_sub.add_parser("save", help="Save a preset from current options")
    preset_save.add_argument("name")
    preset_save.add_argument("--preset", help="Base preset name")
    preset_save.add_argument("--lang", choices=_lang_choices(catalog), help="Language pack to load")
    _add_encode_flags(preset_save)

    return parser


def _translator_for_args(
    args: argparse.Namespace,
    config_dir: Path,
    catalog: TranslationCatalog,
):
    app_config = load_app_config(config_dir)
    language = getattr(args, "lang", None) or app_config.get("language", "en")
    return catalog.translator(language)


def _run_plan(args: argparse.Namespace, config_dir: Path, catalog: TranslationCatalog) -> int:
    tr = _translator_for_args(args, config_dir, catalog)
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
        config_dir=config_dir,
    )
    print_plan(plan, tr)
    return 0


def _run_encode(args: argparse.Namespace, config_dir: Path, catalog: TranslationCatalog) -> int:
    tr = _translator_for_args(args, config_dir, catalog)
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
        config_dir=config_dir,
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
            constraint_policy=constraint_policy_from_size_blocked(options.size_blocked_policy),
        )
    else:
        results = execute_plan(
            plan,
            workdir,
            constraint_policy=constraint_policy_from_size_blocked(options.size_blocked_policy),
        )
    print_encode_results(results, tr)
    if options.skipped_output_policy == SkippedOutputPolicy.COPY:
        by_source = {result.source_path: result for result in results}
        published = publish_skipped_sources(
            [(item, by_source[item.source_path]) for item in plan.items if item.source_path in by_source]
        )
        for item in published:
            if item.copied:
                print(f"Copied Smart-skipped source {item.source_path.name} -> {item.output_path}")
            elif item.reason:
                print(f"Did not copy Smart-skipped source {item.source_path.name}: {item.reason}")
    elif options.skipped_output_policy == SkippedOutputPolicy.ASK:
        print("Skipped-output policy is ask; CLI leaves Smart-skipped sources in place.")
    if any(result.needs_decision for result in results):
        return 3
    return 0 if all(result.success or result.skipped for result in results) else 2


def _run_preset(args: argparse.Namespace, config_dir: Path, catalog: TranslationCatalog) -> int:
    tr = _translator_for_args(args, config_dir, catalog)
    if args.preset_command == "list":
        names = list_presets(config_dir)
        if not names:
            print(tr.t("cli.no_presets"))
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
        print(tr.t("cli.preset_deleted", name=args.name))
        return 0

    if args.preset_command == "save":
        options = _options_from_args(args, config_dir)
        _validate_compression_options(options, args)
        _validate_encoder_preset(options, args)
        path = save_preset(args.name, options, config_dir)
        print(tr.t("cli.preset_saved", name=args.name, path=path))
        return 0

    raise ValueError(f"Unsupported preset command: {args.preset_command}")


def run_cli(argv: list[str] | None = None) -> int:
    ensure_runtime_layout()
    catalog = _default_catalog()
    for diagnostic in catalog.diagnostics():
        print(
            f"i18n warning ({diagnostic.locale}): {diagnostic.message}",
            file=sys.stderr,
        )
    parser = _build_parser(catalog)
    args = parser.parse_args(argv)
    config_dir = _config_dir()

    try:
        if args.command == "plan":
            return _run_plan(args, config_dir, catalog)
        if args.command == "encode":
            return _run_encode(args, config_dir, catalog)
        if args.command == "preset":
            return _run_preset(args, config_dir, catalog)
    except Exception as exc:
        tr = _translator_for_args(args, config_dir, catalog)
        print(f"{tr.t('cli.error')}: {exc}", file=sys.stderr)
        return 2

    parser.print_help()
    return 1
