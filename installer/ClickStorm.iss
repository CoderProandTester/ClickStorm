; ClickStorm installer (Inno Setup 6.3+).
;
; Compile:
;   ISCC.exe /DMyAppArch=x64 /DMyAppVersion=4.4.0 installer\ClickStorm.iss
;
; MyAppArch is one of: x64, x86, arm64. Build dist\ClickStorm.exe with the
; matching Python architecture first (PyInstaller is not a cross-compiler).

#ifndef MyAppVersion
  #define MyAppVersion "4.4.0"
#endif
#ifndef MyAppArch
  #define MyAppArch "x64"
#endif

#define MyAppName "ClickStorm"
#define MyAppExeName "ClickStorm.exe"

#if MyAppArch == "x64"
  #define MyAppArchAllowed "x64compatible"
#elif MyAppArch == "arm64"
  #define MyAppArchAllowed "arm64"
#else
  #define MyAppArchAllowed "x86compatible"
#endif

[Setup]
; Keep this AppId stable: it is how Windows recognises upgrades of ClickStorm.
AppId={{7C4B1E52-9A3D-4F68-B2E1-3A5C8D7F0B94}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppName}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\release
OutputBaseFilename=ClickStorm-Setup-{#MyAppVersion}-{#MyAppArch}
SetupIconFile=..\autoclicker.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; Windows 10 and Windows 11 (and later). 10.0 is the NT version of Windows 10.
MinVersion=10.0
ArchitecturesAllowed={#MyAppArchAllowed}
#if MyAppArch != "x86"
ArchitecturesInstallIn64BitMode={#MyAppArchAllowed}
#endif
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName} installer
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Checked by default; the user can clear it to skip the desktop shortcut.
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\autoclicker.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; Offered on the final page (checked by default) and run as the original user
; so ClickStorm does not inherit the installer's administrator token.
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent runasoriginaluser
