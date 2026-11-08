# Launch the unchanged integration assertions from the existing Explorer desktop.
# Task Scheduler also applies a non-breakaway Job Object on hosted runners.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python -CommandType Application | Select-Object -First 1).Source
$report = Join-Path $root '.runtime\verification\github-integration\report.json'
if (Test-Path -LiteralPath $report) { throw 'Integration report already exists' }
. (Join-Path $root 'desktop\process_launcher.ps1')
$env:PYTHONIOENCODING = 'utf-8'
$process = Start-WeChatIndependentProcess $python ('-X utf8 "' + (Join-Path $root 'tools\verify.py') + '" --suite integration --label github-integration') $root
try {
    if (-not $process.WaitForExit(360000)) { throw 'Independent integration timed out' }
    if (-not (Test-Path -LiteralPath $report)) { throw 'Independent integration did not produce a report' }
    $result = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
    $result.results | Select-Object id,status,counts | Format-List
    if (-not $result.ok -or @($result.results | Where-Object status -ne 'passed').Count) {
        throw 'Independent integration tests failed; see uploaded report and logs'
    }
} finally {
    if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit() }
    $process.Dispose()
}
