"""The strict-debt ratchet's configuration contract.

The measurement itself runs in the Quality job, where pyright is installed once.
These tests keep the wiring honest: the probe config must still raise exactly the
rules the project disables, and the comparison must fail on growth while allowing
shrinkage.
"""

from __future__ import annotations

import json
import unittest

from scripts.check_strict_debt import (
    BASELINE,
    PROBE_CONFIG,
    ROOT,
    RULES,
    measure,
    probe_rules,
    pyright_settings,
    regressions,
)


class StrictDebtConfigTestCase(unittest.TestCase):
    def test_probe_raises_exactly_the_disabled_rules_to_warnings(self) -> None:
        settings = pyright_settings()
        disabled = {rule for rule in RULES if settings.get(rule) == "none"}
        self.assertEqual(disabled, set(RULES), "pyproject.toml must still disable exactly these six rules")
        overrides = probe_rules()
        self.assertEqual(
            {rule for rule in RULES if overrides.get(rule) == "warning"},
            set(RULES),
        )

    def test_probe_extends_the_project_config(self) -> None:
        with PROBE_CONFIG.open("rb") as handle:
            config = json.load(handle)
        self.assertEqual(config["extends"], "./pyproject.toml")

    def test_probe_config_is_not_the_default_pyright_config(self) -> None:
        # `pyright` with no arguments must keep reading pyproject.toml, so this
        # file has to stay outside pyright's automatic config discovery.
        self.assertNotEqual(PROBE_CONFIG.name, "pyrightconfig.json")


class StrictDebtReportTestCase(unittest.TestCase):
    def test_report_is_grouped_by_top_level_package(self) -> None:
        report = {
            "generalDiagnostics": [
                {"file": str(ROOT / "core" / "models.py"), "severity": "warning", "rule": "reportUnknownMemberType"},
                {"file": str(ROOT / "core" / "media" / "paths.py"), "severity": "warning", "rule": "reportUnknownArgumentType"},
                {"file": str(ROOT / "gui" / "queue_model.py"), "severity": "warning", "rule": "reportUnknownVariableType"},
                {"file": str(ROOT / "core" / "models.py"), "severity": "error", "rule": "reportIndexIssue"},
            ]
        }
        measured = measure(report)
        self.assertEqual(measured["core"], 2)
        self.assertEqual(measured["gui"], 1)
        self.assertEqual(measured["cli"], 0)
        self.assertEqual(measured["total"], 3)

    def test_growth_is_a_regression_and_shrinkage_is_not(self) -> None:
        grown = dict(BASELINE)
        grown["gui"] = BASELINE["gui"] + 1
        self.assertTrue(regressions(grown))

        shrunk = {key: 0 for key in BASELINE}
        self.assertEqual(regressions(shrunk), [])

    def test_baseline_total_matches_its_packages(self) -> None:
        packages = {key: value for key, value in BASELINE.items() if key != "total"}
        self.assertEqual(BASELINE["total"], sum(packages.values()))


if __name__ == "__main__":
    unittest.main()
