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
$MuxSource = Join-Path $RepoRoot 'scripts\agents\multiplexer.py'
$CatalogSource = Join-Path $RepoRoot 'agents\catalog'
$RoutesSource = Join-Path $RepoRoot 'agents\multiplexer.json'
$RuleDest = Join-Path $ClaudeHome 'rules\delegation-protocol.md'
$AgentDest = Join-Path $ClaudeHome 'agents\bulk-worker.md'
$HookDest = Join-Path $ClaudeHome 'hooks\delegation-enforcer.py'
$MuxDest = Join-Path $ClaudeHome '.delegation-protocol\multiplexer.py'
$CatalogDest = Join-Path $ClaudeHome '.delegation-protocol\catalog'
$RoutesDest = Join-Path $ClaudeHome '.delegation-protocol\multiplexer.json'

& $PythonExe (Join-Path $RepoRoot 'scripts\claude\manage-settings.py') uninstall --claude-home $ClaudeHome --hook-path $HookDest --python $PythonExe
if ($LASTEXITCODE -ne 0) { throw "Claude settings uninstallation failed with exit code $LASTEXITCODE" }

Remove-IfOurs $RuleDest $RuleSource
Remove-IfOurs $AgentDest $AgentSource
Remove-IfOurs $HookDest $HookSource
Remove-IfOurs $MuxDest $MuxSource
Remove-IfOurs $CatalogDest $CatalogSource
Remove-IfOurs $RoutesDest $RoutesSource

$ProtocolState = Join-Path $ClaudeHome '.delegation-protocol'
foreach ($owned in @('settings.before-first-install.json', 'settings-manifest.json')) {
    $path = Join-Path $ProtocolState $owned
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
}
if ((Test-Path $ProtocolState) -and @(Get-ChildItem -LiteralPath $ProtocolState -Force).Count -eq 0) {
    Remove-Item -LiteralPath $ProtocolState -Force
}
Write-Host 'Uninstalled Claude delegation protocol only; unrelated Claude settings and hooks were preserved.'
