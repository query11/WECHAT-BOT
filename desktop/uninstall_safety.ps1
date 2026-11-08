# Guard all paths before Inno or the cleanup helper can remove anything.
function Assert-WeChatUninstallSafe([string]$Root) {
    $Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $managed = @('app','desktop','runtime','tests','tools','docs','.github','installer','__pycache__','.runtime','shots','_bak')
    $config = Join-Path $Root 'config.json'
    if (Test-Path -LiteralPath $config) {
        $cfg = Get-Content -LiteralPath $config -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($value in @($cfg.workdir, $cfg.inbox.dir)) {
            if (-not $value) { continue }
            $path = [Environment]::ExpandEnvironmentVariables([string]$value)
            if (-not [IO.Path]::IsPathRooted($path)) { $path = Join-Path $Root $path }
            $path = [IO.Path]::GetFullPath($path).TrimEnd('\')
            foreach ($name in $managed) {
                $target = Join-Path $Root $name
                if ($path.Equals($target, [StringComparison]::OrdinalIgnoreCase) -or
                    $path.StartsWith($target + '\', [StringComparison]::OrdinalIgnoreCase) -or
                    $target.StartsWith($path + '\', [StringComparison]::OrdinalIgnoreCase)) {
                    throw ('Work/inbox overlaps program or data files. Move it and update config before uninstall/upgrade: ' + $path)
                }
            }
        }
    }
    # Refuse junctions/symlinks, including ancestors, without traversing them.
    $ancestor = Get-Item -LiteralPath $Root -Force
    while ($ancestor) {
        if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw ('Reparse point in installation path: ' + $ancestor.FullName) }
        $ancestor = $ancestor.Parent
    }
    $pending = New-Object 'Collections.Generic.Stack[string]'
    foreach ($name in $managed) {
        $path = Join-Path $Root $name
        if (Test-Path -LiteralPath $path) { $pending.Push($path) }
    }
    while ($pending.Count) {
        $item = Get-Item -LiteralPath $pending.Pop() -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw ('Refusing to traverse a reparse point: ' + $item.FullName) }
        if ($item.PSIsContainer) {
            foreach ($child in Get-ChildItem -LiteralPath $item.FullName -Force) { $pending.Push($child.FullName) }
        }
    }
}
