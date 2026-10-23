from __future__ import annotations

from core.i18n import Text, Translator
from core.media import human_kbps
from core.models import CompressionMode, EncodePlan, EncodeResult, SmartAlgorithm
from core.smart import build_decision_options


OUTPUT = Text(
    en='Output',
    zh_cn='输出',
    ja='出力',
)

REASON = Text(
    en='Reason',
    zh_cn='原因',
    ja='理由',
)


def print_plan(plan: EncodePlan, tr: Translator) -> None:
    print(tr.t(Text(en='Plan items: {count}', zh_cn='计划项数量: {count}', ja='プラン項目数: {count}'), count=len(plan.items)))
    for item in plan.items:
        if item.skip_reason:
            print(f"[{tr.t(Text(en='SKIP', zh_cn='跳过', ja='スキップ'))}] {item.source_path}")
            print(f"  {tr.t(REASON)}: {item.skip_reason}")
            print(f"  {tr.t(OUTPUT)}: {item.output_path}")
            continue

        media = item.media_info
        encoder = item.encoder_info
        if media is None or encoder is None:
            raise ValueError(tr.t(Text(
                en='Planned item is missing media or encoder information: {path}',
                zh_cn='计划项缺少媒体或编码器信息: {path}',
                ja='計画項目にメディア情報またはエンコーダー情報がありません: {path}',
            ), path=item.source_path))
        fps = f"{media.fps:.3f}" if media and media.fps else "n/a"
        wh = f"{media.width}x{media.height}" if media and media.width and media.height else "n/a"
        print(f"[{tr.t(Text(en='READY', zh_cn='可执行', ja='準備完了'))}] {item.source_path}")
        print(f"  {tr.t(Text(en='Resolution', zh_cn='分辨率', ja='解像度'))}: {wh}")
        print(f"  {tr.t(Text(en='FPS', zh_cn='帧率', ja='FPS'))}: {fps}")
        print(f"  {tr.t(Text(en='Source video bitrate', zh_cn='源视频码率', ja='元動画ビットレート'))}: {human_kbps(media.video_bitrate_bps)}")
        target = (
            tr.t(Text(en='pending smart analysis', zh_cn='等待智能分析', ja='Smart分析待ち'))
            if item.options.compression_mode == CompressionMode.SMART and item.target_video_bitrate_bps <= 0
            else human_kbps(item.target_video_bitrate_bps)
        )
        print(f"  {tr.t(Text(en='Target video bitrate', zh_cn='目标视频码率', ja='目標動画ビットレート'))}: {target}")
        print(f"  {tr.t(Text(en='Encoder', zh_cn='编码器', ja='エンコーダー'))}: {encoder.encoder_name} ({encoder.backend.value})")
        if item.options.compression_mode == CompressionMode.SMART and item.options.smart_algorithm == SmartAlgorithm.V2_EXPERIMENTAL:
            print('  ' + tr.t(Text(
                en='Smart v2 (experimental): whole-video mean VMAF target {value:.1f}',
                zh_cn='Smart v2（实验性）：整片平均 VMAF 目标 {value:.1f}',
                ja='Smart v2 (実験的): 動画全体の平均VMAF目標 {value:.1f}',
            ), value=item.options.min_vmaf))
        print(f"  {tr.t(OUTPUT)}: {item.output_path}")
        for warning in item.warnings:
            print(f"  {tr.t(Text(en='Note', zh_cn='说明', ja='注記'))}: {warning}")
    print(f"{tr.t(Text(en='FFmpeg', zh_cn='FFmpeg', ja='FFmpeg'))}: {plan.ffmpeg_path}")
    print(f"{tr.t(Text(en='FFprobe', zh_cn='FFprobe', ja='FFprobe'))}: {plan.ffprobe_path}")
    print(f"{tr.t(Text(en='Output root', zh_cn='输出根目录', ja='出力ルートフォルダー'))}: {plan.output_root}")


def print_encode_results(results: list[EncodeResult], tr: Translator) -> None:
    for result in results:
        if result.needs_decision:
            print(f"[{tr.t(Text(en='NEEDS DECISION', zh_cn='等待决定', ja='決定が必要'))}] {result.source_path}")
            print(f"  {tr.t(REASON)}: {result.error_message}")
            if result.rejected_output_path is not None:
                print(f"  {tr.t(Text(en='Preserved encoded file', zh_cn='已保留的编码文件', ja='保持されたエンコードファイル'))}: {result.rejected_output_path}")
            elif result.quality_search_result is not None or result.segmented_analysis_result is not None:
                decision_result = result.quality_search_result or result.segmented_analysis_result
                assert decision_result is not None
                for option in build_decision_options(decision_result):
                    suffix = f"={option.suggested_value}" if option.suggested_value is not None else ""
                    print(f"  {tr.t(Text(en='Available decision', zh_cn='可选操作', ja='選択可能な操作'))}: {option.action_code.value}{suffix}")
        elif result.skipped:
            print(f"[{tr.t(Text(en='SKIPPED', zh_cn='已跳过', ja='スキップ済み'))}] {result.source_path}")
            print(f"  {tr.t(REASON)}: {result.error_message}")
        elif result.success:
            print(f"[{tr.t(Text(en='OK', zh_cn='成功', ja='成功'))}] {result.source_path} -> {result.output_path}")
        else:
            print(f"[{tr.t(Text(en='FAILED', zh_cn='失败', ja='失敗'))}] {result.source_path}")
            print(f"  {tr.t(REASON)}: {result.error_message}")
        quality = result.quality_search_result
        segmented = result.segmented_analysis_result
        if segmented is not None:
            mean = segmented.final_mean_vmaf if segmented.final_mean_vmaf is not None else segmented.predicted_mean_vmaf
            text = Text(
                en='Smart v2 (experimental): {count} shots; measured mean VMAF={mean}',
                zh_cn='Smart v2（实验性）：{count} 个镜头；实测平均 VMAF={mean}',
                ja='Smart v2 (実験的): {count} ショット; 測定平均VMAF={mean}',
            ) if segmented.final_mean_vmaf is not None else Text(
                en='Smart v2 (experimental): {count} shots; predicted mean VMAF={mean}',
                zh_cn='Smart v2（实验性）：{count} 个镜头；预测平均 VMAF={mean}',
                ja='Smart v2 (実験的): {count} ショット; 予測平均VMAF={mean}',
            )
            print('  ' + tr.t(text, count=len(segmented.shots), mean=mean))
        if quality is not None:
            if quality.min_vmaf is not None:
                print(f"  {tr.t(Text(en='Minimum SMART quality score', zh_cn='最低 SMART 画质分数', ja='最小SMART品質スコア'))}: {quality.min_vmaf:.2f}")
            if quality.predicted_output_ratio is not None:
                print(f"  {tr.t(Text(en='Predicted output ratio', zh_cn='预测输出比例', ja='予測出力比率'))}: {quality.predicted_output_ratio:.3f}")
            if quality.required_output_ratio is not None:
                print(f"  {tr.t(Text(en='Estimated required ratio', zh_cn='估算所需比例', ja='推定必要比率'))}: {quality.required_output_ratio:.3f}")
        for copied_path in result.copied_external_subtitle_paths:
            print(f"  {tr.t(Text(en='External subtitle copied', zh_cn='已复制外挂字幕', ja='外部字幕をコピーしました'))}: {copied_path}")
        for warning in result.external_subtitle_warnings:
            print(f"  {tr.t(Text(en='External subtitle warning', zh_cn='外挂字幕警告', ja='外部字幕の警告'))}: {warning}")
        if result.log_path:
            print(f"  {tr.t(Text(en='Log path', zh_cn='日志路径', ja='ログパス'))}: {result.log_path}")
