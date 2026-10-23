from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.cli_entry import run_cli
from cli.cli_interactive import print_encode_results, print_plan
from core.config import encode_options_to_preset_data
from core.i18n import Translator
from core.media.skipped import SkippedPublishResult
from core.models import (
    BackendChoice, CodecChoice, EncodeOptions, EncodePlan, EncodePlanItem,
    EncodeResult, EncoderInfo, MediaInfo, QualitySearchStatus,
    SegmentedAnalysisResult, SmartAlgorithm,
)


def _plan() -> EncodePlan:
    source = Path('日本語.mp4')
    item = EncodePlanItem(
        source, Path('output.mp4'),
        MediaInfo(source, 12.0, 1_128_000, 1_000_000, 128_000, 640, 360, 30.0, 'h264', 'aac'),
        EncoderInfo(CodecChoice.HEVC, BackendChoice.CPU, 'libx265', True, 'slow'),
        EncodeOptions(smart_algorithm=SmartAlgorithm.V2_EXPERIMENTAL),
    )
    return EncodePlan([item], Path('ffmpeg'), Path('ffprobe'), Path('.'), Path('out'))


class CliTranslationTestCase(unittest.TestCase):
    def _run(self, argv: list[str], saved_language: str = 'en') -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch('cli.cli_entry.ensure_runtime_layout'),
            patch('cli.cli_entry.load_app_config', return_value={'language': saved_language}),
            patch('cli.cli_entry._config_dir', return_value=Path('config')),
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
        ):
            try:
                status = run_cli(argv)
            except SystemExit as exc:
                self.assertIsInstance(exc.code, int)
                status = int(exc.code or 0)
        return status, output.getvalue(), errors.getvalue()

    def test_help_uses_explicit_language_before_or_after_subcommands(self) -> None:
        for argv in (
            ['--lang', 'ja', '--help'],
            ['plan', '--lang=ja', '--help'],
            ['--lang', 'ja', 'encode', '--help'],
            ['preset', '--lang', 'ja', 'save', '--help'],
        ):
            with self.subTest(argv=argv):
                status, output, errors = self._run(argv, saved_language='zh_cn')
                self.assertEqual(status, 0)
                self.assertIn('使い方:', output)
                self.assertIn('ヘルプ', output)
                self.assertNotIn('show this help message', output)
                self.assertEqual(errors, '')
        status, output, _ = self._run(['encode', '--lang', 'ja', '--help'])
        self.assertEqual(status, 0)
        self.assertIn('最大同時エンコード数', output)
        self.assertIn('--no-copy-subtitles', output)

    def test_saved_language_and_unknown_locale_help_fallback(self) -> None:
        for locale, expected in (('ja', 'エンコード計画を確認'), ('zh_cn', '检查编码计划'), ('removed', 'Inspect the encode plan')):
            with self.subTest(locale=locale):
                status, output, errors = self._run(['--help'], saved_language=locale)
                self.assertEqual(status, 0)
                self.assertIn(expected, output)
                self.assertIn('QualityBound CLI', output)
                self.assertEqual(errors, '')

    def test_explicit_language_overrides_saved_language_and_preserves_json(self) -> None:
        options = EncodeOptions()
        with patch('cli.cli_entry.load_preset', return_value=options):
            status, output, errors = self._run(['--lang', 'ja', 'preset', 'load', 'default'], saved_language='zh_cn')
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output), encode_options_to_preset_data(options))
        self.assertEqual(errors, '')

    def test_japanese_validation_keeps_option_names_and_exit_code(self) -> None:
        cases = (
            (['--min-vmaf', '0'], '--min-vmaf は0より大きく100以下'),
            (['--max-output-ratio', '2'], '--max-output-ratio は0より大きく1以下'),
            (['--ratio', '0.5'], '--ratio は --compression-mode smart と併用'),
            (['--compression-mode', 'fixed_bitrate', '--min-vmaf', '90'], '--compression-mode smart が必要'),
            (['--backend', 'auto', '--encoder-preset', 'slow'], '--encoder-preset は --backend auto と併用'),
            (['--compression-mode', 'fixed_bitrate', '--smart-algorithm', 'v2_experimental'], '--compression-mode smart が必要'),
        )
        for flags, expected in cases:
            with self.subTest(flags=flags):
                status, _, errors = self._run(['plan', 'input.mp4', '--lang', 'ja', *flags])
                self.assertEqual(status, 2)
                self.assertIn('エラー:', errors)
                self.assertIn(expected, errors)

    def test_warning_and_plan_follow_explicit_language(self) -> None:
        with (
            patch('cli.cli_entry._load_base_options', return_value=EncodeOptions(encoder_preset='slow')),
            patch('cli.cli_entry.build_encode_plan', return_value=_plan()),
        ):
            status, output, errors = self._run(['plan', 'input.mp4', '--lang', 'ja'], saved_language='zh_cn')
        self.assertEqual(status, 0)
        self.assertIn('警告:', errors)
        self.assertIn('継承されたエンコーダープリセットを無視', errors)
        self.assertIn('プラン項目数:', output)

    def test_encoder_preset_error_is_localized_and_preserves_encoder_choices(self) -> None:
        with (
            patch('cli.cli_entry._resolve_encoder_info', return_value=(Path('ffmpeg'), _plan().items[0].encoder_info)),
            patch('cli.cli_entry.preset_choices_for_encoder', return_value=['fast', 'slow']),
        ):
            status, _, errors = self._run(['plan', 'input.mp4', '--lang', 'ja', '--backend', 'cpu', '--encoder-preset', 'bad'])
        self.assertEqual(status, 2)
        self.assertIn('無効なエンコーダープリセット', errors)
        self.assertIn('libx265', errors)
        self.assertIn('fast, slow', errors)

    def test_external_diagnostic_remains_unchanged(self) -> None:
        diagnostic = "[ffmpeg] Unrecognized option 'sentinel'"
        with patch('cli.cli_entry.build_encode_plan', side_effect=ValueError(diagnostic)):
            status, _, errors = self._run(['plan', 'input.mp4', '--lang', 'ja'])
        self.assertEqual(status, 2)
        self.assertEqual(errors, f'エラー: {diagnostic}\n')

    def test_smart_v2_summary_distinguishes_measured_and_predicted_in_japanese(self) -> None:
        plan = _plan()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            print_plan(plan, Translator('ja'))
        self.assertIn('動画全体の平均VMAF目標 90.0', output.getvalue())
        for measured, expected in ((None, '予測平均VMAF'), (93.0, '測定平均VMAF')):
            analysis = SegmentedAnalysisResult(
                QualitySearchStatus.FOUND, 'libx265', BackendChoice.CPU,
                predicted_mean_vmaf=91.0, final_mean_vmaf=measured,
            )
            result = EncodeResult(plan.items[0].source_path, plan.items[0].output_path, True, segmented_analysis_result=analysis)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                print_encode_results([result], Translator('ja'))
            self.assertIn('Smart v2 (実験的): 0 ショット', output.getvalue())
            self.assertIn(expected, output.getvalue())
            self.assertNotIn('experimental', output.getvalue())

    def test_skipped_output_messages_preserve_success_and_decision_exit_codes(self) -> None:
        plan = _plan()
        source, output = plan.items[0].source_path, plan.items[0].output_path
        for copied, reason, expected in ((True, None, 'Smartでスキップされたソースをコピー'), (False, 'raw diagnostic', 'Smartでスキップされたソースをコピーしませんでした')):
            result = EncodeResult(source, output, False, skipped=True)
            with (
                patch('cli.cli_entry.build_encode_plan', return_value=plan),
                patch('cli.cli_entry.execute_plan', return_value=[result]),
                patch('cli.cli_entry.publish_skipped_sources', return_value=[SkippedPublishResult(source, output, copied, reason)]),
            ):
                status, text, _ = self._run(['encode', 'input.mp4', '--lang', 'ja'])
            self.assertEqual(status, 0)
            self.assertIn(expected, text)
            if reason:
                self.assertIn(reason, text)
        result = EncodeResult(source, output, False, needs_decision=True)
        with (
            patch('cli.cli_entry.build_encode_plan', return_value=plan),
            patch('cli.cli_entry.execute_plan', return_value=[result]),
        ):
            status, text, _ = self._run(['encode', 'input.mp4', '--lang', 'ja', '--skipped-output-policy', 'ask'])
        self.assertEqual(status, 3)
        self.assertIn('スキップされたソースを元の場所に保持', text)


if __name__ == '__main__':
    unittest.main()
