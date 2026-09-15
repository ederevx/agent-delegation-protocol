$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$hostHome = if ($env:PI_CODING_AGENT_DIR) { $env:PI_CODING_AGENT_DIR } else { Join-Path $HOME '.pi\agent' }
$python = if ($env:PYTHON) { $env:PYTHON } else { (Get-Command python -ErrorAction Stop).Source }
& $python (Join-Path $repo 'scripts\hosts\install.py') uninstall --host pi --home $hostHome --repo $repo
if ($LASTEXITCODE) { exit $LASTEXITCODE }
