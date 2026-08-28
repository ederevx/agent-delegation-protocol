$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$StateDir = Join-Path $CodexHome '.delegation-protocol'
$RuntimeDir = Join-Path $RepoRoot '.runtime\codex'

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $Python) { throw 'Python 3 is required for Codex hook enforcement.' }
$PythonExe = $Python.Source

& $PythonExe (Join-Path $RepoRoot 'scripts\agents\render-bulk-workers.py') --check
if ($LASTEXITCODE -ne 0) { throw "Generated bulk workers are stale (exit code $LASTEXITCODE)" }

New-Item -ItemType Directory -Force -Path $CodexHome, (Join-Path $CodexHome 'agents'), (Join-Path $CodexHome 'hooks'), $StateDir, $RuntimeDir | Out-Null

function New-SafeSymlink([string]$Source, [string]$Destination) {
    if (Test-Path -LiteralPath $Destination) {
        $item = Get-Item -LiteralPath $Destination -Force
        if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Source) { return }
        throw "Refusing to overwrite existing path: $Destination"
    }
    New-Item -ItemType SymbolicLink -Path $Destination -Target $Source | Out-Null
}

$Protocol = Join-Path $RepoRoot 'codex\AGENTS.md'
$Agents = Join-Path $CodexHome 'AGENTS.md'
$Override = Join-Path $CodexHome 'AGENTS.override.md'
$State = Join-Path $StateDir 'state'
$Backup = Join-Path $StateDir 'original-active-global.md'
$Composed = Join-Path $RuntimeDir 'AGENTS.composed.md'

$InstructionsReady = $false
if ((Test-Path $Agents) -and -not (Test-Path $Override)) {
    $item = Get-Item $Agents -Force
    if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Protocol) {
        Set-Content -Path $State -Value "mode=direct`n"
        $InstructionsReady = $true
    }
}

if (-not $InstructionsReady -and (Test-Path $Override) -and (Test-Path $State)) {
    $item = Get-Item $Override -Force
    if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Composed) {
        $existing = if (Test-Path $Backup) { Get-Content -Raw $Backup } else { '' }
        Set-Content -Path $Composed -Value ($existing + "`r`n`r`n" + (Get-Content -Raw $Protocol))
        $InstructionsReady = $true
    }
}

if (-not $InstructionsReady) {
    if (-not (Test-Path $Override) -and -not (Test-Path $Agents)) {
        New-SafeSymlink $Protocol $Agents
        Set-Content -Path $State -Value "mode=direct`n"
    } else {
        if (Test-Path $Override) {
            Copy-Item -LiteralPath $Override -Destination $Backup -Force
            Move-Item -LiteralPath $Override -Destination (Join-Path $StateDir 'original-AGENTS.override.md.path-backup')
            $source = 'override'
        } else {
            Copy-Item -LiteralPath $Agents -Destination $Backup -Force
            $source = 'agents'
        }
        Set-Content -Path $Composed -Value ((Get-Content -Raw $Backup) + "`r`n`r`n" + (Get-Content -Raw $Protocol))
        New-SafeSymlink $Composed $Override
        Set-Content -Path $State -Value "mode=composed`nsource=$source`n"
    }
}

$BulkSource = Join-Path $RepoRoot 'codex\agents\bulk-worker.toml'
$BalancedSource = Join-Path $RepoRoot 'codex\agents\balanced-worker.toml'
$HookSource = Join-Path $RepoRoot 'codex\hooks\delegation-enforcer.py'
$MuxSource = Join-Path $RepoRoot 'scripts\agents\multiplexer.py'
$CatalogSource = Join-Path $RepoRoot 'agents\catalog'
$RoutesSource = Join-Path $RepoRoot 'agents\multiplexer.json'
$BulkDest = Join-Path $CodexHome 'agents\bulk-worker.toml'
$BalancedDest = Join-Path $CodexHome 'agents\balanced-worker.toml'
$HookDest = Join-Path $CodexHome 'hooks\delegation-enforcer.py'
$MuxDest = Join-Path $StateDir 'multiplexer.py'
$CatalogDest = Join-Path $StateDir 'catalog'
$RoutesDest = Join-Path $StateDir 'multiplexer.json'

New-SafeSymlink $BulkSource $BulkDest
New-SafeSymlink $BalancedSource $BalancedDest
New-SafeSymlink $HookSource $HookDest
New-SafeSymlink $MuxSource $MuxDest
New-SafeSymlink $CatalogSource $CatalogDest
New-SafeSymlink $RoutesSource $RoutesDest

& $PythonExe (Join-Path $RepoRoot 'scripts\codex\manage-hooks.py') install --codex-home $CodexHome --hook-path $HookDest --python $PythonExe
if ($LASTEXITCODE -ne 0) { throw "Codex hook installation failed with exit code $LASTEXITCODE" }

Write-Host 'Installed Codex delegation protocol only: supplementary AGENTS instructions, worker tiers, agent multiplexer, and lifecycle hooks.'
Write-Host 'Restart Codex, run /hooks, and review/trust the Agent Delegation Protocol hooks before relying on mechanical enforcement.'
