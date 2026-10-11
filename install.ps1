<#
install.ps1 - NSLS Builder Toolkit installer for Windows (full native).

Windows counterpart to install.sh. Bash isn't on a stock Windows box, so this
does the whole install in PowerShell (the Windows hooks are native PowerShell).
It also provisions the runtime prerequisites the toolkit's skills need - Python
3.12 + python-docx + python-pptx (and the nsls-python launcher the document
skills call), the gws CLI, and Node.js for the signal MCP server - and
checks for (but never installs) the MS Visual C++ x64 runtime that gws depends
on:

  0. Provision prerequisites (Python + doc libraries + nsls-python, gws, Node); VC++ check
  1. Clone / update the org plugin
  2. Enable it + register the PowerShell hooks in settings.json
  3. Fire an install event to the Automation Tracker (platform: windows)
  4. Install the superpowers + compound-engineering marketplace plugins
  5. Register bundled MCP servers (stdio directly; http via --transport http,
     deferring token-gated servers to /signal-setup)
  6. Sync slash-command pointer skills
  7. Print next steps (desktop-first)

Self-bootstrapping - safe to run piped straight from the web:
  powershell -NoProfile -ExecutionPolicy Bypass -Command "iwr -useb https://raw.githubusercontent.com/thensls/nsls-builder-toolkit/main/install.ps1 | iex"
It clones the repo itself (Step 1) before needing any file from it, so it never
depends on $PSScriptRoot. Also runs from a local checkout.

Idempotent: re-running updates the clone and no-ops anything already in place.
  -Test   install into a throwaway $HOME\.claude-kit-test (or $env:CLAUDE_CONFIG_DIR)
#>
param([switch]$Test)
$ErrorActionPreference = 'Stop'

# TLS 1.2 for the web calls below (gws zip, VC_redist, tracker ping). Win11
# defaults already negotiate it; this is insurance for older hosts. -bor so a
# host that also speaks TLS 1.3 is never downgraded.
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

# How were we launched? `-File` (the /nsls-setmeup re-provision path, or any wrapper)
# sets $PSCommandPath; a bare `iwr | iex` paste does not. Under -File we hand
# back real exit codes; under a bare paste we must NEVER `exit` -- that closes
# the user's interactive PowerShell window and swallows the summary/error. Each
# stop site therefore does: if ($RunningFromFile) { exit N } else { return }.
# The `return` runs at top-level script scope, which halts the script under
# `iex` without terminating the session (a function-scoped return would not).
$RunningFromFile = [bool]$PSCommandPath

# --- Config dir resolution (mirrors install.sh) ---
if ($env:CLAUDE_CONFIG_DIR) {
    $ConfigDir = $env:CLAUDE_CONFIG_DIR
} elseif ($Test) {
    $ConfigDir = if ($env:CLAUDE_KIT_TEST_DIR) { $env:CLAUDE_KIT_TEST_DIR } else { Join-Path $env:USERPROFILE '.claude-kit-test' }
} else {
    $ConfigDir = Join-Path $env:USERPROFILE '.claude'
}

$LocalDir  = Join-Path $ConfigDir 'local-plugins'
$PluginDir = Join-Path $LocalDir  'nsls-builder-toolkit'
$HooksDir  = Join-Path $PluginDir 'hooks'
$Settings  = Join-Path $ConfigDir 'settings.json'
$SkillsDir = Join-Path $ConfigDir 'skills'
# Repo/branch overridable for fork testing (install a feature branch from a fork
# before it merges to thensls/main). Defaults are production.
$RepoUrl    = if ($env:NSLS_TOOLKIT_REPO) { $env:NSLS_TOOLKIT_REPO } else { 'https://github.com/thensls/nsls-builder-toolkit.git' }
$RepoBranch = if ($env:NSLS_TOOLKIT_BRANCH) { $env:NSLS_TOOLKIT_BRANCH } else { 'main' }
$Tracker   = if ($env:NSLS_TRACKER_URL) { $env:NSLS_TRACKER_URL } else { 'https://web-production-6281e.up.railway.app' }

# --- Helpers ---------------------------------------------------------------
# BOM-less UTF-8 writer. PowerShell 5.1's `Set-Content -Encoding utf8` ALWAYS
# emits a BOM, and a leading BOM breaks `json.load()` for every downstream
# consumer of settings.json (confirmed live). Route every JSON/text write
# through this so nothing we write ever carries a BOM.
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
# True only if $Text is exactly a pointer this toolkit writes: front matter,
# then a single "Read and follow" line naming this skill's own path. This is the
# only ownership check before an overwrite. Merely CONTAINING the path is not
# enough: a builder's own skill that credits or links the toolkit skill would
# match that and be overwritten, and so would a pointer the builder extended
# with notes of their own. Backslashes are turned forward first, so a pointer
# naming a Windows path still counts. The front matter must be exactly what
# the writers emit (a name line, a one-line folded description): a key of the
# builder's own, or a body `---` passing for the closing one, makes it theirs.
function Test-OwnPointer {
    param([string]$Text, [string]$OwnPath)
    $t = (($Text -replace '\\', '/') -replace "`r`n", "`n").TrimStart([char]0xFEFF)
    $pattern = '\A---\nname:[^\n]*\ndescription: >-\n  [^\n]*\n---\n\s*Read and follow the full skill at `(?:[^`\n]*/)?' +
        [regex]::Escape($OwnPath) + '`\.\s*\z'
    return [regex]::IsMatch($t, $pattern)
}

# Whole-file swap: write beside the target, then swap it in. Writing in place
# meant an install stopped mid-write left a truncated settings.json. File.Replace
# keeps the existing file's permissions and attributes on the new copy; a plain
# move would hand a locked-down file the folder's inherited permissions.
function Write-TextNoBom {
    param([string]$Path, [string]$Content)
    # One temp name per write, so two installs at once cannot swap in each
    # other's half-written file; and none is left behind if the swap fails.
    $tmp = "$Path.$([guid]::NewGuid().ToString('N')).nsls-tmp"
    try {
        [System.IO.File]::WriteAllText($tmp, $Content, $Utf8NoBom)
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            [System.IO.File]::Replace($tmp, $Path, [NullString]::Value)
        } else {
            Move-Item -Force -LiteralPath $tmp -Destination $Path
        }
    } finally {
        if (Test-Path -LiteralPath $tmp) {
            # ReplaceFile can fail after removing the original (it documents
            # this), leaving only the new copy here. Put it in place rather than
            # delete the only settings.json there is.
            if (-not (Test-Path -LiteralPath $Path)) {
                Move-Item -Force -LiteralPath $tmp -Destination $Path -ErrorAction SilentlyContinue
            } else {
                Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
            }
        }
    }
}

# settings.json is UTF-8 with no BOM, and Windows PowerShell 5.1 reads a
# BOM-less file as ANSI unless told otherwise. Any non-ASCII text Claude Code
# had saved there (a folder under an accented user name, a permission rule) was
# read as mojibake and written back that way, for good, on every re-run. A copy
# of the file as it was goes beside it first, because this rewrites it whole.
function Read-SettingsJson {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return [pscustomobject]@{} }
    $bak = "$Path.pre-nsls-install"
    Copy-Item -Force -LiteralPath $Path -Destination $bak
    # The copy holds the same settings, so it gets the same permissions, not
    # the folder's defaults.
    try { Set-Acl -LiteralPath $bak -AclObject (Get-Acl -LiteralPath $Path) } catch { }
    $parsed = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $parsed) { return [pscustomobject]@{} }
    return $parsed
}

# Add a directory to the persistent user PATH (and this session), idempotently.
function Add-ToUserPath {
    param([string]$Dir)
    $cur = [Environment]::GetEnvironmentVariable('Path', 'User'); if (-not $cur) { $cur = '' }
    if (($cur -split ';') -notcontains $Dir) {
        $new = if ($cur) { "$cur;$Dir" } else { $Dir }
        [Environment]::SetEnvironmentVariable('Path', $new, 'User')
    }
    if (($env:Path -split ';') -notcontains $Dir) { $env:Path = "$env:Path;$Dir" }
}

# Run a native command and return combined stdout+stderr as text WITHOUT letting
# stderr trip $ErrorActionPreference='Stop' (native stderr merged via 2>&1 under
# Stop otherwise raises a terminating NativeCommandError - e.g. gws prints a
# harmless "Using keyring backend" line to stderr). Used for --version probes.
function Invoke-Native {
    param([string]$Exe, [string[]]$CmdArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # ForEach-Object { "$_" } turns each stderr ErrorRecord back into git's own line. Without
    # it, Windows PowerShell 5.1 renders stderr as a NativeCommandError dump ("+ CategoryInfo
    # ..."), so a builder saw PowerShell noise instead of git's message (PC Test 4).
    try { $out = (& $Exe @CmdArgs 2>&1 | ForEach-Object { "$_" } | Out-String) } catch { $out = '' }
    finally { $ErrorActionPreference = $prev }
    return $out.Trim()
}

Write-Host ""
Write-Host "=== NSLS Builder Toolkit (Windows) ==="
if ($Test) { Write-Host "  (TEST MODE - installing into $ConfigDir; your real .claude is untouched)" }
Write-Host ""

# --- Prerequisites ---
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Git isn't installed yet - the toolkit needs it (a one-time setup)."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "  Installing Git for you via winget..."
        winget install --id Git.Git -e --source winget --accept-source-agreements --accept-package-agreements
        Write-Host ""
        Write-Host "  [ok] Git installed. Now CLOSE this PowerShell, open a NEW one, and re-run this"
        Write-Host "    installer - a fresh shell is needed so Git is on your PATH."
    } else {
        Write-Host "  Easiest path: if Claude Code offers to install Git, say YES."
        Write-Host "  Otherwise install it from https://git-scm.com/download/win (default options),"
        Write-Host "  then reopen PowerShell and re-run this installer."
    }
    if ($RunningFromFile) { exit 1 } else { return }
}

# Git Bash, specifically - not just git.exe. Every one of the plugin's hooks is
# registered in hooks.json as a bash command, because hooks.json has no OS
# conditional and bash is the one interpreter both platforms can share. Git for
# Windows ships bash, so this normally passes the moment git does; it is checked
# because the failure it prevents is silent. Without a callable bash the hooks
# do not error, they simply never run, and a builder with no gate and no credit
# logging looks exactly like a builder with nothing to report - which is the
# whole outage this release exists to end.
$bashOk = $false
$bashCmd = Get-Command bash -ErrorAction SilentlyContinue
if ($bashCmd) {
    try {
        $probe = (& bash -c 'printf NSLS-BASH-OK' 2>$null)
        $bashOk = ($probe -eq 'NSLS-BASH-OK')
    } catch { $bashOk = $false }
}
# Claude Code does not need bash on PATH: the desktop app and the CLI find Git
# Bash themselves, from CLAUDE_CODE_GIT_BASH_PATH or Git for Windows' own
# folder. On PC test 4 (2026-10-03), with C:\Program Files\Git\bin off PATH, the
# plugin's hooks and Claude's Bash tool ran fine while this warned on every run
# that the guardrails would never fire - a false alarm that would send builders
# to edit their PATH for nothing. So a Git Bash where Claude Code looks counts.
if (-not $bashOk) {
    $places = @()
    if ($env:CLAUDE_CODE_GIT_BASH_PATH) { $places += $env:CLAUDE_CODE_GIT_BASH_PATH }
    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    if ($gitCmd -and $gitCmd.Source) {
        # ...\Git\cmd\git.exe (or ...\Git\bin\git.exe) -> ...\Git\bin\bash.exe
        $places += (Join-Path (Split-Path (Split-Path $gitCmd.Source)) 'bin\bash.exe')
    }
    if ($env:ProgramFiles) { $places += (Join-Path $env:ProgramFiles 'Git\bin\bash.exe') }
    if (${env:ProgramFiles(x86)}) { $places += (Join-Path ${env:ProgramFiles(x86)} 'Git\bin\bash.exe') }
    if ($env:LOCALAPPDATA) { $places += (Join-Path $env:LOCALAPPDATA 'Programs\Git\bin\bash.exe') }
    # A file at the path is not proof: a folder, a stub or a broken install
    # would silence the warning with nothing able to run the hooks. Same
    # sentinel probe as the PATH check above, tried one place at a time.
    $foundBash = $places | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } | Where-Object {
        try { (& $_ -c 'printf NSLS-BASH-OK' 2>$null) -eq 'NSLS-BASH-OK' } catch { $false }
    } | Select-Object -First 1
    if ($foundBash) {
        $bashOk = $true
        Write-Host "Git Bash: found at $foundBash (Claude Code finds it there on its own)."
    }
}
if (-not $bashOk) {
    Write-Host ""
    Write-Host "Warning: Git Bash isn't installed."
    Write-Host "  The toolkit's hooks run through it, so without it the guardrails and the"
    Write-Host "  work-credit logging will be installed but will never fire."
    Write-Host "  Fix: reinstall Git for Windows from https://git-scm.com/download/win,"
    Write-Host "  accepting every default option, then reopen PowerShell and re-run this installer."
    Write-Host "  Continuing - everything else installs normally, and the hooks start working"
    Write-Host "  as soon as Git Bash is installed."
    Write-Host ""
}

if ($Test) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
} elseif (-not (Test-Path $ConfigDir)) {
    Write-Host "Error: Claude Code doesn't appear to be set up ($ConfigDir not found)."
    Write-Host "  Install Claude Code first, then re-run this script."
    if ($RunningFromFile) { exit 1 } else { return }
}

# --- Step 0: Provision runtime prerequisites (Python, gws, Node); VC++ check ---
# The document skills need Python + libraries: /gdoc-build needs python-docx,
# /nsls-slides needs python-pptx, and both are invoked through the nsls-python
# launcher written below (Mac/Linux get the same launcher from install.sh).
# /gdoc-edit needs gws; the signal MCP server needs Node. Provision what's missing, verify each
# with a real invocation, and DEGRADE to a warning on failure - a failed
# provision must never abort the toolkit install. Every step is idempotent: it
# skips work already done, so re-running is safe.
Write-Host ""
Write-Host "Step 0: Prerequisites (Python, gws, Node)..."
$PrereqReport = @()
$HasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)
$IsElevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$PyExe  = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
$GwsDir = Join-Path $env:LOCALAPPDATA 'Programs\gws'
$GwsExe = Join-Path $GwsDir 'gws.exe'

# --- (a) Python 3.12 (winget installs it per-user to the path above) ---
if (-not (Test-Path $PyExe)) {
    if ($HasWinget) {
        Write-Host "  Installing Python 3.12..."
        try { winget install --id Python.Python.3.12 -e --source winget --accept-source-agreements --accept-package-agreements 2>$null | Out-Null } catch {}
    }
}
# python-docx (/gdoc-build) + python-pptx (/nsls-slides). pip is idempotent - a
# no-op when already satisfied. python-pptx was missing here, so /nsls-slides
# failed on Windows with ModuleNotFoundError even on a fully provisioned machine.
if (Test-Path $PyExe) {
    # 'Continue' for the duration: under 'Stop', PowerShell 5.1 turns any redirected
    # pip stderr line (even a WARNING) into a terminating error mid-install.
    $prevEap = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { & $PyExe -m pip install --user python-docx python-pptx --quiet 2>$null | Out-Null } catch {}
    finally { $ErrorActionPreference = $prevEap }
}

# --- (a2) nsls-python shim - the launcher the document skills call ---
# Mac/Linux get ~/.local/bin/nsls-python from install.sh; this is the Windows
# half, so a skill's `nsls-python build.py` works identically on both. It pins
# the interpreter choice ONCE here instead of every skill hardcoding a version
# (stock Win11 `python`/`python3` are Microsoft Store stubs that print
# "Python was not found" and exit 0 - a naive check passes while nothing runs).
# Rewritten every run so a Python upgrade self-heals on the next install.
#
# The pip install above is `--user`, so the libraries are already importable by
# $PyExe without any PYTHONPATH. The shim sets it anyway for the SECOND location
# the gdoc-build skill documents on Windows ($env:LOCALAPPDATA\nsls-pydeps, for a
# manual --target repair) - belt-and-braces, not load-bearing.
$ShimPath = Join-Path (Join-Path $env:LOCALAPPDATA 'Programs\nsls-bin') 'nsls-python.cmd'
$ShimOk = $false
if (Test-Path $PyExe) {
    # EVERYTHING here is inside the try: $ErrorActionPreference is 'Stop' for the
    # whole script, so an ACL/disk failure on the bare New-Item would terminate
    # the installer instead of degrading to a prerequisite warning.
    try {
        $ShimDir = Join-Path $env:LOCALAPPDATA 'Programs\nsls-bin'
        New-Item -ItemType Directory -Force -Path $ShimDir | Out-Null
        $PyDeps  = Join-Path $env:LOCALAPPDATA 'nsls-pydeps'
        $shim = @"
@echo off
REM nsls-python - the Python the NSLS toolkit's document skills run on.
REM Generated by install.ps1; regenerated on every install. Don't edit.
if defined PYTHONPATH (set "PYTHONPATH=$PyDeps;%PYTHONPATH%") else (set "PYTHONPATH=$PyDeps")
"$PyExe" %*
"@
        # Must be BOM-less: cmd.exe treats a leading BOM as part of the first
        # line and the shim dies on "'<BOM>@echo' is not recognized" (the BOM shows as three odd characters).
        # Write then rename, so an interrupted write never leaves a truncated launcher.
        Write-TextNoBom -Path "$ShimPath.tmp" -Content $shim
        Move-Item -Force -Path "$ShimPath.tmp" -Destination $ShimPath
        # Claude Code runs commands through Git Bash, which does not apply PATHEXT,
        # so a bare `nsls-python` never finds the .cmd. Write an extensionless
        # sh wrapper beside it. LF endings only: a CR breaks the shebang line.
        # Forward slashes for Git Bash, and an apostrophe (C:\Users\O'Neil) escaped
        # as '\'' so it can't end the single-quoted string early.
        $PyExeFwd  = ($PyExe  -replace '\\', '/') -replace "'", "'\''"
        $PyDepsFwd = ($PyDeps -replace '\\', '/') -replace "'", "'\''"
        $bashShim = "#!/bin/sh`n" +
            "# nsls-python (Git Bash) - generated by install.ps1; regenerated on every install. Don't edit.`n" +
            "PYDEPS='$PyDepsFwd'`n" +
            "if [ -n `"`${PYTHONPATH:-}`" ]; then PYTHONPATH=`"`$PYDEPS;`$PYTHONPATH`"; else PYTHONPATH=`"`$PYDEPS`"; fi`n" +
            "export PYTHONPATH`n" +
            "exec '$PyExeFwd' `"`$@`"`n"
        $bashShimPath = Join-Path $ShimDir 'nsls-python'
        Write-TextNoBom -Path "$bashShimPath.tmp" -Content $bashShim
        Move-Item -Force -Path "$bashShimPath.tmp" -Destination $bashShimPath
        # Persistent user PATH + this session (exact-segment match, so a path
        # that merely CONTAINS the dir name doesn't suppress the append).
        Add-ToUserPath -Dir $ShimDir
        # Only true once the write AND the PATH registration both came back
        # clean. Existence alone is not proof: a stale .cmd from an earlier run
        # would survive a failed write and read as success.
        $ShimOk = (($env:Path -split ';') -contains $ShimDir)
    } catch { $ShimOk = $false }
}

# --- (b) gws CLI - download the Windows release zip, extract gws.exe, add to PATH ---
# (Per-user OAuth stays manual: `gws auth login`, run from the gdoc skills.)
if (-not (Test-Path $GwsExe) -and -not (Get-Command gws -ErrorAction SilentlyContinue)) {
    Write-Host "  Installing gws (Google Workspace CLI)..."
    try {
        New-Item -ItemType Directory -Path $GwsDir -Force | Out-Null
        $gzip = Join-Path $env:TEMP 'gws-win.zip'
        $gtmp = Join-Path $env:TEMP 'gws-extract'
        $gurl = 'https://github.com/googleworkspace/cli/releases/latest/download/google-workspace-cli-x86_64-pc-windows-msvc.zip'
        Invoke-WebRequest -Uri $gurl -OutFile $gzip -UseBasicParsing
        if (Test-Path $gtmp) { Remove-Item $gtmp -Recurse -Force }
        Expand-Archive -Path $gzip -DestinationPath $gtmp -Force
        $gfound = Get-ChildItem $gtmp -Recurse -Filter 'gws.exe' | Select-Object -First 1
        if ($gfound) { Copy-Item $gfound.FullName $GwsExe -Force }
        Remove-Item $gzip -Force -ErrorAction SilentlyContinue
        Remove-Item $gtmp -Recurse -Force -ErrorAction SilentlyContinue
    } catch {}
}
if (Test-Path $GwsExe) { Add-ToUserPath $GwsDir }

# --- (c) Node.js LTS - the signal MCP server runs on it ---
# This winget package needs elevation. From an interactive human shell winget
# raises a UAC prompt and can succeed; from a NON-interactive shell (an
# agent-driven install) the UAC prompt never reaches the screen and winget
# fails with exit 1602. Attempt it, but CAPTURE the outcome - the report below
# must give the real remedy (elevated shell), never re-suggest the same
# command that just failed.
$NodeInstallExit = $null
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    if ($HasWinget) {
        Write-Host "  Installing Node.js LTS (a UAC prompt may appear - click Yes)..."
        try {
            winget install --id OpenJS.NodeJS.LTS -e --source winget --accept-source-agreements --accept-package-agreements 2>$null | Out-Null
            $NodeInstallExit = $LASTEXITCODE
        } catch { $NodeInstallExit = -1 }
        # winget writes Node onto the machine PATH; refresh this session's PATH
        # so the verify below (and Step 3.5) can see it without a reopen.
        $mp = [Environment]::GetEnvironmentVariable('Path','Machine')
        $up = [Environment]::GetEnvironmentVariable('Path','User')
        $env:Path = (@($mp, $up) | Where-Object { $_ }) -join ';'
    }
}

# --- Verify each provision with a real invocation, and report ---
# Python + python-docx (probe the FULL path, never PATH `python` - on stock
# Win11 `python`/`python3` are Microsoft Store stubs that print a message and
# exit 0, so a naive check passes while nothing works).
if (Test-Path $PyExe) {
    $pyVer  = Invoke-Native $PyExe @('--version')
    # Probe BOTH libraries: python-docx powers /gdoc-build, python-pptx powers
    # /nsls-slides. Reporting only docx let a broken /nsls-slides ship green.
    $docxOk = (Invoke-Native $PyExe @('-c', 'import docx; print("ok")')) -match 'ok'
    $pptxOk = (Invoke-Native $PyExe @('-c', 'import pptx; print("ok")')) -match 'ok'
    # Verify through the SHIM the skills actually call, not just $PyExe. A shim
    # that exists but can't run (stale content, unreachable PATH, bad quoting)
    # is the worst outcome: the report reads green and every /gdoc-build still
    # dies on an import traceback. $ShimOk already gates on write+PATH success.
    $shimRuns = $false
    if ($ShimOk -and (Test-Path $ShimPath)) {
        $shimRuns = (Invoke-Native $ShimPath @('-c', 'import docx, pptx; print("ok")')) -match 'ok'
    }
    if ($pyVer -and $docxOk -and $pptxOk -and $shimRuns) {
        $PrereqReport += "  [ok]   Python: $pyVer (python-docx + python-pptx installed; nsls-python ready)"
    } elseif ($pyVer) {
        $missing = @()
        if (-not $docxOk) { $missing += 'python-docx' }
        if (-not $pptxOk) { $missing += 'python-pptx' }
        if ($missing.Count -gt 0) {
            $PrereqReport += "  [warn] Python: $pyVer but $($missing -join ' + ') missing - run: `"$PyExe`" -m pip install --user $($missing -join ' ')"
        }
        # Only call out the shim when it's the DISTINCT problem - if a library is
        # missing the shim can't import either, and two warnings for one cause
        # just reads as two broken things.
        if ($missing.Count -eq 0 -and -not $shimRuns) {
            $PrereqReport += "  [warn] Python: $pyVer with both libraries, but the nsls-python launcher isn't working"
            $PrereqReport += "                  (the document skills call it by name). Re-run this installer to rewrite it."
        }
    } else {
        $PrereqReport += "  [warn] Python 3.12 present but not runnable - reinstall from https://www.python.org/downloads/ (3.12)"
    }
} else {
    $PrereqReport += "  [warn] Python 3.12 not installed - /gdoc-build and /nsls-slides need it. Install: winget install Python.Python.3.12"
}

# Node
$NodeCmd = Get-Command node -ErrorAction SilentlyContinue
if ($NodeCmd) {
    $nodeVer = Invoke-Native $NodeCmd.Source @('--version')
    if ($nodeVer -match 'v\d') { $PrereqReport += "  [ok]   Node: $nodeVer" }
    else { $PrereqReport += "  [warn] Node present but 'node --version' failed - reopen PowerShell and retry." }
} else {
    # Blocking action item, not a passing warn: without Node, /signal-setup
    # fails and the day-planner dashboard (the Step 7 payoff) stays locked.
    $PrereqReport += "  [ACTION NEEDED] Node.js is not installed - /signal-setup and the day-planner dashboard won't work without it."
    # $NodeInstallExit distinguishes "needs elevation" (retry as admin will work)
    # from any other winget failure (download error, source error, package not
    # found) where re-running the identical command as admin changes nothing.
    # Surface the code and route to the manual installer in that case, rather
    # than prescribing a retry that already failed.
    $NodeRetryWorthwhile = $true
    if ($NodeInstallExit -eq 1602) {
        $PrereqReport += "                  The automatic install failed (winget 1602): it needs an elevation prompt this shell can't show."
    } elseif ($null -ne $NodeInstallExit -and $NodeInstallExit -ne 0) {
        $PrereqReport += "                  The automatic install failed (winget exit $NodeInstallExit) - not an elevation problem, so re-running it as administrator won't help."
        $NodeRetryWorthwhile = $false
    } elseif (-not $IsElevated) {
        $PrereqReport += "                  Installing it needs an administrator PowerShell."
    }
    if ($NodeRetryWorthwhile -and $HasWinget) {
        $PrereqReport += "                  Open one: Start -> type 'powershell' -> right-click Windows PowerShell -> Run as administrator. Then run:"
        $PrereqReport += "                  winget install --id OpenJS.NodeJS.LTS -e --source winget"
    } else {
        $PrereqReport += "                  Download Node LTS from https://nodejs.org and run the installer (no winget needed)."
        if (-not $HasWinget) {
            $PrereqReport += "                  (This machine has no winget, so that's the only path.)"
        }
    }
    $PrereqReport += "                  Afterwards restart Claude Code and run /signal-setup."
}

# (d) MS Visual C++ x64 runtime - gws.exe (a Rust/MSVC binary) needs it or it
# exits 0xC0000135 with NO output. A silent install is impossible from a
# non-interactive shell (winget returns 1602; the UAC prompt never reaches the
# screen), so we only DETECT it and, if missing, stage the redist in Downloads
# and print the manual click-path. We never attempt to install it here.
$VcOk = Test-Path (Join-Path $env:SystemRoot 'System32\vcruntime140.dll')
if (-not $VcOk) {
    $vcDl = Join-Path $env:USERPROFILE 'Downloads\VC_redist.x64.exe'
    if (-not (Test-Path $vcDl)) {
        try { Invoke-WebRequest -Uri 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile $vcDl -UseBasicParsing } catch {}
    }
    if (Test-Path $vcDl) {
        $PrereqReport += "  [ACTION NEEDED] Microsoft VC++ x64 runtime is missing - gws can't run without it."
        $PrereqReport += "                  Open File Explorer -> Downloads -> double-click VC_redist.x64.exe -> click Yes -> Install."
    } else {
        $PrereqReport += "  [ACTION NEEDED] Microsoft VC++ x64 runtime is missing (and the download failed)."
        $PrereqReport += "                  Download & install: https://aka.ms/vs/17/release/vc_redist.x64.exe"
    }
}

# gws (verified last so the message can point back at the VC++ step)
$GwsResolved = if (Test-Path $GwsExe) { $GwsExe } elseif (Get-Command gws -ErrorAction SilentlyContinue) { (Get-Command gws).Source } else { $null }
if ($GwsResolved) {
    $gwsRaw  = Invoke-Native $GwsResolved @('--version')
    $gwsLine = ($gwsRaw -split '\r?\n' | Where-Object { $_ -match 'gws' } | Select-Object -First 1)
    if ($gwsLine) { $PrereqReport += "  [ok]   gws: $($gwsLine.Trim())" }
    elseif (-not $VcOk) { $PrereqReport += "  [warn] gws installed but can't run yet - install the VC++ runtime (above), then it works." }
    else { $PrereqReport += "  [warn] gws installed but not runnable - reopen PowerShell (PATH refresh) and retry: gws --version" }
} else {
    $PrereqReport += "  [warn] gws not installed - /gdoc-build & /gdoc-edit need it (see skills/gws Windows install)."
}

# --- Step 1: Clone / update the org toolkit ---
Write-Host "Step 1: Installing org skills..."
New-Item -ItemType Directory -Path $LocalDir -Force | Out-Null
# A failed update must say so: errors used to go to $null and "Done." printed regardless.
# A failed FETCH changes nothing on disk, so warn and carry on. A failed RESET
# may leave files half-updated, so that one stops rather than claim "unchanged".
$UpdateFailed = $false   # read by the closing banner, so it doesn't claim success
if (Test-Path (Join-Path $PluginDir '.git')) {
    Write-Host "  Updating existing installation..."
    $updOut = Invoke-Native 'git' @('-C', $PluginDir, 'fetch', 'origin', $RepoBranch, '--quiet')
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  WARNING: couldn't download the update, so your toolkit is UNCHANGED (still the version you had)."
        ($updOut -split "`n" | Select-Object -Last 3) | ForEach-Object { Write-Host "    $_" }
        # Name the likely cause: a test branch override beats "check your internet".
        if ($env:NSLS_TOOLKIT_BRANCH) { Write-Host "  (You set NSLS_TOOLKIT_BRANCH=$($env:NSLS_TOOLKIT_BRANCH): check that branch exists, then re-run this installer.)" }
        else { Write-Host "  Check your internet connection, then re-run this installer." }
        $UpdateFailed = $true
    } else {
        $updOut = Invoke-Native 'git' @('-C', $PluginDir, 'reset', '--hard', "origin/$RepoBranch", '--quiet')
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ERROR: the update stopped partway, so the toolkit may be incomplete."
            ($updOut -split "`n" | Select-Object -Last 3) | ForEach-Object { Write-Host "    $_" }
            Write-Host "  Re-run this installer to finish it."
            if ($RunningFromFile) { exit 1 } else { return }
        }
        Write-Host "  Done."
    }
} else {
    Write-Host "  Cloning plugin..."
    $null = Invoke-Native 'git' @('clone', '--branch', $RepoBranch, $RepoUrl, $PluginDir, '--quiet')
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: couldn't download the toolkit. Check your internet connection, then re-run this installer."
        if ($RunningFromFile) { exit 1 } else { return }
    }
    Write-Host "  Done."
}
# The receipt: which code is actually installed.
$installed = Invoke-Native 'git' @('-C', $PluginDir, 'log', '-1', '--format=%h %s')
if ($LASTEXITCODE -ne 0 -or -not $installed) { $installed = 'unknown' }
Write-Host "  Installed commit: $installed"

# --- Find the claude CLI (best-effort; several steps need it) ---
$ClaudeBin = (Get-Command claude -ErrorAction SilentlyContinue).Source
if (-not $ClaudeBin) {
    foreach ($c in @(
        (Join-Path $env:APPDATA 'npm\claude.cmd'),
        (Join-Path $env:APPDATA 'npm\claude.ps1'),
        (Join-Path $env:USERPROFILE '.local\bin\claude.exe'),
        (Join-Path $env:USERPROFILE '.claude\bin\claude.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\claude\claude.exe')
    )) { if ($c -and (Test-Path $c)) { $ClaudeBin = $c; break } }
}
# Desktop app bundles the CLI at %APPDATA%\Claude\claude-code\<version>\claude.exe
# (confirmed on a real Windows box; also try the -vm variant). Not on PATH and
# not in the list above, which is why Steps 3 / 3.5 were silently skipping.
# Pick the highest version.
# The Microsoft Store app keeps the real copy under its package's LocalCache:
# a Store (MSIX) install virtualises %APPDATA%\Claude for the app alone, so
# from this shell the APPDATA path is empty and every run said "Could not find
# the 'claude' CLI" and skipped Steps 3 and 3.5 (PC test 4, 2026-10-03). Same
# search as session-start.py's _find_claude: both roots, highest version wins.
if (-not $ClaudeBin) {
    $roots = @(Join-Path $env:APPDATA 'Claude')
    $roots += @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Packages\Claude_*') -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude' })
    # Two layouts: <version>\claude.exe, and <version>\<hash>\claude.exe, which
    # the Store app moved to on 2026-10-03. The version is the folder directly
    # under claude-code either way; the newest file breaks a tie.
    $cands = foreach ($root in $roots) {
        foreach ($sub in @('claude-code', 'claude-code-vm')) {
            Get-ChildItem -Path (Join-Path $root "$sub\*\claude.exe") -ErrorAction SilentlyContinue
            Get-ChildItem -Path (Join-Path $root "$sub\*\*\claude.exe") -ErrorAction SilentlyContinue
        }
    }
    $cand = $cands |
            Sort-Object -Property @{ Expression = {
                $vdir = if ($_.Directory.Parent.Name -like 'claude-code*') { $_.Directory } else { $_.Directory.Parent }
                try { [version]$vdir.Name } catch { [version]'0.0.0' } } }, LastWriteTime |
            Select-Object -Last 1
    if ($cand) { $ClaudeBin = $cand.FullName }
}

# --- Step 2: Enable plugin + register hooks in settings.json ---
Write-Host ""
Write-Host "Step 2: Enabling plugin and registering hooks..."

# Seed a fresh test config dir with an empty settings.json (BOM-less).
if ($Test -and -not (Test-Path $Settings)) { Write-TextNoBom $Settings '{}' }

# Hooks the toolkit plugin has already been seen running on this machine. The
# migration retires those settings.json shims, and once session-start is proven
# it retires the org pointer files too. Re-adding them here only undid it: every
# re-install put the shims and 69 pointers back, and the next session start
# removed them again (PC test 4, 2026-10-03). plugin_beacon.py decides, so the
# installer and the migration use one definition of evidence.
function Get-BeaconAnswer {
    param([string]$Flag)
    $beaconPy = Join-Path $HooksDir 'plugin_beacon.py'
    $runner = if (Test-Path $PyExe) { $PyExe } elseif (Get-Command py -ErrorAction SilentlyContinue) { 'py' } else { $null }
    if (-not $runner -or -not (Test-Path $beaconPy)) { return @() }
    $savedCfg = $env:CLAUDE_CONFIG_DIR
    try {
        $env:CLAUDE_CONFIG_DIR = $ConfigDir
        $beaconArgs = @($beaconPy, $Flag)
        if ($runner -eq 'py') { $beaconArgs = @('-3') + $beaconArgs }
        return @(& $runner @beaconArgs 2>$null | ForEach-Object { "$_".Trim() } | Where-Object { $_ })
    } catch { return @() } finally { $env:CLAUDE_CONFIG_DIR = $savedCfg }
}
$Proven = @(Get-BeaconAnswer '--proven')

$ssCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$HooksDir\session-start.ps1`""
$ptCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$HooksDir\skill-event.ps1`""

$cfg = Read-SettingsJson $Settings

# No `nsls-builder-toolkit@local` key. It named a marketplace that does not
# exist, so it never enabled anything; what it did do was look like a second
# live installation, and that appearance is what produced the false conclusion
# that bundled plugin hooks do not load. Step 3 installs the real plugin.

if (-not ($cfg.PSObject.Properties.Name -contains 'hooks') -or $null -eq $cfg.hooks) {
    $cfg | Add-Member -NotePropertyName hooks -NotePropertyValue ([pscustomobject]@{}) -Force
}

# Drop any prior NSLS entries for an event (matched by script filename) so
# re-running never duplicates and stale registrations get replaced; non-NSLS
# hooks are kept. Entry by entry, not group by group: a builder's own hook in
# the same matcher group as ours used to go with it, on every re-install. A
# group left with no entries is dropped. @(...) forces an array (PS unrolls a
# single match to a scalar).
function Without-Matching {
    param($EventArray, [string]$Needle)
    if ($null -eq $EventArray) { return @() }
    $kept = @()
    foreach ($group in @($EventArray)) {
        $entries = @($group.hooks)
        $rest = @($entries | Where-Object { "$($_.command)" -notlike "*$Needle*" })
        if ($rest.Count -eq $entries.Count) { $kept += $group; continue }
        if ($rest.Count -gt 0) {
            $group.hooks = $rest
            $kept += $group
        }
    }
    @($kept)
}

# SessionStart timeout 90s: must clear git pull + a replayed ping + the live ping
# on a Railway cold start (parity with install.sh, which was killed at 15s).
$ss = @(Without-Matching $cfg.hooks.SessionStart 'session-start.ps1')
if ($Proven -notcontains 'session-start') {
    $ss += , @{ matcher = 'startup'; hooks = @(@{ type = 'command'; command = $ssCmd; timeout = 90; statusMessage = 'Syncing NSLS toolkit...' }) }
}

$pt = @(Without-Matching $cfg.hooks.PreToolUse 'skill-event.ps1')
if ($Proven -notcontains 'skill-event') {
    $pt += , @{ matcher = 'Skill'; hooks = @(@{ type = 'command'; command = $ptCmd; timeout = 5; statusMessage = 'Logging skill use so you get NSLS credit (nothing else)...' }) }
}

# Guardrail gate - same registration reasoning as the skill-event hook: bundled
# plugin hooks don't reliably load, so without this the four hard gates never
# run. The gate is Python (shared with macOS); `py -3` is the Windows launcher
# the toolkit already provisions. A machine with no Python simply never runs
# the hook - the gate fails open by absence, matching its own design rules.
$gateCmd = 'py -3 "' + (Join-Path $ConfigDir 'local-plugins\nsls-builder-toolkit\hooks\guardrail-gate.py') + '"'
$pt = @(Without-Matching $pt 'guardrail-gate.py')
if ($Proven -notcontains 'guardrail-gate') {
    $pt += , @{ matcher = 'Bash|PowerShell|Write|Edit'; hooks = @(@{ type = 'command'; command = $gateCmd; timeout = 10; statusMessage = 'Checking builder guardrails...' }) }
}

$cfg.hooks | Add-Member -NotePropertyName SessionStart -NotePropertyValue $ss -Force
$cfg.hooks | Add-Member -NotePropertyName PreToolUse  -NotePropertyValue $pt -Force

# BOM-less write: PowerShell 5.1 `Set-Content -Encoding utf8` emits a BOM that
# breaks json.load() for every downstream consumer of settings.json.
Write-TextNoBom $Settings ($cfg | ConvertTo-Json -Depth 12)
if ($Proven.Count -gt 0) {
    Write-Host "  The toolkit plugin already runs these hooks here, so no shim was added: $($Proven -join ', ')."
} else {
    Write-Host "  Enabled plugin + registered SessionStart / PreToolUse(Skill) hooks."
}

# --- Step 2.5: Fire an install event to the Automation Tracker (best-effort) ---
$InstallEmail = ""
$EnvFile = Join-Path $LocalDir 'nsls-personal-toolkit\.env'
if (Test-Path $EnvFile) {
    $line = (Get-Content $EnvFile | Where-Object { $_ -match '^BUILDER_EMAIL=' } | Select-Object -First 1)
    if ($line) { $InstallEmail = ($line -replace '^BUILDER_EMAIL=', '').Trim('"') }
}
if (-not $InstallEmail) { $InstallEmail = (& git config user.email 2>$null) }
if (-not $InstallEmail) { $InstallEmail = "$($env:USERNAME)@$($env:COMPUTERNAME)" }

# Persist the EXACT provisional identity used for the install/skill events so
# /nsls-setmeup Step 1.5 can reconcile early events WITHOUT recomputing. On Windows Git
# Bash the old recompute (`${USER:-unknown}@$(hostname -s)`) yields
# `unknown@unknown` and could never match this. Written beside the toolkit (NOT
# .env, which doesn't exist yet); untracked (see .gitignore). Idempotent.
try { Write-TextNoBom (Join-Path $PluginDir '.install-identity') $InstallEmail } catch {}

# gh is fully optional and only feeds github_username. Guard the probe: a MISSING
# `gh` raises a PowerShell-engine error that `2>$null` does NOT suppress, which
# under $ErrorActionPreference='Stop' would kill everything after this point.
$InstallGh = ""
if (Get-Command gh -ErrorAction SilentlyContinue) {
    try { $InstallGh = (& gh api user --jq .login 2>$null) } catch { $InstallGh = "" }
}
try {
    $body = @{ builder_email = $InstallEmail; github_username = $InstallGh;
               platform = 'windows'; install_source = 'cc-builder-kit' } | ConvertTo-Json -Compress
    Invoke-RestMethod -Uri "$Tracker/install-event" -Method Post -Body $body `
        -ContentType 'application/json' -TimeoutSec 40 | Out-Null
} catch { }  # a failed ping never blocks the install

# --- Step 3: Install marketplace plugins ---
Write-Host ""
Write-Host "Step 3: Installing recommended plugins..."
if ($ClaudeBin) {
    function Install-Plugin {
        param([string]$Name, [string]$Spec, [string]$Market)
        $installed = (& $ClaudeBin plugin list 2>$null | Out-String)
        if ($installed -match [regex]::Escape($Name)) { Write-Host "  ${Name}: already installed"; return }
        # A native command's non-zero exit does NOT trip try/catch, so we check
        # $LASTEXITCODE and warn explicitly. Redirect stderr to $null (NOT `2>&1`):
        # merging stderr into the pipeline under $ErrorActionPreference='Stop'
        # raises a terminating NativeCommandError before the warning below runs.
        if ($Market) {
            & $ClaudeBin plugin marketplace add $Market 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { Write-Host "  Warning: failed to add $Name marketplace. Retry: claude plugin marketplace add $Market"; return }
        }
        Write-Host "  Installing $Name..."
        & $ClaudeBin plugin install $Spec 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Host "  Warning: '$Name' install failed. Retry: claude plugin install $Spec" }
    }
    # Migrate compound off the renamed 'every-marketplace' (parity with install.sh).
    if ((& $ClaudeBin plugin marketplace list 2>$null | Out-String) -match 'every-marketplace') {
        Write-Host "  Migrating compound-engineering off 'every-marketplace'..."
        foreach ($scope in 'local','project','user') {
            & $ClaudeBin plugin disable compound-engineering@every-marketplace --scope $scope 2>$null | Out-Null
        }
        & $ClaudeBin plugin uninstall compound-engineering@every-marketplace 2>$null | Out-Null
        & $ClaudeBin plugin uninstall compound-engineering 2>$null | Out-Null
        & $ClaudeBin plugin marketplace remove every-marketplace 2>$null | Out-Null
    }
    # superpowers needs its marketplace registered first - a bare install spec
    # with no marketplace can never resolve on a fresh machine.
    # The org toolkit itself. Nothing on Windows has ever installed it: the
    # self-migration's stage A only runs from session-start.py's main(), and
    # session-start.ps1 enters Python solely through the guardrails block. That
    # is why no PC has the plugin, its three agents, or its bundled hooks -
    # not a shortage of installs. With the plugin present, hooks.json is what
    # registers this machine's hooks, and the settings.json entries below
    # become the transitional copy that stage B retires once per-hook beacons
    # prove the plugin's own hooks fire here.
    # The plugin must come from the same branch as the clone in Step 1. Without a
    # pin, 'marketplace add' takes the repo's default branch, so testing a branch
    # ran the branch's hooks (the clone) with main's skills (the plugin). A local
    # folder can't carry a pin: Claude Code reads it as it is on disk.
    $ToolkitMarket = $RepoUrl
    if ($RepoBranch -ne 'main' -and $RepoUrl -match '^(https?://|ssh://|git@)') {
        $ToolkitMarket = "$RepoUrl#$RepoBranch"
    }
    # Install-Plugin skips a plugin that is already installed, so on a machine
    # that has the toolkit the pin never took effect. Start the plugin over when
    # it can't be what was asked for: on every run that names a branch or a repo
    # (a branch moves without a version bump, which 'plugin update' ignores), and
    # on a plain run that finds a test branch or another repo still registered.
    # The normal case, production and unpinned, is left alone.
    $Testing = [bool]("$env:NSLS_TOOLKIT_BRANCH$env:NSLS_TOOLKIT_REPO")
    $Registry = Join-Path $ConfigDir 'plugins\known_marketplaces.json'
    $Repoint = $false
    if (Test-Path $Registry) {
        try {
            $src = (Get-Content $Registry -Raw -Encoding UTF8 | ConvertFrom-Json).'nsls-toolkit'.source
            if ($src) {
                $url = if ($src.url) { $src.url } elseif ($src.path) { $src.path } elseif ($src.repo) { "https://github.com/$($src.repo)" } else { '' }
                $norm = { param($u) $u = "$u".Trim().TrimEnd('/').ToLowerInvariant(); if ($u.EndsWith('.git')) { $u.Substring(0, $u.Length - 4) } else { $u } }
                $Repoint = $Testing -or [bool]$src.ref -or ((& $norm $url) -ne (& $norm $RepoUrl))
            }
        } catch { }  # unreadable registry: leave it, as before
    }
    if ($Repoint) {
        Write-Host "  Pointing the toolkit plugin at $RepoBranch..."
        & $ClaudeBin plugin marketplace remove nsls-toolkit 2>$null | Out-Null
    }
    Install-Plugin 'nsls-builder-toolkit' 'nsls-builder-toolkit@nsls-toolkit' `
        $ToolkitMarket
    # The plugin's half of the receipt. On a test run, a plugin that isn't the
    # commit the clone is on is the failure the pin above exists to prevent.
    try {
        $entry = (Get-Content (Join-Path $ConfigDir 'plugins\installed_plugins.json') -Raw -Encoding UTF8 |
            ConvertFrom-Json).plugins.'nsls-builder-toolkit@nsls-toolkit' | Where-Object { $_.gitCommitSha } | Select-Object -First 1
        if ($entry) {
            $sha = "$($entry.gitCommitSha)"
            Write-Host "  Plugin commit: $($sha.Substring(0, [Math]::Min(7, $sha.Length)))"
            $clone = "$(Invoke-Native 'git' @('-C', $PluginDir, 'rev-parse', 'HEAD'))".Trim()
            if ($env:NSLS_TOOLKIT_BRANCH -and $clone -match '^[0-9a-f]{40}$' -and $clone -ne $sha) {
                Write-Host "  [!] The plugin is not the commit you asked for ($($clone.Substring(0, 7)))."
                Write-Host "      Re-run this installer; if it happens again, the branch moved mid-install."
            }
        }
    } catch { }  # no registry yet: nothing to report

    Install-Plugin 'superpowers' 'superpowers@superpowers-marketplace' `
        'https://github.com/obra/superpowers-marketplace.git'
    Install-Plugin 'compound-engineering' 'compound-engineering@compound-engineering-plugin' `
        'https://github.com/EveryInc/compound-engineering-plugin.git'
} else {
    Write-Host "  Could not find the 'claude' CLI. After your next Claude Code session, run /nsls-setmeup."
}

# --- Step 3.5: Register bundled MCP servers (A1 parity: stdio + http) ---
Write-Host ""
Write-Host "Step 3.5: Registering bundled MCP servers..."
$McpJson = Join-Path $PluginDir '.mcp.json'
# Once the toolkit plugin is installed it registers these servers itself. A
# user-scope copy beside it is the duplicate the migration removes, and the
# reason every session showed two "signal ... Connection closed" errors
# (PC test, 2026-10-03). Step 3 above installs the plugin, so this check
# comes after it.
$PluginInstalled = (@(Get-BeaconAnswer '--installed') -contains 'installed')
if ($PluginInstalled) {
    Write-Host "  Skipped: the toolkit plugin registers its own MCP servers."
} elseif ($ClaudeBin -and (Test-Path $McpJson)) {
    function Expand-Vars {
        param([string]$v)
        if ($null -eq $v) { return $v }
        $v = $v -replace [regex]::Escape('${CLAUDE_PLUGIN_ROOT}'), $PluginDir
        [regex]::Replace($v, '\$\{([A-Z0-9_]+)\}', {
            param($m) $val = [Environment]::GetEnvironmentVariable($m.Groups[1].Value)
            if ($val) { $val } else { $m.Value }
        })
    }
    function Has-Unresolved { param([string]$v) $v -match '\$\{[A-Z0-9_]+\}' }

    $servers = (Get-Content $McpJson -Raw | ConvertFrom-Json).mcpServers
    $new = 0; $skipped = @()
    foreach ($p in $servers.PSObject.Properties) {
        $name = $p.Name; $s = $p.Value
        $stype = if ($s.PSObject.Properties.Name -contains 'type') { $s.type } else { 'stdio' }
        if ($stype -eq 'http') {
            # http servers take a URL + auth header, NOT a command. The bearer
            # tokens don't exist on a fresh machine - defer to /signal-setup
            # rather than register a server that 401s silently.
            $url = Expand-Vars $s.url
            $headerArgs = @(); $blocked = (Has-Unresolved $url)
            if ($s.PSObject.Properties.Name -contains 'headers') {
                foreach ($h in $s.headers.PSObject.Properties) {
                    $hv = Expand-Vars $h.Value
                    if (Has-Unresolved $hv) { $blocked = $true }
                    $headerArgs += @('--header', "$($h.Name): $hv")
                }
            }
            if ($blocked) { $skipped += $name; continue }
            $mcpArgs = @('mcp','add','--transport','http',$name,$url,'--scope','user') + $headerArgs
        } else {
            $cmd = Expand-Vars $s.command
            $sargs = @(); if ($s.PSObject.Properties.Name -contains 'args') { $sargs = @($s.args | ForEach-Object { Expand-Vars $_ }) }
            $mcpArgs = @('mcp','add',$name,'--scope','user','--env',"CLAUDE_PLUGIN_ROOT=$PluginDir")
            if ($s.PSObject.Properties.Name -contains 'env') {
                foreach ($e in $s.env.PSObject.Properties) { $mcpArgs += @('--env', "$($e.Name)=$(Expand-Vars $e.Value)") }
            }
            $mcpArgs += @('--', $cmd) + $sargs
        }
        # Capture via Invoke-Native so `claude`'s stderr can't raise a terminating
        # NativeCommandError under $ErrorActionPreference='Stop'.
        $out = Invoke-Native $ClaudeBin $mcpArgs
        if ($out -match 'already exists') { Write-Host "  ${name}: already registered" }
        elseif ($out -match 'Added') { Write-Host "  ${name}: registered (user scope)"; $new++ }
        else { Write-Host "  ${name}: registration failed - $out" }
    }
    Write-Host "  $new MCP server(s) newly registered (restart Claude Code to load)"
    if ($skipped.Count) { Write-Host "  Deferred (needs an access token): $($skipped -join ', ') - run /signal-setup to connect these." }
    # signal is a stdio server that runs on Node. Registration succeeds either
    # way, but without Node the server reports "Failed to connect" on restart -
    # so say what to do rather than leave a bare failed server.
    if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
        Write-Host "  Note: 'signal' needs Node.js, which isn't installed - install Node, then run /signal-setup."
    }
} else {
    Write-Host "  Skipped - 'claude' CLI or .mcp.json not found."
}

# --- Step 4: Sync slash-command pointer skills ---
Write-Host ""
Write-Host "Step 4: Creating slash-command pointers..."
New-Item -ItemType Directory -Path $SkillsDir -Force | Out-Null
$count = 0
# Once the plugin's session start is proven, the plugin delivers these skills
# itself and the migration retires the pointers; writing them again only put
# every skill in the list twice until the next session start.
$pointerSources = if ($Proven -contains 'session-start') { @() } else { @(Get-ChildItem (Join-Path $PluginDir 'skills') -Directory) }
if ($Proven -contains 'session-start') { Write-Host "  Skipped: the toolkit plugin provides these skills on this machine." }
foreach ($skillFolder in $pointerSources) {
    $src = Join-Path $skillFolder.FullName 'SKILL.md'
    if (-not (Test-Path $src)) { continue }
    $content = Get-Content $src -Raw -Encoding UTF8
    $fmName = [regex]::Match($content, '(?m)^name:\s*(.+)$')
    if (-not $fmName.Success) { continue }
    $name = $fmName.Groups[1].Value.Trim()
    $destDir = Join-Path $SkillsDir $skillFolder.Name
    $destMd  = Join-Path $destDir 'SKILL.md'
    # Same exact-ownership rule as session-start.ps1: a file that merely
    # mentions a toolkit path may be a skill the builder wrote.
    $ownPath = "local-plugins/nsls-builder-toolkit/skills/$($skillFolder.Name)/SKILL.md"
    if (Test-Path $destMd) {
        $existing = [string](Get-Content $destMd -Raw -Encoding UTF8)
        if (-not (Test-OwnPointer -Text $existing -OwnPath $ownPath)) { continue }
    }
    if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir | Out-Null }
    $ptr = "~/.claude/$ownPath"
    $pointer = @"
---
name: $name
description: >-
  NSLS Builder Toolkit skill: $($skillFolder.Name)
---

Read and follow the full skill at ``$ptr``.
"@
    Write-TextNoBom $destMd ($pointer + "`r`n")
    $count++
}
Write-Host "  $count skill pointers synced"

# --- Done ---
$skillTotal = (Get-ChildItem (Join-Path $PluginDir 'skills') -Directory).Count
if ($PrereqReport.Count) {
    Write-Host ""
    Write-Host "Prerequisite check:"
    $PrereqReport | ForEach-Object { Write-Host $_ }
}
Write-Host ""
Write-Host "==============================="
if ($UpdateFailed) { Write-Host "  NSLS Builder Toolkit set up, but NOT updated (see the WARNING above)." }
else { Write-Host "  NSLS Builder Toolkit installed!" }
Write-Host "==============================="
Write-Host ""
if ($ClaudeBin) {
    Write-Host "  ORG SKILLS ($skillTotal skills), plus superpowers + compound-engineering."
} else {
    # Honest banner: without the CLI, Steps 3 and 3.5 were skipped - don't imply
    # a clean install. Name exactly what didn't happen and how to finish it.
    Write-Host "  ORG SKILLS ($skillTotal skills) installed and enabled."
    Write-Host ""
    Write-Host "  NOTE: the 'claude' CLI wasn't found, so these steps were SKIPPED:"
    Write-Host "    - Step 3:   plugins (superpowers, compound-engineering) - NOT installed"
    Write-Host "    - Step 3.5: bundled MCP servers (e.g. signal) - NOT registered"
    Write-Host "  Finish them after your first Claude Code session by running:  /nsls-setmeup"
    Write-Host "  (or re-run this installer from a shell where 'claude' is on PATH)."
}
Write-Host ""
if ($Test) {
    Write-Host "=== TEST INSTALL ==="
    Write-Host "  Everything went into: $ConfigDir (your real .claude was NOT touched)."
    Write-Host "  NOTE: -Test is only usable from the terminal via CLAUDE_CONFIG_DIR; the"
    Write-Host "  desktop app always launches against your real .claude."
    Write-Host "  Reset with:  Remove-Item -Recurse -Force `"$ConfigDir`""
} else {
    Write-Host "=== NEXT STEP ==="
    Write-Host "  1. Restart Claude Code (quit and reopen - a restart loads the MCP servers"
    Write-Host "     and hooks). In the desktop app, click Code (top left) when it reopens."
    Write-Host "  2. Say:  /nsls-setmeup"
    Write-Host "     It connects your tools (Slack, Google Drive, Calendar, Gmail, Fathom -"
    Write-Host "     one at a time, with you) and offers the personal productivity skills."
}
Write-Host ""
# Explicit success exit under -File: native calls above leak their code into
# $LASTEXITCODE, so a good install would otherwise return non-zero and any
# wrapper checking the code (or /nsls-setmeup's re-provision) would treat it as failed.
# Under a bare `iwr | iex` paste there's no wrapper to see a code and `exit`
# would close the user's window, so just return.
if ($RunningFromFile) { exit 0 } else { return }
