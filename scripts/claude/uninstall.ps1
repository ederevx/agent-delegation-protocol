$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$ClaudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }

$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $Python) { throw 'Python 3 is required to remove the protocol-owned Claude settings safely.' }
$PythonExe = $Python.Source

function Remove-IfOurs([string]$Destination, [string]$Expected) {
    if (Test-Path -LiteralPath $Destination) {
        $item = Get-Item -LiteralPath $Destination -Force
        if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Expected) {
            Remove-Item -LiteralPath $Destination -Force
        }
    }
}

$RuleSource = Join-Path $RepoRoot 'claude\rules\delegation-protocol.md'
$AgentSource = Join-Path $RepoRoot 'claude\agents\bulk-worker.md'
$HookSource = Join-Path $RepoRoot 'claude\hooks\delegation-enforcer.py'
$RuleDest = Join-Path $ClaudeHome 'rules\delegation-protocol.md'
$AgentDest = Join-Path $ClaudeHome 'agents\bulk-worker.md'
$HookDest = Join-Path $ClaudeHome 'hooks\delegation-enforcer.py'

& $PythonExe (Join-Path $RepoRoot 'scripts\claude\manage-settings.py') uninstall --claude-home $ClaudeHome --hook-path $HookDest --python $PythonExe
if ($LASTEXITCODE -ne 0) { throw "Claude settings uninstallation failed with exit code $LASTEXITCODE" }

Remove-IfOurs $RuleDest $RuleSource
Remove-IfOurs $AgentDest $AgentSource
Remove-IfOurs $HookDest $HookSource

$ProtocolState = Join-Path $ClaudeHome '.delegation-protocol'
if (Test-Path $ProtocolState) { Remove-Item -LiteralPath $ProtocolState -Recurse -Force }
Write-Host 'Uninstalled Claude delegation protocol only; unrelated Claude settings and hooks were preserved.'
