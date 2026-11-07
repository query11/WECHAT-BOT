# toggle.ps1 -- one switch: turn the WeChat bridge ON if it is off, OFF if it is on.
# ASCII only on purpose: PowerShell here often mis-decodes non-ASCII script files.
#
# Usage (or just double-click the .bat next to this file):
#   powershell -ExecutionPolicy Bypass -File toggle.ps1            # toggle
#   powershell -ExecutionPolicy Bypass -File toggle.ps1 -Status    # report only
#   powershell -ExecutionPolicy Bypass -File toggle.ps1 -Off       # force stop
#   powershell -ExecutionPolicy Bypass -File toggle.ps1 -DebugLog  # start with BRIDGE_DEBUG=1
#
# -DebugLog, not -Debug: PowerShell treats -Debug as a reserved common parameter.

param([switch]$Status, [switch]$On, [switch]$Off, [switch]$DebugLog)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

# bridge.log holds Chinese text; without this the tail printed on failure is mojibake
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$pidFile = Join-Path $here "bridge.pid"
$logFile = Join-Path $here "bridge.log"

function Info($m) { Write-Host "[INFO] $m" -ForegroundColor Cyan }
function Good($m) { Write-Host "[ ON ] $m" -ForegroundColor Green }
function Gone($m) { Write-Host "[OFF ] $m" -ForegroundColor Yellow }
function Fail($m) { throw $m }

# Finds a bridge that was started some other way (start_wechat.ps1, or by hand)
# and therefore left no pid file. Without this check the switch would happily
# start a second one, and two pollers would fight over the same cursor.
function Find-Stray {
    try {
        $rows = Get-CimInstance Win32_Process -Filter "Name like 'python%'" -ErrorAction Stop
    } catch {
        throw "Cannot inspect bridge processes; refusing to start a duplicate."
    }
    foreach ($r in $rows) {
        if ($r.CommandLine -and $r.CommandLine -like "*wechat_bridge.py*") {
            return (Get-Process -Id $r.ProcessId -ErrorAction SilentlyContinue)
        }
    }
    return $null
}

# Returns the running bridge process, or $null. Never trust the pid file alone:
# Windows reuses pids, so the process must still look like our python run.
function Get-Bridge {
    # Validate the command line, not only a recycled PID or a python process name.
    $found = Find-Stray
    if (-not $found -and (Test-Path -LiteralPath $pidFile)) {
        Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
    }
    return $found
}

function Stop-Bridge($p) {
    # /T also kills the claude subprocess a running task may have spawned
    & taskkill /PID $($p.Id) /T /F 2>&1 | Out-Null
    Start-Sleep -Milliseconds 400
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    Gone "bridge stopped (pid $($p.Id)). WeChat messages will queue up on Tencent's side."
}

function Start-Bridge {
    if (-not (Test-Path (Join-Path $here "config.json"))) {
        Fail "config.json missing. Copy config.example.json to config.json first."
        return
    }
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        Fail "python not found in PATH. Install Python 3.10+ first."
        return
    }
    # -Encoding UTF8 is REQUIRED: config.json is written by Python as UTF-8 (see
    # bridge_config.save_config) and Windows PowerShell 5.1 decodes files as the
    # system ANSI code page (gb2312 here) unless told otherwise. With enough Chinese
    # in prompt_prefix that mis-decoding swallows a closing quote and ConvertFrom-Json
    # dies with "invalid object, expected ':' or '}'" -- hit for real on 2026-09-26.
    # With -Encoding UTF8 it reads correctly whether or not the file has a BOM.
    # Keep this file ASCII-only (comments included): PowerShell mis-decodes
    # non-ASCII script files here -- see the note at the top of this file.
    $cfg = Get-Content (Join-Path $here "config.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $cfg.bot_token) {
        Fail "not logged in yet. Run: powershell -ExecutionPolicy Bypass -File start_wechat.ps1 -Login"
        return
    }
    if (-not $cfg.allowed_user_ids -or $cfg.allowed_user_ids.Count -eq 0) {
        Info "allowed_user_ids is empty: the bridge will only reply with your user id."
    }

    $env:PYTHONIOENCODING = "utf-8"
    if ($DebugLog) { $env:BRIDGE_DEBUG = "1" } else { $env:BRIDGE_DEBUG = "" }

    # Use a relative script name under the project working directory so spaces in
    # the project path do not split Python's script argument.
    . (Join-Path $here "process_launcher.ps1")
    $pythonPath = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $p = Start-WeChatIndependentProcess $pythonPath "wechat_bridge.py run" $here
    Set-Content -Path $pidFile -Value $p.Id -Encoding ASCII

    # a bad config dies within a second or two -- catch that here instead of
    # telling the user "started" and letting them find out from WeChat silence
    Start-Sleep -Seconds 3
    if ($p.HasExited) {
        Remove-Item $pidFile -ErrorAction SilentlyContinue
        Fail "bridge exited right after starting (code $($p.ExitCode)). Tail of bridge.log:"
        # bridge.log is UTF-8; without -Encoding UTF8 the Chinese lines come out as mojibake
        if (Test-Path $logFile) {
            Get-Content $logFile -Tail 8 -Encoding UTF8 | ForEach-Object { "       $_" }
        }
        return
    }
    $dbg = if ($DebugLog) { " (BRIDGE_DEBUG=1)" } else { "" }
    Good "bridge running, pid $($p.Id)$dbg. Running in the background."
    if ($cfg.standby_on_start) {
        Info "Standby enabled. Send the wake command in WeChat first (see usage guide)."
    } else {
        Info "workdir $($cfg.workdir); send '/cc ...' in WeChat now."
    }
}

$switchMutex = New-Object System.Threading.Mutex($false, 'Local\WeChatClaudeBridgeSwitch')
try { $locked = $switchMutex.WaitOne(15000) } catch [System.Threading.AbandonedMutexException] { $locked = $true }
if (-not $locked) { throw 'Another bridge switch is running; try again shortly.' }
try {
$proc = Get-Bridge

if ($Status) {
    if ($proc) { Good "running, pid $($proc.Id)" } else { Gone "not running" }
    exit 0
}

if ($Off) {
    if ($proc) { Stop-Bridge $proc } else { Gone "was not running anyway" }
    exit 0
}

if ($On) {
    if ($proc) { Good "already running, pid $($proc.Id)" } else { Start-Bridge }
} else {
    if ($proc) { Stop-Bridge $proc } else { Start-Bridge }
}

} finally {
    $switchMutex.ReleaseMutex()
    $switchMutex.Dispose()
}
