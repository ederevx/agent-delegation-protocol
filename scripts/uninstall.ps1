$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$ClaudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$StateDir = Join-Path $CodexHome '.delegation-protocol'
$State = Join-Path $StateDir 'state'

function Remove-IfOurs([string]$Destination, [string]$Expected) {
    if (Test-Path -LiteralPath $Destination) {
        $item = Get-Item -LiteralPath $Destination -Force
        if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Expected) {
            Remove-Item -LiteralPath $Destination -Force
        }
    }
}

Remove-IfOurs (Join-Path $ClaudeHome 'rules\delegation-protocol.md') (Join-Path $RepoRoot 'claude\rules\delegation-protocol.md')
Remove-IfOurs (Join-Path $ClaudeHome 'agents\bulk-worker.md') (Join-Path $RepoRoot 'claude\agents\bulk-worker.md')

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
Write-Host 'Uninstalled delegation protocol links and restored preserved Codex override when applicable.'
