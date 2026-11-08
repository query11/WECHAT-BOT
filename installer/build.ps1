# Build from committed source only. Run with Windows PowerShell 5.1 or newer.
param([string]$Ref = 'HEAD', [string]$ISCC = '', [string]$OutputDir = '')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutputDir) { $OutputDir = Join-Path $root 'dist' }
$OutputDir = [IO.Path]::GetFullPath($OutputDir)
[IO.Directory]::CreateDirectory($OutputDir) | Out-Null
$buildRoot = Join-Path $root ('.runtime\installer-build\' + [Guid]::NewGuid().ToString('N'))
$stage = Join-Path $buildRoot 'payload'
[IO.Directory]::CreateDirectory($stage) | Out-Null
$archive = Join-Path $buildRoot 'source.zip'
& git -C $root archive --format=zip ('--output=' + $archive) $Ref
if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
Expand-Archive -LiteralPath $archive -DestinationPath $stage
$version = (Get-Content -LiteralPath (Join-Path $stage 'VERSION') -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'VERSION must be X.Y.Z' }
foreach ($name in @('config.json','BOT_MEMORY.md','.runtime','runtime','sessions.json','inbox.json','bridge.log')) {
    if (Test-Path -LiteralPath (Join-Path $stage $name)) { throw ('Private or generated data in archive: ' + $name) }
}

$pythonVersion = '3.12.10'
# Pinned after obtaining the official archive and published Sigstore digest in CI.
$pythonSha256 = '4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3'
if ($pythonSha256 -notmatch '^[a-f0-9]{64}$') { throw 'Embedded Python SHA256 has not been verified and pinned' }
$pythonUrl = 'https://www.python.org/ftp/python/' + $pythonVersion + '/python-' + $pythonVersion + '-embed-amd64.zip'
$pythonArchive = Join-Path $buildRoot 'python-embed.zip'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -UseBasicParsing -Uri $pythonUrl -OutFile $pythonArchive
if ((Get-FileHash -LiteralPath $pythonArchive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $pythonSha256) { throw 'Embedded Python checksum mismatch' }
$pythonDir = Join-Path $stage 'runtime\python'
[IO.Directory]::CreateDirectory($pythonDir) | Out-Null
Expand-Archive -LiteralPath $pythonArchive -DestinationPath $pythonDir
$pth = "python312.zip`r`n.`r`nLib\site-packages`r`n..\..\app`r`nimport site`r`n"
[IO.File]::WriteAllText((Join-Path $pythonDir 'python312._pth'), $pth, [Text.Encoding]::ASCII)

$buildPython = (Get-Command python -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$site = Join-Path $pythonDir 'Lib\site-packages'
# Only pure Python wheels are allowed in the embedded runtime.
& $buildPython -m pip install --disable-pip-version-check --only-binary=:all: --platform any --implementation py --python-version 3.12 --target $site 'pip==25.0.1' -r (Join-Path $stage 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Bundled dependency installation failed' }
$python = Join-Path $pythonDir 'python.exe'
& $python -c 'import segno, pip, sqlite3, ctypes'
if ($LASTEXITCODE -ne 0) { throw 'Embedded Python import check failed' }
$env:PYTHONIOENCODING = 'utf-8'
$reply = '{"operation":"status"}' | & $python (Join-Path $stage 'app\onboarding.py') --root $stage
if ($LASTEXITCODE -ne 0) { throw 'Onboarding process failed' }
$status = $reply | ConvertFrom-Json
if (-not $status.ok -or -not $status.python.ok -or -not $status.qr_library) { throw 'Embedded onboarding status failed' }
# Status must be read-only in a pristine payload.
foreach ($name in @('config.json','BOT_MEMORY.md','.runtime')) {
    if (Test-Path -LiteralPath (Join-Path $stage $name)) { throw ('Self-check generated personal data: ' + $name) }
}

if (-not $ISCC) {
    $candidates = @()
    foreach ($programDir in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if ($programDir) { $candidates += Get-ChildItem -Path (Join-Path $programDir 'Inno Setup*\ISCC.exe') -File -ErrorAction SilentlyContinue }
    }
    $compiler = $candidates | Sort-Object FullName -Descending | Select-Object -First 1
    if (-not $compiler) { throw 'Install Inno Setup 7.0.2+ or supply -ISCC' }
    $ISCC = $compiler.FullName
}
& $ISCC ('/DAppVersion=' + $version) ('/DPayloadDir=' + $stage) ('/O' + $OutputDir) (Join-Path $stage 'installer\WeChatAssistant.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup compilation failed' }
$setup = Join-Path $OutputDir ('WeChatAssistant-Setup-' + $version + '.exe')
if (-not (Test-Path -LiteralPath $setup)) { throw 'Installer output missing' }
$metadata = @{version=$version; python=$pythonVersion; python_sha256=$pythonSha256; source=(git -C $root rev-parse $Ref); installer=$setup}
$metadata | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $OutputDir 'build-info.json') -Encoding UTF8
Write-Output $setup
