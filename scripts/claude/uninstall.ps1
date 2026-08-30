$ErrorActionPreference = 'Stop'

$ClaudeHome = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$Python = Get-Command python -ErrorAction SilentlyContinue
if (-not $Python) { $Python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $Python) { throw 'Python 3 is required to remove the Claude delegation protocol safely.' }
& $Python.Source (Join-Path $PSScriptRoot 'manage-install.py') uninstall --claude-home $ClaudeHome --python $Python.Source
if ($LASTEXITCODE -ne 0) { throw "Claude protocol uninstallation failed with exit code $LASTEXITCODE" }
