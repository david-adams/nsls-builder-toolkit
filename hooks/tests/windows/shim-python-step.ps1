# windows-ci: ps51 | The hook's Python step runs, and says so when it cannot, under the exact settings.json command
# The Windows hook's Python step, run exactly the way settings.json runs it:
# `powershell -NoProfile -ExecutionPolicy Bypass -File ...\session-start.ps1`,
# which is Windows PowerShell 5.1 on every PC. Run this file under 5.1 too.
#
# The step used to call `py -3 -c` with a program containing double quotes.
# 5.1 strips those quotes from a native argument, Python raised a NameError, and
# the step - guardrails context, plugin stage A, freshness - never ran on any
# PC. PowerShell 7.3+ escapes the quotes, so a test under pwsh could not see it.
# This test runs the real hook in a throwaway profile and checks three things:
# the policy reaches stdout; a Python step that fails is said out loud once a
# day and recorded; and a later good run clears that record.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}

# A copy of the toolkit (hooks + the CLAUDE.md the policy is read from) and an
# empty profile, both under one temp root. Nothing in the real profile is read.
function New-World {
    $root = Join-Path ([IO.Path]::GetTempPath()) ('shimpy-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    $tk = Join-Path $root 'toolkit'
    New-Item -ItemType Directory -Path (Join-Path $root 'home\.claude') -Force | Out-Null
    New-Item -ItemType Directory -Path $tk -Force | Out-Null
    Copy-Item -Path (Join-Path $repo 'hooks') -Destination $tk -Recurse
    Copy-Item -Path (Join-Path $repo 'CLAUDE.md') -Destination $tk
    # The full hook also launches the tracker ping and the collector, which
    # can reach production. Neither is under test: both become no-ops here.
    foreach ($n in 'session-ping.ps1', 'collector_bootstrap.ps1') {
        Set-Content -Path (Join-Path $tk "hooks\$n") -Value 'function Invoke-CollectorBootstrap { }' -Encoding ASCII
    }
    New-Item -ItemType Directory -Path (Join-Path $root 'cfg') -Force | Out-Null
    return $root
}

# CLAUDE_CONFIG_DIR deliberately differs from %USERPROFILE%\.claude, so a
# record written to the wrong one of the two is caught.
function Invoke-Hook([string]$Root) {
    $env:USERPROFILE = Join-Path $Root 'home'
    $env:HOME = Join-Path $Root 'home'
    $env:CLAUDE_CONFIG_DIR = Join-Path $Root 'cfg'
    $env:NSLS_NO_PLUGIN_MIGRATION = '1'   # never install anything from CI
    $env:NSLS_COLLECTOR_OPTOUT = '1'
    $hook = Join-Path $Root 'toolkit\hooks\session-start.ps1'
    # The exact command install.ps1 registers in settings.json, its stdout read
    # as raw UTF-8 the way Claude Code reads a hook's - not through this
    # process's console code page, which a CI runner cannot set.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'powershell'
    $psi.Arguments = ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $hook)
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = New-Object System.Text.UTF8Encoding $false
    $p = [System.Diagnostics.Process]::Start($psi)
    $errTask = $p.StandardError.ReadToEndAsync()
    $out = $p.StandardOutput.ReadToEnd()
    $p.WaitForExit()
    $null = $errTask.Result
    return $out
}

function Status-Path([string]$Root) { return (Join-Path $Root 'cfg\.nsls-plugin-migration-status') }
function Read-Status([string]$Root) {
    $f = Status-Path $Root
    if (-not (Test-Path $f)) { return $null }
    return (Get-Content $f -Raw | ConvertFrom-Json)
}

Write-Host "Windows PowerShell $($PSVersionTable.PSVersion)"
$saved = @{}
foreach ($v in 'USERPROFILE', 'HOME', 'CLAUDE_CONFIG_DIR', 'NSLS_NO_PLUGIN_MIGRATION', 'NSLS_COLLECTOR_OPTOUT') {
    $saved[$v] = [Environment]::GetEnvironmentVariable($v, 'Process')
}
$w = New-World
try {

$out = Invoke-Hook $w
Check 'the guardrail policy reaches stdout under Windows PowerShell 5.1' ($out -match 'org guardrail policy') $out.Substring(0, [Math]::Min(300, $out.Length))
Check 'and its non-ASCII text arrives intact' ($out.Contains("1 $([char]0x00B7) Personal"))
Check 'a good run leaves no stuck record' ($null -eq (Read-Status $w))
Check 'no traceback reaches stdout' (-not ($out -match 'Traceback'))

# Break the step the same way the old quoting did: Python starts and dies.
$entry = Join-Path $w 'toolkit\hooks\guardrails_entry.py'
$good = Get-Content $entry -Raw
Set-Content -Path $entry -Value "raise NameError(`"name '__guardrails__' is not defined`")" -Encoding ASCII
$out = Invoke-Hook $w
$st = Read-Status $w
Check 'a Python step that fails is said out loud' ($out -match 'Setup could not finish on this machine') $out
Check 'the failure is recorded as the shim-python stage' ($st -and $st.stage -eq 'shim-python') ($st | ConvertTo-Json -Compress)
Check 'the record keeps the exit code and the error line' ($st -and $st.detail -match 'exit 1' -and $st.detail -match 'NameError') ($st | ConvertTo-Json -Compress)
Check 'the raw error never reaches stdout' (-not ($out -match 'NameError'))

$out = Invoke-Hook $w
Check 'the second failing session that day stays quiet' (-not ($out -match 'Setup could not finish')) $out

Set-Content -Path $entry -Value $good -NoNewline -Encoding ASCII
$out = Invoke-Hook $w
Check 'a good run afterwards clears the shim-python record' ($null -eq (Read-Status $w))
Check 'and the policy is back' ($out -match 'org guardrail policy')

# Python's own stage-A record shares the file; a good wrapper run must leave it.
Set-Content -Path (Status-Path $w) -Value '{"at": 1, "stage": "a", "reason": "x", "noticed": 0}' -Encoding ASCII
$out = Invoke-Hook $w
$st = Read-Status $w
Check "a good run leaves Python's own stage-A record alone" ($st -and $st.stage -eq 'a') ($st | ConvertTo-Json -Compress)
Check 'nothing was written under %USERPROFILE%\.claude instead' (-not (Test-Path (Join-Path $w 'home\.claude\.nsls-plugin-migration-status')))
} finally {
    foreach ($v in $saved.Keys) { [Environment]::SetEnvironmentVariable($v, $saved[$v], 'Process') }
    Remove-Item -Recurse -Force $w -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) failed"; exit 1 }
Write-Host 'all checks passed'
