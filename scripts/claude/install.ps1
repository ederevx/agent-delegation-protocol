$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$ClaudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $Python) { throw 'Python 3 is required for the Claude delegation hook.' }
$PythonExe = $Python.Source

& $PythonExe (Join-Path $RepoRoot 'scripts\agents\render-bulk-workers.py') --check
if ($LASTEXITCODE -ne 0) { throw "Generated bulk workers are stale (exit code $LASTEXITCODE)" }

New-Item -ItemType Directory -Force -Path (Join-Path $ClaudeHome 'rules'), (Join-Path $ClaudeHome 'agents'), (Join-Path $ClaudeHome 'hooks'), (Join-Path $ClaudeHome '.delegation-protocol') | Out-Null

function New-SafeSymlink([string]$Source, [string]$Destination) {
    if (Test-Path -LiteralPath $Destination) {
        $item = Get-Item -LiteralPath $Destination -Force
        if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Source) { return }
        throw "Refusing to overwrite existing path: $Destination"
    }
    New-Item -ItemType SymbolicLink -Path $Destination -Target $Source | Out-Null
}

function Remove-LegacyLinkIfOurs([string]$Destination, [string]$Expected) {
    $item = Get-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if ($item -and $item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Expected) {
        Remove-Item -LiteralPath $Destination -Force
    }
}

$RuleSource = Join-Path $RepoRoot 'claude\rules\delegation-protocol.md'
$AgentSource = Join-Path $RepoRoot 'claude\agents\bulk-worker.md'
$HookSource = Join-Path $RepoRoot 'claude\hooks\delegation-enforcer.py'
$MuxSource = Join-Path $RepoRoot 'scripts\agents\mux-scheduler.py'
$ClassifierSource = Join-Path $RepoRoot 'scripts\agents\delegation-classifier.py'
$QueueSource = Join-Path $RepoRoot 'scripts\agents\delegation_queue.py'
$CatalogSource = Join-Path $RepoRoot 'agents\catalog'
$RoutesSource = Join-Path $RepoRoot 'agents\mux-scheduler.json'
$RuleDest = Join-Path $ClaudeHome 'rules\delegation-protocol.md'
$AgentDest = Join-Path $ClaudeHome 'agents\bulk-worker.md'
$HookDest = Join-Path $ClaudeHome 'hooks\delegation-enforcer.py'
$MuxDest = Join-Path $ClaudeHome '.delegation-protocol\mux-scheduler.py'
$ClassifierDest = Join-Path $ClaudeHome '.delegation-protocol\delegation-classifier.py'
$QueueDest = Join-Path $ClaudeHome '.delegation-protocol\delegation_queue.py'
$CatalogDest = Join-Path $ClaudeHome '.delegation-protocol\catalog'
$RoutesDest = Join-Path $ClaudeHome '.delegation-protocol\mux-scheduler.json'

New-SafeSymlink $RuleSource $RuleDest
New-SafeSymlink $AgentSource $AgentDest
New-SafeSymlink $HookSource $HookDest
Remove-LegacyLinkIfOurs (Join-Path $ClaudeHome '.delegation-protocol\multiplexer.py') (Join-Path $RepoRoot 'scripts\agents\multiplexer.py')
Remove-LegacyLinkIfOurs (Join-Path $ClaudeHome '.delegation-protocol\multiplexer.json') (Join-Path $RepoRoot 'agents\multiplexer.json')
New-SafeSymlink $MuxSource $MuxDest
New-SafeSymlink $ClassifierSource $ClassifierDest
New-SafeSymlink $QueueSource $QueueDest
New-SafeSymlink $CatalogSource $CatalogDest
New-SafeSymlink $RoutesSource $RoutesDest

& $PythonExe (Join-Path $RepoRoot 'scripts\claude\manage-settings.py') install --claude-home $ClaudeHome --hook-path $HookDest --python $PythonExe
if ($LASTEXITCODE -ne 0) { throw "Claude settings installation failed with exit code $LASTEXITCODE" }

Write-Host 'Installed Claude delegation protocol only: hooks, settings, rule, bulk-worker, and agent mux-scheduler. Restart Claude Code sessions.'
