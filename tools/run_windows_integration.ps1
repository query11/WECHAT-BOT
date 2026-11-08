# Task Scheduler starts outside the GitHub runner's non-breakaway Job Object.
# Run the unchanged integration assertions in the current interactive session.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = (Get-Command python -CommandType Application).Source
$name = 'WeChatIntegration-' + [Guid]::NewGuid().ToString('N')
$report = Join-Path $root '.runtime\verification\github-integration\report.json'
if (Test-Path -LiteralPath $report) { throw 'Integration report already exists' }
$action = New-ScheduledTaskAction -Execute $python -Argument ('"' + (Join-Path $root 'tools\verify.py') + '" --suite integration --label github-integration') -WorkingDirectory $root
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 6)
try {
    Register-ScheduledTask -TaskName $name -Action $action -Principal $principal -Settings $settings | Out-Null
    Start-ScheduledTask -TaskName $name
    $deadline = [DateTime]::UtcNow.AddMinutes(6)
    do {
        Start-Sleep -Seconds 2
        if ((Test-Path -LiteralPath $report) -and (Get-ScheduledTask -TaskName $name).State -ne 'Running') { break }
    } while ([DateTime]::UtcNow -lt $deadline)
    if (-not (Test-Path -LiteralPath $report)) { throw 'Independent integration task did not produce a report' }
    $result = Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
    $result.results | Select-Object id,status,counts | Format-List
    if ((Get-ScheduledTaskInfo -TaskName $name).LastTaskResult -ne 0 -or @($result.results | Where-Object status -ne 'passed').Count) {
        throw 'Independent integration tests failed; see uploaded report and logs'
    }
} finally {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
}
