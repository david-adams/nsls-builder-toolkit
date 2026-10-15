# windows-ci: ps51 | The session that updates the hook runs the new copy, not the old one
# The Windows hook runs the NEW copy of itself in the session whose update
# changed it. PowerShell parses a script before running it, so without this the
# session that pulled a fix kept running the old code (PC test, 2026-10-03).
# A throwaway world: NSLS as a bare repo, the builder's checkout cloned from it,
# the real session-start.ps1 with one marker line appended. The update changes
# only that marker, so which copy printed is visible in the output.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$real = Get-Content (Join-Path $repo 'hooks\session-start.ps1') -Raw

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}
function TGit { $d = $args[0]; $rest = @($args | Select-Object -Skip 1); & git -C $d @rest 2>$null | Out-Null; if ($LASTEXITCODE -ne 0) { throw "git $($rest -join ' ') failed in $d" } }
function Run-Hook([string]$HomeDir, [string]$Inherited = '') {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'powershell.exe'
    $hook = Join-Path $HomeDir '.claude\local-plugins\nsls-builder-toolkit\hooks\session-start.ps1'
    $psi.Arguments = ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $hook)
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $psi.EnvironmentVariables['USERPROFILE'] = $HomeDir
    $psi.EnvironmentVariables['NSLS_COLLECTOR_OPTOUT'] = '1'
    $psi.EnvironmentVariables.Remove('NSLS_SESSION_START_RERUN')
    if ($Inherited) { $psi.EnvironmentVariables['NSLS_SESSION_START_RERUN'] = $Inherited }
    $p = [System.Diagnostics.Process]::Start($psi)
    $e = $p.StandardError.ReadToEndAsync(); $o = $p.StandardOutput.ReadToEnd(); $p.WaitForExit(); $null = $e.Result
    return $o
}

$root = Join-Path ([IO.Path]::GetTempPath()) ('rerun-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
try {
    $seed = Join-Path $root 'seed'
    & git init --quiet -b main $seed 2>$null
    TGit $seed config user.email t@example.com
    TGit $seed config user.name t
    New-Item -ItemType Directory -Path (Join-Path $seed 'hooks') | Out-Null
    $hookPath = Join-Path $seed 'hooks\session-start.ps1'
    Set-Content -Path $hookPath -Value ($real + "`r`nWrite-Output 'NSLS-VERSION-A'`r`n") -Encoding ASCII -NoNewline
    TGit $seed add -A
    TGit $seed commit --quiet -m a
    $bare = Join-Path $root 'nsls.git'
    & git clone --quiet --bare $seed $bare 2>$null
    $homeDir = Join-Path $root 'home'
    $checkout = Join-Path $homeDir '.claude\local-plugins\nsls-builder-toolkit'
    New-Item -ItemType Directory -Path (Split-Path $checkout) -Force | Out-Null
    & git clone --quiet $bare $checkout 2>$null

    # NSLS ships a new version of the hook.
    Set-Content -Path $hookPath -Value ($real + "`r`nWrite-Output 'NSLS-VERSION-B'`r`n") -Encoding ASCII -NoNewline
    TGit $seed commit --quiet -am b
    TGit $seed push --quiet $bare main

    $out = Run-Hook $homeDir
    Check 'the session that pulls the update runs the new copy' ($out -match 'NSLS-VERSION-B') $out
    Check 'and not the old one' (-not ($out -match 'NSLS-VERSION-A')) $out
    Check 'the new copy prints once' (([regex]::Matches($out, 'NSLS-VERSION-B')).Count -eq 1) $out

    $out = Run-Hook $homeDir
    Check 'with nothing new, it runs once, as before' (([regex]::Matches($out, 'NSLS-VERSION-B')).Count -eq 1) $out

    # A marker inherited from elsewhere is not this session's re-run: it must
    # neither stop the update nor stop the new copy running.
    Set-Content -Path $hookPath -Value ($real + "`r`nWrite-Output 'NSLS-VERSION-C'`r`n") -Encoding ASCII -NoNewline
    TGit $seed commit --quiet -am c
    TGit $seed push --quiet $bare main
    $out = Run-Hook $homeDir '1'
    Check 'an inherited marker does not stop the update or the re-run' ((([regex]::Matches($out, 'NSLS-VERSION-C')).Count -eq 1) -and -not ($out -match 'NSLS-VERSION-B')) $out
} finally {
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
}
if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) failed"; exit 1 }
Write-Host 'all checks passed'
