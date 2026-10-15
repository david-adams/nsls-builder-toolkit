# windows-ci: ps51 | install.ps1 finds the claude CLI a Store-installed Claude app hides
# install.ps1's claude CLI search, run for real under Windows PowerShell 5.1.
# The Store app's CLI lives under %LOCALAPPDATA%\Packages\Claude_*\LocalCache,
# because a Store install hides %APPDATA%\Claude from every other process. The
# installer only looked at %APPDATA% and said "Could not find the 'claude' CLI"
# on every run (PC test 4, 2026-10-03). The block is lifted from install.ps1
# between its own markers, so this can never test a copy.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$src = Get-Content (Join-Path $repo 'install.ps1') -Raw
$start = $src.IndexOf('# --- Find the claude CLI')
$end = $src.IndexOf('# --- Step 2:')
if ($start -lt 0 -or $end -le $start) { Write-Host 'FAIL could not find the CLI search block in install.ps1'; exit 1 }
$block = $src.Substring($start, $end - $start)

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}
function Exe([string]$Path) { New-Item -ItemType Directory -Path (Split-Path $Path) -Force | Out-Null; Set-Content -Path $Path -Value 'stub' }

$saved = @{ APPDATA = $env:APPDATA; LOCALAPPDATA = $env:LOCALAPPDATA; USERPROFILE = $env:USERPROFILE; PATH = $env:PATH }
$root = Join-Path ([IO.Path]::GetTempPath()) ('findcli-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
try {
    $env:APPDATA = Join-Path $root 'Roaming'
    $env:LOCALAPPDATA = Join-Path $root 'Local'
    $env:USERPROFILE = Join-Path $root 'home'
    $env:PATH = "$env:SystemRoot\System32"   # no claude on PATH, as on the PC

    $store = Join-Path $env:LOCALAPPDATA 'Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\2.1.284\claude.exe'
    Exe $store
    Exe (Join-Path $env:LOCALAPPDATA 'Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\2.1.9\claude.exe')
    $ClaudeBin = $null
    Invoke-Expression $block
    Check 'a Store-only install is found, newest version' ($ClaudeBin -eq $store) "$ClaudeBin"

    $appdata = Join-Path $env:APPDATA 'Claude\claude-code\2.1.300\claude.exe'
    Exe $appdata
    $ClaudeBin = $null
    Invoke-Expression $block
    Check 'the newest version wins across both locations' ($ClaudeBin -eq $appdata) "$ClaudeBin"

    # The Store app's newer layout: <version>\<hash>\claude.exe (PC Test Round 5).
    $hashed = Join-Path $env:LOCALAPPDATA 'Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\2.1.400\635c1867224a\claude.exe'
    Exe $hashed
    $ClaudeBin = $null
    Invoke-Expression $block
    Check 'a CLI inside a hash folder is found, and its version counts' ($ClaudeBin -eq $hashed) "$ClaudeBin"

    # Two hash folders of one version: the newer file wins.
    $twin = Join-Path $env:LOCALAPPDATA 'Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude-code\2.1.400\bbbbbbbbbbbb\claude.exe'
    Exe $twin
    (Get-Item $hashed).LastWriteTime = (Get-Date).AddDays(-2)
    (Get-Item $twin).LastWriteTime = (Get-Date).AddDays(-1)
    $ClaudeBin = $null
    Invoke-Expression $block
    Check 'two hash folders of one version: the newer file wins' ($ClaudeBin -eq $twin) "$ClaudeBin"
} finally {
    foreach ($k in $saved.Keys) { Set-Item -Path "Env:$k" -Value $saved[$k] }
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
}
if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) failed"; exit 1 }
Write-Host 'all checks passed'
