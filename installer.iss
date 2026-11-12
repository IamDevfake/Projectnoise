; AutoBuild Inno Setup Script
[Setup]
AppId={{DENOISEGUI-AUTOBUILD-SETUP}}
AppName=denoise_gui
AppVersion=1.1.0
AppPublisher=denoise_gui Team
DefaultDirName={autopf}\denoise_gui
DefaultGroupName=denoise_gui
OutputDir=installer_output
OutputBaseFilename=denoise_gui_Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DisableProgramGroupPage=yes
UsedUserAreasWarning=no
VersionInfoVersion=1.1.0
VersionInfoCompany=denoise_gui
VersionInfoDescription=denoise_gui Setup
VersionInfoProductName=denoise_gui
; SetupIconFile omitted: file not found

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist\denoise_gui.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\denoise_gui"; Filename: "{app}\denoise_gui.exe"
Name: "{group}\{cm:UninstallProgram,denoise_gui}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\denoise_gui"; Filename: "{app}\denoise_gui.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\denoise_gui.exe"; Description: "{cm:LaunchProgram,denoise_gui}"; Flags: nowait postinstall skipifsilent
