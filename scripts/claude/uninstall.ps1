$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$home = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$python = if ($env:PYTHON) { $env:PYTHON } else { (Get-Command python -ErrorAction Stop).Source }
& $python (Join-Path $repo 'scripts\hosts\install.py') uninstall --host claude --home $home --repo $repo
if ($LASTEXITCODE) { exit $LASTEXITCODE }
