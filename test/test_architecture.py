from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path


# N2 ← S1 — the layer, package and public-contract boundaries this project commits to.
ROOT = Path(__file__).resolve().parent.parent
APP_PACKAGES = ("core", "cli", "gui")
QT_ROOTS = {"PySide2", "PySide6", "PyQt5", "PyQt6", "qtpy", "Qt"}
CORE_ROOT_MODULES = {"core", "core.i18n", "core.models", "core.progress_events"}
CORE_PACKAGES = ("config", "media", "ffmpeg", "smart", "encoding")
ALLOWED_CORE_PACKAGE_DEPENDENCIES = {
    "config": set(),
    "media": set(),
    "ffmpeg": {"config", "media"},
    "smart": {"ffmpeg", "media"},
    "encoding": {"config", "ffmpeg", "media", "smart"},
}


def _module_name(path: Path) -> str:
    relative = path.relative_to(ROOT)
    if relative == Path("main.py"):
        return "main"
    if relative.name == "__init__.py":
        return ".".join(relative.parent.parts)
    return ".".join(relative.with_suffix("").parts)


def _app_modules() -> dict[str, Path]:
    paths = [ROOT / "main.py"]
    for package in APP_PACKAGES:
        paths.extend(sorted((ROOT / package).rglob("*.py")))
    return {_module_name(path): path for path in paths if path.is_file()}


def _resolve_relative(source: str, level: int, imported: str | None, *, is_package: bool = False) -> str:
    package_parts = source.split(".") if is_package else source.split(".")[:-1]
    if level > len(package_parts) + 1:
        return imported or ""
    base = package_parts[: len(package_parts) - level + 1]
    return ".".join((*base, *(imported or "").split("."))).rstrip(".")


def _imports(source: str, path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        raise AssertionError(f"Cannot inspect {path}: fix its syntax before changing dependencies: {exc}") from exc

    imported_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _resolve_relative(source, node.level, node.module, is_package=path.name == "__init__.py")
            elif node.module:
                base = node.module
            else:
                continue
            imported_names.append(base)
            # ``from core import models`` depends on the concrete module too;
            # include it when the imported name resolves to an app module.
            imported_names.extend(
                f"{base}.{alias.name}" for alias in node.names if base
            )
    return imported_names


def _top_level(name: str) -> str:
    return name.split(".", 1)[0]


def _is_qt_import(name: str) -> bool:
    top = _top_level(name)
    lower = top.lower()
    return top in QT_ROOTS or lower.startswith(("pyside", "pyqt")) or lower == "qt"


def _layer_violations(source: str, path: Path) -> list[str]:
    layer = _top_level(source)
    allowed = set(sys.stdlib_module_names) | {layer, "core"}
    if layer == "gui" and source not in {"gui.queue_state", "gui.queue_actions"}:
        allowed.add("PySide6")
    return [name for name in _imports(source, path) if _top_level(name) not in allowed]


def _dependency_graph() -> dict[str, set[str]]:
    modules = _app_modules()
    graph: dict[str, set[str]] = {name: set() for name in modules}
    for source, path in modules.items():
        for imported in _imports(source, path):
            if imported in modules:
                graph[source].add(imported)
                continue
            # An import of a package itself is represented by its package node;
            # submodule imports are represented by the concrete module when it exists.
            prefix = imported
            while prefix and prefix not in modules:
                prefix = prefix.rpartition(".")[0]
            if prefix:
                graph[source].add(prefix)
    return graph


def _cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    cycles: list[list[str]] = []
    visiting: list[str] = []
    active: set[str] = set()
    completed: set[str] = set()

    def visit(node: str) -> None:
        if node in active:
            start = visiting.index(node)
            cycles.append([*visiting[start:], node])
            return
        if node in completed:
            return
        active.add(node)
        visiting.append(node)
        for dependency in sorted(graph[node]):
            visit(dependency)
        visiting.pop()
        active.remove(node)
        completed.add(node)

    for node in sorted(graph):
        visit(node)
    return cycles


class ArchitectureTestCase(unittest.TestCase):
    def test_core_root_contains_only_public_contracts_and_capability_packages(self) -> None:
        root_entries = {
            path.name
            for path in (ROOT / "core").iterdir()
            if path.name != "AGENTS.md" and path.name != "__pycache__"
        }
        expected = {
            "__init__.py",
            "i18n.py",
            "models.py",
            "progress_events.py",
            *CORE_PACKAGES,
        }
        self.assertEqual(
            root_entries,
            expected,
            "core root is a small public contract surface; place implementation "
            "inside its owning capability package",
        )

    def test_application_layers_use_only_allowed_dependencies(self) -> None:
        violations = {
            source: invalid
            for source, path in _app_modules().items()
            if _top_level(source) in APP_PACKAGES
            and (invalid := _layer_violations(source, path))
        }
        self.assertFalse(violations, f"Application layer dependency violations: {violations}")

    def test_layer_checks_reject_forbidden_imports(self) -> None:
        cases = [
            ("core.example", "example.py", "import requests", "requests"),
            ("core.example", "example.py", "from gui import queue_model", "gui"),
            ("gui.example", "example.py", "from cli import cli_entry", "cli"),
            ("gui.example", "example.py", "import main", "main"),
            ("gui.queue_state", "queue_state.py", "from PySide6.QtCore import QObject", "PySide6.QtCore"),
            ("gui.queue_actions", "queue_actions.py", "import PySide6", "PySide6"),
            ("core.example", "example.py", "from ..gui import queue_model", "gui"),
            ("core", "__init__.py", "from ..gui import queue_model", "gui"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            for source, filename, code, expected in cases:
                with self.subTest(source=source, code=code):
                    path = Path(directory) / filename
                    path.write_text(code, encoding="utf-8")
                    self.assertIn(expected, _layer_violations(source, path))

    def test_package_relative_imports_resolve_to_concrete_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "__init__.py"
            # Mirrors core/smart/v1/__init__.py: one sibling owner, one core contract.
            path.write_text("from . import workflow\nfrom ...models import EncodePlanItem\n", encoding="utf-8")
            imports = _imports("core.smart.v1", path)
            self.assertIn("core.smart.v1.workflow", imports)
            self.assertIn("core.models", imports)
            self.assertEqual(_layer_violations("core.smart.v1", path), [])

    def test_core_has_no_ui_or_entrypoint_dependencies(self) -> None:
        violations: list[str] = []
        modules = _app_modules()
        for source, path in modules.items():
            if not source == "core" and not source.startswith("core."):
                continue
            for imported in _imports(source, path):
                if _top_level(imported) in {"cli", "gui"} or _is_qt_import(imported):
                    violations.append(f"{path.relative_to(ROOT)} imports {imported!r}")
        self.assertFalse(
            violations,
            "core is the reusable, UI-free layer; move this dependency upward or "
            "extract a neutral core abstraction:\n" + "\n".join(sorted(violations)),
        )

    def test_cli_has_no_gui_dependencies(self) -> None:
        violations: list[str] = []
        modules = _app_modules()
        for source, path in modules.items():
            if not source == "cli" and not source.startswith("cli."):
                continue
            for imported in _imports(source, path):
                if _top_level(imported) == "gui":
                    violations.append(f"{path.relative_to(ROOT)} imports {imported!r}")
        self.assertFalse(
            violations,
            "cli must remain usable without the GUI; depend on core or keep UI "
            "selection in main.py:\n" + "\n".join(sorted(violations)),
        )

    def test_app_modules_are_acyclic(self) -> None:
        cycles = _cycles(_dependency_graph())
        rendered = "\n".join(" -> ".join(cycle) for cycle in cycles)
        self.assertFalse(
            cycles,
            "Application modules must form an acyclic dependency graph; move shared "
            "logic into a lower layer to break this cycle:\n" + rendered,
        )

    def test_gui_submodules_do_not_import_main_window(self) -> None:
        violations: list[str] = []
        modules = _app_modules()
        for source, path in modules.items():
            if not source.startswith("gui.") or source == "gui.gui_entry":
                continue
            for imported in _imports(source, path):
                if imported.startswith("gui.gui_mainwindow"):
                    violations.append(f"{path.relative_to(ROOT)} imports {imported!r}")
        self.assertFalse(
            violations,
            "gui.gui_entry is the only GUI entrypoint; other gui sub-modules must not "
            "import MainWindow (it is the composition root):\n" + "\n".join(sorted(violations)),
        )

    def test_queue_state_and_model_do_not_import_queue_view(self) -> None:
        violations: list[str] = []
        modules = _app_modules()
        for source, path in modules.items():
            if source not in {"gui.queue_state", "gui.queue_model"}:
                continue
            for imported in _imports(source, path):
                if imported.startswith("gui.queue_view"):
                    violations.append(f"{path.relative_to(ROOT)} imports {imported!r}")
        self.assertFalse(
            violations,
            "queue_state is Qt-free and queue_model is the model layer; neither may "
            "depend on the view:\n" + "\n".join(sorted(violations)),
        )

    def test_smart_sampling_dependency_direction(self) -> None:
        graph = _dependency_graph()
        expected = {
            "core.smart.v1.sampling.complexity": {"core.ffmpeg.filters"},
            "core.smart.v1.sampling.planner": {"core.models"},
            "core.smart.v1.sampling.scout": {
                "core.models",
                "core.smart.v1.sampling.complexity",
                "core.smart.v1.sampling.planner",
            },
        }
        for module, allowed_core_dependencies in expected.items():
            actual = {
                dependency
                for dependency in graph[module]
                if dependency.startswith("core.")
            }
            self.assertEqual(
                actual,
                allowed_core_dependencies,
                f"{module} crossed the Smart sampling module boundary",
            )

    def test_smart_quality_module_boundaries(self) -> None:
        graph = _dependency_graph()
        focused_modules = {
            "core.smart.v1.bitrate",
            "core.smart.v1.cache",
            "core.smart.v1.measurement",
            "core.smart.v1.runtime",
        }
        forbidden_dependencies = {
            "core.smart.v1.workflow",
            "core.smart.v1.decisions",
            "core.smart.v1.session",
            "core.smart.v1.search",
        }
        violations = {
            module: sorted(graph[module] & forbidden_dependencies)
            for module in focused_modules
            if graph[module] & forbidden_dependencies
        }
        self.assertFalse(
            violations,
            "Focused Smart modules are lower-level owners and may not reach back "
            f"into orchestration or decisions: {violations}",
        )
        self.assertFalse(
            graph["core.smart.v1.workflow"] & {"core.smart.v1.decisions"},
            "Smart workflow must orchestrate focused modules without importing the "
            "queue decision policy",
        )

    def test_smart_session_and_search_have_no_upward_dependencies(self) -> None:
        graph = _dependency_graph()
        self.assertFalse(graph["core.smart.v1.session"] & {
            "core.smart", "core.smart.v1.workflow", "core.smart.v1.search", "core.smart.v1.decisions",
        })
        self.assertFalse(graph["core.smart.v1.search"] & {
            "core.smart", "core.smart.v1.workflow", "core.smart.v1.decisions",
        })

    def test_smart_v2_reuses_only_v1_leaf_primitives(self) -> None:
        graph = _dependency_graph()
        v2_modules = {source for source in graph if source.startswith("core.smart.v2.")}
        self.assertTrue(v2_modules, "core.smart.v2 owns an independent implementation path")
        allowed_v1_dependencies = {
            "core.smart.v1.bitrate",
            "core.smart.v1.cache",
            "core.smart.v1.measurement",
            "core.smart.v1.vmaf",
        }
        violations = [
            f"{source} imports {dependency}"
            for source in sorted(v2_modules)
            for dependency in sorted(graph[source])
            if dependency.startswith("core.smart.v1") and dependency not in allowed_v1_dependencies
        ]
        self.assertFalse(
            violations,
            "Smart v2 owns its own orchestration (workflow, runtime, optimizer, receipts) "
            "and may reuse only the listed v1 leaf primitives; v1 workflow, search, session "
            "and decisions stay out of reach, and a new shared primitive needs review: "
            + ", ".join(violations),
        )

    def test_smart_v1_reaches_into_v2_only_for_reselect(self) -> None:
        # D2 ← S1
        graph = _dependency_graph()
        v1_modules = {source for source in graph if source.startswith("core.smart.v1")}
        allowed_v2_dependencies = {("core.smart.v1.decisions", "core.smart.v2.workflow")}
        observed = {
            (source, dependency)
            for source in sorted(v1_modules)
            for dependency in sorted(graph[source])
            if dependency.startswith("core.smart.v2")
        }
        self.assertEqual(
            observed,
            allowed_v2_dependencies,
            "v1 is the default algorithm and must not depend on experimental v2. "
            "core.smart.v1.decisions may reuse v2's reselect for a segmented result; "
            "any other edge makes the default path depend on v2, and a removed edge "
            "makes this allow-list stale:",
        )

    def test_smart_public_surface_matches_adapter_operations(self) -> None:
        tree = ast.parse((ROOT / "core/smart/__init__.py").read_text(encoding="utf-8"))
        exports = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
        )
        used = {"analyze_quality"}
        for source, path in _app_modules().items():
            if not source.startswith(("cli.", "gui.")):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module == "core.smart":
                    used.update(alias.name for alias in node.names)
        self.assertEqual(set(exports), used)

    def test_core_capability_dependency_direction(self) -> None:
        graph = _dependency_graph()
        violations: list[str] = []
        for source, dependencies in graph.items():
            source_parts = source.split(".")
            if len(source_parts) < 2 or source_parts[0] != "core" or source_parts[1] not in CORE_PACKAGES:
                continue
            source_package = source_parts[1]
            allowed = ALLOWED_CORE_PACKAGE_DEPENDENCIES[source_package]
            for dependency in dependencies:
                dependency_parts = dependency.split(".")
                if len(dependency_parts) < 2 or dependency_parts[0] != "core":
                    continue
                dependency_package = dependency_parts[1]
                if dependency_package in CORE_PACKAGES and dependency_package != source_package and dependency_package not in allowed:
                    violations.append(f"{source} imports {dependency}")
        self.assertFalse(
            violations,
            "core capability packages must follow config/media -> ffmpeg -> smart -> "
            "encoding:\n" + "\n".join(sorted(violations)),
        )

    def test_cli_and_gui_use_core_public_package_contracts(self) -> None:
        modules = _app_modules()
        allowed = CORE_ROOT_MODULES | {f"core.{package}" for package in CORE_PACKAGES}
        violations: list[str] = []
        for source, path in modules.items():
            if not source.startswith(("cli.", "gui.")):
                continue
            for imported in _imports(source, path):
                if not imported.startswith("core.") or imported not in modules:
                    continue
                if imported not in allowed:
                    violations.append(f"{path.relative_to(ROOT)} imports {imported!r}")
        self.assertFalse(
            violations,
            "CLI and GUI must consume core package contracts instead of package "
            "implementation modules:\n" + "\n".join(sorted(violations)),
        )

    def test_queue_model_has_no_domain_side_effect_dependencies(self) -> None:
        path = _app_modules()["gui.queue_model"]
        forbidden = (
            "core.smart.v1.receipts",
            "core.smart.v1.decisions",
            "core.media.subtitles",
        )
        violations = [
            imported
            for imported in _imports("gui.queue_model", path)
            if imported.startswith(forbidden)
        ]
        self.assertFalse(
            violations,
            "QueueTableModel should emit Qt notifications and delegate record mutations "
            f"to gui.queue_actions, not own domain/file side effects: {violations}",
        )

    def test_queue_completion_is_consumed_only_by_main_window(self) -> None:
        graph = _dependency_graph()
        callers = {source for source, dependencies in graph.items() if "gui.queue_completion" in dependencies}
        self.assertEqual(callers, {"gui.gui_mainwindow"})
        self.assertFalse(graph["gui.queue_completion"] & {
            "gui.gui_mainwindow", "gui.queue_manager", "gui.gui_workers", "gui.queue_view",
        })

    def test_options_panel_does_not_probe_ffmpeg(self) -> None:
        path = _app_modules()["gui.encode_options_panel"]
        forbidden_names = {
            "discover_ffmpeg_tools",
            "list_available_encoders",
            "preset_choices_for_encoder",
            "resolve_encoder",
        }
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        self.assertFalse(
            imported & forbidden_names,
            "EncodeOptionsPanel must render the worker-produced capability snapshot; "
            f"it may not probe FFmpeg synchronously: {sorted(imported & forbidden_names)}",
        )

    def test_main_window_uses_options_panel_public_contract(self) -> None:
        path = _app_modules()["gui.gui_mainwindow"]
        allowed = {
            "analysis_profile_changed",
            "apply_analysis_profile_settings",
            "apply_options",
            "begin_capability_detection",
            "current_analysis_profile_name",
            "notify_capability_detection_failed",
            "read_options",
            "set_busy",
            "set_runtime_capabilities",
            "set_translator",
            "sync_dependent_controls",
        }
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        used = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "self"
            and node.value.attr == "options_panel"
        }
        self.assertFalse(
            used - allowed,
            "MainWindow must use EncodeOptionsPanel's public contract instead of raw "
            f"widgets: {sorted(used - allowed)}",
        )

    def test_queue_view_depends_on_model(self) -> None:
        view_path = _app_modules().get("gui.queue_view")
        self.assertIsNotNone(view_path)
        self.assertTrue(
            any(
                imported.startswith("gui.queue_model")
                for imported in _imports("gui.queue_view", view_path)
            ),
            "gui.queue_view must use the column definitions from gui.queue_model",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
