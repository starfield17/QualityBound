from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.build_nuitka import normalize_version, project_root  # noqa: E402


# Inno Setup architecture directives. x86_64 installers also run on Windows 11
# ARM64 through the x64 emulation layer; native ARM64 installers are restricted
# to native ARM64. This is the single place that maps release architectures to
# Inno directives; CI and the .iss file consume these values via defines.
ARCHITECTURES = {
    "x86_64": {
        "architectures_allowed": "x64compatible",
        "architectures_install_in_64bit_mode": "x64compatible",
    },
    "arm64": {
        "architectures_allowed": "arm64",
        "architectures_install_in_64bit_mode": "arm64",
    },
}

def _release_version(version: str) -> str:
    value = version.strip()
    # Accept MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH.BUILD (shared validator).
    normalize_version(value)
    return value


def architecture_directives(architecture: str) -> dict[str, str]:
    try:
        return ARCHITECTURES[architecture]
    except KeyError as exc:
        raise ValueError(f"Unsupported Setup architecture: {architecture}") from exc


def build_iscc_command(
    *,
    version: str,
    architecture: str,
    source_dir: Path,
    output_path: Path,
    intermediate_dir: Path,
    icon_path: Path,
    iscc_executable: str = "ISCC.exe",
    iss_path: Path | None = None,
) -> list[str]:
    directives = architecture_directives(architecture)
    release_version = _release_version(version)
    return [
        iscc_executable,
        str((iss_path or project_root() / "packaging" / "windows" / "installer.iss").resolve()),
        f"/O{output_path.parent.resolve()}",
        f"/F{output_path.stem}",
        f"/DReleaseVersion={release_version}",
        f"/DVersionInfo={normalize_version(release_version)}",
        f"/DSourceDir={source_dir.resolve()}",
        f"/DSetupIcon={icon_path.resolve()}",
        f"/DArchitecturesAllowed={directives['architectures_allowed']}",
        f"/DArchitecturesInstallIn64BitMode={directives['architectures_install_in_64bit_mode']}",
        "/Qp",
    ]


def validate_source(source_dir: Path, icon_path: Path) -> None:
    required = (
        source_dir / "qualitybound.exe",
        source_dir / "config" / "i18n" / "en.json",
        source_dir / "config" / "i18n" / "zh_cn.json",
        source_dir / "README.md",
        source_dir / "LICENSE",
        source_dir / "FFmpeg" / "bin" / "ffmpeg.exe",
        source_dir / "FFmpeg" / "bin" / "ffprobe.exe",
        icon_path,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError("Setup source is incomplete: " + ", ".join(missing))


def _argument_parser() -> argparse.ArgumentParser:
    root = project_root()
    parser = argparse.ArgumentParser(
        description="Build the Windows Setup.exe from a staged Nuitka directory."
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--architecture", choices=tuple(ARCHITECTURES), required=True)
    parser.add_argument("--source-dir", type=Path, default=root / "dist" / "qualitybound")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--intermediate-dir",
        type=Path,
        default=root / "workdir" / "innosetup",
    )
    parser.add_argument("--icon", type=Path, default=root / "packaging" / "assets" / "app.ico")
    parser.add_argument("--iscc", help="Path to ISCC.exe (defaults to PATH lookup)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    iscc_executable = args.iscc or shutil.which("ISCC.exe")
    if not iscc_executable:
        print("Setup build failed: ISCC.exe was not found.", file=sys.stderr)
        return 2

    try:
        validate_source(args.source_dir.resolve(), args.icon.resolve())
        intermediate_dir = args.intermediate_dir.resolve() / args.architecture
        intermediate_dir.mkdir(parents=True, exist_ok=True)
        args.output.resolve().parent.mkdir(parents=True, exist_ok=True)
        command = build_iscc_command(
            version=args.version,
            architecture=args.architecture,
            source_dir=args.source_dir,
            output_path=args.output,
            intermediate_dir=intermediate_dir,
            icon_path=args.icon,
            iscc_executable=iscc_executable,
        )
        print("Running:", " ".join(command))
        subprocess.run(command, check=True, cwd=project_root())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Setup build failed: {exc}", file=sys.stderr)
        return 2
    print(f"Setup package: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
