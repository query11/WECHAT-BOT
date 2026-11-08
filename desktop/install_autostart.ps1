# Install a current-user logon shortcut. No stored password needed.
param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startupDir = [Environment]::GetFolderPath('Startup')
if (-not $startupDir) { throw 'Current-user Startup folder unavailable.' }
$shortcutPath = Join-Path $startupDir 'WeChatClaudeStandby.lnk'
if ($Remove) {
    if (Test-Path -LiteralPath $shortcutPath) { Remove-Item -LiteralPath $shortcutPath }
    Write-Host 'Autostart removed; the current bridge process is unchanged.'
    exit 0
}
$scriptPath = Join-Path $projectDir 'launch_tray.ps1'
if (-not (Test-Path -LiteralPath $scriptPath)) { throw 'launch_tray.ps1 missing.' }
$shellObject = New-Object -ComObject WScript.Shell
$shortcut = $shellObject.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$shortcut.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $scriptPath + '"'
$shortcut.WorkingDirectory = Split-Path -Parent $projectDir
$shortcut.WindowStyle = 7
$shortcut.Description = 'WeChat assistant background standby at user logon'
$shortcut.Save()
$verified = $shellObject.CreateShortcut($shortcutPath)
if ($verified.Arguments -ne $shortcut.Arguments) { throw 'Shortcut verification failed.' }
Write-Host ('Autostart installed: ' + $shortcutPath)
