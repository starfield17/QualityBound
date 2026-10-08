from __future__ import annotations

from core.i18n import Translator
from core.media import human_kbps
from core.models import CompressionMode, EncodePlan, EncodeResult, SmartAlgorithm
from core.smart import build_decision_options


def print_plan(plan: EncodePlan, tr: Translator) -> None:
    print(tr.t("cli.plan_header", count=len(plan.items)))
    for item in plan.items:
        if item.skip_reason:
            print(f"[{tr.t('cli.plan_skip')}] {item.source_path}")
            print(f"  {tr.t('cli.reason')}: {item.skip_reason}")
            print(f"  {tr.t('cli.output')}: {item.output_path}")
            continue

        media = item.media_info
        encoder = item.encoder_info
        if media is None or encoder is None:
            raise ValueError(f"Planned item is missing media or encoder information: {item.source_path}")
        fps = f"{media.fps:.3f}" if media and media.fps else "n/a"
        wh = f"{media.width}x{media.height}" if media and media.width and media.height else "n/a"
        print(f"[{tr.t('cli.plan_ready')}] {item.source_path}")
        print(f"  {tr.t('cli.resolution')}: {wh}")
        print(f"  {tr.t('cli.fps')}: {fps}")
        print(f"  {tr.t('cli.source_bitrate')}: {human_kbps(media.video_bitrate_bps)}")
        target = (
            tr.t("cli.pending_analysis")
            if item.options.compression_mode == CompressionMode.SMART and item.target_video_bitrate_bps <= 0
            else human_kbps(item.target_video_bitrate_bps)
        )
        print(f"  {tr.t('cli.target_bitrate')}: {target}")
        print(f"  {tr.t('cli.encoder')}: {encoder.encoder_name} ({encoder.backend.value})")
        if item.options.compression_mode == CompressionMode.SMART and item.options.smart_algorithm == SmartAlgorithm.V2_EXPERIMENTAL:
            print(f"  Smart v2 (experimental): whole-video mean VMAF target {item.options.min_vmaf:.1f}")
        print(f"  {tr.t('cli.output')}: {item.output_path}")
        for warning in item.warnings:
            print(f"  {tr.t('cli.note')}: {warning}")
    print(f"{tr.t('cli.ffmpeg')}: {plan.ffmpeg_path}")
    print(f"{tr.t('cli.ffprobe')}: {plan.ffprobe_path}")
    print(f"{tr.t('cli.output_root')}: {plan.output_root}")


def print_encode_results(results: list[EncodeResult], tr: Translator) -> None:
    for result in results:
        if result.needs_decision:
            print(f"[{tr.t('cli.result_needs_decision')}] {result.source_path}")
            print(f"  {tr.t('cli.reason')}: {result.error_message}")
            if result.rejected_output_path is not None:
                print(f"  {tr.t('cli.rejected_output')}: {result.rejected_output_path}")
            elif result.quality_search_result is not None or result.segmented_analysis_result is not None:
                decision_result = result.quality_search_result or result.segmented_analysis_result
                assert decision_result is not None
                for option in build_decision_options(decision_result):
                    suffix = f"={option.suggested_value}" if option.suggested_value is not None else ""
                    print(f"  {tr.t('cli.available_decision')}: {option.action_code.value}{suffix}")
        elif result.skipped:
            print(f"[{tr.t('cli.result_skipped')}] {result.source_path}")
            print(f"  {tr.t('cli.reason')}: {result.error_message}")
        elif result.success:
            print(f"[{tr.t('cli.result_success')}] {result.source_path} -> {result.output_path}")
        else:
            print(f"[{tr.t('cli.result_failed')}] {result.source_path}")
            print(f"  {tr.t('cli.reason')}: {result.error_message}")
        quality = result.quality_search_result
        segmented = result.segmented_analysis_result
        if segmented is not None:
            mean = segmented.final_mean_vmaf if segmented.final_mean_vmaf is not None else segmented.predicted_mean_vmaf
            print(f"  Smart v2 (experimental): {len(segmented.shots)} shots; "
                  f"{'measured' if segmented.final_mean_vmaf is not None else 'predicted'} mean VMAF={mean}")
        if quality is not None:
            if quality.min_vmaf is not None:
                print(f"  {tr.t('cli.minimum_vmaf')}: {quality.min_vmaf:.2f}")
            if quality.predicted_output_ratio is not None:
                print(f"  {tr.t('cli.predicted_ratio')}: {quality.predicted_output_ratio:.3f}")
            if quality.required_output_ratio is not None:
                print(f"  {tr.t('cli.required_ratio')}: {quality.required_output_ratio:.3f}")
        for copied_path in result.copied_external_subtitle_paths:
            print(f"  {tr.t('cli.external_subtitle_copied')}: {copied_path}")
        for warning in result.external_subtitle_warnings:
            print(f"  {tr.t('cli.external_subtitle_warning')}: {warning}")
        if result.log_path:
            print(f"  {tr.t('cli.log_path')}: {result.log_path}")
