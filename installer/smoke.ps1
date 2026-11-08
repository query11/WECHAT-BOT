# Real installation lifecycle, using synthetic data in a unique temporary folder.
param([string]$Setup = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$version = (Get-Content (Join-Path $root 'VERSION') -Raw).Trim()
if (-not $Setup) { $Setup = Join-Path $root ('dist\WeChatAssistant-Setup-' + $version + '.exe') }
$logs = Join-Path $root '.runtime\installer-smoke'
[IO.Directory]::CreateDirectory($logs) | Out-Null
$base = Join-Path ([IO.Path]::GetTempPath()) ('wechat-install-' + [Guid]::NewGuid().ToString('N'))
$install = Join-Path $base ('App with spaces ' + [char]0x5FAE + [char]0x4FE1)
$external = Join-Path $base 'external-work'
[IO.Directory]::CreateDirectory($external) | Out-Null
[IO.File]::WriteAllText((Join-Path $external 'keep.txt'), 'user work')
$python = Join-Path $install 'runtime\python\python.exe'
$ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
function Assert($condition, $message) { if (-not $condition) { throw $message } }
function Run-Exe($path, $arguments) {
    $proc = Start-Process -FilePath $path -ArgumentList $arguments -WindowStyle Hidden -PassThru
    if (-not $proc.WaitForExit(180000)) { throw ('Timed out: ' + $path) }
    Assert ($proc.ExitCode -eq 0) ('Exit code ' + $proc.ExitCode + ': ' + $path)
}
function Install($label) {
    Run-Exe $Setup ('/VERYSILENT /SUPPRESSMSGBOXES /CURRENTUSER /NORESTART /TASKS="desktopicon" /DIR="' + $install + '" /LOG="' + (Join-Path $logs ($label + '.log')) + '"')
}
function Uninstall($label) {
    Run-Exe (Join-Path $install 'unins000.exe') ('/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="' + (Join-Path $logs ($label + '.log')) + '"')
    foreach ($name in @('app','desktop','runtime','tests','tools','docs','.github','installer','__pycache__')) {
        Assert (-not (Test-Path -LiteralPath (Join-Path $install $name))) ('Program directory remains: ' + $name)
    }
    Assert (-not (Test-Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\{7B90F140-0A4A-4EC0-BE50-786F15A7C6A5}_is1')) 'Uninstall registration remains'
    Assert ((Get-Content -LiteralPath (Join-Path $external 'keep.txt') -Raw) -eq 'user work') 'External work was changed'
}
function Helper($arguments) {
    Run-Exe $ps ('-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $install 'desktop\uninstall.ps1') + '" ' + $arguments)
}
$owned = $null
$foreign = $null
try {
    Install 'install'
    foreach ($name in @('app\wechat_bridge.py','app\onboarding.py','desktop\launch_tray.ps1','runtime\python\python.exe','VERSION','LICENSE','unins000.exe')) {
        Assert (Test-Path -LiteralPath (Join-Path $install $name)) ('Missing installed file: ' + $name)
    }
    Assert (-not (Test-Path -LiteralPath (Join-Path $install 'config.json'))) 'Installer contains personal configuration'
    & $python -c 'import segno,pip,sqlite3,ctypes,wechat_bridge'
    Assert ($LASTEXITCODE -eq 0) 'Installed Python imports failed'
    $reply = '{"operation":"status"}' | & $python (Join-Path $install 'app\onboarding.py') --root $install
    Assert ($LASTEXITCODE -eq 0) 'Installed onboarding failed'
    $status = $reply | ConvertFrom-Json
    Assert ($status.ok -and $status.python.ok -and $status.qr_library) 'Installed onboarding status failed'
    $reply | Set-Content -LiteralPath (Join-Path $logs 'onboarding.json') -Encoding UTF8
    Run-Exe $ps ('-NoProfile -STA -ExecutionPolicy Bypass -File "' + (Join-Path $install 'desktop\tray_app.ps1') + '" -VerifyUX')
    $shell = New-Object -ComObject WScript.Shell
    $shortcuts = @()
    foreach ($folder in @('Programs','Desktop')) {
        $shortcuts += @(Get-ChildItem -LiteralPath ([Environment]::GetFolderPath($folder)) -Filter '*.lnk' | Where-Object {
            $link = $shell.CreateShortcut($_.FullName)
            $link.Arguments.Contains((Join-Path $install 'desktop\launch_tray.ps1'))
        })
    }
    Assert ($shortcuts.Count -eq 2) 'Start Menu and optional desktop shortcuts missing'
    foreach ($shortcut in $shortcuts) {
        Assert ($shell.CreateShortcut($shortcut.FullName).WorkingDirectory -eq $install) 'Shortcut working directory wrong'
    }
    $cfg = @{workdir=$external; inbox=@{dir=$external}; smoke='preserve me'} | ConvertTo-Json
    [IO.File]::WriteAllText((Join-Path $install 'config.json'), $cfg)
    [IO.File]::WriteAllText((Join-Path $install 'BOT_MEMORY.md'), 'personal memory')
    [IO.Directory]::CreateDirectory((Join-Path $install '.runtime')) | Out-Null
    [IO.File]::WriteAllText((Join-Path $install '.runtime\tasks.sqlite'), 'synthetic tasks')
    [IO.File]::WriteAllText((Join-Path $install 'VERSION'), '0.0.0')
    # A real running embedded process must be stopped before its files are replaced.
    [IO.File]::WriteAllText((Join-Path $install 'app\wechat_bridge.py'), 'import time; time.sleep(150)')
    $owned = Start-Process $python -ArgumentList ('"' + (Join-Path $install 'app\wechat_bridge.py') + '" run') -WindowStyle Hidden -PassThru
    $other = Join-Path $base 'other\app'
    [IO.Directory]::CreateDirectory($other) | Out-Null
    [IO.File]::WriteAllText((Join-Path $other 'wechat_bridge.py'), 'import time; time.sleep(150)')
    $foreign = Start-Process (Get-Command python -CommandType Application).Source -ArgumentList ('"' + (Join-Path $other 'wechat_bridge.py') + '" run') -WindowStyle Hidden -PassThru
    Install 'upgrade'
    Assert ($owned.WaitForExit(1000)) 'Upgrade failed to stop its backend'
    Assert (-not $foreign.HasExited) 'Upgrade killed another installation'
    Assert ((Get-Content -LiteralPath (Join-Path $install 'VERSION') -Raw).Trim() -eq $version) 'Upgrade did not replace program files'
    Assert ((Get-Content -LiteralPath (Join-Path $install 'config.json') -Raw) -eq $cfg) 'Upgrade changed personal config'
    Assert ((Get-Content -LiteralPath (Join-Path $install 'BOT_MEMORY.md') -Raw) -eq 'personal memory') 'Upgrade changed memory'
    Assert ((Get-Content -LiteralPath (Join-Path $install '.runtime\tasks.sqlite') -Raw) -eq 'synthetic tasks') 'Upgrade changed task history'
    [IO.File]::WriteAllText((Join-Path $install 'app\added-by-update.py'), '# not in Inno file log')
    Helper '-StopOnly -Quiet'
    Uninstall 'uninstall-preserve'
    foreach ($shortcut in $shortcuts) { Assert (-not (Test-Path -LiteralPath $shortcut.FullName)) 'Shortcut remains' }
    Assert ((Get-Content -LiteralPath (Join-Path $install 'config.json') -Raw) -eq $cfg) 'Silent uninstall removed config'
    Assert ((Get-Content -LiteralPath (Join-Path $install 'BOT_MEMORY.md') -Raw) -eq 'personal memory') 'Silent uninstall removed memory'
    Assert ((Get-Content -LiteralPath (Join-Path $install '.runtime\tasks.sqlite') -Raw) -eq 'synthetic tasks') 'Silent uninstall removed history'
    Install 'reinstall'
    Assert ((Get-Content -LiteralPath (Join-Path $install 'config.json') -Raw) -eq $cfg) 'Reinstall changed config'
    # Exercise the same explicit removal helper used by the interactive Yes choice.
    Helper '-RemoveData -Quiet'
    foreach ($name in @('config.json','BOT_MEMORY.md','.runtime\tasks.sqlite')) {
        Assert (-not (Test-Path -LiteralPath (Join-Path $install $name))) ('Personal data remains: ' + $name)
    }
    Uninstall 'uninstall-remove'
    Assert (-not $foreign.HasExited) 'Uninstall killed another installation'
    'Installation, embedded imports, UI, running-process upgrade, data preservation, removal, shortcuts and isolation passed.' | Set-Content (Join-Path $logs 'result.txt')
} finally {
    foreach ($process in @($owned,$foreign)) {
        if ($process -and -not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
    }
    # Keep the unique test fixture for diagnosis on failure; CI discards the runner.
    Write-Host ('Smoke fixture: ' + $base)
}
