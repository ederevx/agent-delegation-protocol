$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$home = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
$python = if ($env:CODEX_PYTHON) { $env:CODEX_PYTHON } else { (Get-Command python -ErrorAction Stop).Source }
& $python (Join-Path $repo 'scripts\hosts\install.py') uninstall --host codex --home $home --repo $repo
if ($LASTEXITCODE) { exit $LASTEXITCODE }
