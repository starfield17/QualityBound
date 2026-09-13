from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


DEFAULT_NAME = "qualitybound"
DEFAULT_OUTPUT_DIR = "dist"
DEFAULT_VERSION = "0.0.0"
MACOS_APP_NAME = "QualityBound"
MACOS_SIGNED_APP_NAME = "com.starfield17.QualityBound"
_VERSION_PATTERN = re.compile(r"^\d+(?:\.\d+){2,3}$")
_WINDOWS_COMPILERS = {"auto", "mingw64", "msvc", "clang"}
_TARGET_ARCHITECTURES = {"native", "x86_64", "arm64"}


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def normalize_version(version: str) -> str:
    """Validate a numeric release version and normalize it for Windows metadata."""
    value = version.strip()
    if not _VERSION_PATTERN.fullmatch(value):
        raise ValueError(
            f"Invalid version {version!r}; expected MAJOR.MINOR.PATCH or "
            "MAJOR.MINOR.PATCH.BUILD using numeric components."
        )
    components = value.split(".")
    if len(components) == 3:
        components.append("0")
    return ".".join(components)


def normalise_version(version: str) -> str:
    """British-spelling alias for callers that use the wording from the CLI docs."""
    return normalize_version(version)


def _is_windows(platform_name: str | None = None) -> bool:
    value = sys.platform if platform_name is None else platform_name
    return value.lower().startswith("win")


def _is_macos(platform_name: str | None = None) -> bool:
    value = sys.platform if platform_name is None else platform_name
    return value.lower().startswith(("darwin", "macos"))


def normalized_machine(machine: str | None = None) -> str:
    value = (machine or platform.machine()).strip().lower()

    if value in {"amd64", "x86_64"}:
        return "x86_64"

    if value in {"arm64", "aarch64"}:
        return "arm64"

    return value


def resolve_windows_compiler(
    requested: str,
    *,
    machine: str | None = None,
    python_version: tuple[int, int] | None = None,
) -> str:
    if requested not in _WINDOWS_COMPILERS:
        raise ValueError(f"Unsupported Windows compiler: {requested}")

    if requested != "auto":
        return requested

    if normalized_machine(machine) == "arm64":
        return "clang"

    version = python_version or sys.version_info[:2]
    if version >= (3, 13):
        return "msvc"
    return "mingw64"


def resolve_macos_target_arch(
    requested: str,
    *,
    machine: str | None = None,
) -> str:
    if requested not in _TARGET_ARCHITECTURES:
        raise ValueError(f"Unsupported target architecture: {requested}")

    native_arch = normalized_machine(machine)
    target_arch = native_arch if requested == "native" else requested
    if target_arch not in {"x86_64", "arm64"}:
        raise ValueError(
            f"Cannot determine a supported native macOS architecture from {native_arch!r}."
        )
    if target_arch != native_arch:
        raise ValueError(
            "macOS app builds must use the native runner architecture: "
            f"Python is running as {native_arch}, but {target_arch} was requested."
        )
    return target_arch


def _is_conda_python(python_executable: str) -> bool:
    executable = str(Path(python_executable).resolve()).lower()
    prefix = str(Path(sys.prefix).resolve()).lower()
    return bool(os.environ.get("CONDA_PREFIX")) or "conda" in executable or "conda" in prefix


def _validated_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("The executable name must be a non-empty filename without path separators.")
    return name


def _repo_path(value: str | Path, root: Path) -> Path:
    candidate = Path(value)
    resolved = (root / candidate if not candidate.is_absolute() else candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"Output directory must be inside the repository: {resolved}") from exc
    return resolved


@dataclass(frozen=True)
class BuildPaths:
    root: Path
    output_dir: Path
    package_dir: Path
    nuitka_output_dir: Path
    reports_dir: Path
    report_path: Path
    executable_path: Path
    resources_dir: Path
    app_bundle_path: Path | None = None
    dmg_path: Path | None = None
    target_arch: str | None = None


def build_paths(
    *,
    name: str = DEFAULT_NAME,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    root: Path | None = None,
    platform_name: str | None = None,
    macos_app_bundle: bool = False,
    target_arch: str = "native",
    machine: str | None = None,
) -> BuildPaths:
    root = (root or project_root()).resolve()
    name = _validated_name(name)
    if macos_app_bundle and not _is_macos(platform_name):
        raise ValueError("--macos-app-bundle is only supported on macOS.")

    resolved_output_dir = _repo_path(output_dir, root)
    resolved_target_arch = (
        resolve_macos_target_arch(target_arch, machine=machine)
        if macos_app_bundle
        else None
    )
    if macos_app_bundle:
        package_dir = resolved_output_dir / f"{MACOS_APP_NAME}.app"
        executable_path = package_dir / "Contents" / "MacOS" / name
        resources_dir = package_dir / "Contents" / "Resources"
        app_bundle_path: Path | None = package_dir
        dmg_path: Path | None = resolved_output_dir / f"{name}.dmg"
    else:
        executable_name = f"{name}.exe" if _is_windows(platform_name) else name
        package_dir = resolved_output_dir / name
        executable_path = package_dir / executable_name
        resources_dir = package_dir
        app_bundle_path = None
        dmg_path = None

    return BuildPaths(
        root=root,
        output_dir=resolved_output_dir,
        package_dir=package_dir,
        nuitka_output_dir=root / "build" / "nuitka",
        reports_dir=root / "build" / "reports",
        report_path=root / "build" / "reports" / f"{name}.xml",
        executable_path=executable_path,
        resources_dir=resources_dir,
        app_bundle_path=app_bundle_path,
        dmg_path=dmg_path,
        target_arch=resolved_target_arch,
    )


def final_package_dir(
    *,
    name: str = DEFAULT_NAME,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    root: Path | None = None,
    platform_name: str | None = None,
    macos_app_bundle: bool = False,
    target_arch: str = "native",
    machine: str | None = None,
) -> Path:
    """Return the normalized public package directory for a build."""
    return build_paths(
        name=name,
        output_dir=output_dir,
        root=root,
        platform_name=platform_name,
        macos_app_bundle=macos_app_bundle,
        target_arch=target_arch,
        machine=machine,
    ).package_dir


def _icon_path(root: Path, platform_name: str | None = None) -> Path | None:
    names = (
        ("app.ico", "icon.ico")
        if _is_windows(platform_name)
        else ("app.icns", "icon.icns", "app.png", "icon.png")
    )
    for name in names:
        relative_path = Path("packaging") / "assets" / name
        candidate = root / relative_path
        if candidate.is_file():
            return candidate
    return None


def build_nuitka_command(
    version: str,
    *,
    name: str = DEFAULT_NAME,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    report_path: str | Path | None = None,
    root: Path | None = None,
    platform_name: str | None = None,
    python_executable: str | None = None,
    windows_compiler: str = "auto",
    machine: str | None = None,
    macos_app_bundle: bool = False,
    target_arch: str = "native",
) -> list[str]:
    """Construct the single canonical Nuitka command used by local and CI builds."""
    normalized_version = normalize_version(version)
    release_version = version.strip()
    paths = build_paths(
        name=name,
        output_dir=output_dir,
        root=root,
        platform_name=platform_name,
        macos_app_bundle=macos_app_bundle,
        target_arch=target_arch,
        machine=machine,
    )
    executable = python_executable or sys.executable
    report = Path(report_path) if report_path is not None else paths.report_path
    if not report.is_absolute():
        report = paths.root / report
    report = report.resolve()
    mode = "app-dist" if macos_app_bundle else "standalone"
    command = [
        executable,
        "-m",
        "nuitka",
        f"--mode={mode}",
        "--assume-yes-for-downloads",
        "--enable-plugin=pyside6",
        "--include-package=cli",
        "--include-package=core",
        "--include-package=gui",
        f"--output-dir={paths.nuitka_output_dir}",
        f"--output-filename={name}",
        f"--report={report}",
    ]

    if _is_conda_python(executable):
        # Conda does not ship libpython-static in a normal environment. Dynamic
        # libpython is sufficient for standalone output and keeps local Conda
        # builds usable without adding a Conda-only package requirement.
        command.append("--static-libpython=no")

    if _is_windows(platform_name):
        resolved_compiler = resolve_windows_compiler(windows_compiler, machine=machine)
        compiler_flag = {
            "mingw64": "--mingw64",
            "msvc": "--msvc=latest",
            "clang": "--clang",
        }[resolved_compiler]
        command.append(compiler_flag)
        command.extend(
            [
                "--windows-console-mode=attach",
                "--product-name=QualityBound",
                "--file-description=QualityBound",
                "--company-name=starfield17",
                f"--product-version={normalized_version}",
                f"--file-version={normalized_version}",
            ]
        )
        icon_path = _icon_path(paths.root, platform_name)
        if icon_path is not None:
            command.append(f"--windows-icon-from-ico={icon_path}")

    if macos_app_bundle:
        icon_path = _icon_path(paths.root, platform_name)
        command.extend(
            [
                f"--macos-app-name={MACOS_APP_NAME}",
                "--macos-app-mode=gui",
                f"--macos-app-version={release_version}",
                f"--macos-signed-app-name={MACOS_SIGNED_APP_NAME}",
                f"--macos-target-arch={paths.target_arch}",
                "--macos-app-create-dmg",
            ]
        )
        if icon_path is not None:
            command.append(f"--macos-app-icon={icon_path}")

    command.append("main.py")
    return command


def _remove_generated_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _assert_inside(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} must be inside {root}: {resolved}") from exc
    return resolved


def clean_generated_paths(
    root: Path | None = None,
    package_dir: Path | None = None,
    *,
    output_dir: Path | None = None,
) -> None:
    """Remove only build output paths owned by this wrapper."""
    root = (root or project_root()).resolve()
    targets: list[Path] = [
        root / "build" / "nuitka",
        root / "build" / "reports",
        root / "dist" / DEFAULT_NAME,
        root / "dist" / f"{MACOS_APP_NAME}.app",
    ]
    if package_dir is not None:
        targets.append(_assert_inside(package_dir, root, label="Package directory"))

    if output_dir is None:
        output_dir = package_dir.parent if package_dir is not None else root / DEFAULT_OUTPUT_DIR
    resolved_output_dir = _assert_inside(output_dir, root, label="Output directory")
    if resolved_output_dir.exists() and resolved_output_dir.is_dir():
        targets.extend(
            _assert_inside(path, root, label="Generated DMG")
            for path in resolved_output_dir.glob("*.dmg")
        )

    seen: set[Path] = set()
    for target in targets:
        resolved_target = target.resolve()
        if resolved_target in seen:
            continue
        seen.add(resolved_target)
        _remove_generated_path(resolved_target)


def _controlled_output_candidates(controlled_dir: Path, pattern: str) -> list[Path]:
    controlled_dir = controlled_dir.resolve()
    candidates: list[Path] = []
    for candidate in sorted(controlled_dir.rglob(pattern)):
        resolved = candidate.resolve()
        try:
            resolved.relative_to(controlled_dir)
        except ValueError as exc:
            raise RuntimeError(
                f"Generated output is outside the controlled build directory: {candidate}"
            ) from exc
        candidates.append(candidate)
    return candidates


def locate_distribution_dir(nuitka_output_dir: Path) -> Path:
    candidates = [
        path
        for path in _controlled_output_candidates(nuitka_output_dir, "*.dist")
        if path.is_dir()
    ]
    if len(candidates) != 1:
        details = ", ".join(str(path) for path in candidates) or "none"
        raise RuntimeError(
            "Expected exactly one Nuitka standalone distribution directory in "
            f"{nuitka_output_dir}, found {len(candidates)}: {details}"
        )
    return candidates[0]


def locate_app_bundle(build_dir: Path) -> Path:
    candidates = [
        path
        for path in _controlled_output_candidates(build_dir, "*.app")
        if path.is_dir()
    ]
    if len(candidates) != 1:
        details = ", ".join(str(path) for path in candidates) or "none"
        raise RuntimeError(
            "Expected exactly one macOS app bundle in "
            f"{build_dir}, found {len(candidates)}: {details}"
        )
    return candidates[0]


def locate_dmg(build_dir: Path) -> Path:
    candidates = [
        path
        for path in _controlled_output_candidates(build_dir, "*.dmg")
        if path.is_file()
    ]
    if len(candidates) != 1:
        details = ", ".join(str(path) for path in candidates) or "none"
        raise RuntimeError(
            "Expected exactly one macOS DMG in "
            f"{build_dir}, found {len(candidates)}: {details}"
        )
    return candidates[0]


def _architecture_tokens(text: str) -> set[str]:
    lower = text.lower()
    architectures: set[str] = set()
    if re.search(r"(?<![a-z0-9])(?:arm64|aarch64)(?![a-z0-9])", lower):
        architectures.add("arm64")
    if re.search(r"(?<![a-z0-9])(?:x86_64|x86-64|amd64)(?![a-z0-9])", lower):
        architectures.add("x86_64")
    return architectures


def binary_architectures(binary_path: Path) -> set[str]:
    """Return architectures reported by macOS tools for a Mach-O binary."""
    binary_path = binary_path.resolve()
    try:
        lipo_result = subprocess.run(
            ["lipo", "-archs", str(binary_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        lipo_result = None
    if lipo_result is not None and lipo_result.returncode == 0:
        architectures = _architecture_tokens(lipo_result.stdout + "\n" + lipo_result.stderr)
        if architectures:
            return architectures

    try:
        file_result = subprocess.run(
            ["file", str(binary_path)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return set()
    return _architecture_tokens(file_result.stdout + "\n" + file_result.stderr)


def verify_binary_architecture(
    binary_path: Path,
    target_arch: str,
    *,
    exact: bool = False,
) -> set[str]:
    architectures = binary_architectures(binary_path)
    expected = {target_arch}
    if not architectures:
        raise RuntimeError(f"Could not determine the architecture of {binary_path}.")
    if (exact and architectures != expected) or (not exact and target_arch not in architectures):
        requirement = f"exactly {target_arch}" if exact else f"{target_arch}"
        found = ", ".join(sorted(architectures))
        raise RuntimeError(
            f"Architecture mismatch for {binary_path}: expected {requirement}, found {found}."
        )
    return architectures


def find_ffmpeg_pair(ffmpeg_dir: Path, platform_name: str | None = None) -> tuple[Path, Path] | None:
    """Find a complete FFmpeg/FFprobe pair matching the target platform."""
    if _is_windows(platform_name):
        ffmpeg_name, ffprobe_name = "ffmpeg.exe", "ffprobe.exe"
    else:
        ffmpeg_name, ffprobe_name = "ffmpeg", "ffprobe"

    for relative_dir in (Path(), Path("bin")):
        ffmpeg_path = ffmpeg_dir / relative_dir / ffmpeg_name
        ffprobe_path = ffmpeg_dir / relative_dir / ffprobe_name
        if ffmpeg_path.is_file() and ffprobe_path.is_file():
            return ffmpeg_path, ffprobe_path
    return None


def stage_ffmpeg(
    root: Path,
    package_dir: Path,
    platform_name: str | None = None,
    *,
    resource_dir: Path | None = None,
    target_arch: str | None = None,
    ffmpeg_dir: Path | None = None,
    required: bool = False,
) -> bool:
    source_dir = (ffmpeg_dir or root / "FFmpeg").resolve()
    resource_dir = (resource_dir or package_dir).resolve()
    target_dir = resource_dir / "FFmpeg"
    pair = find_ffmpeg_pair(source_dir, platform_name)
    if pair is None:
        if target_dir.exists():
            _remove_generated_path(target_dir)
        expected = "ffmpeg.exe and ffprobe.exe" if _is_windows(platform_name) else "ffmpeg and ffprobe"
        message = f"No complete compatible FFmpeg pair ({expected}) found under {source_dir}."
        if required:
            raise RuntimeError(message)
        print(f"Info: {message} Skipping bundle.")
        return False

    if _is_macos(platform_name) and target_arch is not None:
        try:
            for binary_path in pair:
                verify_binary_architecture(binary_path, target_arch)
        except RuntimeError as exc:
            if target_dir.exists():
                _remove_generated_path(target_dir)
            if required:
                raise
            print(f"Info: skipping incompatible FFmpeg bundle: {exc}")
            return False

    if target_dir.exists():
        _remove_generated_path(target_dir)
    shutil.copytree(source_dir, target_dir)
    print(f"Staged FFmpeg bundle from {source_dir}.")
    return True


def stage_release_resources(
    package_dir: Path,
    *,
    root: Path | None = None,
    platform_name: str | None = None,
    resource_dir: Path | None = None,
    target_arch: str | None = None,
    ffmpeg_dir: Path | None = None,
    require_ffmpeg: bool = False,
) -> None:
    root = (root or project_root()).resolve()
    package_dir = package_dir.resolve()
    package_dir.mkdir(parents=True, exist_ok=True)
    if resource_dir is None:
        resource_dir = (
            package_dir / "Contents" / "Resources"
            if package_dir.suffix == ".app"
            else package_dir
        )
    resource_dir = _assert_inside(resource_dir, package_dir, label="Resource directory")
    resource_dir.mkdir(parents=True, exist_ok=True)

    config_source = root / "config"
    readme_source = root / "README.md"
    license_source = root / "LICENSE"
    if not config_source.is_dir():
        raise FileNotFoundError(f"Required release resource is missing: {config_source}")
    if not readme_source.is_file():
        raise FileNotFoundError(f"Required release resource is missing: {readme_source}")
    if not license_source.is_file():
        raise FileNotFoundError(f"Required release resource is missing: {license_source}")

    shutil.copytree(config_source, resource_dir / "config", dirs_exist_ok=True)
    shutil.copy2(readme_source, resource_dir / "README.md")
    shutil.copy2(license_source, resource_dir / "LICENSE")
    icon_source = root / "packaging" / "assets" / "app.svg"
    if not icon_source.is_file():
        raise FileNotFoundError(f"Required release resource is missing: {icon_source}")
    assets_dir = resource_dir / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(icon_source, assets_dir / "app.svg")
    stage_ffmpeg(
        root,
        package_dir,
        platform_name,
        resource_dir=resource_dir,
        target_arch=target_arch,
        ffmpeg_dir=ffmpeg_dir,
        required=require_ffmpeg,
    )


def refresh_macos_dmg(app_bundle: Path, dmg_path: Path) -> None:
    """Repack the staged app so wrapper-added resources are present in the DMG."""
    dmg_path = dmg_path.resolve()
    dmg_path.parent.mkdir(parents=True, exist_ok=True)
    create_dmg = Path(__file__).resolve().with_name("create-dmg")
    subprocess.run(
        [
            str(create_dmg),
            str(dmg_path),
            str(app_bundle),
        ],
        check=True,
        cwd=app_bundle.parent,
    )


def sign_macos_app(app_bundle: Path) -> None:
    """Re-sign after wrapper resources are staged into the app bundle."""
    subprocess.run(
        [
            "codesign",
            "--force",
            "--deep",
            "--sign",
            "-",
            "--timestamp=none",
            str(app_bundle),
        ],
        check=True,
    )


def _build_environment(root: Path, *, macos_app_bundle: bool = False) -> dict[str, str]:
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(root) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    environment["NUITKA_CACHE_DIR"] = str(root / "workdir" / "nuitka-cache")
    environment["CCACHE_DIR"] = str(root / "workdir" / "ccache")
    if macos_app_bundle:
        # Nuitka's create-dmg helper runs a Finder AppleScript by default,
        # which can hang on headless CI runners. The repository shim creates a
        # valid UDZO image with hdiutil and keeps the --macos-app-create-dmg
        # build mode enabled.
        scripts_dir = root / "scripts"
        environment["PATH"] = str(scripts_dir) + os.pathsep + environment.get("PATH", "")
    return environment


def patch_nuitka_windows_arm64_clang_probe(
    *,
    platform_name: str | None = None,
    machine: str | None = None,
    nuitka_root: Path | None = None,
) -> bool:
    """Teach the pinned Nuitka release to recognize native ARM64 Clang.

    Nuitka 4.1.3 checks for ``ARM64`` in ClangCL's stderr, but the native
    Visual Studio ARM64 toolchain reports ``aarch64`` in stdout instead. The
    build must still use Nuitka's ``--clang`` mode, so patch only this narrow
    architecture probe in the isolated packaging environment.
    """
    if not (
        _is_windows(platform_name)
        and normalized_machine(machine) == "arm64"
    ):
        return False

    if nuitka_root is None:
        import nuitka

        nuitka_root = Path(nuitka.__file__).resolve().parent

    probe_path = nuitka_root / "build" / "SconsUtils.py"
    source = probe_path.read_text(encoding="utf-8")
    old = 'elif b"ARM64" in process_result.stderr:'
    new = 'elif b"ARM64" in process_result.stderr or b"aarch64" in process_result.stdout:'

    if new in source:
        return False
    if old not in source:
        raise RuntimeError(
            f"Unsupported Nuitka compiler probe layout in {probe_path}"
        )

    probe_path.write_text(source.replace(old, new, 1), encoding="utf-8")
    return True


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the app with Nuitka")
    parser.add_argument("--clean", action="store_true", help="Remove this wrapper's generated build output first")
    parser.add_argument("--version", default=DEFAULT_VERSION, help="Numeric release version")
    parser.add_argument("--name", default=DEFAULT_NAME, help="Executable and normalized package name")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="Package output directory inside the repository")
    parser.add_argument(
        "--windows-compiler",
        choices=("auto", "mingw64", "msvc", "clang"),
        default="auto",
        help=(
            "Windows compiler backend. Auto selects clang on ARM64 "
            "and Nuitka-managed MinGW64 on x86-64."
        ),
    )
    parser.add_argument(
        "--macos-app-bundle",
        action="store_true",
        help="Build a macOS native .app bundle and DMG (macOS only)",
    )
    parser.add_argument(
        "--target-arch",
        choices=("native", "x86_64", "arm64"),
        default="native",
        help="Native target architecture for a macOS app build",
    )
    parser.add_argument(
        "--ffmpeg-dir",
        type=Path,
        help="Directory containing the FFmpeg/FFprobe pair to bundle (defaults to ./FFmpeg)",
    )
    parser.add_argument(
        "--require-ffmpeg",
        action="store_true",
        help="Fail the build unless a complete compatible FFmpeg/FFprobe pair is bundled",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    root = project_root()
    ffmpeg_dir = (
        (root / args.ffmpeg_dir if not args.ffmpeg_dir.is_absolute() else args.ffmpeg_dir).resolve()
        if args.ffmpeg_dir is not None
        else None
    )
    try:
        normalize_version(args.version)
        paths = build_paths(
            name=args.name,
            output_dir=args.output_dir,
            root=root,
            platform_name=sys.platform,
            macos_app_bundle=args.macos_app_bundle,
            target_arch=args.target_arch,
        )
        resolved_compiler = resolve_windows_compiler(args.windows_compiler)
    except ValueError as exc:
        print(f"Build configuration error: {exc}", file=sys.stderr)
        return 2

    if (
        _is_windows()
        and resolved_compiler == "mingw64"
        and sys.version_info >= (3, 13)
    ):
        print(
            "Build configuration error: Nuitka MinGW64 builds require "
            "Python 3.12 or older. Use Python 3.12 or select "
            "--windows-compiler msvc/clang with the native toolchain.",
            file=sys.stderr,
        )
        return 2

    if args.clean:
        clean_generated_paths(root, paths.package_dir, output_dir=paths.output_dir)

    paths.nuitka_output_dir.mkdir(parents=True, exist_ok=True)
    paths.reports_dir.mkdir(parents=True, exist_ok=True)
    if resolved_compiler == "clang":
        patch_nuitka_windows_arm64_clang_probe(
            platform_name=sys.platform,
            machine=platform.machine(),
        )
    command = build_nuitka_command(
        args.version,
        name=args.name,
        output_dir=args.output_dir,
        report_path=paths.report_path,
        root=root,
        platform_name=sys.platform,
        windows_compiler=args.windows_compiler,
        macos_app_bundle=args.macos_app_bundle,
        target_arch=args.target_arch,
    )
    print("Running:", " ".join(command))
    subprocess.run(
        command,
        check=True,
        cwd=root,
        env=_build_environment(root, macos_app_bundle=args.macos_app_bundle),
    )

    paths.output_dir.mkdir(parents=True, exist_ok=True)
    if args.macos_app_bundle:
        generated_app = locate_app_bundle(paths.nuitka_output_dir)
        generated_dmg = locate_dmg(paths.nuitka_output_dir)
        if paths.package_dir.exists():
            _remove_generated_path(paths.package_dir)
        shutil.move(str(generated_app), str(paths.package_dir))
        stage_release_resources(
            paths.package_dir,
            root=root,
            platform_name=sys.platform,
            resource_dir=paths.resources_dir,
            target_arch=paths.target_arch,
            ffmpeg_dir=ffmpeg_dir,
            require_ffmpeg=args.require_ffmpeg,
        )
        sign_macos_app(paths.package_dir)
        if paths.dmg_path is None:
            raise RuntimeError("macOS app build did not define a deterministic DMG path.")
        if paths.dmg_path.exists():
            _remove_generated_path(paths.dmg_path)
        shutil.move(str(generated_dmg), str(paths.dmg_path))
        # Nuitka creates its DMG before this wrapper stages project resources.
        # Repack the normalized app so the DMG and tarball contain the same app.
        refresh_macos_dmg(paths.package_dir, paths.dmg_path)
    else:
        distribution_dir = locate_distribution_dir(paths.nuitka_output_dir)
        if paths.package_dir.exists():
            _remove_generated_path(paths.package_dir)
        shutil.move(str(distribution_dir), str(paths.package_dir))
        stage_release_resources(
            paths.package_dir,
            root=root,
            platform_name=sys.platform,
            ffmpeg_dir=ffmpeg_dir,
            require_ffmpeg=args.require_ffmpeg,
        )

    if not paths.executable_path.is_file():
        raise RuntimeError(f"Nuitka build completed but executable was not found: {paths.executable_path}")

    if args.macos_app_bundle and paths.target_arch is not None:
        verify_binary_architecture(paths.executable_path, paths.target_arch, exact=True)

    print(f"Final package: {paths.package_dir}")
    print(f"Executable: {paths.executable_path}")
    if paths.dmg_path is not None:
        print(f"DMG: {paths.dmg_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
