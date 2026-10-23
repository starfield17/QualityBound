from __future__ import annotations

from core.i18n import Text, Translator
from core.media import human_kbps
from core.models import CompressionMode, EncodePlan, EncodeResult, SmartAlgorithm
from core.smart import build_decision_options


OUTPUT = Text(
    en='Output',
    zh_cn='输出',
)

REASON = Text(
    en='Reason',
    zh_cn='原因',
)


def print_plan(plan: EncodePlan, tr: Translator) -> None:
    print(tr.t(Text(en='Plan items: {count}', zh_cn='计划项数量: {count}'), count=len(plan.items)))
    for item in plan.items:
        if item.skip_reason:
            print(f"[{tr.t(Text(en='SKIP', zh_cn='跳过'))}] {item.source_path}")
            print(f"  {tr.t(REASON)}: {item.skip_reason}")
            print(f"  {tr.t(OUTPUT)}: {item.output_path}")
            continue

        media = item.media_info
        encoder = item.encoder_info
        if media is None or encoder is None:
            raise ValueError(f"Planned item is missing media or encoder information: {item.source_path}")
        fps = f"{media.fps:.3f}" if media and media.fps else "n/a"
        wh = f"{media.width}x{media.height}" if media and media.width and media.height else "n/a"
        print(f"[{tr.t(Text(en='READY', zh_cn='可执行'))}] {item.source_path}")
        print(f"  {tr.t(Text(en='Resolution', zh_cn='分辨率'))}: {wh}")
        print(f"  {tr.t(Text(en='FPS', zh_cn='帧率'))}: {fps}")
        print(f"  {tr.t(Text(en='Source video bitrate', zh_cn='源视频码率'))}: {human_kbps(media.video_bitrate_bps)}")
        target = (
            tr.t(Text(en='pending smart analysis', zh_cn='等待智能分析'))
            if item.options.compression_mode == CompressionMode.SMART and item.target_video_bitrate_bps <= 0
            else human_kbps(item.target_video_bitrate_bps)
        )
        print(f"  {tr.t(Text(en='Target video bitrate', zh_cn='目标视频码率'))}: {target}")
        print(f"  {tr.t(Text(en='Encoder', zh_cn='编码器'))}: {encoder.encoder_name} ({encoder.backend.value})")
        if item.options.compression_mode == CompressionMode.SMART and item.options.smart_algorithm == SmartAlgorithm.V2_EXPERIMENTAL:
            print(f"  Smart v2 (experimental): whole-video mean VMAF target {item.options.min_vmaf:.1f}")
        print(f"  {tr.t(OUTPUT)}: {item.output_path}")
        for warning in item.warnings:
            print(f"  {tr.t(Text(en='Note', zh_cn='说明'))}: {warning}")
    print(f"{tr.t(Text(en='FFmpeg', zh_cn='FFmpeg'))}: {plan.ffmpeg_path}")
    print(f"{tr.t(Text(en='FFprobe', zh_cn='FFprobe'))}: {plan.ffprobe_path}")
    print(f"{tr.t(Text(en='Output root', zh_cn='输出根目录'))}: {plan.output_root}")


def print_encode_results(results: list[EncodeResult], tr: Translator) -> None:
    for result in results:
        if result.needs_decision:
            print(f"[{tr.t(Text(en='NEEDS DECISION', zh_cn='等待决定'))}] {result.source_path}")
            print(f"  {tr.t(REASON)}: {result.error_message}")
            if result.rejected_output_path is not None:
                print(f"  {tr.t(Text(en='Preserved encoded file', zh_cn='已保留的编码文件'))}: {result.rejected_output_path}")
            elif result.quality_search_result is not None or result.segmented_analysis_result is not None:
                decision_result = result.quality_search_result or result.segmented_analysis_result
                assert decision_result is not None
                for option in build_decision_options(decision_result):
                    suffix = f"={option.suggested_value}" if option.suggested_value is not None else ""
                    print(f"  {tr.t(Text(en='Available decision', zh_cn='可选操作'))}: {option.action_code.value}{suffix}")
        elif result.skipped:
            print(f"[{tr.t(Text(en='SKIPPED', zh_cn='已跳过'))}] {result.source_path}")
            print(f"  {tr.t(REASON)}: {result.error_message}")
        elif result.success:
            print(f"[{tr.t(Text(en='OK', zh_cn='成功'))}] {result.source_path} -> {result.output_path}")
        else:
            print(f"[{tr.t(Text(en='FAILED', zh_cn='失败'))}] {result.source_path}")
            print(f"  {tr.t(REASON)}: {result.error_message}")
        quality = result.quality_search_result
        segmented = result.segmented_analysis_result
        if segmented is not None:
            mean = segmented.final_mean_vmaf if segmented.final_mean_vmaf is not None else segmented.predicted_mean_vmaf
            print(f"  Smart v2 (experimental): {len(segmented.shots)} shots; "
                  f"{'measured' if segmented.final_mean_vmaf is not None else 'predicted'} mean VMAF={mean}")
        if quality is not None:
            if quality.min_vmaf is not None:
                print(f"  {tr.t(Text(en='Minimum SMART quality score', zh_cn='最低 SMART 画质分数'))}: {quality.min_vmaf:.2f}")
            if quality.predicted_output_ratio is not None:
                print(f"  {tr.t(Text(en='Predicted output ratio', zh_cn='预测输出比例'))}: {quality.predicted_output_ratio:.3f}")
            if quality.required_output_ratio is not None:
                print(f"  {tr.t(Text(en='Estimated required ratio', zh_cn='估算所需比例'))}: {quality.required_output_ratio:.3f}")
        for copied_path in result.copied_external_subtitle_paths:
            print(f"  {tr.t(Text(en='External subtitle copied', zh_cn='已复制外挂字幕'))}: {copied_path}")
        for warning in result.external_subtitle_warnings:
            print(f"  {tr.t(Text(en='External subtitle warning', zh_cn='外挂字幕警告'))}: {warning}")
        if result.log_path:
            print(f"  {tr.t(Text(en='Log path', zh_cn='日志路径'))}: {result.log_path}")
