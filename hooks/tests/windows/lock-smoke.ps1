# windows-ci: ps7 | One holder at a time, PowerShell and Python, on a real PC
# The fork-check lock on a real PC. Claim-Lock and Release-Lock are lifted out
# of session-start.ps1 by AST (its top level pulls and pings, so it is never
# dot-sourced) and proven to admit one holder at a time: PowerShell against
# PowerShell, Python against Python, and each against the other, because a PC
# with Python runs both copies of the check against this same file. Also the
# compatibility with the lock this one replaced: a token file left by a hook
# still on the previous code. Run by .github/workflows/windows-hooks.yml under
# PowerShell 7 (ProcessStartInfo.ArgumentList needs .NET Core).
$ErrorActionPreference = 'Stop'
$hooks  = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$script = Join-Path $hooks 'session-start.ps1'
$pyHook = Join-Path $hooks 'session-start.py'
$probe  = Join-Path $PSScriptRoot 'lock-probe.py'

$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($script, [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw "session-start.ps1 does not parse: $($errors[0].Message)" }
$fns = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and @('Claim-Lock', 'Release-Lock') -contains $n.Name }, $true)
if ($fns.Count -ne 2) { throw "expected Claim-Lock and Release-Lock in session-start.ps1, found $($fns.Count)" }
foreach ($f in $fns) { Invoke-Expression $f.Extent.Text }

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}
function Start-Probe([string]$Mode) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'python'
    foreach ($a in @($probe, $pyHook, $lock, $Mode, $legacy)) { $psi.ArgumentList.Add($a) }
    $psi.UseShellExecute = $false
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    return [System.Diagnostics.Process]::Start($psi)
}
function Read-Probe($Proc) {
    $Proc.WaitForExit(30000) | Out-Null
    $out = $Proc.StandardOutput.ReadToEnd()
    $err = $Proc.StandardError.ReadToEnd()
    if ($err) { Write-Host $err }
    $lines = @($out -split "`r?`n" | Where-Object { $_ -ne '' })
    # The leading comma stops PowerShell unrolling a one-line result into a bare
    # string - which made `$lines[0]` the letter "r" of "refused" on the first run.
    return ,$lines
}
function Probe-State([string]$Why) {
    # One JSON line from a fresh Python: can it open the file, can it lock it, and
    # what size and mtime age does it see - so a failing check names the step.
    $l = Read-Probe (Start-Probe 'probe')
    Write-Host "     python sees ($Why): $($l -join ' | ')"
}

$stem   = Join-Path ([System.IO.Path]::GetTempPath()) ("fork-check-{0}" -f [guid]::NewGuid().ToString('N'))
$lock   = "$stem.flock"   # this protocol's file
$legacy = "$stem.lock"    # the previous protocol's file: read, never written

# --- PowerShell against PowerShell --------------------------------------------
$a = Claim-Lock -Path $lock
Check 'PS: the first claim returns a handle' ($null -ne $a)
Check 'PS: a second claim while held is refused' ($null -eq (Claim-Lock -Path $lock))
Release-Lock -Handle $a
$b = Claim-Lock -Path $lock
Check 'PS: a claim after release succeeds' ($null -ne $b)
Release-Lock -Handle $b
Check 'PS: the lock file is never deleted' (Test-Path $lock)

# --- compatibility with the previous, token-in-a-file lock --------------------
[System.IO.File]::WriteAllText($legacy, '4242-1700000000-deadbeef')
Check 'PS: a fresh old-style token (an old hook mid-check) is yielded to' ($null -eq (Claim-Lock -Path $lock -LegacyPath $legacy))
Check '...and the old file is left exactly as it was' ([System.IO.File]::ReadAllText($legacy) -eq '4242-1700000000-deadbeef')
[System.IO.File]::SetLastWriteTime($legacy, (Get-Date).AddSeconds(-300))
$c = Claim-Lock -Path $lock -LegacyPath $legacy
Check 'PS: a stale old-style token (a dead old hook) does not block' ($null -ne $c)
Check '...and the dead token is replaced by our EMPTY shadow, fresh - an old hook looking now sees a live lock' (((Get-Item $legacy).Length -eq 0) -and (((Get-Date) - (Get-Item $legacy).LastWriteTime).TotalSeconds -lt 30))
Release-Lock -Handle $c
$e = Claim-Lock -Path $lock -LegacyPath $legacy
Check 'PS: our own empty shadow from last time does not block the next claim' ($null -ne $e)
Release-Lock -Handle $e

# --- Python against Python, and Python holding while PowerShell tries ---------
$p = Start-Probe 'hold'
$first = $p.StandardOutput.ReadLine()
$second = $p.StandardOutput.ReadLine()
Check 'PY: the first claim returns a handle' ($first -eq 'held')
Check 'PY: a second claim through another descriptor is refused' ($second -eq 'second-refused')
Check 'PS: refused while Python holds' ($null -eq (Claim-Lock -Path $lock))
$p.StandardInput.WriteLine('go')
$null = Read-Probe $p
Check 'PY: released cleanly' ($p.ExitCode -eq 0)
$d = Claim-Lock -Path $lock
Check 'PS: claims once Python has released' ($null -ne $d)

# --- PowerShell holding while Python tries ------------------------------------
Write-Host "     PowerShell handle: $($d.GetType().FullName), closed=$($d.SafeFileHandle.IsClosed)"
Probe-State 'while PowerShell holds'
$lines = Read-Probe (Start-Probe 'try')
Check 'PY: refused while PowerShell holds' ($lines[0] -eq 'refused') ($lines -join ' | ')
Release-Lock -Handle $d
$lines = Read-Probe (Start-Probe 'try')
Check 'PY: claims once PowerShell has released' ($lines[0] -eq 'held') ($lines -join ' | ')

# --- Python: a fresh old-style token is yielded to, a stale one is not --------
[System.IO.File]::WriteAllText($legacy, '4242-1700000000-deadbeef')
Probe-State 'fresh old-style token'
$lines = Read-Probe (Start-Probe 'try')
Check 'PY: a fresh old-style token (an old hook mid-check) is yielded to' ($lines[0] -eq 'refused') ($lines -join ' | ')
[System.IO.File]::SetLastWriteTime($legacy, (Get-Date).AddSeconds(-300))
Probe-State 'stale old-style token'
$lines = Read-Probe (Start-Probe 'try')
Check 'PY: a stale old-style token (a dead old hook) does not block' ($lines[0] -eq 'held') ($lines -join ' | ')
Check '...and the dead token is replaced by an EMPTY shadow' ((Get-Item $legacy).Length -eq 0)

Write-Host ''
if ($script:failures -gt 0) { Write-Host "$($script:failures) FAILED"; exit 1 }
Write-Host 'all Windows lock checks passed'
