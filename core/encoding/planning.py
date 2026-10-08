from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, cast

from core.config.paths import config_dir as app_config_dir
from core.ffmpeg.capabilities import ensure_encoder_capabilities
from core.ffmpeg.discovery import discover_ffmpeg_tools
from core.ffmpeg.encoders import (
    is_valid_preset,
    preset_choices_for_encoder,
    preset_choices_from_capabilities,
    resolve_encoder,
)
from core.media.bitrate import choose_ratio, compute_target_video_bitrate
from core.models import (
    CompressionMode,
    DecodeAcceleration,
    EncodeOptions,
    EncodePlan,
    EncodePlanItem,
    EncoderInfo,
    OperationCancelledError,
    VideoFileItem,
)
from core.ffmpeg.probe import probe_media_info
from core.media.discovery import collect_video_files
from core.media.paths import (
    build_explicit_file_output_path,
    build_output_path,
    choose_output_root,
    disambiguate_output_path,
)
from core.media.subtitles import discover_external_subtitles
from core.media.validation import (
    normalized_output_path,
    validate_plan_item,
    validate_unique_output_paths,
    validate_workdir,
)
from core.progress_events import ProgressCallback, ProgressEvent


def _emit(progress_callback: Callable[[str], None] | None, message: str) -> None:
    if progress_callback is not None:
        progress_callback(message)


def _emit_progress(
    event_callback: ProgressCallback | None,
    **event: object,
) -> None:
    if event_callback is not None:
        event_callback(cast(ProgressEvent, event))


def _iter_sources(
    input_path: Path | None,
    recursive: bool,
    files: Iterable[VideoFileItem] | None,
    on_error: Callable[[str], None] | None = None,
) -> tuple[Path, list[VideoFileItem]]:
    if files is not None:
        file_items = []
        seen_sources: set[str] = set()
        for file_item in files:
            source = file_item.path.expanduser().resolve()
            source_key = str(source).casefold()
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            file_items.append(
                VideoFileItem(path=source, relative_path=file_item.relative_path)
            )
        if not file_items:
            raise ValueError("No video files were provided for planning.")
        root = file_items[0].path.parent
        return root, file_items
    if input_path is None:
        raise ValueError("input_path or files must be provided.")
    input_root = input_path.expanduser().resolve()
    return input_root, collect_video_files(input_root, recursive, on_error)


def _usable_encoder_count(runtime_capabilities: dict) -> int:
    return sum(len(items) for items in runtime_capabilities.get("codecs", {}).values())


def _validate_decode_acceleration(
    options: EncodeOptions,
    runtime_capabilities: dict,
) -> None:
    if options.decode_acceleration != DecodeAcceleration.VIDEOTOOLBOX:
        return

    hwaccels = runtime_capabilities.get("hwaccels", [])
    if "videotoolbox" not in hwaccels:
        raise RuntimeError(
            "VideoToolbox decoding was requested, but the selected FFmpeg build "
            "does not expose the videotoolbox hardware accelerator."
        )


def _options_with_default_preset(
    options: EncodeOptions,
    ffmpeg: Path,
    encoder_info: EncoderInfo,
    progress_callback: Callable[[str], None] | None,
    runtime_capabilities: dict | None = None,
) -> EncodeOptions:
    if options.encoder_preset is not None:
        choices = (
            preset_choices_from_capabilities(
                runtime_capabilities, encoder_info.codec, encoder_info.backend
            )
            if runtime_capabilities is not None
            else preset_choices_for_encoder(ffmpeg, encoder_info.encoder_name)
        )
        if options.encoder_preset.strip() not in choices:
            raise RuntimeError(
                f"Encoder preset {options.encoder_preset!r} is not valid for "
                f"{encoder_info.encoder_name}."
            )
        return options

    default_preset = encoder_info.default_preset
    if default_preset:
        choices = (
            preset_choices_from_capabilities(
                runtime_capabilities, encoder_info.codec, encoder_info.backend
            )
            if runtime_capabilities is not None
            else preset_choices_for_encoder(ffmpeg, encoder_info.encoder_name)
        )
        valid_default = (
            default_preset in choices
            if runtime_capabilities is not None
            else is_valid_preset(ffmpeg, encoder_info.encoder_name, default_preset)
        )
        if choices and not valid_default:
            _emit(
                progress_callback,
                f"Default encoder preset {default_preset!r} is not valid for {encoder_info.encoder_name}; falling back to encoder defaults.",
            )
            default_preset = None
    options = replace(options, encoder_preset=default_preset)
    if default_preset:
        _emit(progress_callback, f"Using default encoder preset: {default_preset}")
    return options


def _resolve_plan_encoder(
    options: EncodeOptions,
    ffmpeg: Path,
    config_dir: Path | None,
    progress_callback: Callable[[str], None] | None,
    runtime_capabilities: dict | None = None,
) -> tuple[EncoderInfo, EncodeOptions]:
    if runtime_capabilities is None:
        runtime_capabilities = ensure_encoder_capabilities(
            config_dir or app_config_dir(),
            ffmpeg,
            progress_callback=progress_callback,
        )
    _validate_decode_acceleration(options, runtime_capabilities)
    _emit(progress_callback, f"Detected {_usable_encoder_count(runtime_capabilities)} usable encoder candidate(s).")
    encoder_info = resolve_encoder(
        options.codec,
        options.backend,
        set(),
        None,
        runtime_capabilities=runtime_capabilities,
    )
    _emit(
        progress_callback,
        f"Resolved encoder: {encoder_info.encoder_name} ({encoder_info.backend.value})",
    )
    return encoder_info, _options_with_default_preset(
        options,
        ffmpeg,
        encoder_info,
        progress_callback,
        runtime_capabilities,
    )


def _build_default_output(
    file_item: VideoFileItem,
    input_root: Path,
    output_root: Path,
    options: EncodeOptions,
) -> Path:
    return build_output_path(
        source_path=file_item.path,
        input_root=input_root if input_root.is_dir() else file_item.path.parent,
        output_root=output_root,
        codec=options.codec,
        container=options.container,
    )


def _build_explicit_outputs(
    file_items: list[VideoFileItem],
    output_dir: Path | None,
    options: EncodeOptions,
) -> list[Path]:
    outputs = [
        build_explicit_file_output_path(
            file_item.path,
            file_item.relative_path,
            output_dir,
            options.codec,
            options.container,
        )
        for file_item in file_items
    ]
    groups: dict[str, list[int]] = {}
    for index, output_path in enumerate(outputs):
        groups.setdefault(normalized_output_path(output_path), []).append(index)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        for index in indexes:
            outputs[index] = disambiguate_output_path(
                outputs[index], file_items[index].path
            )
    validate_unique_output_paths(
        (file_item.path, output_path)
        for file_item, output_path in zip(file_items, outputs, strict=True)
    )
    return outputs


def reconfigure_plan_item(
    item: EncodePlanItem,
    options: EncodeOptions,
    *,
    ffmpeg_path: Path,
    workdir: Path,
    config_dir: Path | None = None,
    runtime_capabilities: dict | None = None,
    output_path: Path | None = None,
    create_directories: bool = True,
) -> EncodePlanItem:
    """Return a validated, independently bound copy of an existing plan item."""

    candidate = copy.deepcopy(item)
    resolved_encoder, resolved_options = _resolve_plan_encoder(
        copy.deepcopy(options),
        ffmpeg_path,
        config_dir,
        None,
        runtime_capabilities,
    )
    candidate.options = resolved_options
    candidate.encoder_info = copy.deepcopy(resolved_encoder)
    candidate.output_path = (
        output_path.expanduser().resolve()
        if output_path is not None
        else candidate.output_path.with_name(
            f"{candidate.source_path.stem}_{resolved_options.codec.value}."
            f"{resolved_options.container.value}"
        )
    )
    candidate.quality_search_result = None
    candidate.segmented_analysis_result = None
    candidate.segmented_video_budget_bytes = None
    candidate.skip_reason = None
    if resolved_options.compression_mode == CompressionMode.FIXED_BITRATE:
        if candidate.media_info is None:
            raise RuntimeError("A probed media item is required for fixed-bitrate reconfiguration.")
        candidate.target_video_bitrate_bps = compute_target_video_bitrate(
            candidate.media_info.video_bitrate_bps,
            choose_ratio(resolved_options.codec, resolved_options.ratio),
            resolved_options.min_video_kbps,
            resolved_options.max_video_kbps,
        )
    else:
        candidate.target_video_bitrate_bps = 0
    validate_plan_item(
        candidate.source_path,
        candidate.output_path,
        candidate.options,
        resolved_encoder,
        workdir,
        create_directories=create_directories,
    )
    return candidate


def _successful_plan_item(
    file_item: VideoFileItem,
    default_output: Path,
    ffprobe: Path,
    ratio: float | None,
    options: EncodeOptions,
    encoder_info: EncoderInfo,
    workdir: Path,
    cancel_check: Callable[[], bool] | None = None,
) -> EncodePlanItem:
    media_info = probe_media_info(ffprobe, file_item.path, cancel_check=cancel_check)
    target_bitrate = 0
    if options.compression_mode == CompressionMode.FIXED_BITRATE:
        if ratio is None:
            raise ValueError("Fixed bitrate mode requires an effective ratio.")
        target_bitrate = compute_target_video_bitrate(
            media_info.video_bitrate_bps,
            ratio,
            options.min_video_kbps,
            options.max_video_kbps,
        )
    item = EncodePlanItem(
        source_path=file_item.path,
        output_path=default_output,
        media_info=media_info,
        encoder_info=encoder_info,
        options=options,
        target_video_bitrate_bps=target_bitrate,
        ffprobe_path=ffprobe,
    )
    if options.copy_external_subtitles:
        sidecars = discover_external_subtitles(file_item.path)
        if sidecars:
            item.warnings.append(
                f"Will copy {len(sidecars)} external subtitle file(s) next to the output."
            )
    validate_plan_item(
        source_path=file_item.path,
        output_path=default_output,
        options=options,
        encoder_info=encoder_info,
        workdir=workdir,
    )
    return item


def _skipped_plan_item(
    file_item: VideoFileItem,
    default_output: Path,
    options: EncodeOptions,
    encoder_info: EncoderInfo,
    exc: Exception,
) -> EncodePlanItem:
    return EncodePlanItem(
        source_path=file_item.path,
        output_path=default_output,
        media_info=None,
        encoder_info=encoder_info,
        options=options,
        target_video_bitrate_bps=0,
        skip_reason=str(exc),
    )


def _emit_probe_started(
    file_item: VideoFileItem,
    index: int,
    total: int,
    progress_callback: Callable[[str], None] | None,
    progress_event_callback: ProgressCallback | None,
) -> None:
    _emit(
        progress_callback,
        f"[{index}/{total}] Probing source: {file_item.path}",
    )
    _emit_progress(
        progress_event_callback,
        stage="planning",
        state="probing",
        file_path=str(file_item.path),
        file_name=file_item.path.name,
        current=index,
        total=total,
        percent=((index - 1) / max(total, 1)) * 100.0,
    )


def _emit_plan_item_status(
    state: str,
    file_item: VideoFileItem,
    index: int,
    total: int,
    progress_callback: Callable[[str], None] | None,
    progress_event_callback: ProgressCallback | None,
    *,
    output_path: Path | None = None,
    error: str | None = None,
) -> None:
    if state == "planned" and output_path is not None:
        _emit(
            progress_callback,
            f"[{index}/{total}] Planned: {file_item.path.name} -> {output_path}",
        )
    elif state == "skipped" and error is not None:
        _emit(progress_callback, f"[{index}/{total}] Skipped: {file_item.path.name} | {error}")

    event: dict[str, object] = {
        "stage": "planning",
        "state": state,
        "file_path": str(file_item.path),
        "file_name": file_item.path.name,
        "current": index,
        "total": total,
        "percent": (index / max(total, 1)) * 100.0,
    }
    if output_path is not None:
        event["output_path"] = str(output_path)
    if error is not None:
        event["error"] = error
    _emit_progress(progress_event_callback, **event)


def build_encode_plan(
    input_path: Path | None,
    options: EncodeOptions,
    *,
    output_dir: Path | None = None,
    workdir: Path = Path("workdir"),
    ffmpeg_path: str | None = None,
    ffprobe_path: str | None = None,
    config_dir: Path | None = None,
    files: Iterable[VideoFileItem] | None = None,
    progress_callback: Callable[[str], None] | None = None,
    progress_event_callback: ProgressCallback | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> EncodePlan:
    _emit(progress_callback, "Planning started.")
    _emit_progress(progress_event_callback, stage="planning", state="started", percent=0.0)
    explicit_files = files is not None
    input_root, file_items = _iter_sources(input_path, options.recursive, files, progress_callback)
    if not file_items:
        raise FileNotFoundError("No processable video files were found.")

    workdir = validate_workdir(workdir)
    _emit(progress_callback, f"Validated workdir: {workdir}")
    ffmpeg, ffprobe = discover_ffmpeg_tools(ffmpeg_path, ffprobe_path)
    _emit(progress_callback, f"Using ffmpeg: {ffmpeg}")
    _emit(progress_callback, f"Using ffprobe: {ffprobe}")
    encoder_info, options = _resolve_plan_encoder(options, ffmpeg, config_dir, progress_callback)

    output_root = choose_output_root(input_root, output_dir, options.codec)
    _emit(progress_callback, f"Output root: {output_root}")
    items: list[EncodePlanItem] = []
    ratio = None
    if options.compression_mode == CompressionMode.FIXED_BITRATE:
        ratio = choose_ratio(options.codec, options.ratio)
        _emit(progress_callback, f"Effective bitrate ratio: {ratio:.3f}")
    else:
        _emit(progress_callback, "Smart compression: target bitrate will be selected during execution.")
    _emit(progress_callback, f"Discovered {len(file_items)} input item(s).")

    explicit_outputs = (
        _build_explicit_outputs(file_items, output_dir, options)
        if explicit_files
        else None
    )

    for index, file_item in enumerate(file_items, start=1):
        if cancel_check is not None and cancel_check():
            _emit(progress_callback, "Planning cancelled by user.")
            _emit_progress(
                progress_event_callback,
                stage="planning",
                state="cancelled",
                percent=((index - 1) / max(len(file_items), 1)) * 100.0,
            )
            raise OperationCancelledError("Planning cancelled.")
        total_items = len(file_items)
        _emit_probe_started(file_item, index, total_items, progress_callback, progress_event_callback)
        default_output = (
            explicit_outputs[index - 1]
            if explicit_outputs is not None
            else _build_default_output(file_item, input_root, output_root, options)
        )
        try:
            item = _successful_plan_item(
                file_item,
                default_output,
                ffprobe,
                ratio,
                options,
                encoder_info,
                workdir,
                cancel_check,
            )
            _emit_plan_item_status(
                "planned",
                file_item,
                index,
                total_items,
                progress_callback,
                progress_event_callback,
                output_path=default_output,
            )
        except OperationCancelledError:
            _emit(progress_callback, "Planning cancelled by user.")
            _emit_progress(
                progress_event_callback,
                stage="planning",
                state="cancelled",
                percent=((index - 1) / max(len(file_items), 1)) * 100.0,
            )
            raise
        except Exception as exc:
            # Probing or validation failures produce skipped items rather than
            # aborting the whole batch.
            item = _skipped_plan_item(file_item, default_output, options, encoder_info, exc)
            _emit_plan_item_status(
                "skipped",
                file_item,
                index,
                total_items,
                progress_callback,
                progress_event_callback,
                error=str(exc),
            )
        items.append(item)

    validate_unique_output_paths(
        (item.source_path, item.output_path) for item in items
    )

    _emit(progress_callback, "Planning finished.")
    _emit_progress(progress_event_callback, stage="planning", state="finished", percent=100.0)
    return EncodePlan(
        items=items,
        ffmpeg_path=ffmpeg,
        ffprobe_path=ffprobe,
        input_root=input_root,
        output_root=output_root,
    )
