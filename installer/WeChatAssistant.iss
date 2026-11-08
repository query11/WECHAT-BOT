#ifndef AppVersion
  #error AppVersion must be supplied by build.ps1
#endif
#ifndef PayloadDir
  #error PayloadDir must be supplied by build.ps1
#endif

[Setup]
AppId={{7B90F140-0A4A-4EC0-BE50-786F15A7C6A5}
AppName=微信助手
AppVersion={#AppVersion}
AppPublisher=query11
AppPublisherURL=https://github.com/query11/WECHAT-BOT
DefaultDirName={localappdata}\Programs\WeChatAssistant
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile={#PayloadDir}\desktop\wechat-assistant-v2.ico
UninstallDisplayIcon={app}\desktop\wechat-assistant-v2.ico
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
LicenseFile={#PayloadDir}\LICENSE
DisableProgramGroupPage=yes
OutputBaseFilename=WeChatAssistant-Setup-{#AppVersion}
CloseApplications=no
RestartApplications=no
SetupLogging=yes

[Languages]
#if FileExists(CompilerPath + "Languages\ChineseSimplified.isl")
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
#else
  #pragma message "WARNING: Simplified Chinese requires Inno Setup 7.0.2+; using English."
Name: "english"; MessagesFile: "compiler:Default.isl"
#endif

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\微信助手"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\desktop\launch_tray.ps1"" -Show"; WorkingDir: "{app}"; IconFilename: "{app}\desktop\wechat-assistant-v2.ico"
Name: "{userdesktop}\微信助手"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\desktop\launch_tray.ps1"" -Show"; WorkingDir: "{app}"; IconFilename: "{app}\desktop\wechat-assistant-v2.ico"; Tasks: desktopicon

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""{app}\desktop\launch_tray.ps1"" -Show"; WorkingDir: "{app}"; Description: "启动微信助手"; Flags: postinstall nowait skipifsilent

[Code]
var
  RemovePersonalData: Boolean;

function RunHelper(Arguments: String): Boolean;
var
  ExitCode: Integer;
begin
  Result := Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\desktop\uninstall.ps1') + '" ' + Arguments,
    ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ExitCode);
  if Result then Result := ExitCode = 0;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\desktop\uninstall.ps1')) then
    if not RunHelper('-StopOnly -Quiet') then
      Result := '程序未能完全停止。请结束正在进行的任务并退出微信助手后重试。';
end;

function InitializeUninstall(): Boolean;
begin
  RemovePersonalData := False;
  if not UninstallSilent then
    RemovePersonalData := MsgBox('是否同时删除个人数据（配置、固定记忆、任务记录）？' + #13#10 +
      '选择“否”可供重新安装后继续使用。工作目录和收件目录不会删除。',
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  { Stop failures must abort before Inno starts deleting registered files. }
  Result := RunHelper('-StopOnly -Quiet');
  if not Result then
    MsgBox('程序未能完全停止，卸载已取消。请关闭微信助手后重试。', mbError, MB_OK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Arguments: String;
  I: Integer;
  Directories: TArrayOfString;
begin
  if CurUninstallStep = usUninstall then begin
    Arguments := '-Quiet';
    if RemovePersonalData then Arguments := Arguments + ' -RemoveData';
    if not RunHelper(Arguments) then
      RaiseException('清理失败，卸载已停止。请关闭微信助手后重试。');
  end;
  if CurUninstallStep = usPostUninstall then begin
    Directories := ['app', 'desktop', 'runtime', 'tests', 'tools', 'docs', '.github', 'installer', '__pycache__'];
    for I := 0 to GetArrayLength(Directories) - 1 do
      DelTree(ExpandConstant('{app}\') + Directories[I], True, True, True);
    { Keep any user-selected work/inbox or unknown files inside the install folder. }
    RemoveDir(ExpandConstant('{app}'));
  end;
end;
