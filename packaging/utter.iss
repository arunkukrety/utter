; Windows installer for Utter (Inno Setup 6). Built by .github/workflows/build.yml:
;   iscc /DVERSION=2.0.0 packaging\utter.iss      -> dist\Utter-Setup-2.0.0.exe
; Installs for the current user only (no admin prompt), adds a Start menu entry, and starts Utter when done.
#ifndef VERSION
  #define VERSION "0.0.0"
#endif

[Setup]
AppId={{6C2B7F0E-4C1D-4F5B-9E2A-8D3B1A7C5E91}
AppName=Utter
AppVersion={#VERSION}
AppPublisher=Utter
DefaultDirName={localappdata}\Programs\Utter
DefaultGroupName=Utter
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Utter-Setup-{#VERSION}
SetupIconFile=..\build\utter.ico
UninstallDisplayIcon={app}\Utter.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=force

[Files]
Source: "..\dist\Utter\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\Utter"; Filename: "{app}\Utter.exe"
Name: "{group}\Uninstall Utter"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\Utter.exe"; Description: "Start Utter"; Flags: nowait postinstall

[UninstallRun]
Filename: "taskkill.exe"; Parameters: "/f /im Utter.exe"; Flags: runhidden; RunOnceId: "StopUtter"

[Registry]
; the app adds itself to start at login on first run; remove that entry with the app
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Utter"; Flags: uninsdeletevalue dontcreatekey
