$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$StateDir = Join-Path $CodexHome '.delegation-protocol'
$State = Join-Path $StateDir 'state'

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

$BulkSource = Join-Path $RepoRoot 'codex\agents\bulk-worker.toml'
$BalancedSource = Join-Path $RepoRoot 'codex\agents\balanced-worker.toml'
$HookSource = Join-Path $RepoRoot 'codex\hooks\delegation-enforcer.py'
$BulkDest = Join-Path $CodexHome 'agents\bulk-worker.toml'
$BalancedDest = Join-Path $CodexHome 'agents\balanced-worker.toml'
$HookDest = Join-Path $CodexHome 'hooks\delegation-enforcer.py'

& $PythonExe (Join-Path $RepoRoot 'scripts\codex\manage-hooks.py') uninstall --codex-home $CodexHome --hook-path $HookDest --python $PythonExe
if ($LASTEXITCODE -ne 0) { throw "Codex hook uninstallation failed with exit code $LASTEXITCODE" }

Remove-IfOurs $BulkDest $BulkSource
Remove-IfOurs $BalancedDest $BalancedSource
Remove-IfOurs $HookDest $HookSource

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

if (Test-Path $StateDir) { Remove-Item -LiteralPath $StateDir -Recurse -Force }
$Runtime = Join-Path $RepoRoot '.runtime\codex'
if (Test-Path $Runtime) { Remove-Item -LiteralPath $Runtime -Recurse -Force }
Write-Host 'Uninstalled Codex delegation protocol only; unrelated hooks/configuration were preserved and the prior Codex override was restored when applicable.'
