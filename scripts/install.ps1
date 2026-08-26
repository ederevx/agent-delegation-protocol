$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$ClaudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$StateDir = Join-Path $CodexHome '.delegation-protocol'
$RuntimeDir = Join-Path $RepoRoot '.runtime\codex'

New-Item -ItemType Directory -Force -Path $CodexHome, (Join-Path $ClaudeHome 'rules'), (Join-Path $ClaudeHome 'agents'), $StateDir, $RuntimeDir | Out-Null

function New-SafeSymlink([string]$Source, [string]$Destination) {
    if (Test-Path -LiteralPath $Destination) {
        $item = Get-Item -LiteralPath $Destination -Force
        if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Source) { return }
        throw "Refusing to overwrite existing path: $Destination"
    }
    New-Item -ItemType SymbolicLink -Path $Destination -Target $Source | Out-Null
}

$ClaudeRule = Join-Path $ClaudeHome 'rules\delegation-protocol.md'
$ClaudeAgent = Join-Path $ClaudeHome 'agents\bulk-worker.md'
New-SafeSymlink (Join-Path $RepoRoot 'claude\rules\delegation-protocol.md') $ClaudeRule
New-SafeSymlink (Join-Path $RepoRoot 'claude\agents\bulk-worker.md') $ClaudeAgent

$Protocol = Join-Path $RepoRoot 'codex\AGENTS.md'
$Agents = Join-Path $CodexHome 'AGENTS.md'
$Override = Join-Path $CodexHome 'AGENTS.override.md'
$State = Join-Path $StateDir 'state'
$Backup = Join-Path $StateDir 'original-active-global.md'
$Composed = Join-Path $RuntimeDir 'AGENTS.composed.md'

if ((Test-Path $Agents) -and -not (Test-Path $Override)) {
    $item = Get-Item $Agents -Force
    if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Protocol) {
        Set-Content -Path $State -Value "mode=direct`n"
        Write-Host 'Installed delegation protocol. Restart Codex/Claude Code sessions.'
        exit 0
    }
}

if ((Test-Path $Override) -and (Test-Path $State)) {
    $item = Get-Item $Override -Force
    if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Composed) {
        $existing = if (Test-Path $Backup) { Get-Content -Raw $Backup } else { '' }
        Set-Content -Path $Composed -Value ($existing + "`r`n`r`n" + (Get-Content -Raw $Protocol))
        Write-Host 'Installed delegation protocol. Restart Codex/Claude Code sessions.'
        exit 0
    }
}

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

Write-Host 'Installed delegation protocol. Restart Codex/Claude Code sessions.'
