$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$StateDir = Join-Path $CodexHome '.delegation-protocol'
$State = Join-Path $StateDir 'state'
$LegacyBulkHash = '1a53df02818dafb46b90fa0fea2bc840e50fcc4758c4172c40b8f48db23222f4'

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $Python) { throw 'Python 3 is required to remove protocol-owned Codex hooks safely.' }
$PythonExe = $Python.Source

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
            $recordedHash = (Get-Content -Raw -LiteralPath $HashPath).Trim().ToLowerInvariant()
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

if (Test-Path $State) {
    $lines = Get-Content $State
    $mode = (($lines | Where-Object { $_ -like 'mode=*' }) -replace '^mode=', '')
    $source = (($lines | Where-Object { $_ -like 'source=*' }) -replace '^source=', '')
    if ($mode -eq 'direct') {
        Remove-IfOurs (Join-Path $CodexHome 'AGENTS.md') (Join-Path $RepoRoot 'codex\AGENTS.md')
    } elseif ($mode -eq 'composed') {
        Remove-IfOurs (Join-Path $CodexHome 'AGENTS.override.md') (Join-Path $RepoRoot '.runtime\codex\AGENTS.composed.md')
        $saved = Join-Path $StateDir 'original-AGENTS.override.md.path-backup'
        if ($source -eq 'override' -and (Test-Path $saved)) {
            Move-Item -LiteralPath $saved -Destination (Join-Path $CodexHome 'AGENTS.override.md')
        }
    }
}

foreach ($owned in @(
    'state',
    'original-active-global.md',
    'hooks.before-first-install.json',
    'hooks-manifest.json'
)) {
    $path = Join-Path $StateDir $owned
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
}
$HookStateDir = Join-Path $StateDir 'hook-state'
if (Test-Path -LiteralPath $HookStateDir) {
    Remove-Item -LiteralPath $HookStateDir -Recurse -Force
}
if ((Test-Path $StateDir) -and @(Get-ChildItem -LiteralPath $StateDir -Force).Count -eq 0) {
    Remove-Item -LiteralPath $StateDir -Force
}
$Runtime = Join-Path $RepoRoot '.runtime\codex'
if (Test-Path (Join-Path $Runtime 'AGENTS.composed.md')) {
    Remove-Item -LiteralPath (Join-Path $Runtime 'AGENTS.composed.md') -Force
}
if ((Test-Path $Runtime) -and @(Get-ChildItem -LiteralPath $Runtime -Force).Count -eq 0) {
    Remove-Item -LiteralPath $Runtime -Force
}
Write-Host 'Uninstalled Codex delegation protocol only; unrelated hooks/configuration were preserved and the prior Codex override was restored when applicable.'
