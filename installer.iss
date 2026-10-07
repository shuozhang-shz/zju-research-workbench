; 科研工作台 Windows 安装包脚本（Inno Setup 7）
; 编译：ISCC installer.iss → Output/科研工作台-Setup-1.0.0.exe

#define MyAppName "科研工作台"
#define MyAppNameEn "Research Workbench"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Research Workbench contributors"
#define MyAppURL "https://github.com/REPO-PLACEHOLDER/zju-research-workbench"
#define MyAppExeName "浙大科研工作台.exe"

[Setup]
AppId={{8C1F5A62-4D3E-4B9B-9F7A-1D2C3E4F5A6B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={localappdata}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=科研工作台-Setup-{#MyAppVersion}
SetupIconFile=wb.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppExeName}

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "dist_staging\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{#MyAppName} 桥接模式（浏览器）"; Filename: "{app}\启动浙大桥.bat"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时清理运行时生成的缓存（保留用户数据：config/session/state/seen.json）
Type: files; Name: "{app}\04-代码与数据\zju-bridge\cache.json"
