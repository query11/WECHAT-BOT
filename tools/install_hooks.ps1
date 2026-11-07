# Install this repository's pre-commit hook. The hook body lives in tools/ (version
# controlled); .git/hooks is not, so this installer is the reproducible source.
# Re-runnable. Refuses to overwrite a hook it did not write unless -Force is given.
# ASCII only on purpose: Windows PowerShell 5.1 mis-decodes non-ASCII .ps1 files.
#
# The target directory is asked of Git (git rev-parse --git-path hooks) instead of
# being guessed as "<root>\.git\hooks". That guess is wrong for a linked worktree,
# where .git is a file, and wrong when core.hooksPath is configured.
# The repository installed into is always the one that owns THIS script
# ($PSScriptRoot -> show-toplevel), so running the installer from an unrelated
# current directory cannot install into that directory's repository.
param([switch]$Force)

$ErrorActionPreference = 'Stop'
$marker = 'wechat-bridge pre-commit hook'

function Write-Failure([string]$message) {
    Write-Output ('install_hooks: ' + $message)
    Write-Output 'install_hooks: nothing was written.'
    # Keep the reason visible on stderr too; Write-Error would abort under
    # ErrorActionPreference = 'Stop'.
    [Console]::Error.WriteLine('install_hooks: ' + $message)
}

# Run git in $startDir and return its trimmed stdout, or $null when it fails.
# Native stderr must not become a terminating error under ErrorActionPreference.
function Invoke-GitProbe([string]$startDir, [string[]]$gitArgs) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $text = $null
    $code = -1
    try {
        $text = (& git -C $startDir @gitArgs 2>$null | Out-String).Trim()
        $code = $LASTEXITCODE
    } catch {
        $code = -1
    } finally {
        $ErrorActionPreference = $saved
    }
    if ($code -ne 0) { return $null }
    return $text
}

# 1. The repository is the one that owns this script, not the caller's cwd.
$probeDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($probeDir)) {
    $probeDir = Split-Path -Parent $PSCommandPath
}
if ([string]::IsNullOrWhiteSpace($probeDir)) {
    Write-Failure 'cannot determine where this script lives ($PSScriptRoot/$PSCommandPath are empty).'
    exit 1
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Failure 'git was not found in PATH, so the hooks directory cannot be located.'
    exit 1
}

$root = Invoke-GitProbe $probeDir @('rev-parse', '--show-toplevel')
if ([string]::IsNullOrWhiteSpace($root)) {
    Write-Failure ('no git repository owns ' + $probeDir + '; refusing to guess a hooks directory.')
    exit 1
}
$root = $root.Replace('/', '\')

# 2. Ask Git for the effective hooks directory. It already honours the linked-worktree
#    layout (.git as a file) and both relative and absolute core.hooksPath values.
$hookDir = Invoke-GitProbe $probeDir @('rev-parse', '--path-format=absolute', '--git-path', 'hooks')
if ([string]::IsNullOrWhiteSpace($hookDir)) {
    # Git before 2.31 has no --path-format and fails with "unknown argument", so
    # ask again without it.
    $hookDir = Invoke-GitProbe $probeDir @('rev-parse', '--git-path', 'hooks')
}
if (-not [string]::IsNullOrWhiteSpace($hookDir)) {
    $hookDir = $hookDir.Replace('/', '\')
    # Without --path-format the answer is relative to the directory the query ran
    # in, which is $probeDir because every query passes -C. Never trust a relative
    # answer against the caller's cwd.
    if (-not [IO.Path]::IsPathRooted($hookDir)) {
        $hookDir = [IO.Path]::GetFullPath((Join-Path $probeDir $hookDir))
    }
}
if ([string]::IsNullOrWhiteSpace($hookDir)) {
    Write-Failure ('git rev-parse --git-path hooks failed for ' + $root + '; refusing to guess a hooks directory.')
    exit 1
}
$hookPath = Join-Path $hookDir 'pre-commit'

# Single-quoted so nothing here is expanded by PowerShell: the shim must compute the
# worktree root at commit time and must not contain a machine path.
$content = @'
#!/bin/sh
# __MARKER__
# Installed by tools/install_hooks.ps1 -- edit tools/pre-commit.ps1 instead.
# The worktree root is resolved when the hook runs, so this shim follows the
# worktree the commit happens in and hardcodes no machine path.
root=$(git rev-parse --show-toplevel) || exit 1
exec powershell -NoProfile -ExecutionPolicy Bypass -File "$root/tools/pre-commit.ps1"
'@
$content = $content.Replace('__MARKER__', $marker)

try {
    New-Item -ItemType Directory -Force -Path $hookDir | Out-Null
} catch {
    Write-Failure ('cannot create ' + $hookDir + ': ' + $_.Exception.Message + ' (target hook: ' + $hookPath + ')')
    exit 1
}

# 3. Never overwrite a hook this project did not write, unless -Force is given.
if (Test-Path -LiteralPath $hookPath) {
    $existing = [IO.File]::ReadAllText($hookPath)
    if (-not $Force -and $existing -notlike "*$marker*") {
        Write-Failure ('existing pre-commit hook was not installed by this project: ' + $hookPath + ' (use -Force to replace)')
        exit 1
    }
}

# sh needs LF and no BOM, so do not use Set-Content here.
try {
    [IO.File]::WriteAllText($hookPath, ($content.Replace("`r`n", "`n")),
        (New-Object Text.UTF8Encoding($false)))
} catch {
    Write-Failure ('cannot write ' + $hookPath + ': ' + $_.Exception.Message)
    exit 1
}
Write-Output "installed $hookPath"
Write-Output 'it calls tools/pre-commit.ps1 (fast subset, about 5 seconds)'
Write-Output 'temporary bypass: git commit --no-verify'
exit 0
