# session-start.ps1 - Windows SessionStart hook for the NSLS toolkits.
# Windows counterpart to session-start.py (which needs python3 that Windows
# lacks). Does the same three things:
#   1. git pull both toolkits to get latest
#   2. re-sync pointer skills into ~/.claude/skills/
#   3. fire the tracker session ping (detached, non-blocking)
# Fast and silent on failure. Registered in settings.json by install.ps1.
$ErrorActionPreference = 'SilentlyContinue'

# --- hook start time ---
# When this hook started, for the plugin update in step 4: Python decides there
# whether enough of the 90-second hook limit is left to run it. Whole Unix
# seconds, so no locale can turn the number into "1792269584,5". A copy that
# re-runs itself after pulling an update (step 1) passes its environment on, so
# a start time from the last 90 seconds is kept: the re-run spends the same
# 90 seconds, not a fresh allowance.
$hookNow = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$hookStarted = 0L
if (-not ([long]::TryParse("$env:NSLS_HOOK_STARTED_AT", [ref]$hookStarted) -and
          ($hookNow - $hookStarted) -ge 0 -and ($hookNow - $hookStarted) -le 90)) {
    $env:NSLS_HOOK_STARTED_AT = [string]$hookNow
}
# --- end hook start time ---

$ClaudeDir   = Join-Path $env:USERPROFILE '.claude'
$SkillsDir   = Join-Path $ClaudeDir 'skills'
$LocalDir    = Join-Path $ClaudeDir 'local-plugins'
$BuilderDir  = Join-Path $LocalDir  'nsls-builder-toolkit'
$PersonalDir = Join-Path $LocalDir  'nsls-personal-toolkit'

# --- 1. update (direct calls; the prior Start-Process form failed silently on
#        Windows, freezing toolkits weeks behind). ff-only never merges.
#        No remote/refspec: each checkout follows its configured
#        upstream, so a customized fork (NSLS_PERSONAL_REPO/BRANCH) or a pinned
#        checkout is never fast-forwarded onto a branch it doesn't track.
#        A pull refused by the checkout's own state (divergence, dirty tree) is
#        announced on stdout - SessionStart stdout reaches the model's context -
#        so a frozen toolkit is never silent. Offline failures stay quiet. ---
# git reads these ahead of -C, so a session launched from inside a git hook (git
# exports GIT_DIR and GIT_INDEX_FILE to its hooks) would aim every git call below
# at that repository instead of the toolkit. git's own list, from
# `git rev-parse --local-env-vars`. Same reason as _git_env in the .py. The
# personal toolkit's pull entry clears the same list, in the same order (1a).
function Get-GitRepoEnv {
    return @('GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_CONFIG', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT',
             'GIT_OBJECT_DIRECTORY', 'GIT_DIR', 'GIT_WORK_TREE', 'GIT_IMPLICIT_WORK_TREE', 'GIT_GRAFT_FILE',
             'GIT_INDEX_FILE', 'GIT_NO_REPLACE_OBJECTS', 'GIT_REPLACE_REF_BASE', 'GIT_PREFIX',
             'GIT_INTERNAL_SUPER_PREFIX', 'GIT_SHALLOW_FILE', 'GIT_COMMON_DIR')
}
function Clear-GitRepoEnv {
    foreach ($v in (Get-GitRepoEnv)) {
        Remove-Item -Path "Env:$v" -ErrorAction SilentlyContinue
    }
}
Clear-GitRepoEnv
$env:GIT_TERMINAL_PROMPT = '0'   # credentialed remotes fail fast, never prompt-hang the hook

# git's markers for an operation in progress. A checkout mid-bisect, mid-rebase,
# mid-am or mid-revert can look clean and sit on its branch, and moving that branch
# changes what the operation does. Both unattended fast-forwards check them: the
# daily update below and the clean-fork catch-up in 1b.
$GitOpState = @('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_START', 'BISECT_LOG', 'rebase-merge', 'rebase-apply', 'sequencer')

function Test-GitPath {
    param([string]$Dir, [string]$Rel)
    # rev-parse --git-path answers relative to the checkout, or absolute.
    if (-not $Rel) { return $false }
    $p = if ([System.IO.Path]::IsPathRooted($Rel)) { $Rel } else { Join-Path $Dir $Rel }
    return (Test-Path -LiteralPath $p)
}

function Get-FastForwardArgs {
    param([string]$Dir, [string]$Branch, [string]$Target)
    # The git arguments (after -C) for an unattended fast-forward of $Branch onto
    # $Target. Both the daily update and the clean-fork catch-up use these, so the
    # two cannot drift apart. Same list as _guarded_ff_merge in the .py:
    #   * the branch's mergeOptions blanked - '-s ours' there makes even
    #     `merge --ff-only` exit 0 with a merge commit that discards every incoming
    #     change; --squash stages the new tree without moving the branch - with
    #     --no-squash and --no-autostash stated outright;
    #   * hooks, via a hooks path that is a file (.git/HEAD; in a linked worktree
    #     .git is itself a file, so the path cannot exist) - forward slashes: a
    #     backslash path would hand git the characters '\n';
    #   * background maintenance and auto-gc, which run after the move;
    #   * overwriting an ignored local file the update starts tracking.
    # Returns $null for a branch whose mergeOptions cannot be blanked, which is
    # then not merged at all: printable ASCII without '=' only, because git splits
    # -c at the first '=' (which a branch name may contain) and a non-ASCII name
    # may not survive the console code page. Same rule as _CONFIG_SAFE_BRANCH.
    if ($Branch -cnotmatch '^[\x21-\x3c\x3e-\x7e]+$') { return $null }
    $noHooks = (Join-Path $Dir '.git/HEAD') -replace '\\', '/'   # a file: no hook can live under it
    return @('-c', "core.hooksPath=$noHooks", '-c', 'maintenance.auto=false', '-c', 'gc.auto=0',
             '-c', "branch.$($Branch).mergeOptions=",
             'merge', '--ff-only', '--no-squash', '--no-autostash', '--no-overwrite-ignore', '--quiet', $Target)
}

function Invoke-GitBounded {
    param([string]$Dir, [string[]]$GitArgs, [int]$TimeoutMs = 3000)
    # Every git call in 1b, and the update's path list in 1, runs through here:
    # a hard time limit (the
    # direct `& git` form has none, and a fetch can hang for a minute on a captive
    # portal; a rev-list walk has no natural bound either), the whole process
    # TREE killed on timeout (git's HTTPS remote helper is a child that
    # Process.Kill() alone leaves running the fetch after we release the lock),
    # and output captured only for our own parsing. Everything this hook prints
    # is the model's context and git echoes server-controlled text, so nothing
    # captured here is ever surfaced - only our literals and a verified integer.
    # Returns @{ Code = exit code (-1 on timeout or failure to start); Out = stdout }.
    $result = @{ Code = -1; Out = '' }
    try {
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = 'git'
        $parts = @('-C', $Dir) + $GitArgs
        $psi.Arguments = (($parts | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' ')
        $psi.UseShellExecute = $false
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        # UTF-8, not the console code page: git writes paths as UTF-8, and a
        # mis-decoded name never matches the file on disk.
        $psi.StandardOutputEncoding = New-Object System.Text.UTF8Encoding $false
        $psi.CreateNoWindow = $true
        $psi.EnvironmentVariables['GIT_TERMINAL_PROMPT'] = '0'
        $p = [System.Diagnostics.Process]::Start($psi)
        # Drain both pipes while waiting - a chatty child deadlocks on a full pipe
        # otherwise. stdout is kept (for the count); stderr is discarded.
        $outTask = $p.StandardOutput.ReadToEndAsync()
        $p.BeginErrorReadLine()
        if (-not $p.WaitForExit($TimeoutMs)) {
            try { & taskkill /T /F /PID $p.Id 2>$null | Out-Null } catch { }
            try { $p.WaitForExit(3000) | Out-Null } catch { }
            return $result
        }
        $p.WaitForExit()   # lets the async readers finish after a timed wait
        $result.Code = $p.ExitCode
        $result.Out = $outTask.Result.Trim()
        return $result
    } catch {
        return $result
    }
}

function Update-Checkout {
    param([string]$Dir)
    # A fetch and then a guarded merge, not one `git pull`: --no-overwrite-ignore
    # is a merge option `git pull` rejects outright (exit 129). The bare fetch is
    # the one pull runs; '@{u}' is what pull merges. Same steps as
    # _update_checkout in the .py; the .py adds the 15s envelope, which this
    # section has never had - the merge is a local write and is never stopped.
    $noHooks = (Join-Path $Dir '.git/HEAD') -replace '\\', '/'   # a file: no hook can live under it
    & git -C $Dir -c "core.hooksPath=$noHooks" fetch --quiet 2>&1 | Out-Null
    $fetchCode = $LASTEXITCODE
    # Read AFTER the fetch, immediately before the merge: a branch switched or an
    # operation started while the fetch ran must be what the merge is judged
    # against. Same order as the .py.
    $opArgs = @()
    foreach ($n in $GitOpState) { $opArgs += @('--git-path', $n) }
    $probe = @(& git -C $Dir rev-parse --symbolic-full-name HEAD @opArgs 2>$null)
    if ($LASTEXITCODE -ne 0 -or $probe.Count -ne (1 + $GitOpState.Count)) { return }
    foreach ($rel in ($probe | Select-Object -Skip 1)) {
        if (Test-GitPath -Dir $Dir -Rel $rel.Trim()) {
            # Held back silently, an operation abandoned weeks ago would freeze the
            # toolkit with no signal, so it is said out loud.
            Write-Output ("WARNING - $(Split-Path $Dir -Leaf) did not self-update this session: the checkout at $Dir has an unfinished git operation in progress (a merge, rebase, cherry-pick, revert or bisect), and updating under it would change what that operation does. If nobody is working in that checkout right now, it was left behind and automatic updates stay FROZEN until it is finished or abandoned. Tell the user at the first natural moment and offer to look; finish or abort the operation only after they confirm nothing of theirs depends on it.")
            return
        }
    }
    # Offline, refused or stopped: nothing merged, as with a failed pull. The
    # upstream ref may still hold what some earlier session fetched, which NSLS may
    # since have withdrawn.
    if ($fetchCode -ne 0) { return }
    if ($probe[0] -cmatch '^refs/heads/(.+)$') { $branch = $Matches[1] } else { return }   # detached: a deliberate pin
    $mergeArgs = Get-FastForwardArgs -Dir $Dir -Branch $branch -Target '@{u}'
    if (-not $mergeArgs) {
        # A branch name whose mergeOptions cannot be blanked: not merged. Said out
        # loud only when that leaves it behind; the name itself is not printed.
        $behind = (& git -C $Dir rev-list --count 'HEAD..@{u}' 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -eq 0 -and $behind -match '^\d+$' -and [int]$behind -gt 0) {
            Write-Output ("WARNING - $(Split-Path $Dir -Leaf) did not self-update: the checkout at $Dir is on a branch whose name the automatic update cannot protect (it contains '=' or characters outside plain ASCII) and is behind its upstream, so automatic updates are FROZEN while it stays on that branch. Tell the user at the first natural moment and offer the fix: rename the branch using only letters, digits, '-', '_', '.' and '/', or put the checkout back on main.")
        }
        return
    }
    # Windows PowerShell 5.1 applies $ErrorActionPreference to native stderr
    # redirected with 2>&1, and this script runs under SilentlyContinue, which
    # drops it - and git's refusal is on stderr. 'Continue' for this one capture,
    # so the freeze check below sees what git said.
    $eap = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        # ForEach-Object { "$_" }: 5.1 wraps each stderr line in a NativeCommandError
        # dump, which can split the words the regex below looks for.
        $pullOut = (& git -C $Dir @mergeArgs 2>&1 | ForEach-Object { "$_" } | Out-String)
        $mergeCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $eap
    }
    if ($mergeCode -ne 0 -and $pullOut -match 'fast-forward|would be overwritten|unmerged|not concluded') {
        # Two gates, not one - matching session-start.py's `_checkout_blocks_update`.
        # The regex says git's complaint LOOKS checkout-local; this checks whether it
        # IS. Clean, up-to-date checkouts were reported FROZEN every session, sending
        # builders to back up local changes that did not exist.
        $dirty  = (& git -C $Dir status --porcelain 2>$null | Out-String).Trim()
        $ahead  = (& git -C $Dir rev-list --count '@{u}..HEAD' 2>$null | Out-String).Trim()
        # `@{u}`, not origin/main: a fork checkout tracks something else, and the
        # update being diagnosed follows the same upstream.
        $haveUpstream = ($LASTEXITCODE -eq 0 -and $ahead -match '^\d+$')
        $commits = 0
        if ($haveUpstream) { $commits = [int]$ahead }
        $blocker = $null
        $fix = 'preserve their local changes on a backup branch, then fast-forward the checkout to its upstream'
        if ($commits -gt 0 -and $dirty) { $blocker = "$commits local commit(s) and uncommitted edits" }
        elseif ($commits -gt 0)         { $blocker = "$commits local commit(s) not in its upstream" }
        elseif ($dirty)                 { $blocker = 'uncommitted local edits' }
        elseif ($haveUpstream) {
            # Clean and level, yet refused: the update adds a path that already
            # exists here as a file git does not show (ignored, typically), and
            # --no-overwrite-ignore refused rather than replace it. Counted from the
            # paths the update adds, never from git's error text.
            # NUL-separated and read as UTF-8: git quotes unusual names otherwise,
            # and a quoted spelling never matches the file on disk.
            $r = Invoke-GitBounded -Dir $Dir -GitArgs @('diff', '--name-only', '-z', '--no-renames', '--diff-filter=A', 'HEAD', '@{u}')
            if ($r.Code -eq 0) {
                $added = @($r.Out -split [char]0)
                $hits = @($added | Where-Object { $_ -and (Test-Path -LiteralPath (Join-Path $Dir $_)) }).Count
                if ($hits -gt 0) {
                    $blocker = "$hits ignored local file(s) that the update would replace"
                    # A backup branch would not hold these: git does not track them.
                    $fix = 'find those files (a fast-forward attempt names them), copy them somewhere safe and move them out of the way, then fast-forward the checkout to its upstream and help them keep whatever in those files was theirs'
                }
            }
        }
        # $null means verified clean and level, or unknowable - either way NOT frozen.
        # Staying quiet costs one session's update; the next session picks it up.
        if ($blocker) {
            # Which phrase matched, never git's raw text: everything written here
            # reaches the model's context, and git echoes attacker-controlled
            # content (`remote:` lines come verbatim from the server; branch, ref
            # and URL names appear in error text). $matched is one of OUR OWN four
            # literals, so it carries the diagnostic value with none of the surface.
            $matched = @('fast-forward','would be overwritten','unmerged','not concluded') |
                Where-Object { $pullOut -match [regex]::Escape($_) } | Select-Object -First 1
            Write-Output ("WARNING - $(Split-Path $Dir -Leaf) could not self-update: the checkout at $Dir has $blocker, so automatic updates are FROZEN and this toolkit is going stale. Tell the user at the first natural moment and offer the fix: $fix. (Skills in ~/.claude/skills are the right place for personal edits and are unaffected.) git refused with: '$matched'")
        }
    }
}

# PowerShell parses this whole file before running a line of it, so when the
# update below changes this script, everything after it would still run the OLD
# code for the rest of the session. On the PC test (2026-10-03) the session that
# pulled the quoting fix ran the broken copy and printed nothing; the fix only
# took effect a session later, and "open a second session" is not something to
# ask staff. So the file is hashed before the update and, if the update changed
# it, the new copy is run once in this same session and this copy stops.
$ownBefore = $null
if ($PSCommandPath) { try { $ownBefore = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash } catch { } }

# The copy that starts a re-run sets this to its own process id. Only a process
# whose parent has that id is the re-run; a value inherited from anywhere else
# is dropped, so it can never stop a normal session from updating.
$isRerun = $false
if ($env:NSLS_SESSION_START_RERUN) {
    try {
        $parentPid = (Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction Stop).ParentProcessId
        $isRerun = ("$parentPid" -eq $env:NSLS_SESSION_START_RERUN)
    } catch { }
    if (-not $isRerun) { Remove-Item Env:NSLS_SESSION_START_RERUN -ErrorAction SilentlyContinue }
}

# The re-run copy skips the update: this session already pulled, and pulling
# again would only repeat the fetches and any "frozen" warning.
if (-not $isRerun) {
    foreach ($dir in @($BuilderDir, $PersonalDir)) {
        if (-not (Test-Path $dir)) { continue }
        Update-Checkout -Dir $dir
    }
}

if ($ownBefore -and -not $isRerun) {
    $ownAfter = $null
    try { $ownAfter = (Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash } catch { }
    if ($ownAfter -and $ownAfter -ne $ownBefore) {
        # Inherited by the new copy, which checks it names its parent, so it
        # can never start a third.
        $env:NSLS_SESSION_START_RERUN = "$PID"
        $started = $false
        try {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = 'powershell.exe'
            $psi.Arguments = ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $PSCommandPath)
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $proc = [System.Diagnostics.Process]::Start($psi)
            $started = $true
            $errTask = $proc.StandardError.ReadToEndAsync()
            # Bytes straight through: the new copy writes UTF-8 for the policy, and
            # decoding it here would garble it through the console code page.
            [Console]::Out.Flush()
            $stdout = [Console]::OpenStandardOutput()
            $proc.StandardOutput.BaseStream.CopyTo($stdout)
            $stdout.Flush()
            $proc.WaitForExit()
            $null = $errTask.Result
            exit $proc.ExitCode
        } catch {
            # Once the new copy has started it is doing this session's work, so a
            # failure relaying its output must not let this copy do it all again
            # (a second ping, a second policy). Only a copy that never started
            # hands back to this one.
            if ($started) { exit 0 }
        }
    }
}

# --- 1a. the personal toolkit's own pull entry, made safe where install.ps1 wrote it ---
# The personal toolkit's install.ps1 registered its update in settings.json as a
# bare `git -C "<toolkit>" pull --ff-only --quiet`. git reads the variables in
# Get-GitRepoEnv ahead of -C, so a session started from inside a git hook pulled
# the builder's PROJECT, mid-commit, instead of the toolkit. That installer now
# writes `Remove-Item Env:<that list> -ErrorAction Ignore; <the pull>` with
# "shell": "powershell", and upgrades its old entry when it re-runs - but nobody
# re-runs an installer, and on Windows none of the personal toolkit's own hook
# code runs (its session-start.py does this on Macs). This hook runs every
# session, so it makes the change, once:
#   * only that exact command, or its `unset <list>; ` form, as the command of a
#     SessionStart hook with no shell of its own - built the way install.ps1
#     builds it, so an entry anyone has edited no longer matches and is left be;
#   * by splicing that one JSON string and putting "shell" right after it, so
#     every other byte of settings.json - a BOM, line endings, layout - stays;
#   * written only when re-parsing proves that is the whole change, and dropped
#     if settings.json changed after it was read here.
# After the re-run above, so only the newest copy of this hook ever writes.
# Nothing on stdout (it is the model's context); never throws.
function Update-PersonalPullHook {
    param([string]$Settings, [string]$PluginDir)
    $tmp = $null
    try {
        $item = Get-Item -LiteralPath $Settings -Force -ErrorAction Stop
        # A link stays a link: leave that file to the installer.
        if ($item.PSIsContainer -or ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) { return }
        $data = [System.IO.File]::ReadAllBytes($Settings)
        $bom = ($data.Length -ge 3 -and $data[0] -eq 0xEF -and $data[1] -eq 0xBB -and $data[2] -eq 0xBF)
        $skip = 0
        if ($bom) { $skip = 3 }
        # Strict: a byte that is not UTF-8 throws, and nothing is written.
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $raw = $utf8.GetString($data, $skip, $data.Length - $skip)
        if (-not $raw.Contains('pull --ff-only --quiet')) { return }

        $vars   = @(Get-GitRepoEnv)
        $legacy = 'git -C "' + $PluginDir + '" pull --ff-only --quiet'
        $unset  = 'unset ' + ($vars -join ' ') + '; '
        $guard  = 'Remove-Item ' + (($vars | ForEach-Object { 'Env:' + $_ }) -join ',') + ' -ErrorAction Ignore; '

        # Text: each "command": "<string>" whose value is one of the two. The new
        # string is the old one with $guard in place of its start, so the path
        # keeps whatever escaping it had ($guard itself needs none). "shell"
        # follows it, on a line of its own when "command" has one.
        $rx = New-Object System.Text.RegularExpressions.Regex('(?<ws>\r?\n[ \t]*)?"command"(?<sep>\s*:\s*)(?<lit>"(?:[^"\\]|\\.)*")')
        $sb = New-Object System.Text.StringBuilder
        $at = 0
        $spliced = 0
        foreach ($m in $rx.Matches($raw)) {
            $lit = $m.Groups['lit']
            if (-not $lit.Value.Contains('pull --ff-only --quiet')) { continue }
            $val = $null
            try { $val = ('{"v":' + $lit.Value + '}' | ConvertFrom-Json).v } catch { continue }
            if ($val -ceq $legacy) { $rest = $lit.Value.Substring(1) }
            elseif ($val -ceq ($unset + $legacy) -and $lit.Value.StartsWith('"' + $unset, [System.StringComparison]::Ordinal)) {
                $rest = $lit.Value.Substring(1 + $unset.Length)
            } else { continue }
            $sep = $m.Groups['sep'].Value
            $lead = $m.Groups['ws'].Value
            if (-not $lead -and $sep.Contains(' ')) { $lead = ' ' }
            [void]$sb.Append($raw, $at, $lit.Index - $at)
            [void]$sb.Append('"' + $guard + $rest + ',' + $lead + '"shell"' + $sep + '"powershell"')
            $at = $lit.Index + $lit.Length
            $spliced++
        }
        if ($spliced -eq 0) { return }
        [void]$sb.Append($raw, $at, $raw.Length - $at)
        $text = $sb.ToString()

        # Proof: in the new tree, undo exactly the change meant - each such hook
        # back to its old command, its "shell" taken out. What is left must be
        # the old tree, or nothing is written: a string spelt or placed any other
        # way (another event, a hook that has a shell) fails here.
        $before = $raw | ConvertFrom-Json
        $after  = $text | ConvertFrom-Json
        $undone = 0
        $oldSs = @($before.hooks.SessionStart)
        $newSs = @($after.hooks.SessionStart)
        if ($oldSs.Count -ne $newSs.Count) { return }
        for ($e = 0; $e -lt $oldSs.Count; $e++) {
            if ($null -eq $oldSs[$e] -or $null -eq $newSs[$e]) { continue }
            $oldHs = @($oldSs[$e].hooks)
            $newHs = @($newSs[$e].hooks)
            if ($oldHs.Count -ne $newHs.Count) { return }
            for ($i = 0; $i -lt $oldHs.Count; $i++) {
                $o = $oldHs[$i]
                $n = $newHs[$i]
                if ($null -eq $o -or $null -eq $n) { continue }
                $oc = @($o.PSObject.Properties | Where-Object { $_.Name -ceq 'command' })
                if ($oc.Count -ne 1 -or $oc[0].Value -isnot [string]) { continue }
                if (-not ($oc[0].Value -ceq $legacy -or $oc[0].Value -ceq ($unset + $legacy))) { continue }
                if (@($o.PSObject.Properties | Where-Object { $_.Name -ieq 'shell' }).Count -gt 0) { continue }
                $ns = @($n.PSObject.Properties | Where-Object { $_.Name -ceq 'shell' })
                if (-not ($n.command -ceq ($guard + $legacy)) -or $ns.Count -ne 1 -or -not ($ns[0].Value -ceq 'powershell')) { return }
                $n.command = $oc[0].Value
                $n.PSObject.Properties.Remove('shell')
                $undone++
            }
        }
        if ($undone -ne $spliced) { return }
        if (($after | ConvertTo-Json -Depth 100 -Compress) -cne ($before | ConvertTo-Json -Depth 100 -Compress)) { return }

        # Beside the file, then swapped in: File.Replace keeps its permissions,
        # and a session stopped mid-write leaves the old file whole.
        $out = $utf8.GetBytes($text)
        $tmp = "$Settings.$([guid]::NewGuid().ToString('N')).nsls-tmp"
        $fs = [System.IO.File]::Open($tmp, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write)
        try {
            if ($bom) { $fs.Write($data, 0, 3) }
            $fs.Write($out, 0, $out.Length)
        } finally {
            $fs.Dispose()
        }
        # Anything that wrote settings.json since it was read here (Claude Code,
        # an installer, an editor) wins: this write is dropped, the next session
        # tries again. The check and the swap happen while this handle holds the
        # file shared for reading and deleting only, so nothing can write into it
        # in between (and if something has it open for writing now, the open
        # fails and this session leaves it). Delete has to be shared, or
        # File.Replace could not swap it either - so a writer that swaps in a
        # whole new file in that instant can still lose its change. Nothing else
        # that writes settings.json takes a lock this could share, and this runs
        # once per machine.
        $hold = [System.IO.File]::Open($Settings, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read,
            ([System.IO.FileShare]::Read -bor [System.IO.FileShare]::Delete))
        try {
            $ms = New-Object System.IO.MemoryStream
            $hold.CopyTo($ms)
            if ([System.Convert]::ToBase64String($ms.ToArray()) -cne [System.Convert]::ToBase64String($data)) { return }
            [System.IO.File]::Replace($tmp, $Settings, [NullString]::Value)
            $tmp = $null
        } finally {
            $hold.Dispose()
        }
        [Console]::Error.WriteLine('personal-toolkit update hook now ignores git variables from the calling repository')
    } catch {
    } finally {
        if ($tmp -and (Test-Path -LiteralPath $tmp)) {
            # ReplaceFile can fail after removing the original (it documents
            # this), leaving only the new copy. Put it in place rather than
            # delete the only settings.json there is. Same as install.ps1.
            if (-not (Test-Path -LiteralPath $Settings)) {
                Move-Item -Force -LiteralPath $tmp -Destination $Settings -ErrorAction SilentlyContinue
            } else {
                Remove-Item -Force -LiteralPath $tmp -ErrorAction SilentlyContinue
            }
        }
    }
}
# Settings from $ClaudeDir (USERPROFILE), the file Claude Code reads; the
# toolkit path from $HOME, exactly as the personal install.ps1 builds it. They
# differ only on a redirected home, where that installer's entry went to a file
# Claude Code never read.
Update-PersonalPullHook -Settings (Join-Path $ClaudeDir 'settings.json') -PluginDir (Join-Path $HOME '.claude\local-plugins\nsls-personal-toolkit')

# --- 1b. personal-toolkit forks: measured against NSLS, not against their own copy ---
# The block above speaks only when a pull FAILS. A fork's pull succeeds - the
# fork genuinely has nothing new - so a builder whose personal toolkit is their
# own GitHub fork was reported healthy every session while nothing NSLS shipped
# ever reached them (196 commits behind on a real machine, 2026-09-09).
# Windows parity with session-start.py's report_personal_fork_drift(): same
# private ref, same stamp, same lock file, same wording. NSLS is fetched by URL, so
# a fork's own remotes - whatever they are named or aimed at - are neither read
# nor changed. Python is not guaranteed on a PC, so this is the ONLY place the
# check runs on Windows - and this script self-updates from NSLS on every
# machine, which is what lets it reach a fork at all (the fork's own hook is a
# file inside the fork, so the fork can never receive it).
$PersonalUpstreamUrl     = 'https://github.com/thensls/nsls-personal-toolkit.git'
$PersonalUpstreamRef     = 'refs/nsls/upstream-main'   # a private ref of our own; this hook creates and touches NO remote
$PersonalUpstreamRefspec = "+refs/heads/main:$PersonalUpstreamRef"
$PersonalUpstreamStamp   = Join-Path $ClaudeDir '.nsls-personal-upstream-check'
$PersonalUpstreamLock    = Join-Path $ClaudeDir '.nsls-personal-upstream-check.flock'
$PersonalLegacyLock      = Join-Path $ClaudeDir '.nsls-personal-upstream-check.lock'   # the previous protocol's file: shadow-claimed (empty) while ours is held
$PersonalCheckEveryH     = 12
# The operations-in-progress list is $GitOpState, in section 1.
# The status that counts everything: untracked files even where
# status.showUntrackedFiles=no hides them, and submodules even where configured away.
$PersonalStatusArgs = @('status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none')

function Test-CanonicalOrigin {
    param([string]$Url)
    # True only for NSLS's own repository on github.com, in any spelling git
    # accepts (https, ssh://, scp-like; user info, port, www., trailing slash,
    # .git, any case). Host and path are compared exactly - a substring test
    # called a mirror on another host, or a longer-named repo under the same
    # owner, canonical, and skipped the check for it. Schemes are an allow-list.
    $u = if ($Url) { $Url.Trim() } else { '' }
    $hostName = $null
    $repoPath = $null
    $m = [regex]::Match($u, '^([A-Za-z][A-Za-z0-9+.-]*)://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.*)$')
    if ($m.Success) {
        if (@('https', 'http', 'ssh', 'git', 'git+ssh', 'ssh+git') -notcontains $m.Groups[1].Value.ToLowerInvariant()) { return $false }
        $hostName = $m.Groups[2].Value
        $repoPath = $m.Groups[3].Value
    } else {
        $m = [regex]::Match($u, '^(?:[^@/:]+@)?([^/:]+):(?!//)/?(.*)$')
        if ($m.Success) {
            $hostName = $m.Groups[1].Value
            $repoPath = $m.Groups[2].Value
        }
    }
    if ($null -eq $hostName) { return $false }
    if ($hostName -match '^www\.') { $hostName = $hostName.Substring(4) }
    $repoPath = $repoPath.Trim('/')
    if ($repoPath -match '\.git$') { $repoPath = $repoPath.Substring(0, $repoPath.Length - 4) }
    # -eq and -match are case-insensitive in PowerShell, which is what GitHub names need.
    return (($hostName -eq 'github.com') -and ($repoPath.TrimEnd('/') -eq 'thensls/nsls-personal-toolkit'))
}

function Get-PullSourceUrl {
    param([string]$Dir)
    # The remote this checkout's branch actually pulls from - the same source a
    # bare `git pull` uses - falling back to origin. A canonical origin beside a
    # branch that tracks a personal fork is a fork in every way that matters; a
    # fork whose remote is not called origin is still a fork. Empty when neither
    # resolves. Read from config rather than by splitting the tracking ref on
    # "/": a remote may itself be named with a slash (personal/fork).
    $remote = 'origin'
    $r = Invoke-GitBounded -Dir $Dir -GitArgs @('symbolic-ref', '--short', 'HEAD')   # fails when detached
    $branch = if ($r.Code -eq 0) { $r.Out } else { '' }
    $seen = @()
    while ($branch -and ($seen -cnotcontains $branch)) {
        $seen += $branch   # stop only on a cycle, not at an arbitrary hop count
        # $($branch) - a bare "$branch.remote" would read .remote as a property.
        $r = Invoke-GitBounded -Dir $Dir -GitArgs @('config', '--get', "branch.$($branch).remote")
        if ($r.Code -ne 0 -or -not $r.Out) { break }
        if ($r.Out -ne '.') { $remote = $r.Out; break }
        # '.' means the branch pulls from a LOCAL branch, not a remote. Follow that
        # chain to the remote it ends at, rather than pretending it is origin - but
        # never give up on the checkout: its remote of record is still the honest
        # fallback, and silence on a fork is the failure here.
        $r = Invoke-GitBounded -Dir $Dir -GitArgs @('config', '--get', "branch.$($branch).merge")
        if ($r.Code -ne 0 -or $r.Out -notlike 'refs/heads/*') { break }
        $branch = $r.Out.Substring(11)
    }
    $r = Invoke-GitBounded -Dir $Dir -GitArgs @('remote', 'get-url', $remote)
    if (($r.Code -ne 0 -or -not $r.Out) -and $remote -ne 'origin') {
        $r = Invoke-GitBounded -Dir $Dir -GitArgs @('remote', 'get-url', 'origin')
    }
    if ($r.Code -ne 0) { return '' }
    return $r.Out
}

function Test-StampFresh {
    # Bounded at BOTH ends: a stamp dated in the future (clock skew, a restored
    # backup) would otherwise read as fresh forever.
    $ageH = $null
    try {
        if (Test-Path $PersonalUpstreamStamp) {
            $ageH = ((Get-Date) - (Get-Item $PersonalUpstreamStamp).LastWriteTime).TotalHours
        }
    } catch { }
    return ($null -ne $ageH -and $ageH -ge 0 -and $ageH -lt $PersonalCheckEveryH)
}

function Claim-Lock {
    param([string]$Path, [string]$LegacyPath = '')
    # An exclusive OS-level open on a file that is never deleted: the open handle
    # when we hold the lock, $null when another hook does. The stamp is only a
    # throttle - two hooks can both read it as stale before either writes it - so
    # this is the claim. FileShare.None makes every other open of the file fail
    # while ours is live (session-start.py's copy of this check, when a PC runs
    # it, opens this same file with the default share mode, so whichever opens
    # first holds it), and Windows drops the handle the instant the process
    # exits - so a hook that dies mid-check leaves nothing to reclaim: no stale
    # threshold, no token, no move-aside, and no step at which a third hook could
    # be handed a lock still in use.
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
    } catch {
        return $null
    }
    # Belt and braces: FileShare.None already refuses any other open handle, and
    # that is what excludes the Python copy (its descriptor stays open for as long
    # as it holds; proven on windows-latest, "PS: refused while Python holds").
    # Take the same one-byte lock Python takes as well, so both languages hold
    # one visible primitive; if even that is refused, someone else holds it.
    try {
        $fs.Lock(0, 1)
    } catch {
        $fs.Dispose()
        return $null
    }
    # A machine may briefly run one old copy of this check beside one new. The old
    # copy claims by creating ITS OWN lock file exclusively with a token inside,
    # and treats it as stale after 120 s. So, while holding the lock above, also
    # claim under that protocol: create the old file exclusively and leave it
    # EMPTY. An old hook arriving now sees a live lock and yields; an old hook
    # already mid-check (a fresh, non-empty token) makes this one yield. Only ever
    # done while holding $fs, so no two new hooks touch the old file at once. Our
    # empty shadow is never deleted by us - old tokens are never empty, so a later
    # new hook does not mistake it for a live old hook, and an old hook's release
    # only deletes a file that still carries its own token.
    if ($LegacyPath) {
        foreach ($attempt in 1, 2) {
            try {
                $shadow = [System.IO.File]::Open($LegacyPath, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
                $shadow.Dispose()
                break
            } catch {
                $old = $null
                try { $old = Get-Item -LiteralPath $LegacyPath -ErrorAction Stop } catch { }
                if ($null -eq $old) { break }                        # cannot create or read it: the stamp arbitrates
                $ageS = ((Get-Date) - $old.LastWriteTime).TotalSeconds
                if ($old.Length -gt 0 -and $ageS -ge 0 -and $ageS -le 120) {
                    $fs.Dispose()
                    return $null                                    # an old hook is mid-check right now
                }
                if ($attempt -eq 2) { break }
                try { [System.IO.File]::Delete($LegacyPath) } catch { break }   # a dead old hook's token, or our own empty shadow
            }
        }
    }
    return $fs
}

function Release-Lock {
    param($Handle)
    # Closing the handle drops the lock; the file itself stays.
    try { if ($null -ne $Handle) { $Handle.Dispose() } } catch { }
}

function Test-CleanToFastForward {
    param([string]$Dir)
    # Every condition under which a fast-forward can only add NSLS's commits.
    # Same list as _clean_to_fast_forward in the .py; see there for why each one.
    $r = Invoke-GitBounded -Dir $Dir -GitArgs @('symbolic-ref', '--short', '-q', 'HEAD')
    if ($r.Code -ne 0 -or $r.Out -cne 'main') { return $false }   # -cne: branch names are case-sensitive
    $gitArgs = @('rev-parse')
    foreach ($n in $GitOpState) { $gitArgs += @('--git-path', $n) }
    $r = Invoke-GitBounded -Dir $Dir -GitArgs $gitArgs
    if ($r.Code -ne 0) { return $false }
    foreach ($rel in ($r.Out -split "`r?`n")) { if (Test-GitPath -Dir $Dir -Rel $rel.Trim()) { return $false } }
    if (Test-Path -LiteralPath (Join-Path $Dir '.gitmodules')) { return $false }
    $r = Invoke-GitBounded -Dir $Dir -GitArgs @('config', '--bool', '--get', 'core.sparseCheckout')
    if ($r.Out -ceq 'true') { return $false }
    $r = Invoke-GitBounded -Dir $Dir -GitArgs @('rev-list', '--count', "$PersonalUpstreamRef..HEAD")
    if ($r.Code -ne 0 -or $r.Out -cne '0') { return $false }
    $r = Invoke-GitBounded -Dir $Dir -GitArgs $PersonalStatusArgs
    return ($r.Code -eq 0 -and -not $r.Out)
}

function Invoke-FastForwardDetached {
    param([string]$Dir, [int]$WaitMs)
    # The fast-forward itself. Returns its exit code, or $null if still running.
    # Never killed: git stopped mid-checkout leaves a half-updated folder and a
    # stale index.lock. Output goes to files, not pipes, so git can outlive this
    # hook without ever writing into a closed pipe. The guards are the daily
    # update's own (Get-FastForwardArgs), for main onto NSLS's main.
    $parts = @('-C', $Dir) + (Get-FastForwardArgs -Dir $Dir -Branch 'main' -Target $PersonalUpstreamRef)
    $argLine = (($parts | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' ')
    $tmpOut = [System.IO.Path]::GetTempFileName()
    $tmpErr = [System.IO.Path]::GetTempFileName()
    try {
        $p = Start-Process -FilePath 'git' -ArgumentList $argLine -NoNewWindow -PassThru -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
        $null = $p.Handle   # without touching Handle first, ExitCode can come back empty after the wait
    } catch { return -1 }
    if (-not $p.WaitForExit($WaitMs)) { return $null }   # still running: left to finish on its own
    $code = $p.ExitCode
    Remove-Item -LiteralPath $tmpOut, $tmpErr -ErrorAction SilentlyContinue
    return $code
}

function Get-CheckoutState {
    param([string]$Dir, [string]$Before, [string]$Target)
    # 'caught_up', 'untouched' or 'broken', read fresh after the attempt.
    $h = Invoke-GitBounded -Dir $Dir -GitArgs @('rev-parse', 'HEAD') -TimeoutMs 5000
    $s = Invoke-GitBounded -Dir $Dir -GitArgs $PersonalStatusArgs -TimeoutMs 10000
    $l = Invoke-GitBounded -Dir $Dir -GitArgs @('rev-parse', '--git-path', 'index.lock') -TimeoutMs 5000
    # Still on main: a checkout moved to another branch between the checks and the
    # merge would have had THAT branch moved. Reported for a look, never caught up.
    $br = Invoke-GitBounded -Dir $Dir -GitArgs @('symbolic-ref', '-q', 'HEAD') -TimeoutMs 5000
    if ($h.Code -ne 0 -or $s.Code -ne 0 -or $l.Code -ne 0 -or $s.Out -or (Test-GitPath -Dir $Dir -Rel $l.Out)) { return 'broken' }
    if ($br.Code -ne 0 -or $br.Out -cne 'refs/heads/main') { return 'broken' }
    if ($h.Out -ceq $Target) { return 'caught_up' }
    if ($h.Out -ceq $Before) { return 'untouched' }
    return 'broken'
}

function Invoke-ForkCatchUp {
    param([string]$Dir, $Clock)
    # 'caught_up'; 'untouched' (conditions not met, too late, or git refused
    # cleanly: the builder gets the offer); 'unfinished'; or 'broken'.
    if (-not (Test-CleanToFastForward -Dir $Dir)) { return 'untouched' }
    $b = Invoke-GitBounded -Dir $Dir -GitArgs @('rev-parse', 'HEAD')
    $t = Invoke-GitBounded -Dir $Dir -GitArgs @('rev-parse', $PersonalUpstreamRef)
    if ($b.Code -ne 0 -or $t.Code -ne 0) { return 'untouched' }
    if ($Clock.Elapsed.TotalSeconds -gt 10) { return 'untouched' }   # a write we might abandon is not begun
    # The wait ends 15s into this check at the latest, the same envelope the .py
    # keeps inside the 90s hook budget. A merge still running then finishes on its own.
    $waitMs = [int][math]::Min(20000, (15 - $Clock.Elapsed.TotalSeconds) * 1000)
    $code = Invoke-FastForwardDetached -Dir $Dir -WaitMs $waitMs
    if ($null -eq $code) { return 'unfinished' }
    return (Get-CheckoutState -Dir $Dir -Before $b.Out -Target $t.Out)
}

function Report-PersonalForkDrift {
    param([string]$Dir)
    if (-not (Test-Path (Join-Path $Dir '.git'))) { return }
    $url = Get-PullSourceUrl -Dir $Dir
    if (-not $url) { return }                        # nothing to measure against
    if (Test-CanonicalOrigin $url) { return }        # NSLS's own repo: the freeze check above owns it
    if (Test-StampFresh) { return }
    # The catch-up below is a local write. It is only ever begun early in this
    # check, and it is never killed once begun: git stopped half-way through a
    # checkout leaves a half-updated folder and a stale index.lock that blocks
    # every git command after it. Same rules as the .py.
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $lock = Claim-Lock -Path $PersonalUpstreamLock -LegacyPath $PersonalLegacyLock
    if ($null -eq $lock) { return }                  # another hook is mid-check this very second; it will speak
    try {
        if (Test-StampFresh) { return }              # it finished between our two looks
        try { [System.IO.File]::WriteAllText($PersonalUpstreamStamp, '') } catch { }
        # Bounded, best-effort: offline, blocked or slow all mean "say nothing this
        # session". '+' so our own ref always follows NSLS's main, even across a
        # force-push there; refs/heads/main so a tag called main cannot stand in;
        # --no-tags so NSLS's tags are not written into the builder's checkout.
        $r = Invoke-GitBounded -Dir $Dir -GitArgs @('fetch', '--quiet', '--no-tags', $PersonalUpstreamUrl, $PersonalUpstreamRefspec) -TimeoutMs 8000
        if ($r.Code -ne 0) { return }
        # A fork shares history with NSLS. A repository that does not - some
        # unrelated project sitting at this path - is not a fork, and telling
        # Claude to merge NSLS's main into it would be an instruction to merge two
        # unrelated histories. Nothing to say about such a checkout.
        $r = Invoke-GitBounded -Dir $Dir -GitArgs @('merge-base', 'HEAD', $PersonalUpstreamRef)
        if ($r.Code -ne 0) { return }
        $r = Invoke-GitBounded -Dir $Dir -GitArgs @('rev-list', '--count', "HEAD..$PersonalUpstreamRef")
        if ($r.Code -ne 0 -or $r.Out -notmatch '^\d+$') { return }
        $behind = [int]$r.Out
        if ($behind -eq 0) { return }
        # Nothing of theirs in the way: catch it up and say so once. Anything of
        # theirs in the way keeps the offer, because only they can decide what
        # happens to it.
        $outcome = Invoke-ForkCatchUp -Dir $Dir -Clock $clock
        # The path is text someone else can choose: printable ASCII only, bounded.
        $safeDir = ($Dir -replace '[^\x20-\x7e]', '?')
        if ($safeDir.Length -gt 200) { $safeDir = $safeDir.Substring(0, 200) }
        if ($outcome -ceq 'caught_up') {
            Write-Output ("[NSLS Personal Toolkit] This builder's toolkit was their OWN FORK, $behind commit(s) behind NSLS, with no commits or unsaved edits of their own, so it has just been caught up with NSLS automatically. Tell them in ONE plain sentence at the start of your first reply - e.g. `"Your toolkit was your own copy, so NSLS updates hadn't been reaching you; I've caught it up.`" - then offer to show them what's new: read and follow skills/update-personal-productivity/SKILL.md in $safeDir (the slash command itself appears after their next restart). NEVER hand them a git command.")
            return
        }
        if ($outcome -ceq 'unfinished' -or $outcome -ceq 'broken') {
            Write-Output ("[NSLS Personal Toolkit] An automatic catch-up of this builder's toolkit (their own fork, $behind commit(s) behind NSLS) did not finish cleanly, so the folder at $safeDir may be part-way through an update. Nothing of theirs was at risk: it only starts on a clean checkout with no commits of their own. Tell them in ONE plain sentence at the start of your first reply that their toolkit needs a quick look, and offer to sort it out. If they agree, inspect before changing anything - whether a git process is still running, whether .git/index.lock exists, git status, and where HEAD sits relative to $PersonalUpstreamRef - then finish the fast-forward or put it back. NEVER hand them a git command.")
            return
        }
        # Same prefix and wording as the personal toolkit's own hook, so Claude
        # sees one identical line whichever of the two spoke first.
        Write-Output ("[NSLS Personal Toolkit] This builder's toolkit is their OWN FORK and is $behind commit(s) behind NSLS - nothing shipped upstream has reached them, and their auto-update never will, because it follows their fork. Tell them in ONE plain sentence at the start of your first reply - e.g. `"Your toolkit is your own copy, so NSLS updates haven't been reaching you - want me to catch it up?`" - and if they agree: run /update-personal-productivity if this machine has it; otherwise, in $safeDir, merge $PersonalUpstreamRef (NSLS's main, fetched from $PersonalUpstreamUrl moments ago) yourself, preserving their own commits and setting aside any uncommitted edits first, then read and follow skills/update-personal-productivity/SKILL.md from the freshly merged checkout to walk them through what's new (the slash command itself appears after their next restart). NEVER hand them a git command.")
    } finally {
        Release-Lock -Handle $lock
    }
}
Report-PersonalForkDrift -Dir $PersonalDir

function Parse-Frontmatter {
    param([string]$Path)
    $content = Get-Content $Path -Raw -Encoding UTF8
    $m = [regex]::Match($content, '^---\r?\n(.*?)\r?\n---', 'Singleline')
    if (-not $m.Success) { return @{ name = $null; desc = $null } }
    $block = $m.Groups[1].Value
    $nameMatch = [regex]::Match($block, '^name:\s*(.+)$', 'Multiline')
    $name = if ($nameMatch.Success) { $nameMatch.Groups[1].Value.Trim() } else { $null }
    # '*' not '+': an empty folded block still belongs to the folded branch.
    # With '+' it fell through to the plain branch, which then captured the
    # literal '>-' and used it as the description.
    $folded = [regex]::Match($block, 'description:\s*>-?\s*\r?\n((?:[ \t]+.+\r?\n?)*)')
    if ($folded.Success) {
        $lines = $folded.Groups[1].Value -split "\r?\n" | ForEach-Object { $_.Trim() } | Where-Object { $_ }
        $d = ($lines -join ' ')
    } else {
        # Single-line scalar: reject a bare block indicator, then strip YAML
        # quotes and decode double-quoted escapes in ONE left-to-right pass.
        # Parity with the .py / .sh extractors.
        $plain = [regex]::Match($block, '^description:[ \t]*(.+)$', 'Multiline')
        $d = if ($plain.Success) { $plain.Groups[1].Value.Trim() } else { '' }
        if (@('>', '>-', '>+', '|', '|-', '|+') -contains $d) {
            $d = ''
        } elseif ($d.Length -gt 1) {
            $q = $d[0]
            if ($d[$d.Length - 1] -eq $q -and ($q -eq '"' -or $q -eq "'")) {
                $inner = $d.Substring(1, $d.Length - 2)
                if ($q -eq '"') {
                    $inner = [regex]::Replace($inner,
                        '\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})|\\(.)',
                        {
                            param($mm)
                            foreach ($g in 1, 2, 3) {
                                if ($mm.Groups[$g].Success) {
                                    # ConvertFromUtf32, not [char]: a valid \U
                                    # escape above U+FFFF (e.g. \U0001F600)
                                    # overflows System.Char and would throw,
                                    # killing the whole pointer sync. This
                                    # returns the surrogate pair instead.
                                    return [char]::ConvertFromUtf32([Convert]::ToInt32($mm.Groups[$g].Value, 16))
                                }
                            }
                            # switch -CaseSensitive, NOT a hashtable: PowerShell
                            # hashtable keys are case-INsensitive, so 'n' and 'N'
                            # collide and @{...} fails to parse outright. YAML
                            # needs both (\n = newline, \N = next-line), and the
                            # same applies to the \L / \P pair.
                            # [char] codes, not backtick escapes: `e and `v don't
                            # exist in Windows PowerShell 5.1, which runs this.
                            $c = $mm.Groups[4].Value
                            switch -CaseSensitive ($c) {
                                '0' { return [char]0 }
                                'a' { return [char]7 }
                                'b' { return [char]8 }
                                't' { return [char]9 }
                                'n' { return [char]10 }
                                'v' { return [char]11 }
                                'f' { return [char]12 }
                                'r' { return [char]13 }
                                'e' { return [char]27 }
                                'N' { return [char]133 }
                                '_' { return [char]160 }
                                'L' { return [char]8232 }
                                'P' { return [char]8233 }
                            }
                            # Anything else (\" \\ \/ \space) stands for itself.
                            return $c
                        })
                } else {
                    $inner = $inner.Replace("''", "'")
                }
                $d = $inner
            }
        }
    }
    # Map decoded control chars (NUL, BEL, ESC) to spaces - they would make the
    # generated pointer unparseable - then collapse whitespace, because the
    # caller embeds this as one indented line under description: >-.
    # Blank means "no description" so the caller's default stands.
    $d = [regex]::Replace($d, '[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]', ' ')
    $d = ($d -split '\s+' | Where-Object { $_ }) -join ' '
    $desc = if ([string]::IsNullOrWhiteSpace($d)) { $null } else { $d }
    return @{ name = $name; desc = $desc }
}

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

# Installed AND not switched off by the builder: same rule as org_plugin_active()
# in session-start.py. A disabled plugin can't deliver skills, so that builder
# keeps the pointer files.
function Test-OrgPluginActive {
    try {
        $reg = Get-Content (Join-Path $ClaudeDir 'plugins\installed_plugins.json') -Raw -ErrorAction Stop | ConvertFrom-Json
        $installed = @($reg.plugins.PSObject.Properties.Name | Where-Object { $_ -like 'nsls-builder-toolkit@*' }).Count -gt 0
    } catch { return $false }
    if (-not $installed) { return $false }
    try {
        $cfg = Get-Content (Join-Path $ClaudeDir 'settings.json') -Raw -ErrorAction Stop | ConvertFrom-Json
        foreach ($prop in $cfg.enabledPlugins.PSObject.Properties) {
            if ($prop.Name -like 'nsls-builder-toolkit@*' -and $prop.Value -eq $false) { return $false }
        }
    } catch { }
    return $true
}

function Sync-Pointers {
    # -NoteOnly: write nothing, only note the builder's own skills that sit where
    # a toolkit pointer would (the notice below still needs them).
    param([string]$PluginDir, [switch]$NoteOnly)
    $skillsRoot = Join-Path $PluginDir 'skills'
    if (-not (Test-Path $skillsRoot)) { return 0 }
    $count = 0
    $pluginName = [System.IO.Path]::GetFileName($PluginDir)
    foreach ($skillFolder in Get-ChildItem $skillsRoot -Directory) {
        $src = Join-Path $skillFolder.FullName 'SKILL.md'
        if (-not (Test-Path $src)) { continue }
        $fm = Parse-Frontmatter -Path $src
        if (-not $fm.name) { continue }
        $desc = if ($fm.desc) { $fm.desc } else { "NSLS toolkit skill: $($skillFolder.Name)" }
        $destDir = Join-Path $SkillsDir $skillFolder.Name
        $destMd  = Join-Path $destDir   'SKILL.md'
        # Refresh an existing file only if it is OUR pointer to THIS skill (see
        # Test-OwnPointer). The old check looked for 'local-plugins\nsls-' with
        # a backslash, while the pointers written here use forward slashes - so
        # it never matched, no pointer was ever refreshed, and changed
        # descriptions never reached Windows builders. Anything looser than an
        # exact pointer would overwrite skills the builder wrote.
        # A toolkit synced earlier already wrote this name: it wins.
        if ($script:PointerWritten.ContainsKey($skillFolder.Name)) { continue }
        $ownPath = "local-plugins/$pluginName/skills/$($skillFolder.Name)/SKILL.md"
        if (Test-Path $destMd) {
            $existing = [string](Get-Content $destMd -Raw -Encoding UTF8)
            # Either toolkit's pointer to THIS skill may be replaced, so the
            # personal copy can take over a name the org pointer held.
            $ours = $false
            foreach ($tk in $PointerPrecedence) {
                $tkPath = "local-plugins/$tk/skills/$($skillFolder.Name)/SKILL.md"
                if (Test-OwnPointer -Text $existing -OwnPath $tkPath) { $ours = $true }
            }
            # Not a pointer: the builder's own skill, and the one that runs.
            if (-not $ours) { $script:OwnSkills[$skillFolder.Name] = $true; continue }
        }
        if ($NoteOnly) { continue }
        if (-not (Test-Path $destDir)) { New-Item -ItemType Directory -Path $destDir | Out-Null }
        $pointerPath = "~/.claude/$ownPath"
        $pointer = @"
---
name: $($fm.name)
description: >-
  $desc
---

Read and follow the full skill at ``$pointerPath``.
"@
        # BOM-less write (matches install.ps1): PS 5.1 `Set-Content -Encoding
        # utf8` prepends a BOM. Harmless-looking on a pointer, but this hook runs
        # every session, so a BOM here would silently re-introduce one on files
        # install.ps1 just wrote clean.
        [System.IO.File]::WriteAllText($destMd, ($pointer + "`r`n"), (New-Object System.Text.UTF8Encoding $false))
        $script:PointerWritten[$skillFolder.Name] = $true
        $count++
    }
    return $count
}

# --- 2. sync pointer skills ---
if (-not (Test-Path $SkillsDir)) { New-Item -ItemType Directory -Path $SkillsDir -Force | Out-Null }
# Personal first: when both toolkits ship a skill with the same name, the
# personal one wins (same order as POINTER_PRECEDENCE in session-start.py).
$PointerPrecedence = @('nsls-personal-toolkit', 'nsls-builder-toolkit')
$script:PointerWritten = @{}
$script:OwnSkills = @{}
$null = Sync-Pointers -PluginDir $PersonalDir
# Once the toolkit is an active plugin, the plugin delivers its skills and the
# migration retires these pointers. Writing them again (as session-start.py has
# long since stopped doing) put every skill in the list twice, and in the
# session where both this shim and the plugin ran, the migration's second pass
# removed 58 pointers this had just rewritten (PC test, 2026-10-03).
$null = Sync-Pointers -PluginDir $BuilderDir -NoteOnly:(Test-OrgPluginActive)
# Same notice as notice_own_skills() in session-start.py.
if ($script:OwnSkills.Count -gt 0) {
    $names = @($script:OwnSkills.Keys | Sort-Object)
    $listed = ($names | ForEach-Object { "/$_" }) -join ', '
    Write-Output ("[NSLS Builder Toolkit] This builder has their own skill with the same name as a toolkit skill, so theirs is the one that runs: $listed. Each time they use one of these, say so in one short line before starting, e.g. `"Using your own /$($names[0]), not the NSLS toolkit's.`" Never change or remove their skill.")
}

# --- 2b. surface announcements stashed by the previous detached ping ---
# session-ping.ps1 runs detached, so it can't print to session context itself;
# it dismisses announcements and writes their text here. We read + print it
# synchronously (this hook's stdout IS session context on every surface),
# wrapped in the same directive prefix session-start.py uses so it shows on the
# desktop app too, then delete the file. One session's lag, but it surfaces.
$AnnounceFile = if (Test-Path $BuilderDir) {
    Join-Path $BuilderDir '.pending-announcements'
} else {
    Join-Path $ClaudeDir '.pending-announcements'
}
if (Test-Path $AnnounceFile) {
    $block = (Get-Content $AnnounceFile -Raw -Encoding UTF8).Trim()
    Remove-Item $AnnounceFile -Force -ErrorAction SilentlyContinue
    if ($block) {
        Write-Output "[NSLS Builder Toolkit - surface the following to the user verbatim at the start of your first reply, then proceed with their request:]`n`n$block"
    }
}

# --- 3. session ping (detached, non-blocking) ---
# The proxy is idempotent, so repeated pings never duplicate. Detached so its
# cold-start latency never delays session start; output -> nsls-session-ping.log.
$pingScript = Join-Path $PSScriptRoot 'session-ping.ps1'
if (Test-Path $pingScript) {
    Start-Process powershell -WindowStyle Hidden -ArgumentList @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $pingScript
    ) | Out-Null
}

# --- 3b. NSLS Claude usage collector bootstrap (detached, silent) ---
# If the collector is not installed, launch the self-serve installer hidden in
# the background, at most once a day and five times ever. Silent by design:
# enrollment DMs the builder via Signal. Opt out: NSLS_COLLECTOR_OPTOUT=1.
# Output discarded - this hook's stdout is session context. See
# collector_bootstrap.ps1.
$bootstrapScript = Join-Path $PSScriptRoot 'collector_bootstrap.ps1'
if (Test-Path $bootstrapScript) {
    try {
        . $bootstrapScript
        $null = Invoke-CollectorBootstrap
    } catch { }
}

# --- 4. Builder Guardrails context, plugin stage A, plugin freshness ---
# All three live in session-start.py's __guardrails__ block, reached through
# guardrails_entry.py: one copy of the section-extraction, migration and
# freshness logic, not two that can drift. Until the plugin is installed, this
# is the only hook that runs on a PC: Windows has had no guardrail gate
# registered in settings.json, so the plugin (stage A) is what brings the gates.
#
# The step used to run `py -3 -c $program`, and Windows PowerShell 5.1 strips
# the double quotes inside an argument it hands to a native program. Python got
# `run_name=__guardrails__`, raised a NameError, and exited 1 - on every PC,
# every session, since the step was added. `2>$null` threw the traceback away
# and try/catch never sees a native exit code, so nothing ever said so. Hence a
# file entry point, whose path has no quotes to lose, and hence the exit code
# is now checked and a failure reported (see Report-ShimStuck).
#
# Interpreter: the stock Windows `python3` is a Store alias that exits without
# running anything, so falling back to it emitted nothing at all. Prefer the
# launcher, then the toolkit-provisioned runtime, then `python`.
$pyExe = $null
if (Get-Command py -ErrorAction SilentlyContinue) { $pyExe = 'py' }
elseif (Test-Path (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe')) {
    $pyExe = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
}
elseif (Get-Command python -ErrorAction SilentlyContinue) { $pyExe = 'python' }

# Where Python keeps the same record: CLAUDE_CONFIG_DIR when set, as
# session-start.py and migrate_to_plugin.py read it. Two files would mean two
# records that never agree on what was said or cleared.
$ShimStateDir = if ([string]::IsNullOrWhiteSpace($env:CLAUDE_CONFIG_DIR)) { $ClaudeDir } else { $env:CLAUDE_CONFIG_DIR }
$ShimStatus = Join-Path $ShimStateDir '.nsls-plugin-migration-status'
$ShimPyLog  = Join-Path $ShimStateDir '.nsls-session-start-py.log'

# Same record and the same once-a-day notice as migrate_to_plugin.py's
# _report_stuck, for the one failure Python cannot report itself: Python not
# starting. Only toolkit-authored words reach stdout, which is the model's
# context; the raw stderr line stays in the local status file, flattened.
function Report-ShimStuck {
    param([string]$Reason, [string]$Detail)
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $noticed = 0
    try {
        $prev = Get-Content $ShimStatus -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($prev.noticed) { $noticed = [long]$prev.noticed }
    } catch { }
    if ($noticed -gt $now) { $noticed = 0 }   # a future stamp must not silence it for good
    $speak = ($now - $noticed) -ge 86400
    $clean = ([regex]::Replace([string]$Detail, '[^\x20-\x7E]', ' ') -replace '\s+', ' ').Trim()
    if ($clean.Length -gt 200) { $clean = $clean.Substring(0, 200) }
    $record = [ordered]@{ at = $now; stage = 'shim-python'; reason = $Reason; detail = $clean
                          noticed = $(if ($speak) { $now } else { $noticed }) }
    try {
        $tmp = "$ShimStatus.tmp"
        [System.IO.File]::WriteAllText($tmp, (($record | ConvertTo-Json -Compress) + "`n"), (New-Object System.Text.UTF8Encoding $false))
        Move-Item -Path $tmp -Destination $ShimStatus -Force
    } catch { }
    if ($speak) {
        Write-Output ("[NSLS Builder Toolkit] Setup could not finish on this machine: $Reason, " +
            "so the toolkit's guardrails are not active here yet. It retries every session. " +
            "Mention this to the user once, in one plain sentence, and suggest they tell the " +
            "NSLS AI team if it keeps happening.")
    }
}

$startPy = Join-Path $PSScriptRoot 'session-start.py'
$entryPy = Join-Path $PSScriptRoot 'guardrails_entry.py'
if ((Test-Path $startPy) -and (Test-Path $entryPy)) {
    if (-not $pyExe) {
        Report-ShimStuck -Reason 'no Python was found' -Detail ''
    } else {
        $pyExit = -1
        $pyErr = ''
        try {
            # Python is started directly rather than through PowerShell's native
            # call. Windows PowerShell 5.1 decodes a child's output with the
            # console code page, and a hook has no console to set one on, so the
            # policy's dashes and dots arrived garbled (CI, 2026-10-03). Here
            # Python writes UTF-8, it is read as UTF-8, and written to this
            # hook's stdout as UTF-8 bytes, untouched. stderr is read apart:
            # diagnosis for a person, never context for the model.
            $utf8 = New-Object System.Text.UTF8Encoding $false
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName = $pyExe
            $psi.Arguments = $(if ($pyExe -eq 'py') { '-3 ' } else { '' }) + ('"{0}" "{1}"' -f $entryPy, $startPy)
            $psi.UseShellExecute = $false
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError = $true
            $psi.StandardOutputEncoding = $utf8
            $psi.StandardErrorEncoding = $utf8
            $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
            $proc = [System.Diagnostics.Process]::Start($psi)
            $errTask = $proc.StandardError.ReadToEndAsync()
            $pyOut = $proc.StandardOutput.ReadToEnd()
            $proc.WaitForExit()
            $pyExit = $proc.ExitCode
            $pyErr = [string]$errTask.Result
            if ($pyOut) {
                [Console]::Out.Flush()   # anything this script printed comes first
                $bytes = $utf8.GetBytes($pyOut)
                $stdout = [Console]::OpenStandardOutput()
                $stdout.Write($bytes, 0, $bytes.Length)
                $stdout.Flush()
            }
        } catch { }
        # A small local log, overwritten each session.
        try { [System.IO.File]::WriteAllText($ShimPyLog, $pyErr, (New-Object System.Text.UTF8Encoding $false)) } catch { }
        if ($pyExit -ne 0) {
            $last = [string]($pyErr -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 1)
            Report-ShimStuck -Reason "the toolkit's Python step could not run" -Detail "exit $pyExit; $last"
        } else {
            # Clear only our own record: migrate_to_plugin.py keeps its stage-A
            # record in the same file and clears that one itself.
            try {
                $prev = Get-Content $ShimStatus -Raw -ErrorAction Stop | ConvertFrom-Json
                if ($prev.stage -eq 'shim-python') { Remove-Item $ShimStatus -Force }
            } catch { }
        }
    }
}
