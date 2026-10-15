# windows-ci: ps51 | The daily update keeps ignored files, neutralises merge settings, runs no hooks, and leaves an operation in progress alone
# The daily update's guards, on a real PC. Update-Checkout and the helpers it
# calls are lifted out of session-start.ps1 by AST (its top level pulls and
# pings, so it is never dot-sourced) and pointed at a throwaway world: NSLS as a
# bare repo and the builder's checkout cloned from it, tracking main. Mirrors
# hooks/tests/test_daily_pull_guards.py; each case fails if its guard is removed.
# Run under Windows PowerShell 5.1, the version the hook runs under.
# The hook's own setting, not 'Continue': under SilentlyContinue, Windows PowerShell
# 5.1 drops native stderr redirected with 2>&1, which is where git's refusals are,
# so only this setting proves the freeze warnings below can still fire.
$ErrorActionPreference = 'SilentlyContinue'
$hooks  = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script = Join-Path $hooks 'session-start.ps1'

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($script, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw "session-start.ps1 does not parse: $($errors[0].Message)" }
$want = @('Get-GitRepoEnv', 'Clear-GitRepoEnv', 'Test-GitPath', 'Get-FastForwardArgs', 'Invoke-GitBounded', 'Update-Checkout')
$fns = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $want -contains $n.Name }, $true)
if ($fns.Count -ne $want.Count) { throw "expected $($want.Count) functions in session-start.ps1, found $($fns.Count)" }
foreach ($f in $fns) { Invoke-Expression $f.Extent.Text }
$lists = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and $n.Left.Extent.Text -eq '$GitOpState' }, $true)
if ($lists.Count -ne 1) { throw "expected one `$GitOpState in session-start.ps1, found $($lists.Count)" }
Invoke-Expression ('$script:' + $lists[0].Extent.Text.Substring(1))
Clear-GitRepoEnv
$env:GIT_TERMINAL_PROMPT = '0'

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}
function TGit {
    # Never name this Git: PowerShell resolves names case-insensitively and puts
    # functions ahead of executables, so `& git` inside it would call itself.
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
function New-World {
    # A space in every path, as under C:\Users\First Last: -c core.hooksPath=<path> must arrive as one argument.
    $root = Join-Path ([IO.Path]::GetTempPath()) ('dp smoke-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Path $root | Out-Null
    $seed = Join-Path $root 'seed'
    & git init --quiet -b main $seed 2>$null
    TGit $seed config user.email t@example.com | Out-Null
    TGit $seed config user.name t | Out-Null
    Commit $seed 'skill.md' "v1`n"
    $nsls = Join-Path $root 'nsls.git'
    & git clone --quiet --bare $seed $nsls 2>$null
    $plugin = Join-Path $root '.claude\local-plugins\nsls-builder-toolkit'
    New-Item -ItemType Directory -Path (Split-Path $plugin) -Force | Out-Null
    & git clone --quiet $nsls $plugin 2>$null
    TGit $plugin config user.email t@example.com | Out-Null
    TGit $plugin config user.name t | Out-Null
    return @{ Root = $root; Seed = $seed; Nsls = $nsls; Plugin = $plugin }
}
function Ship($W, [string]$Name, [string]$Text, [string]$Branch = 'main') {
    Commit $W.Seed $Name $Text
    TGit $W.Seed push --quiet $W.Nsls "HEAD:$Branch" | Out-Null
}
function Head($W)     { TGit $W.Plugin rev-parse HEAD }
function Upstream($W) { TGit $W.Plugin rev-parse '@{u}' }
function Run($W)      { (Update-Checkout -Dir $W.Plugin | Out-String) }
# A file git checked out: a runner's core.autocrlf may have given it CRLF endings.
function Read-Lf([string]$Path) { (Get-Content -LiteralPath $Path -Raw) -replace "`r", '' }

# 0. The plain case: fast-forwarded, silently.
$w = New-World
Ship $w 'skill2.md' "new`n"
$out = Run $w
Check 'a clean checkout behind NSLS is fast-forwarded onto it' ((Head $w) -eq (Upstream $w)) (Head $w)
Check '...silently, and with a clean tree' (($out.Trim() -eq '') -and ((TGit $w.Plugin status --porcelain) -eq '')) $out

# 0b. A commit of her own and one from NSLS: only --ff-only stands between this and
#     an unattended merge commit.
$w = New-World
Commit $w.Plugin 'mine.md' "her own commit`n"
Ship $w 'skill2.md' "new`n"
$before = Head $w
$out = Run $w
Check 'a checkout with its own commit is NOT merged into' (((Head $w) -eq $before) -and -not (Test-Path -LiteralPath (Join-Path $w.Plugin 'skill2.md')))
Check '...and the freeze names her commit' (($out -match 'FROZEN') -and ($out -match '1 local commit\(s\)')) $out

# 0c. A failed fetch merges nothing, even when an earlier fetch left the upstream ref ahead.
$w = New-World
Ship $w 'skill2.md' "new`n"
TGit $w.Plugin fetch --quiet | Out-Null
TGit $w.Plugin remote set-url origin (Join-Path $w.Root 'gone.git') | Out-Null
$before = Head $w
$out = Run $w
Check 'with the fetch failing, an older fetched upstream is NOT merged' (((Head $w) -eq $before) -and ((Upstream $w) -ne $before) -and ($out.Trim() -eq '')) $out

# 1. An ignored file the update starts tracking: kept, and the freeze is said out loud.
$w = New-World
Set-Content -Path (Join-Path $w.Plugin '.git\info\exclude') -Value "harvest-exclude.txt`n" -NoNewline
Set-Content -Path (Join-Path $w.Plugin 'harvest-exclude.txt') -Value "HER PRIVATE DENYLIST`n" -NoNewline
Ship $w 'harvest-exclude.txt' "nsls template`n"
$before = Head $w
$out = Run $w
Check 'an ignored file the update starts tracking is NOT overwritten' ((Get-Content (Join-Path $w.Plugin 'harvest-exclude.txt') -Raw) -eq "HER PRIVATE DENYLIST`n")
Check '...the checkout is left where it was' ((Head $w) -eq $before)
Check '...and the refusal is announced with the ignored-file repair' (($out -match 'FROZEN') -and ($out -match '1 ignored local file\(s\) that the update would replace') -and ($out -match 'copy them somewhere safe') -and ($out -notmatch 'backup branch')) $out
Check '...without echoing a path the update chose' ($out -notmatch 'harvest-exclude') $out

# 1b. A name git would quote (non-ASCII) is still found on disk: read NUL-separated, as UTF-8.
#     Built from a code point so this file stays ASCII for PowerShell 5.1.
$w = New-World
$uName = 'caf' + [char]0xE9 + '.env'
Set-Content -Path (Join-Path $w.Plugin '.git\info\exclude') -Value "*.env`n" -NoNewline
[System.IO.File]::WriteAllText((Join-Path $w.Plugin $uName), "HER KEYS`n")
[System.IO.File]::WriteAllText((Join-Path $w.Seed $uName), "nsls template`n")
TGit $w.Seed add -A | Out-Null
TGit $w.Seed commit --quiet -m 'add a non-ASCII name' | Out-Null
TGit $w.Seed push --quiet $w.Nsls 'HEAD:main' | Out-Null
$out = Run $w
Check 'an ignored file with a non-ASCII name is kept, and still counted in the warning' ((([System.IO.File]::ReadAllText((Join-Path $w.Plugin $uName))) -eq "HER KEYS`n") -and ($out -match '1 ignored local file\(s\)')) $out

# 2. Merge settings are neutralised.
$w = New-World
TGit $w.Plugin config branch.main.mergeOptions '-s ours' | Out-Null
Ship $w 'skill.md' "v2 from NSLS`n"
$out = Run $w
$parents = @((TGit $w.Plugin rev-list --parents -n1 HEAD) -split ' ').Count
Check "with mergeOptions '-s ours', the update is a true fast-forward" (((Head $w) -eq (Upstream $w)) -and ($parents -eq 2)) "parents+1=$parents"
Check "...and NSLS's change actually arrives" ((Read-Lf (Join-Path $w.Plugin 'skill.md')) -eq "v2 from NSLS`n")

$w = New-World
TGit $w.Seed push --quiet $w.Nsls 'HEAD:feat/pinned' | Out-Null
TGit $w.Plugin fetch --quiet | Out-Null
TGit $w.Plugin checkout --quiet -b feat/pinned --track origin/feat/pinned | Out-Null
TGit $w.Plugin config branch.feat/pinned.mergeOptions '-s ours' | Out-Null
Ship $w 'skill.md' "v2 from NSLS`n" 'feat/pinned'
$out = Run $w
Check 'on a branch other than main, its own mergeOptions are blanked too' (((Head $w) -eq (Upstream $w)) -and ((Read-Lf (Join-Path $w.Plugin 'skill.md')) -eq "v2 from NSLS`n"))

# Any printable-ASCII name without '=' is blanked; one with '=' cannot be (git splits
# -c at the first '='), so it is not merged at all rather than merged unguarded.
foreach ($case in @(@('we@ird;x', $true), @('a=b', $false))) {
    $name = $case[0]
    $w = New-World
    TGit $w.Seed push --quiet $w.Nsls "HEAD:$name" | Out-Null
    TGit $w.Plugin fetch --quiet | Out-Null
    TGit $w.Plugin checkout --quiet -b $name --track "origin/$name" | Out-Null
    TGit $w.Plugin config "branch.$name.mergeOptions" '-s ours' | Out-Null
    Ship $w 'skill.md' "v2 from NSLS`n" $name
    $before = Head $w
    $out = Run $w
    if ($case[1]) {
        Check "on branch '$name', mergeOptions are blanked and it is a true fast-forward" (((Head $w) -eq (Upstream $w)) -and ((Read-Lf (Join-Path $w.Plugin 'skill.md')) -eq "v2 from NSLS`n"))
    } else {
        Check "on branch '$name', which cannot be blanked, nothing is merged at all" (((Head $w) -eq $before) -and ((Read-Lf (Join-Path $w.Plugin 'skill.md')) -eq "v1`n"))
        Check '...and, being behind, it says so instead of going stale in silence' (($out -match 'cannot protect') -and ($out -match 'FROZEN') -and ($out -notmatch [regex]::Escape($name))) $out
    }
}

$w = New-World
TGit $w.Plugin config branch.main.mergeOptions --squash | Out-Null
Ship $w 'skill.md' "v2 from NSLS`n"
$out = Run $w
Check 'with mergeOptions=--squash, the branch moves and nothing is left staged' (((Head $w) -eq (Upstream $w)) -and ((TGit $w.Plugin status --porcelain) -eq '')) (TGit $w.Plugin status --porcelain)

$w = New-World
TGit $w.Plugin config merge.autoStash true | Out-Null
Set-Content -Path (Join-Path $w.Plugin 'skill.md') -Value "an edit she has not saved`n" -NoNewline
Ship $w 'skill.md' "v2 from NSLS`n"
$before = Head $w
$out = Run $w
Check 'with merge.autoStash on, an unsaved edit still blocks the update: nothing moves' ((Head $w) -eq $before)
Check '...the edit is exactly as she left it, not stashed or conflicted' (((Get-Content (Join-Path $w.Plugin 'skill.md') -Raw) -eq "an edit she has not saved`n") -and ((TGit $w.Plugin stash list) -eq ''))
Check '...and the freeze is announced' (($out -match 'FROZEN') -and ($out -match 'uncommitted local edits')) $out

# 3. No hooks, ever: installed through the checkout's local core.hooksPath.
$w = New-World
$hooksDir = Join-Path $w.Root 'builder-hooks'
New-Item -ItemType Directory -Path $hooksDir | Out-Null
$mergeMarker = (Join-Path $w.Root 'post-merge-ran') -replace '\\', '/'
$refMarker = (Join-Path $w.Root 'reference-transaction-ran') -replace '\\', '/'
Set-Content -Path (Join-Path $hooksDir 'post-merge') -Value "#!/bin/sh`ntouch '$mergeMarker'`nsleep 30`n" -NoNewline
Set-Content -Path (Join-Path $hooksDir 'reference-transaction') -Value "#!/bin/sh`ntouch '$refMarker'`n" -NoNewline
TGit $w.Plugin config core.hooksPath $hooksDir | Out-Null
Ship $w 'skill2.md' "new`n"
$clockT = [System.Diagnostics.Stopwatch]::StartNew()
$out = Run $w
$took = $clockT.Elapsed.TotalSeconds
Check 'a hanging post-merge hook is never run' (-not (Test-Path -LiteralPath $mergeMarker))
Check '...nor a reference-transaction hook, on the fetch or the merge' (-not (Test-Path -LiteralPath $refMarker))
Check "...so the update completes promptly ($([math]::Round($took, 1))s), fast-forwarded and silent" (($took -lt 15) -and ((Head $w) -eq (Upstream $w)) -and ($out.Trim() -eq '')) $out

# 4. Housekeeping stays off the merge: two packs against a limit of one, no
#    detaching, so auto-gc would consolidate them the moment anything asked.
$w = New-World
Ship $w 'skill2.md' "new`n"
TGit $w.Plugin -c maintenance.auto=false repack -q | Out-Null
TGit $w.Plugin -c maintenance.auto=false fetch --quiet | Out-Null
TGit $w.Plugin -c maintenance.auto=false repack -q | Out-Null
foreach ($kv in @(@('gc.autoPackLimit', '1'), @('gc.autoDetach', 'false'), @('maintenance.autoDetach', 'false'))) { TGit $w.Plugin config $kv[0] $kv[1] | Out-Null }
$packDir = Join-Path $w.Plugin '.git\objects\pack'
$beforePacks = @(Get-ChildItem -LiteralPath $packDir -Filter '*.pack').Count
$ffArgs = Get-FastForwardArgs -Dir $w.Plugin -Branch 'main' -Target '@{u}'
& git -C $w.Plugin @ffArgs 2>$null
$code = $LASTEXITCODE
$afterPacks = @(Get-ChildItem -LiteralPath $packDir -Filter '*.pack').Count
Check 'the fixture is real: two packs, so auto-gc would consolidate them' ($beforePacks -eq 2) "$beforePacks"
Check 'the guarded merge fast-forwards without running auto-gc or maintenance' (($code -eq 0) -and ((Head $w) -eq (Upstream $w)) -and ($afterPacks -eq $beforePacks)) "exit $code, packs $beforePacks -> $afterPacks"

# 5. An operation in progress is left alone, and said out loud.
$w = New-World
TGit $w.Plugin bisect start | Out-Null
Ship $w 'skill2.md' "new`n"
$before = Head $w
$out = Run $w
Check 'a checkout mid-bisect is NOT moved' ((Head $w) -eq $before)
Check '...and it is said out loud' (($out -match 'unfinished git operation') -and ($out -match 'FROZEN')) $out
Check '...while the fetch still ran' ((TGit $w.Plugin rev-parse origin/main) -eq (TGit $w.Nsls rev-parse main))

# 6. A detached checkout is a deliberate pin: not moved, nothing said.
$w = New-World
TGit $w.Plugin checkout --quiet --detach | Out-Null
Ship $w 'skill2.md' "new`n"
$before = Head $w
$out = Run $w
Check 'a detached (pinned) checkout is not moved, and nothing is said' (((Head $w) -eq $before) -and ($out.Trim() -eq '')) $out

if ($script:failures -gt 0) { Write-Host "$($script:failures) FAILED"; exit 1 }
Write-Host 'all daily update checks passed'
