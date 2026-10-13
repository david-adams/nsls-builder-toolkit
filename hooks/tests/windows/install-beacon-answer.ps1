# windows-ci: ps51 | install.ps1 asks the plugin which hooks it already runs, and ignores a disabled plugin
# install.ps1's Get-BeaconAnswer, run for real under Windows PowerShell 5.1.
# It asks hooks/plugin_beacon.py which hooks the toolkit plugin already runs
# here, so a re-install leaves alone what the migration retired. A text check
# on the function cannot see PS 5.1 mangling the call, so this lifts the
# function out of install.ps1 and runs it against a scratch config: evidence
# for two hooks, then the same machine with the plugin disabled.
# Run by .github/workflows/windows-hooks.yml. ASCII only, PS 5.1 compatible.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}

$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repo 'install.ps1'), [ref]$null, [ref]$null)
$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-BeaconAnswer' }, $true)
if (-not $fn) { Write-Host 'FAIL Get-BeaconAnswer is not in install.ps1'; exit 1 }
. ([scriptblock]::Create($fn.Extent.Text))

$utf8 = New-Object System.Text.UTF8Encoding $false
$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('nsls-ba-' + [guid]::NewGuid().ToString('N'))
$ConfigDir = Join-Path $scratch '.claude'
$HooksDir = Join-Path $ConfigDir 'local-plugins\nsls-builder-toolkit\hooks'
$cache = Join-Path $ConfigDir 'plugins\cache\nsls-toolkit\nsls-builder-toolkit\3.16.5'
New-Item -ItemType Directory -Path $HooksDir, (Join-Path $cache 'hooks') -Force | Out-Null
Copy-Item (Join-Path $repo 'hooks\plugin_beacon.py') $HooksDir
$PyExe = (Get-Command python).Source

[System.IO.File]::WriteAllText((Join-Path $cache '.mcp.json'), '{"mcpServers": {}}', (New-Object System.Text.UTF8Encoding $false))
$reg = @{ plugins = @{ 'nsls-builder-toolkit@nsls-toolkit' = @(@{ installPath = $cache }) } } | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText((Join-Path $ConfigDir 'plugins\installed_plugins.json'), $reg, $utf8)
$settings = Join-Path $ConfigDir 'settings.json'
[System.IO.File]::WriteAllText($settings, '{"enabledPlugins": {}}', $utf8)

# Evidence, recorded the way the plugin's own hooks record it.
$rec = Join-Path $scratch 'rec.py'
[System.IO.File]::WriteAllText($rec, @"
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import plugin_beacon
for h in sys.argv[3:]:
    f = Path(sys.argv[2]) / (h + '-copy.py')
    f.write_text('#')
    assert plugin_beacon.record(h, f)
"@, $utf8)
$saved = $env:CLAUDE_CONFIG_DIR
try {
    $env:CLAUDE_CONFIG_DIR = $ConfigDir
    & $PyExe $rec $HooksDir (Join-Path $cache 'hooks') 'session-start' 'guardrail-gate'
    if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL could not record the evidence'; exit 1 }

    $env:CLAUDE_CONFIG_DIR = 'sentinel'
    $p = @(Get-BeaconAnswer '--proven')
    Check 'lists exactly the hooks the plugin has proven' (($p -join ',') -eq 'session-start,guardrail-gate') ($p -join ',')
    Check 'puts CLAUDE_CONFIG_DIR back afterwards' ($env:CLAUDE_CONFIG_DIR -eq 'sentinel') $env:CLAUDE_CONFIG_DIR
    Check 'reports the plugin installed' (@(Get-BeaconAnswer '--installed') -contains 'installed')

    if (Get-Command py -ErrorAction SilentlyContinue) {
        $real = $PyExe
        $PyExe = Join-Path $scratch 'no-such-python.exe'
        $p = @(Get-BeaconAnswer '--proven')
        Check 'falls back to the py launcher with the same answer' (($p -join ',') -eq 'session-start,guardrail-gate') ($p -join ',')
        $PyExe = $real
    }

    [System.IO.File]::WriteAllText($settings, '{"enabledPlugins": {"nsls-builder-toolkit@nsls-toolkit": false}}', $utf8)
    $p = @(Get-BeaconAnswer '--proven')
    Check 'a disabled plugin proves nothing, so every shim goes back' ($p.Count -eq 0) ($p -join ',')
    Check 'and does not count as installed' (-not (@(Get-BeaconAnswer '--installed') -contains 'installed'))
} finally {
    $env:CLAUDE_CONFIG_DIR = $saved
}

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) failed"; exit 1 }
Write-Host 'all checks passed'
