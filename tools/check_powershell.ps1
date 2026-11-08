# Parse with Windows PowerShell 5.1 before executing any program scripts.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$files = @()
foreach ($folder in @('desktop', 'tools', 'installer')) {
    $path = Join-Path $root $folder
    if (Test-Path -LiteralPath $path) { $files += Get-ChildItem -LiteralPath $path -Filter '*.ps1' -File -Recurse }
}
$failed = 0
foreach ($file in $files) {
    $tokens = $null
    $parseErrors = $null
    [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$parseErrors) | Out-Null
    foreach ($item in $parseErrors) {
        Write-Host ($file.Name + ':' + $item.Extent.StartLineNumber + ': ' + $item.Message)
        $failed++
    }
}
if ($failed) { throw ($failed.ToString() + ' PowerShell syntax errors') }
Write-Host ('Parsed ' + $files.Count + ' PowerShell scripts successfully.')
