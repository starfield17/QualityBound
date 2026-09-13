from __future__ import annotations

import shutil
import sys
from pathlib import Path


APP_DISPLAY_NAME = "QualityBound"
APP_ICON_RELATIVE_PATH = Path("assets") / "app.svg"


def is_compiled() -> bool:
    return bool(getattr(sys, "frozen", False) or globals().get("__compiled__") is not None)


def is_frozen() -> bool:
    """Compatibility name for callers that only need compiled-build detection."""
    return is_compiled()


def source_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def macos_app_bundle_path(executable_path: str | Path | None = None) -> Path | None:
    """Return the enclosing ``.app`` for a macOS bundle executable, if any."""
    executable = Path(executable_path or sys.executable).expanduser().resolve()
    macos_dir = executable.parent
    contents_dir = macos_dir.parent
    app_bundle = contents_dir.parent
    if (
        macos_dir.name == "MacOS"
        and contents_dir.name == "Contents"
        and app_bundle.suffix == ".app"
    ):
        return app_bundle
    return None


def is_macos_app_bundle(executable_path: str | Path | None = None) -> bool:
    return macos_app_bundle_path(executable_path) is not None


def bundle_root() -> Path:
    app_bundle = macos_app_bundle_path()
    if app_bundle is not None:
        return app_bundle / "Contents" / "Resources"
    if is_compiled():
        return Path(sys.executable).resolve().parent
    return source_root()


def app_root() -> Path:
    if is_macos_app_bundle():
        return Path.home() / "Library" / "Application Support" / APP_DISPLAY_NAME
    if is_compiled():
        return Path(sys.executable).resolve().parent
    return source_root()


def config_dir() -> Path:
    return app_root() / "config"


def workdir_dir() -> Path:
    return app_root() / "workdir"


def translations_dir() -> Path:
    return app_root() / "translations"


def app_icon_path() -> Path | None:
    """Return the canonical SVG icon in a source checkout or packaged build."""
    candidates = (
        bundle_root() / APP_ICON_RELATIVE_PATH,
        source_root() / "packaging" / "assets" / "app.svg",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def _copy_tree_if_missing(source_dir: Path, target_dir: Path) -> None:
    # Copy files from source to target only when they don't already exist in target.
    # Directories are always created; only leaf files are checked for existence.
    if not source_dir.exists():
        return
    for item in source_dir.rglob("*"):
        relative = item.relative_to(source_dir)
        target = target_dir / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def ensure_runtime_layout() -> tuple[Path, Path]:
    # Seeds the writable runtime directory from the bundled (potentially read-only) config.
    # Built-in language packs are read directly from the bundle; the writable
    # translations/ dir holds explicit user override packages only.
    # Returns (runtime_config_dir, runtime_workdir).
    runtime_root = app_root()
    runtime_root.mkdir(parents=True, exist_ok=True)

    runtime_config = config_dir()
    runtime_workdir = workdir_dir()
    runtime_config.mkdir(parents=True, exist_ok=True)
    runtime_workdir.mkdir(parents=True, exist_ok=True)

    _copy_tree_if_missing(bundle_root() / "config" / "presets", runtime_config / "presets")

    translations_dir().mkdir(parents=True, exist_ok=True)

    for name in ("logs", "temp"):
        (runtime_workdir / name).mkdir(parents=True, exist_ok=True)

    return runtime_config, runtime_workdir
