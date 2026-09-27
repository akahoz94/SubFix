; SubFix Windows 安装器（Inno Setup 6/7）
; 由 build_windows_zip.py --installer 调用，注入 /DVersion /DStageDir
; 对应 macOS 的 pkg：安装到 Resolve 用户脚本目录，可覆盖安装，卸载保留用户数据。

#define AppName "SubFix"
#define AppPublisher "HooperH"
#define AppURL "https://github.com/akahoz94/SubFix"

[Setup]
AppId={{8E7B5E9A-3C42-4B8D-9A57-A4C61E9F0B01}
AppName={#AppName}（达芬奇字幕插件）
AppVersion={#Version}
AppVerName={#AppName} v{#Version} Windows
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
DefaultDirName={userappdata}\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputDir={#StageDir}
OutputBaseFilename=SubFixSetup-stage
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallFilesDir={app}\.subfix_uninstall
; 无数字签名，与上游 macOS 包同样采取未签名发布
PrivilegesRequired=lowest

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "{#StageDir}\SubFix\*"; DestDir: "{app}\SubFix"; Flags: recursesubdirs ignoreversion
Source: "{#StageDir}\.subfix_support\*"; DestDir: "{app}\.subfix_support"; Flags: recursesubdirs ignoreversion
Source: "{#StageDir}\README-win.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\接入本地模型.bat"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "{app}\README-win.md"; Description: "查看说明（README-win.md）"; Flags: postinstall shellexec skipifsilent unchecked
