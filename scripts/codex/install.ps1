$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$CodexHome = [System.IO.Path]::GetFullPath($(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }))
$StateDir = Join-Path $CodexHome '.delegation-protocol'
$LegacyBulkHash = '1a53df02818dafb46b90fa0fea2bc840e50fcc4758c4172c40b8f48db23222f4'
$Utf8NoBom = [System.Text.UTF8Encoding]::new($false)

function Resolve-Python3 {
    $candidates = @()
    if ($env:CODEX_PYTHON) {
        $candidates += [PSCustomObject]@{ Path = $env:CODEX_PYTHON; Prefix = @() }
    }
    foreach ($name in @('python', 'python3', 'py')) {
        foreach ($command in @(Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
            $prefix = if ($name -eq 'py') { @('-3') } else { @() }
            $candidates += [PSCustomObject]@{ Path = $command.Source; Prefix = $prefix }
        }
    }
    $bundled = Join-Path $HOME '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    $candidates += [PSCustomObject]@{ Path = $bundled; Prefix = @() }

    $seen = @{}
    foreach ($candidate in $candidates) {
        $candidatePath = [string]$candidate.Path
        if (-not $candidatePath -or $seen.ContainsKey($candidatePath)) { continue }
        $seen[$candidatePath] = $true
        if (-not (Test-Path -LiteralPath $candidatePath -PathType Leaf)) { continue }

        # Windows Store execution aliases can exist on disk without a usable Python installation.
        if ($candidatePath -match '\\Microsoft\\WindowsApps\\python3?(\.exe)?$') { continue }
        try {
            $prefixArgs = @($candidate.Prefix)
            $probe = @(& $candidatePath @prefixArgs -c 'import os,sys;assert sys.version_info >= (3,11);print(os.path.abspath(sys.executable))' 2>$null)
            if ($LASTEXITCODE -ne 0) { continue }
            $resolvedLine = $probe | Where-Object { $_ -is [string] -and $_.Trim() } | Select-Object -Last 1
            if (-not $resolvedLine) { continue }
            $resolved = $resolvedLine.Trim()
            if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) { continue }
            & $resolved -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>$null
            if ($LASTEXITCODE -eq 0) { return (Get-Item -LiteralPath $resolved).FullName }
        } catch {
            continue
        }
    }
    throw 'Python 3.11 or newer is required for Codex hook enforcement. Set CODEX_PYTHON to a valid python.exe if it is not discoverable.'
}

$PythonExe = Resolve-Python3

& $PythonExe (Join-Path $RepoRoot 'scripts\agents\render-bulk-workers.py') --check
if ($LASTEXITCODE -ne 0) { throw "Generated bulk workers are stale (exit code $LASTEXITCODE)" }

function Read-Utf8Text([string]$Path) {
    return [System.IO.File]::ReadAllText($Path, [System.Text.Encoding]::UTF8)
}

function Write-Utf8NoBom([string]$Path, [string]$Value) {
    [System.IO.File]::WriteAllText($Path, $Value, $Utf8NoBom)
}

function Assert-SafeSymlinkDestination([string]$Source, [string]$Destination) {
    $item = Get-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Source) { return }
    throw "Refusing to overwrite existing path: $Destination"
}

function Assert-ManagedCopyDestination([string]$Source, [string]$Destination, [string]$HashPath) {
    $item = Get-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if ($item.LinkType -eq 'SymbolicLink') {
        if (-not ($item.Target -contains $Source)) {
            throw "Refusing to replace existing symlink: $Destination"
        }
        return
    }
    if ($item.PSIsContainer) { throw "Refusing to overwrite existing path: $Destination" }
    $sourceHash = (Get-FileHash -LiteralPath $Source).Hash.ToLowerInvariant()
    $currentHash = (Get-FileHash -LiteralPath $Destination).Hash.ToLowerInvariant()
    $recordedHash = if (Test-Path -LiteralPath $HashPath) {
        (Read-Utf8Text $HashPath).Trim().ToLowerInvariant()
    } else { '' }
    if (-not $recordedHash) { throw "Refusing to overwrite user-owned worker: $Destination" }
    if ($currentHash -ne $sourceHash -and $currentHash -ne $recordedHash) {
        throw "Refusing to overwrite modified or user-owned worker: $Destination"
    }
}

function Assert-SymlinkCapability {
    if ((Test-Path -LiteralPath $CodexHome) -and -not (Test-Path -LiteralPath $CodexHome -PathType Container)) {
        throw "Codex home exists but is not a directory: $CodexHome"
    }
    $probeRoot = if (Test-Path -LiteralPath $CodexHome -PathType Container) {
        $CodexHome
    } else {
        Split-Path -Parent $CodexHome
    }
    while ($probeRoot -and -not (Test-Path -LiteralPath $probeRoot -PathType Container)) {
        $parent = Split-Path -Parent $probeRoot
        if ($parent -eq $probeRoot) { break }
        $probeRoot = $parent
    }
    if (-not $probeRoot -or -not (Test-Path -LiteralPath $probeRoot -PathType Container)) {
        throw "Cannot find an existing parent directory for Codex home: $CodexHome"
    }
    $suffix = ".delegation-protocol-symlink-probe-$PID-$([guid]::NewGuid().ToString('N'))"
    $target = Join-Path $probeRoot ($suffix + '.target')
    $link = Join-Path $probeRoot ($suffix + '.link')
    try {
        Write-Utf8NoBom $target "probe`n"
        New-Item -ItemType SymbolicLink -Path $link -Target $target | Out-Null
    } catch {
        throw "Unable to create symbolic links for Codex hooks. Enable Windows Developer Mode or rerun PowerShell as Administrator, then retry. $($_.Exception.Message)"
    } finally {
        Remove-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue
    }
}

function Assert-HooksJson {
    $hooksPath = Join-Path $CodexHome 'hooks.json'
    $item = Get-Item -LiteralPath $hooksPath -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if ($item.PSIsContainer -or $item.LinkType) { throw "Refusing unsafe hooks.json path (expected a regular file): $hooksPath" }
    try {
        $parsed = (Read-Utf8Text $hooksPath) | ConvertFrom-Json
    } catch {
        throw "Refusing to modify invalid JSON at $hooksPath. $($_.Exception.Message)"
    }
    if ($null -eq $parsed -or $parsed -is [System.Array] -or $parsed -is [string] -or
        $parsed -is [bool] -or $parsed -is [ValueType]) {
        throw "Refusing to modify non-object JSON at $hooksPath"
    }
    $hooksProperty = $parsed.PSObject.Properties['hooks']
    if ($hooksProperty -and ($null -eq $hooksProperty.Value -or
        (-not ($hooksProperty.Value -is [PSCustomObject]) -and
        -not ($hooksProperty.Value -is [System.Collections.IDictionary])))) {
        throw "Refusing to replace existing non-object hooks value at $hooksPath"
    }
    if ($hooksProperty) {
        foreach ($event in @('UserPromptSubmit', 'SubagentStart', 'SubagentStop', 'PreToolUse', 'PostToolUse', 'Stop')) {
            $eventProperty = $hooksProperty.Value.PSObject.Properties[$event]
            if ($eventProperty -and -not ($eventProperty.Value -is [System.Array])) {
                throw "Refusing to replace existing non-array hooks.$event value at $hooksPath"
            }
        }
    }
}

function Assert-RegularFileIfPresent([string]$Path, [string]$Label) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.PSIsContainer -or $item.LinkType)) {
        throw "Refusing unsafe ${Label} path (expected a regular file): $Path"
    }
}

function Assert-DirectoryIfPresent([string]$Path, [string]$Label) {
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($item -and (-not $item.PSIsContainer -or $item.LinkType)) {
        throw "Refusing unsafe ${Label} path (expected a regular directory): $Path"
    }
}

$Protocol = Join-Path $RepoRoot 'codex\AGENTS.md'
$Agents = Join-Path $CodexHome 'AGENTS.md'
$Override = Join-Path $CodexHome 'AGENTS.override.md'
$State = Join-Path $StateDir 'state'
$Backup = Join-Path $StateDir 'original-active-global.md'
$Composed = Join-Path $StateDir 'AGENTS.composed.md'
$LegacyComposed = Join-Path $RepoRoot '.runtime\codex\AGENTS.composed.md'
$SavedOverride = Join-Path $StateDir 'original-AGENTS.override.md.path-backup'
$BulkSource = Join-Path $RepoRoot 'codex\agents\bulk_worker.toml'
$LegacyBulkSource = Join-Path $RepoRoot 'codex\agents\bulk-worker.toml'
$BalancedSource = Join-Path $RepoRoot 'codex\agents\balanced-worker.toml'
$HookSource = Join-Path $RepoRoot 'codex\hooks\delegation-enforcer.py'
$MuxSource = Join-Path $RepoRoot 'scripts\agents\mux-scheduler.py'
$CatalogSource = Join-Path $RepoRoot 'agents\catalog'
$RoutesSource = Join-Path $RepoRoot 'agents\mux-scheduler.json'
$BulkDest = Join-Path $CodexHome 'agents\bulk_worker.toml'
$LegacyBulkDest = Join-Path $CodexHome 'agents\bulk-worker.toml'
$BalancedDest = Join-Path $CodexHome 'agents\balanced-worker.toml'
$HookDest = Join-Path $CodexHome 'hooks\delegation-enforcer.py'
$MuxDest = Join-Path $StateDir 'mux-scheduler.py'
$CatalogDest = Join-Path $StateDir 'catalog'
$RoutesDest = Join-Path $StateDir 'mux-scheduler.json'
$BulkHashPath = Join-Path $StateDir 'bulk-worker.sha256'

# Validate all known failure points before creating protocol-owned directories or moving user files.
Assert-SymlinkCapability
Assert-HooksJson
Assert-ManagedCopyDestination $BulkSource $BulkDest $BulkHashPath
Assert-SafeSymlinkDestination $BalancedSource $BalancedDest
Assert-SafeSymlinkDestination $HookSource $HookDest
Assert-SafeSymlinkDestination $MuxSource $MuxDest
Assert-SafeSymlinkDestination $CatalogSource $CatalogDest
Assert-SafeSymlinkDestination $RoutesSource $RoutesDest
Assert-DirectoryIfPresent (Join-Path $CodexHome 'agents') 'agents directory'
Assert-DirectoryIfPresent (Join-Path $CodexHome 'hooks') 'hooks directory'
Assert-DirectoryIfPresent $StateDir 'protocol state directory'
Assert-RegularFileIfPresent $State 'protocol state'
Assert-RegularFileIfPresent $Backup 'instruction backup'
Assert-RegularFileIfPresent $Composed 'composed instructions'
Assert-RegularFileIfPresent $BulkHashPath 'worker ownership hash'
Assert-RegularFileIfPresent (Join-Path $StateDir 'hooks.before-first-install.json') 'hook backup'
Assert-RegularFileIfPresent (Join-Path $StateDir 'hooks-manifest.json') 'hook manifest'
$agentsItem = Get-Item -LiteralPath $Agents -Force -ErrorAction SilentlyContinue
if ($agentsItem -and $agentsItem.PSIsContainer) {
    throw "Refusing unsafe global instructions path (expected a file): $Agents"
}
$overrideItem = Get-Item -LiteralPath $Override -Force -ErrorAction SilentlyContinue
if ($overrideItem -and $overrideItem.PSIsContainer) {
    throw "Refusing unsafe override path (expected a file): $Override"
}
$stateText = if (Test-Path -LiteralPath $State) { Read-Utf8Text $State } else { '' }
$stateMode = if ($stateText -match '(?m)^mode=([^\r\n]+)\r?$') { $Matches[1] } else { '' }
$stateSource = if ($stateText -match '(?m)^source=([^\r\n]+)\r?$') { $Matches[1] } else { '' }
$savedOverrideItem = Get-Item -LiteralPath $SavedOverride -Force -ErrorAction SilentlyContinue
if ($savedOverrideItem -and $savedOverrideItem.PSIsContainer) {
    throw "Refusing unsafe override recovery backup (expected a file): $SavedOverride"
}
$ownedComposedOverride = $overrideItem -and $overrideItem.LinkType -eq 'SymbolicLink' -and
    ($overrideItem.Target -contains $Composed -or $overrideItem.Target -contains $LegacyComposed)
$validComposedInstall = $ownedComposedOverride -and $stateMode -eq 'composed' -and
    $stateSource -in @('agents', 'override') -and (Test-Path -LiteralPath $Backup -PathType Leaf) -and
    ($stateSource -ne 'override' -or $savedOverrideItem)
if ($ownedComposedOverride -and -not $validComposedInstall) {
    throw "Refusing to migrate or replace composed instructions with incomplete ownership state: $Override"
}
if ($overrideItem -and $overrideItem.LinkType -eq 'SymbolicLink' -and
    -not $ownedComposedOverride -and -not (Test-Path -LiteralPath $Override)) {
    throw "Refusing to replace unowned dangling override symlink: $Override"
}
if ($savedOverrideItem -and $stateSource -ne 'override') {
    throw "Refusing override recovery backup without source=override ownership state: $SavedOverride"
}
if ($savedOverrideItem -and $overrideItem) {
    if (-not $validComposedInstall) {
        throw "Refusing to overwrite existing override recovery backup: $SavedOverride"
    }
}
$expectedComposed = if ((Test-Path -LiteralPath $Backup -PathType Leaf) -and
    (Test-Path -LiteralPath $Composed -PathType Leaf)) {
    (Read-Utf8Text $Backup) + "`r`n`r`n" + (Read-Utf8Text $Protocol)
} else { $null }
$validInterruptedRecovery = -not $overrideItem -and $savedOverrideItem -and
    $stateMode -eq 'composed' -and $stateSource -eq 'override' -and
    $null -ne $expectedComposed -and
    [string]::Equals((Read-Utf8Text $Composed), $expectedComposed, [StringComparison]::Ordinal)
if ($savedOverrideItem -and -not $overrideItem -and -not $validInterruptedRecovery) {
    throw "Refusing to activate unproven override recovery material: $SavedOverride"
}

New-Item -ItemType Directory -Force -Path $CodexHome, (Join-Path $CodexHome 'agents'), (Join-Path $CodexHome 'hooks'), $StateDir | Out-Null

function New-SafeSymlink([string]$Source, [string]$Destination) {
    $item = Get-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if ($item) {
        if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Source) { return }
        throw "Refusing to overwrite existing path: $Destination"
    }
    try {
        New-Item -ItemType SymbolicLink -Path $Destination -Target $Source | Out-Null
    } catch {
        if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
            throw "Unable to create symbolic link '$Destination'. Enable Windows Developer Mode or rerun PowerShell as Administrator, then retry. $($_.Exception.Message)"
        }
        throw
    }
}

function Remove-LegacyLinkIfOurs([string]$Destination, [string]$Expected) {
    $item = Get-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if ($item -and $item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Expected) {
        Remove-Item -LiteralPath $Destination -Force
    }
}

function Install-ManagedCopy([string]$Source, [string]$Destination, [string]$HashPath) {
    $sourceHash = (Get-FileHash -LiteralPath $Source).Hash.ToLowerInvariant()
    $item = Get-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if ($item) {
        if ($item.LinkType -eq 'SymbolicLink') {
            if (-not ($item.Target -contains $Source)) {
                throw "Refusing to replace existing symlink: $Destination"
            }
            Remove-Item -LiteralPath $Destination -Force
        } elseif ($item.PSIsContainer) {
            throw "Refusing to overwrite existing path: $Destination"
        } else {
            $currentHash = (Get-FileHash -LiteralPath $Destination).Hash.ToLowerInvariant()
            $recordedHash = if (Test-Path -LiteralPath $HashPath) {
                (Read-Utf8Text $HashPath).Trim().ToLowerInvariant()
            } else { '' }
            if (-not $recordedHash) {
                throw "Refusing to overwrite user-owned worker: $Destination"
            }
            if ($currentHash -ne $sourceHash -and $currentHash -ne $recordedHash) {
                throw "Refusing to overwrite modified or user-owned worker: $Destination"
            }
        }
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
    Write-Utf8NoBom $HashPath ($sourceHash + "`n")
}

function Remove-LegacyWorkerIfOurs([string]$Destination, [string]$Expected, [string]$LegacyHash) {
    $item = Get-Item -LiteralPath $Destination -Force -ErrorAction SilentlyContinue
    if (-not $item) { return }
    if ($item.LinkType -eq 'SymbolicLink') {
        if ($item.Target -contains $Expected) {
            Remove-Item -LiteralPath $Destination -Force
        }
        return
    }
    if (-not $item.PSIsContainer -and
        (Get-FileHash -LiteralPath $Destination).Hash -eq $LegacyHash) {
        Remove-Item -LiteralPath $Destination -Force
    }
}

# Recreate only the managed link when state, backup, and composition prove an interrupted owned install.
if ($validInterruptedRecovery) {
    New-SafeSymlink $Composed $Override
    $overrideItem = Get-Item -LiteralPath $Override -Force
    $ownedComposedOverride = $true
    $validComposedInstall = $true
}

$InstructionsReady = $false
if ((Test-Path $Agents) -and -not $overrideItem) {
    $item = Get-Item $Agents -Force
    if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Protocol) {
        Write-Utf8NoBom $State "mode=direct`n"
        $InstructionsReady = $true
    }
}

if (-not $InstructionsReady -and $overrideItem -and $validComposedInstall) {
    $item = $overrideItem
    if ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $Composed) {
        $existing = if (Test-Path $Backup) { Read-Utf8Text $Backup } else { '' }
        Write-Utf8NoBom $Composed ($existing + "`r`n`r`n" + (Read-Utf8Text $Protocol))
        $InstructionsReady = $true
    } elseif ($item.LinkType -eq 'SymbolicLink' -and $item.Target -contains $LegacyComposed) {
        $migrationComposedExisted = Test-Path -LiteralPath $Composed
        $previousMigrationComposed = if ($migrationComposedExisted) {
            [System.IO.File]::ReadAllBytes($Composed)
        } else { $null }
        $existing = if (Test-Path $Backup) { Read-Utf8Text $Backup } else { '' }
        Write-Utf8NoBom $Composed ($existing + "`r`n`r`n" + (Read-Utf8Text $Protocol))
        Remove-Item -LiteralPath $Override -Force
        try {
            New-SafeSymlink $Composed $Override
            $InstructionsReady = $true
        } catch {
            Remove-Item -LiteralPath $Override -Force -ErrorAction SilentlyContinue
            New-SafeSymlink $LegacyComposed $Override
            if ($migrationComposedExisted) {
                [System.IO.File]::WriteAllBytes($Composed, $previousMigrationComposed)
            } else {
                Remove-Item -LiteralPath $Composed -Force -ErrorAction SilentlyContinue
            }
            throw
        }
    }
}

if (-not $InstructionsReady) {
    if (-not $overrideItem -and -not (Test-Path $Agents)) {
        New-SafeSymlink $Protocol $Agents
        try {
            Write-Utf8NoBom $State "mode=direct`n"
        } catch {
            $installedAgents = Get-Item -LiteralPath $Agents -Force -ErrorAction SilentlyContinue
            if ($installedAgents -and $installedAgents.LinkType -eq 'SymbolicLink' -and
                $installedAgents.Target -contains $Protocol) {
                Remove-Item -LiteralPath $Agents -Force
            }
            throw
        }
    } else {
        $backupExisted = Test-Path -LiteralPath $Backup
        $previousBackup = if ($backupExisted) { [System.IO.File]::ReadAllBytes($Backup) } else { $null }
        $composedExisted = Test-Path -LiteralPath $Composed
        $previousComposed = if ($composedExisted) { [System.IO.File]::ReadAllBytes($Composed) } else { $null }
        $stateExisted = Test-Path -LiteralPath $State
        $previousState = if ($stateExisted) { [System.IO.File]::ReadAllBytes($State) } else { $null }
        if ($overrideItem) {
            if (Test-Path -LiteralPath $SavedOverride) {
                throw "Refusing to overwrite existing override recovery backup: $SavedOverride"
            }
            Copy-Item -LiteralPath $Override -Destination $Backup -Force
            $source = 'override'
        } else {
            Copy-Item -LiteralPath $Agents -Destination $Backup -Force
            $source = 'agents'
        }
        Write-Utf8NoBom $Composed ((Read-Utf8Text $Backup) + "`r`n`r`n" + (Read-Utf8Text $Protocol))
        if ($source -eq 'override') {
            Move-Item -LiteralPath $Override -Destination $SavedOverride
        }
        try {
            New-SafeSymlink $Composed $Override
            Write-Utf8NoBom $State "mode=composed`nsource=$source`n"
        } catch {
            $installedOverride = Get-Item -LiteralPath $Override -Force -ErrorAction SilentlyContinue
            if ($installedOverride -and $installedOverride.LinkType -eq 'SymbolicLink' -and
                $installedOverride.Target -contains $Composed) {
                Remove-Item -LiteralPath $Override -Force
            }
            if ($source -eq 'override' -and -not (Test-Path -LiteralPath $Override) -and
                (Test-Path -LiteralPath $SavedOverride)) {
                Move-Item -LiteralPath $SavedOverride -Destination $Override
            }
            if ($backupExisted) { [System.IO.File]::WriteAllBytes($Backup, $previousBackup) }
            else { Remove-Item -LiteralPath $Backup -Force -ErrorAction SilentlyContinue }
            if ($composedExisted) { [System.IO.File]::WriteAllBytes($Composed, $previousComposed) }
            else { Remove-Item -LiteralPath $Composed -Force -ErrorAction SilentlyContinue }
            if ($stateExisted) { [System.IO.File]::WriteAllBytes($State, $previousState) }
            else { Remove-Item -LiteralPath $State -Force -ErrorAction SilentlyContinue }
            throw
        }
    }
}

Remove-LegacyWorkerIfOurs $LegacyBulkDest $LegacyBulkSource $LegacyBulkHash
Install-ManagedCopy $BulkSource $BulkDest $BulkHashPath
New-SafeSymlink $BalancedSource $BalancedDest
New-SafeSymlink $HookSource $HookDest
Remove-LegacyLinkIfOurs (Join-Path $StateDir 'multiplexer.py') (Join-Path $RepoRoot 'scripts\agents\multiplexer.py')
Remove-LegacyLinkIfOurs (Join-Path $StateDir 'multiplexer.json') (Join-Path $RepoRoot 'agents\multiplexer.json')
New-SafeSymlink $MuxSource $MuxDest
New-SafeSymlink $CatalogSource $CatalogDest
New-SafeSymlink $RoutesSource $RoutesDest

& $PythonExe (Join-Path $RepoRoot 'scripts\codex\manage-hooks.py') install --codex-home $CodexHome --hook-path $HookDest --python $PythonExe
if ($LASTEXITCODE -ne 0) { throw "Codex hook installation failed with exit code $LASTEXITCODE" }

Write-Host 'Installed Codex delegation protocol only: supplementary AGENTS instructions, worker tiers, agent mux-scheduler, and lifecycle hooks.'
Write-Host 'Restart Codex, run /hooks, and review/trust the Agent Delegation Protocol hooks before relying on mechanical enforcement.'
