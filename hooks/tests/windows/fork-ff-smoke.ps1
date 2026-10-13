# windows-ci: ps51 | A clean fork catches itself up; nothing of theirs, no operation in progress, no merge setting or hook changes that
# The catch-up for a clean fork, on a real PC. Report-PersonalForkDrift and the
# helpers it calls are lifted out of session-start.ps1 by AST (its top level
# pulls and pings, so it is never dot-sourced) and pointed at a throwaway world:
# NSLS as a bare repo, a fork frozen behind it, and the builder's checkout cloned
# from the fork. A fork with nothing of its own must be fast-forwarded and told
# so; anything of theirs in the way must leave the checkout untouched and get
# the offer. Run under Windows PowerShell 5.1, the version the hook runs under.
$ErrorActionPreference = 'Continue'   # git writes progress to stderr; 5.1 turns that into errors under Stop
$hooks  = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script = Join-Path $hooks 'session-start.ps1'

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($script, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw "session-start.ps1 does not parse: $($errors[0].Message)" }
$want = @('Get-GitRepoEnv', 'Clear-GitRepoEnv', 'Report-PersonalForkDrift', 'Get-PullSourceUrl', 'Test-CanonicalOrigin', 'Test-StampFresh', 'Claim-Lock', 'Release-Lock', 'Invoke-GitBounded',
          'Test-GitPath', 'Get-FastForwardArgs', 'Test-CleanToFastForward', 'Invoke-FastForwardDetached', 'Get-CheckoutState', 'Invoke-ForkCatchUp')
$fns = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $want -contains $n.Name }, $true)
if ($fns.Count -ne $want.Count) { throw "expected $($want.Count) functions in session-start.ps1, found $($fns.Count)" }
foreach ($f in $fns) { Invoke-Expression $f.Extent.Text }
# The two lists the new helpers read, lifted from the script too, so the smoke
# test can never drift from what the hook actually checks.
$lists = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    @('$GitOpState', '$PersonalStatusArgs') -contains $n.Left.Extent.Text }, $true)
if ($lists.Count -ne 2) { throw "expected `$GitOpState and `$PersonalStatusArgs in session-start.ps1, found $($lists.Count)" }
foreach ($a in $lists) { Invoke-Expression ('$script:' + $a.Extent.Text.Substring(1)) }

# The constants the lifted functions read, exactly as session-start.ps1 sets them.
$script:PersonalUpstreamRef     = 'refs/nsls/upstream-main'
$script:PersonalUpstreamRefspec = "+refs/heads/main:$($script:PersonalUpstreamRef)"
$script:PersonalCheckEveryH     = 12

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}
function TGit {
    # Never name this Git: PowerShell resolves names case-insensitively and puts
    # functions ahead of executables, so `& git` inside it would call itself.
    # A simple function on purpose: $args passes '-A' and '--quiet' through as
    # plain strings instead of trying to bind them as parameters.
    $dir = $args[0]; $rest = @($args | Select-Object -Skip 1)
    $out = & git -C $dir @rest 2>$null
    if ($LASTEXITCODE -ne 0) { throw "git $($rest -join ' ') failed (exit $LASTEXITCODE) in $dir" }
    return (($out | Out-String).Trim())
}
function Commit([string]$Repo, [string]$Name, [string]$Text) {
    Set-Content -Path (Join-Path $Repo $Name) -Value $Text -NoNewline
    TGit $Repo add -A | Out-Null
    TGit $Repo commit --quiet -m "add $Name" | Out-Null
}
function New-World([bool]$Customized) {
    $root = Join-Path ([IO.Path]::GetTempPath()) ('ffsmoke-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $root | Out-Null
    $seed = Join-Path $root 'seed'
    & git init --quiet -b main $seed 2>$null
    TGit $seed config user.email t@example.com | Out-Null
    TGit $seed config user.name t | Out-Null
    Commit $seed 'skill.md' "v1`n"
    $nsls = Join-Path $root 'nsls.git'
    & git clone --quiet --bare $seed $nsls 2>$null
    $fork = Join-Path $root 'fork.git'
    & git clone --quiet --bare $nsls $fork 2>$null
    foreach ($i in 1..5) { Commit $seed "skill$i.md" "v$i`n" }
    TGit $seed push --quiet $nsls main | Out-Null
    $claude = Join-Path $root '.claude'
    $plugin = Join-Path $claude 'local-plugins\nsls-personal-toolkit'
    New-Item -ItemType Directory -Path (Split-Path $plugin) -Force | Out-Null
    & git clone --quiet $fork $plugin 2>$null
    TGit $plugin config user.email t@example.com | Out-Null
    TGit $plugin config user.name t | Out-Null
    if ($Customized) { Commit $plugin 'mine.md' "her own customization`n" }
    $script:PersonalUpstreamUrl   = $nsls
    $script:PersonalUpstreamStamp = Join-Path $claude '.nsls-personal-upstream-check'
    $script:PersonalUpstreamLock  = Join-Path $claude '.nsls-personal-upstream-check.flock'
    $script:PersonalLegacyLock    = Join-Path $claude '.nsls-personal-upstream-check.lock'
    return @{ Seed = $seed; Nsls = $nsls; Plugin = $plugin }
}
function Head($W)     { TGit $W.Plugin rev-parse HEAD }
function Upstream($W) { TGit $W.Plugin rev-parse $script:PersonalUpstreamRef }
function Run($W)      { (Report-PersonalForkDrift -Dir $W.Plugin | Out-String) }

# 1. A clean fork on main: caught up, and told so once.
$w = New-World $false
$out = Run $w
Check 'a clean fork on main is caught up: HEAD is now NSLS main' ((Head $w) -eq (Upstream $w)) (Head $w)
Check '...and it gets the caught-up line, not the offer' ($out -match 'caught up with NSLS automatically' -and $out -notmatch 'want me to catch it up') $out
Check '...and the working tree is clean afterwards' ((TGit $w.Plugin status --porcelain) -eq '')

# 2. A commit of her own: untouched, offered.
$w = New-World $true
$before = Head $w
$out = Run $w
Check 'a fork with a commit of its own is NOT moved' ((Head $w) -eq $before)
Check '...and gets the offer instead' ($out -match 'want me to catch it up') $out

# 3. An unsaved edit: untouched, the edit survives, offered.
$w = New-World $false
Set-Content -Path (Join-Path $w.Plugin 'skill.md') -Value "an edit she has not saved`n" -NoNewline
$before = Head $w
$out = Run $w
Check 'a fork with an unsaved edit is NOT moved, and the edit survives' (((Head $w) -eq $before) -and ((Get-Content (Join-Path $w.Plugin 'skill.md') -Raw) -eq "an edit she has not saved`n"))
Check '...and gets the offer instead' ($out -match 'want me to catch it up') $out

# 4. The one way a fast-forward loses work: NSLS starts tracking a path she keeps
#    as an IGNORED local file. Plain git replaces it without a word.
$w = New-World $false
Set-Content -Path (Join-Path $w.Plugin '.git\info\exclude') -Value "local-notes.md`n" -NoNewline
Set-Content -Path (Join-Path $w.Plugin 'local-notes.md') -Value "HER PRIVATE NOTES`n" -NoNewline
Commit $w.Seed 'local-notes.md' "nsls version`n"
TGit $w.Seed push --quiet $w.Nsls main | Out-Null
$before = Head $w
$out = Run $w
Check 'an ignored file NSLS starts tracking is NOT overwritten' ((Get-Content (Join-Path $w.Plugin 'local-notes.md') -Raw) -eq "HER PRIVATE NOTES`n")
Check '...the checkout is left where it was' ((Head $w) -eq $before)
Check '...and it gets the offer, not a false caught-up' ($out -match 'want me to catch it up' -and $out -notmatch 'caught up with NSLS automatically') $out

# 5. branch.main.mergeOptions=--squash would stage NSLS's tree and leave the branch
#    where it is. The merge neutralises it, so this is still a clean catch-up.
$w = New-World $false
TGit $w.Plugin config branch.main.mergeOptions --squash | Out-Null
$out = Run $w
Check 'with mergeOptions=--squash configured, it is still a clean fast-forward' (((Head $w) -eq (Upstream $w)) -and ((TGit $w.Plugin status --porcelain) -eq '')) (TGit $w.Plugin status --porcelain)
Check '...and reports caught up' ($out -match 'caught up with NSLS automatically') $out

# 6. Mid-bisect a checkout can be clean and on main. It must not be moved.
$w = New-World $false
TGit $w.Plugin bisect start | Out-Null
$before = Head $w
$out = Run $w
Check 'a checkout mid-bisect is NOT moved' ((Head $w) -eq $before)
Check '...and gets the offer instead' ($out -match 'want me to catch it up') $out

# 7. A post-merge hook runs after the branch has moved, and can hang. Installed
#    through the repo's local core.hooksPath, the shape a builder would have.
$w = New-World $false
$hooksDir = Join-Path (Split-Path $w.Plugin) 'builder-hooks'
New-Item -ItemType Directory -Path $hooksDir | Out-Null
$marker = (Join-Path (Split-Path $w.Plugin) 'hook-ran') -replace '\\', '/'
Set-Content -Path (Join-Path $hooksDir 'post-merge') -Value "#!/bin/sh`ntouch '$marker'`nsleep 30`n" -NoNewline
TGit $w.Plugin config core.hooksPath $hooksDir | Out-Null
$clockT = [System.Diagnostics.Stopwatch]::StartNew()
$out = Run $w
$took = $clockT.Elapsed.TotalSeconds
Check 'a hanging post-merge hook is never run' (-not (Test-Path -LiteralPath $marker))
Check "...so the catch-up completes promptly ($([math]::Round($took, 1))s) and is reported" (($took -lt 15) -and ((Head $w) -eq (Upstream $w)) -and ($out -match 'caught up with NSLS automatically')) $out

# 8. status.showUntrackedFiles=no hides an untracked file from plain porcelain.
$w = New-World $false
TGit $w.Plugin config status.showUntrackedFiles no | Out-Null
Set-Content -Path (Join-Path $w.Plugin 'scratch.md') -Value "a new file she has not added`n" -NoNewline
$before = Head $w
$out = Run $w
Check 'an untracked file is seen even with status.showUntrackedFiles=no, and nothing moves' (((Head $w) -eq $before) -and ($out -match 'want me to catch it up')) $out

# 9. git reads GIT_DIR and friends ahead of -C. A session started from inside a
#    git hook inherits them, and the hook clears them before any git call.
$w = New-World $false
$decoy = Join-Path (Split-Path $w.Plugin) 'decoy'
& git init --quiet -b main $decoy 2>$null
TGit $decoy config user.email t@example.com | Out-Null
TGit $decoy config user.name t | Out-Null
Commit $decoy 'project.md' "someone else's project`n"
$decoyHead = TGit $decoy rev-parse HEAD
$env:GIT_DIR = Join-Path $decoy '.git'
$env:GIT_WORK_TREE = $decoy
Clear-GitRepoEnv
$out = Run $w
Check 'with GIT_DIR aimed at another repository, the toolkit is still the one caught up' (((Head $w) -eq (Upstream $w)) -and ($out -match 'caught up with NSLS automatically')) $out
Check '...and that other repository is untouched' ((TGit $decoy rev-parse HEAD) -eq $decoyHead)

# 10. Moved off main between the checks and the merge: reported for a look, never
#     called caught up. The merge is wrapped to switch branch first.
$w = New-World $false
$script:origFF = ${function:Invoke-FastForwardDetached}
${function:Invoke-FastForwardDetached} = { param($Dir, $WaitMs) TGit $Dir checkout --quiet -b hers | Out-Null; & $script:origFF -Dir $Dir -WaitMs $WaitMs }
$out = Run $w
${function:Invoke-FastForwardDetached} = $script:origFF
Check 'a checkout moved off main just before the merge is reported for a look, never called caught up' (($out -match 'did not finish cleanly') -and ($out -notmatch 'caught up with NSLS automatically')) $out

if ($script:failures -gt 0) { Write-Host "$($script:failures) FAILED"; exit 1 }
Write-Host 'all fork catch-up checks passed'
