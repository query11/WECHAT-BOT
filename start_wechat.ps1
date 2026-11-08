# start_wechat.ps1 -- run the WeChat (iLink) -> Claude bridge on Windows.
# ASCII only on purpose: PowerShell here often mis-decodes non-ASCII script files.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File start_wechat.ps1 -Login   # scan QR, save creds
#   powershell -ExecutionPolicy Bypass -File start_wechat.ps1 -Check   # config sanity only
#   powershell -ExecutionPolicy Bypass -File start_wechat.ps1          # start polling

param([switch]$Login, [switch]$Check)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

# the bridge logs in Chinese; make sure the console does not mangle it
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Fail($msg) { Write-Host "[FAIL] $msg" -ForegroundColor Red; exit 1 }
function Info($msg) { Write-Host "[INFO] $msg" -ForegroundColor Cyan }

. (Join-Path $here "python_runtime.ps1")
$py = Resolve-WeChatPython $here
if (-not $py) { Fail "python not found (bundled runtime or PATH). Install Python 3.10+ first." }

if (-not (Test-Path (Join-Path $here "config.json"))) {
    Fail "config.json missing. Copy config.example.json to config.json and fill in workdir."
}

# no third-party packages needed for the iLink transport -- stdlib only

if ($Login) {
    Info "starting QR login (a PNG will be written next to this script)"
    & $py (Join-Path $here "wechat_bridge.py") "login"
    exit $LASTEXITCODE
}

if ($Check) {
    & $py (Join-Path $here "wechat_bridge.py") "check"
    exit $LASTEXITCODE
}

$claude = Get-Command claude -ErrorAction SilentlyContinue
if (-not $claude) {
    Write-Host "[WARN] claude not found in PATH. The bridge will start, but every task fails." -ForegroundColor Yellow
    Write-Host "       Install Claude Code CLI, or set claude_bin in config.json." -ForegroundColor Yellow
}

Info "config check"
& $py (Join-Path $here "wechat_bridge.py") "check"

Info "long-polling WeChat. No tunnel or public IP needed. Ctrl+C to stop."
& $py (Join-Path $here "wechat_bridge.py") "run"
