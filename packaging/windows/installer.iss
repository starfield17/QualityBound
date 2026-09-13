; QualityBound Windows Setup script (Inno Setup)
;
; This manifest is compiled by scripts/build_setup.py, which validates the
; staged Nuitka standalone directory and injects the release version, version
; info and architecture defines below. Keep this file declarative: the Python
; builder owns argument validation, version normalization and artifact naming,
; while this file describes the installation behavior.
;
; QualityBound starts a new installer identity. Earlier Video Compressor
; installations are intentionally not upgraded or removed by this setup.
[Setup]
AppId={{F1434831-589A-59FC-9A61-AEA4B9403BFC}
AppName=QualityBound
AppVersion={#ReleaseVersion}
AppVerName=QualityBound {#ReleaseVersion}
AppPublisher=starfield17
AppPublisherURL=https://github.com/starfield17/QualityBound
AppUpdatesURL=https://github.com/starfield17/QualityBound/releases

VersionInfoVersion={#VersionInfo}
VersionInfoProductName=QualityBound
VersionInfoCompany=starfield17

; Per-user installation only: no UAC, no Program Files, no system components.
PrivilegesRequired=lowest

DefaultDirName={localappdata}\Programs\QualityBound
DefaultGroupName=QualityBound

; Stable per-user Add/Remove Programs registration (no custom registry keys).
UninstallDisplayName=QualityBound
UninstallDisplayIcon={app}\qualitybound.exe
OutputBaseFilename=qualitybound-setup

SetupIconFile={#SetupIcon}
Compression=lzma2/ultra
SolidCompression=yes

; x86_64 builds may also install through the Windows 11 ARM64 x64 emulation
; layer; native ARM64 builds restrict to native ARM64.
ArchitecturesAllowed={#ArchitecturesAllowed}
ArchitecturesInstallIn64BitMode={#ArchitecturesInstallIn64BitMode}

; SignTool stays disabled so Inno does not require a certificate at compile
; time; sign_windows.ps1 signs Setup.exe after compilation. Enable both lines
; to sign Setup.exe and its generated uninstaller during compilation instead.
; SignTool=signtool $f
; SignedUninstaller=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; No default Desktop shortcut, matching the previous MSI behavior.

[Files]
; Install the complete Nuitka standalone tree (application, DLLs, Qt, FFmpeg)
; recursively. Do not enumerate individual payload files here.
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "*.pyc,__pycache__"

[Icons]
Name: "{group}\QualityBound"; Filename: "{app}\qualitybound.exe"; WorkingDir: "{app}"
