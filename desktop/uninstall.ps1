# uninstall.ps1 -- stop the WeChat assistant and remove its autostart entry.
# ASCII only on purpose (Windows PowerShell 5.1 reads scripts in the ANSI code page).
#
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1               # keep personal data
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 -RemoveData   # also delete it
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 -StopOnly     # just close it (installer upgrade)
#
# Program files are left to the installer's own uninstaller (or the user, for a zip copy).
# The configured work folder and inbox folder are never touched: they hold the user's files.

param([switch]$RemoveData, [switch]$StopOnly, [switch]$Quiet)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$here = Split-Path -Parent $scriptDir   # project root: personal data lives here
$runtime = Join-Path $here '.runtime'

function Say($m) { if (-not $Quiet) { Write-Host $m } }

# 1. Ask the tray to close itself, then stop the bridge.
try {
    if (-not (Test-Path -LiteralPath $runtime)) { New-Item -ItemType Directory -Path $runtime | Out-Null }
    Set-Content -LiteralPath (Join-Path $runtime 'tray-exit') -Value 'exit'
} catch { Say 'Could not signal the tray window; it may already be closed.' }

try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scriptDir 'toggle.ps1') -Off | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ('toggle.ps1 exit code ' + $LASTEXITCODE) }
    Say 'Background service stopped.'
} catch { Say ('Could not stop the background service: ' + $_.Exception.Message) }

$deadline = (Get-Date).AddSeconds(10)
while ((Get-Date) -lt $deadline) {
    $tray = $null
    if (-not [Threading.Mutex]::TryOpenExisting('Local.WeChatAssistantTray', [ref]$tray)) { break }
    $tray.Dispose()
    Start-Sleep -Milliseconds 300
}

if ($StopOnly) {
    Remove-Item -LiteralPath (Join-Path $runtime 'tray-exit') -ErrorAction SilentlyContinue
    exit 0
}

# 2. Remove the logon shortcut.
try {
    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $scriptDir 'install_autostart.ps1') -Remove | Out-Null
    if ($LASTEXITCODE -ne 0) { throw ('install_autostart.ps1 exit code ' + $LASTEXITCODE) }
    Say 'Autostart removed.'
} catch { Say ('Could not remove autostart: ' + $_.Exception.Message) }

# 3. Personal data, only when asked.
if (-not $RemoveData) {
    Remove-Item -LiteralPath (Join-Path $runtime 'tray-exit') -ErrorAction SilentlyContinue
    Say 'Personal data kept (config.json, BOT_MEMORY.md, task history).'
    exit 0
}

$names = @('config.json', 'BOT_MEMORY.md', 'sessions.json', 'inbox.json', 'meetings.json',
           'reminders.json', 'app_targets.json', 'app_preferences.json', 'bridge.pid',
           'get_updates_buf.txt', 'send_style.txt', 'ilink_login_raw.json', 'relay.log',
           'bridge_debug.log', 'selftest-latest.txt', 'tray-preview.png')
$patterns = @('config.json.*', 'BOT_MEMORY.md.*', 'bridge.log*', 'ilink_qrcode.*',
              'meetings.json.*', 'reminders.json.*', 'inbox.json.*', 'app_preferences.json.*')
$dirs = @('.runtime', 'shots', '_bak')

$failed = 0
$targets = @()
foreach ($n in $names) { $targets += Join-Path $here $n }
foreach ($p in $patterns) {
    foreach ($f in @(Get-ChildItem -LiteralPath $here -Filter $p -File -Force -ErrorAction SilentlyContinue)) { $targets += $f.FullName }
}
foreach ($t in ($targets | Select-Object -Unique)) {
    if (Test-Path -LiteralPath $t -PathType Leaf) {
        try { Remove-Item -LiteralPath $t -Force } catch { $failed++ }
    }
}
foreach ($d in $dirs) {
    $path = Join-Path $here $d
    if (Test-Path -LiteralPath $path -PathType Container) {
        try { Remove-Item -LiteralPath $path -Recurse -Force } catch { $failed++ }
    }
}
if ($failed) {
    Say ('Personal data removed, but ' + $failed + ' item(s) were in use and remain. Close the assistant and run again.')
    exit 1
}
Say 'Personal data removed. Your work folder and inbox folder were not touched.'
exit 0
