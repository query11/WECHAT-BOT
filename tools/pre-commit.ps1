# Fast offline checks for the git pre-commit hook. Installed by install_hooks.ps1.
# Offline and desktop-free: no real WeChat session, no model call, no GUI required.
# ASCII only on purpose: Windows PowerShell 5.1 mis-decodes non-ASCII .ps1 files.
Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host '[pre-commit] python not found in PATH; skipping (needs Python 3.10+).' -ForegroundColor Yellow
    exit 0
}

# A docs-only commit does not need these. A manual run (nothing staged) always does.
# .ps1/.cs count too: toggle.ps1 and tray_app.ps1 are product code, and the subset
# drives process_bootstrap.ps1 directly.
# Python reads Git's NUL-separated bytes, including deleted and renamed paths.
& python (Join-Path $PSScriptRoot 'pre_commit.py')
$selection = $LASTEXITCODE
if ($selection -eq 1) {
    Write-Host '[pre-commit] no code or config changes staged; skipping fast checks.'
    exit 0
}
if ($selection -ne 0) {
    Write-Host '[pre-commit] staged-path check failed; commit blocked.' -ForegroundColor Red
    exit 1
}

$modules = @(
    'tests.selftest_network_route',    # connection route and the encrypted-DNS fallback
    'tests.selftest_runtime',          # tray status and the reconnect notice window
    'tests.selftest_experience',       # plain-language faults and recovery
    'tests.selftest_standby',          # wake / standby gating
    'tests.selftest_reliability',      # config, queue and draining
    'tests.selftest_bot_token',        # bot_token DPAPI storage
    'tests.selftest_key_vault',        # DPAPI primitives
    'tests.selftest_backend_switch',   # saved backend selection
    'tests.selftest_backend_profile',  # backend profile editor
    'tests.selftest_hooks',            # staged-path classification and failure propagation
    'tests.selftest_process_isolation.BootstrapRequestTests'   # launch-request guards, no GUI
)
Write-Host ('[pre-commit] fast subset: ' + $modules.Count + ' modules, about 5 seconds')
# 2>&1 inside cmd keeps PowerShell from turning unittest's stderr into errors.
& cmd /c ("python -m unittest " + ($modules -join ' ') + " 2>&1")
if ($LASTEXITCODE -ne 0) {
    Write-Host '[pre-commit] fast checks FAILED; commit blocked (bypass: git commit --no-verify).' -ForegroundColor Red
    exit 1
}
Write-Host '[pre-commit] fast checks passed.' -ForegroundColor Green
exit 0
