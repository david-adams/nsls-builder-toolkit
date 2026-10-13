# windows-ci: ps51 | install.ps1 counts Git Bash wherever Claude Code finds it
# install.ps1's Git Bash check, run for real under Windows PowerShell 5.1.
# It warned "Git Bash isn't callable from this shell ... the guardrails will
# never fire" whenever Git's bin folder was off PATH, though Claude Code finds
# Git Bash on its own and the hooks ran fine (PC test 4, 2026-10-03). The block
# is lifted from install.ps1 between its own markers. A candidate must answer
# a probe, so the positive cases use the runner's real Git for Windows.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$src = Get-Content (Join-Path $repo 'install.ps1') -Raw
$start = $src.IndexOf('$bashOk = $false')
$end = $src.IndexOf('if ($Test) {', $start)
if ($start -lt 0 -or $end -le $start) { Write-Host 'FAIL could not find the Git Bash block in install.ps1'; exit 1 }
$block = $src.Substring($start, $end - $start)

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}
function Run-Block {
    $global:bashOk = $null
    $out = & { Invoke-Expression $block } 6>&1 | Out-String
    return @{ out = $out; ok = $bashOk }
}

$saved = @{}
foreach ($k in 'PATH', 'ProgramFiles', 'ProgramFiles(x86)', 'LOCALAPPDATA', 'CLAUDE_CODE_GIT_BASH_PATH') { $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process') }
$root = Join-Path ([IO.Path]::GetTempPath()) ('gitbash-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $root | Out-Null
try {
    # Nothing on PATH, no Git anywhere this block looks.
    [Environment]::SetEnvironmentVariable('PATH', "$env:SystemRoot\System32", 'Process')
    [Environment]::SetEnvironmentVariable('ProgramFiles', (Join-Path $root 'pf'), 'Process')
    [Environment]::SetEnvironmentVariable('ProgramFiles(x86)', (Join-Path $root 'pf86'), 'Process')
    [Environment]::SetEnvironmentVariable('LOCALAPPDATA', (Join-Path $root 'local'), 'Process')
    [Environment]::SetEnvironmentVariable('CLAUDE_CODE_GIT_BASH_PATH', $null, 'Process')
    # System32 can hold WSL's bash.exe; keep it out so "nothing installed" is real.
    $wsl = Join-Path $env:SystemRoot 'System32\bash.exe'
    if (Test-Path $wsl) { [Environment]::SetEnvironmentVariable('PATH', (Join-Path $root 'empty'), 'Process') }
    $r = Run-Block
    Check 'with no Git Bash anywhere, it warns' ($r.out -match "Git Bash isn't installed") $r.out

    # Something at the path that is not a working bash does not count.
    $bash = Join-Path $root 'pf\Git\bin\bash.exe'
    New-Item -ItemType Directory -Path (Split-Path $bash) -Force | Out-Null
    Set-Content -Path $bash -Value 'stub'
    $r = Run-Block
    Check 'a stub file where bash.exe should be still warns' ($r.out -match "Git Bash isn't installed") $r.out
    Remove-Item $bash
    New-Item -ItemType Directory -Path $bash -Force | Out-Null
    $r = Run-Block
    Check 'so does a folder named bash.exe' ($r.out -match "Git Bash isn't installed") $r.out

    # The runner's real Git for Windows, with its bin folder off PATH (the PC's case).
    $real = Join-Path $saved['ProgramFiles'] 'Git\bin\bash.exe'
    if (-not (Test-Path $real)) { Check 'this runner has Git for Windows' $false $real }
    [Environment]::SetEnvironmentVariable('ProgramFiles', $saved['ProgramFiles'], 'Process')
    $r = Run-Block
    Check 'Git Bash in Program Files, off PATH: no warning' (-not ($r.out -match 'Warning')) $r.out
    Check 'and it says where it found it' ($r.out -match 'Git Bash: found at') $r.out

    # CLAUDE_CODE_GIT_BASH_PATH pointing at a working bash counts too.
    [Environment]::SetEnvironmentVariable('ProgramFiles', (Join-Path $root 'pf'), 'Process')
    [Environment]::SetEnvironmentVariable('CLAUDE_CODE_GIT_BASH_PATH', $real, 'Process')
    $r = Run-Block
    Check 'CLAUDE_CODE_GIT_BASH_PATH is honoured' (-not ($r.out -match 'Warning')) $r.out
} finally {
    foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
}
if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) failed"; exit 1 }
Write-Host 'all checks passed'
