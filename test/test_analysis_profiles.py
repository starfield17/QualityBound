from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from cli.cli_entry import _build_parser, _options_from_args
from core.smart.v1.profiles import (
    FACTORY_ANALYSIS_PROFILES,
    all_analysis_profile_payloads,
    analysis_profiles_from_config,
    bind_analysis_profile,
    parse_analysis_profile_name,
    resolve_analysis_settings,
    validate_analysis_settings,
)
from core.i18n import get_translator
from core.models import (
    AnalysisProfileName,
    AnalysisProfileSettings,
    EncodeOptions,
    QualityUnreachablePolicy,
    SizeBlockedPolicy,
    SkippedOutputPolicy,
)
from core.config.store import _default_app_config
from gui.gui_mainwindow import MainWindow
from gui.settings_dialog import SettingsDialog


class AnalysisProfileCoreTestCase(unittest.TestCase):
    def test_factory_profiles_match_confidence_mode_defaults(self) -> None:
        expected = {
            AnalysisProfileName.FAST: dict(
                whole_video_max_sec=12.0,
                scout_duration_sec=1.5,
                scout_multiplier=3,
                scout_max_windows=12,
                sample_duration_sec=4.0,
                sample_count_under_10m=3,
                sample_count_10_to_60m=3,
                sample_count_60_to_180m=4,
                sample_count_over_180m=4,
                holdout_window_count=1,
                holdout_window_count_over_180m=1,
                coarse_max_candidates=3,
                exact_max_candidates=2,
                coarse_vmaf_subsample=5,
                exact_vmaf_subsample=1,
                min_search_tolerance_bps=100_000,
                search_tolerance_ratio=0.06,
                max_refinement_rounds=1,
                preferred_vmaf_margin=0.2,
            ),
            AnalysisProfileName.BALANCE: dict(
                whole_video_max_sec=20.0,
                scout_duration_sec=2.0,
                scout_multiplier=4,
                scout_max_windows=32,
                sample_duration_sec=5.0,
                sample_count_under_10m=4,
                sample_count_10_to_60m=5,
                sample_count_60_to_180m=6,
                sample_count_over_180m=6,
                holdout_window_count=2,
                holdout_window_count_over_180m=2,
                coarse_max_candidates=4,
                exact_max_candidates=3,
                coarse_vmaf_subsample=3,
                exact_vmaf_subsample=1,
                min_search_tolerance_bps=50_000,
                search_tolerance_ratio=0.03,
                max_refinement_rounds=2,
                preferred_vmaf_margin=0.4,
            ),
            AnalysisProfileName.PRECISE: dict(
                whole_video_max_sec=30.0,
                scout_duration_sec=2.5,
                scout_multiplier=6,
                scout_max_windows=64,
                sample_duration_sec=6.0,
                sample_count_under_10m=6,
                sample_count_10_to_60m=7,
                sample_count_60_to_180m=8,
                sample_count_over_180m=10,
                holdout_window_count=3,
                holdout_window_count_over_180m=4,
                coarse_max_candidates=5,
                exact_max_candidates=4,
                coarse_vmaf_subsample=3,
                exact_vmaf_subsample=1,
                min_search_tolerance_bps=25_000,
                search_tolerance_ratio=0.015,
                max_refinement_rounds=2,
                preferred_vmaf_margin=0.5,
            ),
        }
        for name, values in expected.items():
            settings = FACTORY_ANALYSIS_PROFILES[name]
            for field, value in values.items():
                self.assertEqual(getattr(settings, field), value, f"{name.value}.{field}")

    def test_unknown_name_falls_back_to_balance(self) -> None:
        self.assertEqual(parse_analysis_profile_name(AnalysisProfileName.FAST), AnalysisProfileName.FAST)
        self.assertEqual(parse_analysis_profile_name("turbo"), AnalysisProfileName.BALANCE)
        name, settings = analysis_profiles_from_config({})
        self.assertEqual(name, AnalysisProfileName.BALANCE)
        self.assertEqual(settings, FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.BALANCE])

    def test_stored_overrides_are_ignored_for_factory_modes(self) -> None:
        settings = resolve_analysis_settings(
            AnalysisProfileName.FAST,
            {"fast": {"sample_duration_sec": 4.5, "coarse_max_candidates": 5}},
        )
        self.assertEqual(settings, FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.FAST])

    def test_even_subsample_is_coerced_to_odd(self) -> None:
        settings = validate_analysis_settings(
            AnalysisProfileSettings(coarse_vmaf_subsample=4, exact_vmaf_subsample=2)
        )
        self.assertEqual(settings.coarse_vmaf_subsample, 3)
        self.assertEqual(settings.exact_vmaf_subsample, 1)

    def test_exact_search_always_allows_budget_and_ceiling_evidence(self) -> None:
        settings = validate_analysis_settings(AnalysisProfileSettings(exact_max_candidates=1))
        self.assertEqual(settings.exact_max_candidates, 2)
        self.assertEqual(
            FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.FAST].exact_max_candidates,
            2,
        )

    def test_bind_analysis_profile_snapshots_settings(self) -> None:
        options = bind_analysis_profile(
            EncodeOptions(),
            name="precise",
            stored_profiles={"precise": {"exact_max_candidates": 7}},
        )
        self.assertEqual(options.analysis_profile, AnalysisProfileName.PRECISE)
        self.assertEqual(options.analysis_settings, FACTORY_ANALYSIS_PROFILES[AnalysisProfileName.PRECISE])

    def test_default_app_config_includes_analysis_profile(self) -> None:
        data = _default_app_config()
        self.assertEqual(data["analysis_profile"], AnalysisProfileName.BALANCE.value)
        self.assertEqual(data["analysis_profiles"], {})
        self.assertEqual(all_analysis_profile_payloads()["balance"]["sample_duration_sec"], 5.0)


class AnalysisProfileCliTestCase(unittest.TestCase):
    def test_analysis_profile_flag_binds_factory_fast_settings(self) -> None:
        args = _build_parser().parse_args(["plan", "input.mp4", "--analysis-profile", "fast"])
        with (
            patch("cli.cli_entry.load_app_config", return_value={}),
            patch("cli.cli_entry._load_base_options", return_value=EncodeOptions()),
        ):
            options = _options_from_args(args, Path("."))
        self.assertEqual(options.analysis_profile, AnalysisProfileName.FAST)
        self.assertEqual(options.analysis_settings.sample_duration_sec, 4.0)
        self.assertEqual(options.analysis_settings.coarse_max_candidates, 3)


class AnalysisProfileGuiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.repo_root = Path(__file__).resolve().parent.parent

    def test_main_window_uses_scan_combo_instead_of_policy_dropdowns(self) -> None:
        window = MainWindow(self.repo_root, language="en")
        try:
            self.assertFalse(hasattr(window, "size_blocked_policy_combo"))
            self.assertFalse(hasattr(window, "quality_unreachable_policy_combo"))
            self.assertFalse(hasattr(window, "skipped_output_policy_combo"))
            self.assertGreaterEqual(window.options_panel.analysis_profile_combo.count(), 3)
            balance_index = window.options_panel.analysis_profile_combo.findData(AnalysisProfileName.BALANCE.value)
            self.assertEqual(window.options_panel.analysis_profile_combo.itemText(balance_index), "Balance")

            window.app_config["size_blocked_policy"] = SizeBlockedPolicy.ASK.value
            window.app_config["analysis_profiles"] = {"fast": {"sample_duration_sec": 99.0}}
            fast_index = window.options_panel.analysis_profile_combo.findData(AnalysisProfileName.FAST.value)
            with patch.object(window, "_save_app_config_preserving_capabilities") as save_config:
                window.options_panel.analysis_profile_combo.setCurrentIndex(balance_index)
                save_config.reset_mock()
                window.options_panel.analysis_profile_combo.setCurrentIndex(fast_index)
                save_config.assert_called_once()
            self.assertEqual(window.app_config["analysis_profile"], AnalysisProfileName.FAST.value)
            options = window.options_panel.read_options()
            self.assertEqual(options.size_blocked_policy, SizeBlockedPolicy.ASK)
            self.assertEqual(options.analysis_profile, AnalysisProfileName.FAST)
            self.assertEqual(options.analysis_settings.sample_duration_sec, 4.0)
        finally:
            window.close()

    def test_initial_state_restores_profile_before_persistence_is_enabled(self) -> None:
        config = _default_app_config()
        config["analysis_profile"] = AnalysisProfileName.PRECISE.value
        with (
            patch("gui.gui_mainwindow.load_app_config", return_value=config),
            patch("gui.gui_mainwindow.update_app_config") as update_config,
        ):
            window = MainWindow(self.repo_root, language="en")
        try:
            self.assertEqual(
                window.options_panel.analysis_profile_combo.currentData(),
                AnalysisProfileName.PRECISE.value,
            )
            self.assertEqual(
                window.app_config["analysis_profile"],
                AnalysisProfileName.PRECISE.value,
            )
            untouched_options = window.options_panel.read_options()
            self.assertEqual(untouched_options.size_blocked_policy, SizeBlockedPolicy.RELAX_SIZE)
            self.assertEqual(
                untouched_options.quality_unreachable_policy,
                QualityUnreachablePolicy.SKIP,
            )
            self.assertEqual(
                untouched_options.skipped_output_policy,
                SkippedOutputPolicy.COPY,
            )
            update_config.assert_not_called()
        finally:
            window.close()

    def test_settings_dialog_uses_profile_combo_and_discards_custom_values(self) -> None:
        tr = get_translator("en", self.repo_root / "config")
        dialog = SettingsDialog(
            tr,
            {
                "language": "en",
                "analysis_profile": "precise",
                "analysis_profiles": {"precise": {"sample_duration_sec": 9.0}},
            },
        )
        try:
            self.assertEqual(dialog.analysis_profile_combo.currentData(), "precise")
            self.assertFalse(hasattr(dialog, "_profile_pages"))
            values = dialog.values()
            self.assertEqual(values["analysis_profile"], "precise")
            self.assertEqual(values["analysis_profiles"], {})
        finally:
            dialog.close()

    def test_settings_dialog_distinguishes_analysis_policies_from_terminal_validation(self) -> None:
        tr = get_translator("en", self.repo_root / "config")
        dialog = SettingsDialog(tr, {"language": "en"})
        try:
            self.assertEqual(
                dialog.skipped_output_policy_label.text(),
                "When Smart analysis skips a file",
            )
            self.assertIn("planning", dialog.skipped_output_policy_combo.toolTip())
            self.assertIn("before the full encode", dialog.size_blocked_policy_combo.toolTip())
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
