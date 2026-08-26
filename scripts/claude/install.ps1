$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$ClaudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $Python) { throw 'Python 3 is required for the Claude delegation hook.' }
$PythonExe = $Python.Source

New-Item -ItemType Directory -Force -Path (Join-Path $ClaudeHome 'rules'), (Join-Path $ClaudeHome 'agents'), (Join-Path $ClaudeHome 'hooks'), (Join-Path $ClaudeHome '.delegation-protocol') | Out-Null

function New-SafeSymlink([string]$Source, [string]$Destination) {
    if (Test-Path -LiteralPath $Destination) {
        $item = Get-Item -LiteralPath $Destination -Force
        if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Source) { return }
        throw "Refusing to overwrite existing path: $Destination"
    }
    New-Item -ItemType SymbolicLink -Path $Destination -Target $Source | Out-Null
}

$RuleSource = Join-Path $RepoRoot 'claude\rules\delegation-protocol.md'
$AgentSource = Join-Path $RepoRoot 'claude\agents\bulk-worker.md'
$HookSource = Join-Path $RepoRoot 'claude\hooks\delegation-enforcer.py'
$RuleDest = Join-Path $ClaudeHome 'rules\delegation-protocol.md'
$AgentDest = Join-Path $ClaudeHome 'agents\bulk-worker.md'
$HookDest = Join-Path $ClaudeHome 'hooks\delegation-enforcer.py'

New-SafeSymlink $RuleSource $RuleDest
New-SafeSymlink $AgentSource $AgentDest
New-SafeSymlink $HookSource $HookDest

& $PythonExe (Join-Path $RepoRoot 'scripts\claude\manage-settings.py') install --claude-home $ClaudeHome --hook-path $HookDest --python $PythonExe
if ($LASTEXITCODE -ne 0) { throw "Claude settings installation failed with exit code $LASTEXITCODE" }

Write-Host 'Installed Claude delegation protocol only: hooks, settings, rule, and bulk-worker. Restart Claude Code sessions.'
