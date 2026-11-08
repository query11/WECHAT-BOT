# Paths and Python interpreter for this program. ASCII only: Windows PowerShell 5.1 reads it.
# The scripts live in desktop\, the Python modules in app\, and configuration, memory and
# .runtime\ in the project root above them ($WeChatHome).
# An installed copy may bundle Python under runtime\python (see docs/reference);
# a developer copy uses the python on PATH. Every launcher resolves it here.
$WeChatHome = Split-Path -Parent $PSScriptRoot
$WeChatApp = Join-Path $WeChatHome 'app'
function Resolve-WeChatPython {
    param([string]$ProjectRoot = $WeChatHome)
    $bundled = Join-Path $ProjectRoot 'runtime\python\python.exe'
    if (Test-Path -LiteralPath $bundled) { return $bundled }
    $found = Get-Command python -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { return $found.Source }
    return $null
}
$WeChatPython = Resolve-WeChatPython $WeChatHome
if (-not $WeChatPython) { $WeChatPython = 'python' }
