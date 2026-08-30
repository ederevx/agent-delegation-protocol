$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$CodexHome = [System.IO.Path]::GetFullPath($(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }))
$StateDir = Join-Path $CodexHome '.delegation-protocol'
$State = Join-Path $StateDir 'state'
$Composed = Join-Path $StateDir 'AGENTS.composed.md'
$LegacyComposed = Join-Path $RepoRoot '.runtime\codex\AGENTS.composed.md'
$LegacyBulkHash = '1a53df02818dafb46b90fa0fea2bc840e50fcc4758c4172c40b8f48db23222f4'

function Resolve-Python3 {
    $candidates = @()
    if ($env:CODEX_PYTHON) {
        $candidates += [PSCustomObject]@{ Path = $env:CODEX_PYTHON; Prefix = @() }
    }
    foreach ($name in @('python', 'python3', 'py')) {
        foreach ($command in @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
            $prefix = if ($name -eq 'py') { @('-3') } else { @() }
            $candidates += [PSCustomObject]@{ Path = $command.Source; Prefix = $prefix }
        }
    }
    $bundled = Join-Path $HOME '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    $candidates += [PSCustomObject]@{ Path = $bundled; Prefix = @() }

    $seen = @{}
    foreach ($candidate in $candidates) {
        $candidatePath = [string]$candidate.Path
        if (-not $candidatePath -or $seen.ContainsKey($candidatePath)) { continue }
        $seen[$candidatePath] = $true
        if (-not (Test-Path -LiteralPath $candidatePath -PathType Leaf)) { continue }
        if ($candidatePath -match '\\Microsoft\\WindowsApps\\python3?(\.exe)?$') { continue }
        try {
            $prefixArgs = @($candidate.Prefix)
            $probe = @(& $candidatePath @prefixArgs -c 'import os,sys;assert sys.version_info >= (3,11);print(os.path.abspath(sys.executable))' 2>$null)
            if ($LASTEXITCODE -ne 0) { continue }
            $resolvedLine = $probe | Where-Object { $_ -is [string] -and $_.Trim() } | Select-Object -Last 1
            if (-not $resolvedLine) { continue }
            $resolved = $resolvedLine.Trim()
            if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { continue }
            & $resolved -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return (Get-Item -LiteralPath $resolved).FullName }
        } catch {
            continue
        }
    }
    throw 'Python 3.11 or newer is required to remove protocol-owned Codex hooks safely. Set CODEX_PYTHON to a valid python.exe if it is not discoverable.'
}

$PythonExe = Resolve-Python3

function Remove-IfOurs([string]$Destination, [string]$Expected) {
    if (Test-Path -LiteralPath $Destination) {
        $item = Get-Item -LiteralPath $Destination -Force
        if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Expected) {
            Remove-Item -LiteralPath $Destination -Force
        }
    }
}

function Remove-ManagedCopyIfOurs([string]$Destination, [string]$Expected, [string]$HashPath) {
    $item = Get-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if ($item) {
        if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Expected) {
            Remove-Item -LiteralPath $Destination -Force
        } elseif (-not $item.PSIsContainer -and (Test-Path -LiteralPath $HashPath)) {
            $actualHash = (Get-FileHash -LiteralPath $Destination).Hash.ToLowerInvariant()
            $recordedHash = [System.IO.File]::ReadAllText($HashPath, [System.Text.Encoding]::UTF8).Trim().ToLowerInvariant()
            if ($actualHash -eq $recordedHash) {
                Remove-Item -LiteralPath $Destination -Force
            }
        }
    }
    Remove-Item -LiteralPath $HashPath -Force -ErrorAction SilentlyContinue
}

function Remove-LegacyWorkerIfOurs([string]$Destination, [string]$Expected, [string]$LegacyHash) {
    $item = Get-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if ($item.LinkType -eq 'SymbolicLink') {
        if ($item.Target -contains $Expected) {
            Remove-Item -LiteralPath $Destination -Force
        }
        return
    }
    if (-not $item.PSIsContainer -and
        (Get-FileHash -LiteralPath $Destination).Hash -eq $LegacyHash) {
        Remove-Item -LiteralPath $Destination -Force
    }
}

function Assert-SafeDirectoryIfPresent([string]$Path, [string]$Label) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    $isReparsePoint = ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    if (-not $item.PSIsContainer -or $item.LinkType -or $isReparsePoint) {
        throw "Refusing unsafe $Label (expected a non-reparse directory): $Path"
    }
}

function Assert-LiveCodexHomeIfPresent([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    # CODEX_HOME is an explicitly selected root, so a live directory reparse path is valid.
    $isReparsePoint = ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    if (-not $item.PSIsContainer -or
        ($isReparsePoint -and -not (Test-Path -LiteralPath $Path -PathType Container))) {
        throw "Refusing unsafe Codex home (expected a live directory): $Path"
    }
}

function Assert-SafeFileIfPresent([string]$Path, [string]$Label) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    $isReparsePoint = ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    if ($item.PSIsContainer -or $item.LinkType -or $isReparsePoint) {
        throw "Refusing unsafe $Label (expected a regular file): $Path"
    }
}

$BulkSource = Join-Path $RepoRoot 'codex\agents\bulk_worker.toml'
$LegacyBulkSource = Join-Path $RepoRoot 'codex\agents\bulk-worker.toml'
$BalancedSource = Join-Path $RepoRoot 'codex\agents\balanced-worker.toml'
$HookSource = Join-Path $RepoRoot 'codex\hooks\delegation-enforcer.py'
$MuxSource = Join-Path $RepoRoot 'scripts\agents\mux-scheduler.py'
$ClassifierSource = Join-Path $RepoRoot 'scripts\agents\delegation-classifier.py'
$QueueSource = Join-Path $RepoRoot 'scripts\agents\delegation_queue.py'
$CatalogSource = Join-Path $RepoRoot 'agents\catalog'
$RoutesSource = Join-Path $RepoRoot 'agents\mux-scheduler.json'
$BulkDest = Join-Path $CodexHome 'agents\bulk_worker.toml'
$LegacyBulkDest = Join-Path $CodexHome 'agents\bulk-worker.toml'
$BalancedDest = Join-Path $CodexHome 'agents\balanced-worker.toml'
$HookDest = Join-Path $CodexHome 'hooks\delegation-enforcer.py'
$MuxDest = Join-Path $StateDir 'mux-scheduler.py'
$ClassifierDest = Join-Path $StateDir 'delegation-classifier.py'
$QueueDest = Join-Path $StateDir 'delegation_queue.py'
$CatalogDest = Join-Path $StateDir 'catalog'
$RoutesDest = Join-Path $StateDir 'mux-scheduler.json'

# Refuse path traversal before hook management or any protocol-owned deletion.
Assert-LiveCodexHomeIfPresent $CodexHome
Assert-SafeDirectoryIfPresent $StateDir 'protocol state directory'
Assert-SafeDirectoryIfPresent (Join-Path $CodexHome 'agents') 'agents directory'
Assert-SafeDirectoryIfPresent (Join-Path $CodexHome 'hooks') 'hooks directory'
Assert-SafeFileIfPresent (Join-Path $CodexHome 'hooks.json') 'hooks.json'

& $PythonExe (Join-Path $RepoRoot 'scripts\codex\manage-hooks.py') uninstall --codex-home $CodexHome --hook-path $HookDest --python $PythonExe
if ($LASTEXITCODE -ne 0) { throw "Codex hook uninstallation failed with exit code $LASTEXITCODE" }

Remove-ManagedCopyIfOurs $BulkDest $BulkSource (Join-Path $StateDir 'bulk-worker.sha256')
Remove-LegacyWorkerIfOurs $LegacyBulkDest $LegacyBulkSource $LegacyBulkHash
Remove-IfOurs $BalancedDest $BalancedSource
Remove-IfOurs $HookDest $HookSource
Remove-IfOurs $MuxDest $MuxSource
Remove-IfOurs $ClassifierDest $ClassifierSource
Remove-IfOurs $QueueDest $QueueSource
Remove-IfOurs (Join-Path $StateDir 'multiplexer.py') (Join-Path $RepoRoot 'scripts\agents\multiplexer.py')
Remove-IfOurs $CatalogDest $CatalogSource
Remove-IfOurs $RoutesDest $RoutesSource
Remove-IfOurs (Join-Path $StateDir 'multiplexer.json') (Join-Path $RepoRoot 'agents\multiplexer.json')

$InstructionCleanupComplete = $false
if (Test-Path $State) {
    $stateItem = Get-Item -LiteralPath $State -Force
}
if ($stateItem -and -not $stateItem.PSIsContainer -and -not $stateItem.LinkType) {
    $lines = [System.IO.File]::ReadAllLines($State, [System.Text.Encoding]::UTF8)
    $mode = (($lines | Where-Object { $_ -like 'mode=*' }) -replace '^mode=', '')
    $source = (($lines | Where-Object { $_ -like 'source=*' }) -replace '^source=', '')
    if ($mode -eq 'direct') {
        $agentsPath = Join-Path $CodexHome 'AGENTS.md'
        $agentsItem = Get-Item -LiteralPath $agentsPath -Force -ErrorAction SilentlyContinue
        if (-not $agentsItem) {
            $InstructionCleanupComplete = $true
        } elseif ($agentsItem.LinkType -eq 'SymbolicLink' -and
            $agentsItem.Target -contains (Join-Path $RepoRoot 'codex\AGENTS.md')) {
            Remove-Item -LiteralPath $agentsPath -Force
            $InstructionCleanupComplete = $true
        }
    } elseif ($mode -eq 'composed') {
        $overridePath = Join-Path $CodexHome 'AGENTS.override.md'
        $overrideItem = Get-Item -LiteralPath $overridePath -Force -ErrorAction SilentlyContinue
        $ownedOverride = $overrideItem -and $overrideItem.LinkType -eq 'SymbolicLink' -and
            ($overrideItem.Target -contains $Composed -or $overrideItem.Target -contains $LegacyComposed)
        $saved = Join-Path $StateDir 'original-AGENTS.override.md.path-backup'
        if ($source -eq 'agents') {
            if ($ownedOverride) { Remove-Item -LiteralPath $overridePath -Force }
            if ($ownedOverride -or -not $overrideItem) { $InstructionCleanupComplete = $true }
        } elseif ($source -eq 'override' -and (Test-Path -LiteralPath $saved -PathType Leaf)) {
            if ($ownedOverride) {
                Remove-Item -LiteralPath $overridePath -Force
                $overrideItem = $null
            }
            if (-not $overrideItem -and -not (Test-Path -LiteralPath $overridePath)) {
                Move-Item -LiteralPath $saved -Destination $overridePath
                $InstructionCleanupComplete = $true
            }
        }
    }
}

foreach ($owned in @(
    'hooks.before-first-install.json',
    'hooks-manifest.json'
)) {
    $path = Join-Path $StateDir $owned
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
}
# Per-session turn state was never reaped before this hook gained a sweep, so a
# long-lived home can carry state for every session it ever ran; it is entirely
# protocol-owned, so uninstall removes it outright rather than leaving it.
$HookStateDir = Join-Path $StateDir 'hook-state'
if (Test-Path -LiteralPath $HookStateDir) { Remove-Item -LiteralPath $HookStateDir -Recurse -Force }
$BytecodeCache = Join-Path $StateDir '__pycache__'
if (Test-Path -LiteralPath $BytecodeCache) { Remove-Item -LiteralPath $BytecodeCache -Recurse -Force }
if ($InstructionCleanupComplete) {
    foreach ($owned in @('state', 'AGENTS.composed.md', 'original-active-global.md')) {
        $path = Join-Path $StateDir $owned
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
}
if ((Test-Path $StateDir) -and @(Get-ChildItem -LiteralPath $StateDir -Force).Count -eq 0) {
    Remove-Item -LiteralPath $StateDir -Force
}
Write-Host 'Uninstalled Codex delegation protocol only; unrelated hooks/configuration were preserved and the prior Codex override was restored when applicable.'
