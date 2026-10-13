# windows-ci: ps51 | Collector bootstrap launches once, hidden, and stays out when installed or opted out
# Collector bootstrap on a real PC, with Start-Process stubbed so the real
# installer never runs. Proves under Windows PowerShell 5.1 that
# Invoke-CollectorBootstrap (hooks/collector_bootstrap.ps1): launches once
# when the collector is missing, writing the marker first; stays out when it
# is installed (config.json plus its scheduled task), when evidence is fresh,
# or when the builder opted out; repairs a config.json with no task; holds
# to one attempt per 24h and stops after 5; stands down while another session
# holds the lock; and that session-start.ps1 wires it with its output
# discarded. Run by .github/workflows/windows-hooks.yml. ASCII only.
$ErrorActionPreference = 'Stop'
$hooks = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}

# The stub. Functions win over cmdlets, so the dot-sourced module resolves
# Start-Process to this.
$script:launches = @()
$script:markerAtLaunch = $null
function Start-Process {
    param([string]$FilePath, [string]$WindowStyle, [object[]]$ArgumentList)
    $script:markerAtLaunch = if (Test-Path -LiteralPath $script:marker) { [System.IO.File]::ReadAllText($script:marker) } else { $null }
    $script:launches += ,@{ file = $FilePath; style = $WindowStyle; args = $ArgumentList }
}

. (Join-Path $hooks 'collector_bootstrap.ps1')

# The real task check, kept for one live probe; then a stub the boxes control.
$realTaskCheck = ${function:Test-CollectorTaskRegistered}
$script:taskRegistered = $true
function Test-CollectorTaskRegistered { return $script:taskRegistered }

$inv = [System.Globalization.CultureInfo]::InvariantCulture
$utf8 = New-Object System.Text.UTF8Encoding $false
$t0 = [DateTime]::new(2026, 9, 26, 12, 0, 0, [DateTimeKind]::Utc)
$saved = @{ cfg = $env:CLAUDE_CONFIG_DIR; lad = $env:LOCALAPPDATA; opt = $env:NSLS_COLLECTOR_OPTOUT; base = $env:NSLS_COLLECTOR_BASE_URL }

function New-Box {
    $root = Join-Path ([System.IO.Path]::GetTempPath()) ('nsls-cb-' + [guid]::NewGuid().ToString('N'))
    $env:CLAUDE_CONFIG_DIR = Join-Path $root 'claude'
    $env:LOCALAPPDATA = Join-Path $root 'LocalAppData'
    New-Item -ItemType Directory -Path $env:CLAUDE_CONFIG_DIR -Force | Out-Null
    New-Item -ItemType Directory -Path $env:LOCALAPPDATA -Force | Out-Null
    Remove-Item Env:NSLS_COLLECTOR_OPTOUT -ErrorAction SilentlyContinue
    Remove-Item Env:NSLS_COLLECTOR_BASE_URL -ErrorAction SilentlyContinue
    $script:marker = Join-Path $env:CLAUDE_CONFIG_DIR '.nsls-collector\bootstrap.json'
    $script:launches = @()
    $script:markerAtLaunch = $null
    $script:taskRegistered = $true
    return $root
}

$roots = @()
try {
    # Missing -> launched once, hidden, marker first, the self-serve command.
    $roots += New-Box
    $r = Invoke-CollectorBootstrap -Now $t0
    Check 'missing collector -> launched' ($r -eq 'launched') "$r"
    Check 'one launch' ($script:launches.Count -eq 1) "$($script:launches.Count)"
    if ($script:launches.Count -ge 1) {
        $l = $script:launches[0]
        Check 'launch is hidden powershell' (($l.file -eq 'powershell') -and ($l.style -eq 'Hidden')) "$($l.file) $($l.style)"
        $cmd = $l.args[-1]
        Check 'runs the self-serve one-liner' ($cmd -like '*iwr -UseBasicParsing https://signal.nsls.org/api/collector/dist/install.ps1 | iex*') $cmd
        Check 'launch flags' ((($l.args[0..3]) -join ' ') -eq '-NoProfile -ExecutionPolicy Bypass -Command') (($l.args) -join ' ')
    }
    Check 'marker written BEFORE the launch' ($script:markerAtLaunch -eq '{"attempted_at":"2026-09-26T12:00:00Z","attempts":1}') "$script:markerAtLaunch"

    # Throttle: 23h later nothing; 24h+ later a second attempt.
    $r = Invoke-CollectorBootstrap -Now $t0.AddHours(23)
    Check 'within 24h -> throttled' ($r -eq 'throttled') "$r"
    $r = Invoke-CollectorBootstrap -Now $t0.AddHours(24).AddMinutes(1)
    Check 'after 24h -> launched again' ($r -eq 'launched') "$r"
    Check 'two launches total' ($script:launches.Count -eq 2) "$($script:launches.Count)"

    # Cap: five attempts, then capped and logged once.
    $t = $t0.AddDays(2).AddMinutes(2)
    for ($i = 0; $i -lt 3; $i++) { $null = Invoke-CollectorBootstrap -Now $t; $t = $t.AddDays(1).AddMinutes(1) }
    Check 'five launches' ($script:launches.Count -eq 5) "$($script:launches.Count)"
    $r = Invoke-CollectorBootstrap -Now $t
    Check 'sixth -> capped' ($r -eq 'capped') "$r"
    $r = Invoke-CollectorBootstrap -Now $t.AddDays(3)
    Check 'still capped' ($r -eq 'capped') "$r"
    $logText = [System.IO.File]::ReadAllText((Join-Path $env:CLAUDE_CONFIG_DIR '.nsls-collector\bootstrap.log'))
    Check 'gave up logged once' (([regex]::Matches($logText, 'giving up')).Count -eq 1) $logText

    # Installed via config.json.
    $roots += New-Box
    $h = Join-Path $env:LOCALAPPDATA 'nsls-collector'
    New-Item -ItemType Directory -Path $h -Force | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $h 'config.json'), '{}', $utf8)
    $r = Invoke-CollectorBootstrap -Now $t0
    Check 'config.json + task -> installed' ($r -eq 'installed') "$r"
    Check 'no launch, no marker' (($script:launches.Count -eq 0) -and -not (Test-Path -LiteralPath $script:marker))

    # Half-finished install: config.json but no scheduled task -> repaired.
    $roots += New-Box
    $h = Join-Path $env:LOCALAPPDATA 'nsls-collector'
    New-Item -ItemType Directory -Path $h -Force | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $h 'config.json'), '{}', $utf8)
    $script:taskRegistered = $false
    $r = Invoke-CollectorBootstrap -Now $t0
    Check 'config.json without task -> launched' ($r -eq 'launched') "$r"
    Check 'half install: one launch' ($script:launches.Count -eq 1) "$($script:launches.Count)"

    # The real check runs schtasks (bounded, never throwing) under PS 5.1: a
    # task name that can't exist reads false. (A missed deadline isn't tested
    # here: schtasks answers a missing task faster than any deadline we could
    # race it against.)
    $ghost = 'NSLS Collector Probe ' + [guid]::NewGuid().ToString('N')
    $probe = & $realTaskCheck -TaskName $ghost
    Check 'real task check: missing task -> false' ($probe -eq $false) "$probe"

    # Installed via fresh evidence.
    $roots += New-Box
    $ev = Join-Path $env:CLAUDE_CONFIG_DIR '.nsls-collector'
    New-Item -ItemType Directory -Path $ev -Force | Out-Null
    $at = [DateTime]::UtcNow.AddHours(-1).ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", $inv)
    [System.IO.File]::WriteAllText((Join-Path $ev 'reported.json'), ('{"machine_id":"m","at":"' + $at + '","version":"1"}'), $utf8)
    $r = Invoke-CollectorBootstrap -Now $t0
    Check 'fresh evidence -> installed' ($r -eq 'installed') "$r"

    # Log goes to the collector home when that folder exists.
    $roots += New-Box
    $h = Join-Path $env:LOCALAPPDATA 'nsls-collector'
    New-Item -ItemType Directory -Path $h -Force | Out-Null
    $r = Invoke-CollectorBootstrap -Now $t0
    Check 'home without config -> launched' ($r -eq 'launched') "$r"
    Check 'logs into the collector home' (Test-Path -LiteralPath (Join-Path $h 'bootstrap.log'))

    # Opt-out.
    $roots += New-Box
    $env:NSLS_COLLECTOR_OPTOUT = '1'
    $r = Invoke-CollectorBootstrap -Now $t0
    Check 'opt-out -> optout' ($r -eq 'optout') "$r"
    Check 'opt-out: no launch, no marker' (($script:launches.Count -eq 0) -and -not (Test-Path -LiteralPath $script:marker))

    # Another session holds the lock.
    $roots += New-Box
    New-Item -ItemType Directory -Path (Split-Path $script:marker -Parent) -Force | Out-Null
    [System.IO.File]::WriteAllText((Join-Path (Split-Path $script:marker -Parent) 'bootstrap.lock'), '', $utf8)
    $r = Invoke-CollectorBootstrap -Now $t0
    Check 'lock held -> busy' ($r -eq 'busy') "$r"
    Check 'busy: no launch' ($script:launches.Count -eq 0)

    # An abandoned lock (older than 10 minutes) is taken over, and cleaned up.
    $roots += New-Box
    $lockDir = Split-Path $script:marker -Parent
    New-Item -ItemType Directory -Path $lockDir -Force | Out-Null
    $staleLock = Join-Path $lockDir 'bootstrap.lock'
    [System.IO.File]::WriteAllText($staleLock, '', $utf8)
    (Get-Item -LiteralPath $staleLock).LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-30)
    $r = Invoke-CollectorBootstrap -Now $t0
    Check 'stale lock -> launched' ($r -eq 'launched') "$r"
    Check 'stale lock: no lock or quarantine left' (@(Get-ChildItem -LiteralPath $lockDir -Filter 'bootstrap.lock*').Count -eq 0) ((@(Get-ChildItem -LiteralPath $lockDir) | ForEach-Object { $_.Name }) -join ', ')

    # A bad base URL override is refused, never launched.
    $roots += New-Box
    $env:NSLS_COLLECTOR_BASE_URL = 'https://x.org; Remove-Item C:\'
    $r = Invoke-CollectorBootstrap -Now $t0
    Check 'bad base url -> error' ($r -eq 'error') "$r"
    Check 'bad base url: no launch' ($script:launches.Count -eq 0)

    # session-start.ps1 dot-sources it and discards its output.
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $hooks 'session-start.ps1'), [ref]$tokens, [ref]$errors)
    $text = $ast.Extent.Text
    Check 'session-start.ps1 parses' ($errors.Count -eq 0)
    Check 'session-start.ps1 calls it with output discarded' ($text -match '\$null\s*=\s*Invoke-CollectorBootstrap')

    foreach ($f in @('collector_bootstrap.ps1', 'session-start.ps1')) {
        $bytes = [System.IO.File]::ReadAllBytes((Join-Path $hooks $f))
        $nonAscii = @($bytes | Where-Object { $_ -gt 127 }).Count
        Check "$f is ASCII-only with no BOM" ($nonAscii -eq 0) "$nonAscii non-ASCII bytes"
    }
} finally {
    $env:CLAUDE_CONFIG_DIR = $saved.cfg
    $env:LOCALAPPDATA = $saved.lad
    $env:NSLS_COLLECTOR_OPTOUT = $saved.opt
    $env:NSLS_COLLECTOR_BASE_URL = $saved.base
    foreach ($r in $roots) { Remove-Item -LiteralPath $r -Recurse -Force -ErrorAction SilentlyContinue }
}

if ($script:failures -gt 0) {
    Write-Host "$($script:failures) check(s) failed"
    exit 1
}
Write-Host 'collector bootstrap: all checks passed'
