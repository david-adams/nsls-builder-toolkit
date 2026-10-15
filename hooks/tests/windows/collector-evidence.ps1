# windows-ci: ps51 | Collector evidence silences the skill hook
# Collector early-exit on a real PC. Proves, under Windows PowerShell:
#   1. Test-CollectorEvidenceFresh (hooks/collector_evidence.ps1) applies the
#      same contract as hooks/collector_evidence.py, over the same cases as
#      hooks/tests/test_collector_evidence.py (exact key set, duplicates,
#      nesting, escapes, calendar validity with a pinned clock, and so on).
#   2. skill-event.ps1 with a fresh evidence file does NOT launch skill-post.ps1,
#      and with a stale one it still does.
#   3. session-ping.ps1 still wires the collector_backed flag.
# The hooks run from a scratch copy with a stub skill-post.ps1 that only drops
# a marker file, so nothing here ever reaches the tracker.
# Run by .github/workflows/windows-hooks.yml. ASCII only, PS 5.1 compatible.
$ErrorActionPreference = 'Stop'
$hooks = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}

$inv = [System.Globalization.CultureInfo]::InvariantCulture
function Iso([DateTime]$utc) { return $utc.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", $inv) }
function Rec([string]$at, [string]$mid = 'm-123') {
    return ('{"machine_id":"' + $mid + '","at":"' + $at + '","version":"1.0.0"}')
}
function Utc([int]$y, [int]$mo, [int]$d, [int]$h) { return [DateTime]::new($y, $mo, $d, $h, 0, 0, [DateTimeKind]::Utc) }
$now = [DateTime]::UtcNow
$utf8 = New-Object System.Text.UTF8Encoding $false

$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('nsls-ce-' + [guid]::NewGuid().ToString('N'))
$cfg = Join-Path $scratch 'config'
$evDir = Join-Path $cfg '.nsls-collector'
$evFile = Join-Path $evDir 'reported.json'
New-Item -ItemType Directory -Path $evDir -Force | Out-Null

$oldCfg = $env:CLAUDE_CONFIG_DIR
$oldProfile = $env:USERPROFILE
try {
    $env:CLAUDE_CONFIG_DIR = $cfg
    # skill-event.ps1 reads BUILDER_EMAIL from %USERPROFILE%\.claude\...\.env.
    $env:USERPROFILE = $scratch
    $envDir = Join-Path $scratch '.claude\local-plugins\nsls-personal-toolkit'
    New-Item -ItemType Directory -Path $envDir -Force | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $envDir '.env'), "BUILDER_EMAIL=builder@nsls.org`n", $utf8)

    # 1. The contract, case by case.
    . (Join-Path $hooks 'collector_evidence.ps1')
    $h1 = Iso $now.AddHours(-1)
    $d8 = Iso $now.AddDays(-8)
    $cases = @(
        @{ n = 'fresh';                   c = (Rec $h1); want = $true },
        @{ n = 'fractional +00:00';       c = (Rec ($now.AddDays(-6).ToString("yyyy-MM-dd'T'HH:mm:ss'.123456+00:00'", $inv))); want = $true },
        @{ n = 'clock skew +12h';         c = (Rec (Iso $now.AddHours(12))); want = $true },
        @{ n = 'pretty, any key order';   c = ("{`r`n  `"version`": `"1.0.0`",`r`n  `"at`": `"$h1`",`r`n  `"machine_id`": `"m-1`"`r`n}`r`n"); want = $true },
        @{ n = '8 days old';              c = (Rec $d8); want = $false },
        @{ n = 'missing';                 c = $null; want = $false },
        @{ n = 'malformed';               c = '{"machine_id": "m-1", "at": '; want = $false },
        @{ n = 'trailing comma';          c = ('{"machine_id":"m","at":"' + $h1 + '","version":"1",}'); want = $false },
        @{ n = 'not an object';           c = ('[' + (Rec $h1) + ']'); want = $false },
        @{ n = 'far future';              c = (Rec (Iso $now.AddDays(2))); want = $false },
        @{ n = 'numeric machine_id';      c = ('{"machine_id":123,"at":"' + $h1 + '","version":"1"}'); want = $false },
        @{ n = 'empty machine_id';        c = (Rec $h1 ''); want = $false },
        @{ n = 'missing machine_id';      c = ('{"at":"' + $h1 + '","version":"1"}'); want = $false },
        @{ n = 'missing version';         c = ('{"machine_id":"m","at":"' + $h1 + '"}'); want = $false },
        @{ n = 'numeric version';         c = ('{"machine_id":"m","at":"' + $h1 + '","version":1}'); want = $false },
        @{ n = 'extra key';               c = ('{"machine_id":"m","at":"' + $h1 + '","version":"1","extra":"x"}'); want = $false },
        @{ n = 'duplicate at fresh,stale'; c = ('{"machine_id":"m","at":"' + $h1 + '","at":"' + $d8 + '","version":"1"}'); want = $false },
        @{ n = 'duplicate at stale,fresh'; c = ('{"machine_id":"m","at":"' + $d8 + '","at":"' + $h1 + '","version":"1"}'); want = $false },
        @{ n = 'nested decoy';            c = ('{"machine_id":"","at":"' + $d8 + '","version":{"machine_id":"m","at":"' + $h1 + '"}}'); want = $false },
        @{ n = 'escaped value';           c = ('{"machine_id":"m\u0041","at":"' + $h1 + '","version":"1"}'); want = $false },
        @{ n = 'non-ASCII value';         c = ('{"machine_id":"m' + [char]0xE9 + '","at":"' + $h1 + '","version":"1"}'); want = $false },
        @{ n = 'NUL byte';                c = ('{"machine_id":"m' + [char]0 + '","at":"' + $h1 + '","version":"1"}'); want = $false },
        @{ n = 'non-UTC offset';          c = (Rec ($now.AddHours(-1).ToString("yyyy-MM-dd'T'HH:mm:ss'-05:00'", $inv))); want = $false },
        @{ n = 'date only';               c = (Rec ($now.ToString('yyyy-MM-dd', $inv))); want = $false },
        @{ n = 'garbage at';              c = (Rec 'yesterday'); want = $false },
        @{ n = 'oversized';               c = ('{"machine_id":"m","at":"' + $h1 + '","version":"' + ('x' * 5000) + '"}'); want = $false },
        @{ n = 'Feb 30';                  c = (Rec '2026-02-30T12:00:00Z'); want = $false; now = (Utc 2026 3 2 12) },
        @{ n = 'Feb 29 non-leap century'; c = (Rec '2100-02-29T12:00:00Z'); want = $false; now = (Utc 2100 3 2 12) },
        @{ n = 'Feb 29 leap year';        c = (Rec '2028-02-29T12:00:00Z'); want = $true;  now = (Utc 2028 3 1 12) },
        @{ n = 'fraction just past 7 days';   c = (Rec '2026-09-01T12:00:00.900Z'); want = $false; now = ([DateTime]::new(2026, 9, 8, 12, 0, 1, [DateTimeKind]::Utc)) },
        @{ n = 'fraction just inside 7 days'; c = (Rec '2026-09-01T12:00:00.900Z'); want = $true;  now = ([DateTime]::new(2026, 9, 8, 12, 0, 0, [DateTimeKind]::Utc)) },
        @{ n = 'Apr 31';                  c = (Rec '2026-04-31T12:00:00Z'); want = $false; now = (Utc 2026 5 2 12) },
        @{ n = 'hour 24';                 c = (Rec '2026-05-01T24:00:00Z'); want = $false; now = (Utc 2026 5 2 12) }
    )
    foreach ($case in $cases) {
        Remove-Item -LiteralPath $evFile -Force -ErrorAction SilentlyContinue
        if ($null -ne $case.c) { [System.IO.File]::WriteAllText($evFile, $case.c, $utf8) }
        if ($case.ContainsKey('now')) { $got = Test-CollectorEvidenceFresh -Now $case.now } else { $got = Test-CollectorEvidenceFresh }
        Check ("fresh() " + $case.n + " -> " + $case.want) ($got -eq $case.want) "$got"
    }

    # A directory where the file should be is not fresh (and does not throw).
    Remove-Item -LiteralPath $evFile -Force -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $evFile -Force | Out-Null
    Check 'fresh() directory at the path -> False' ((Test-CollectorEvidenceFresh) -eq $false)
    Remove-Item -LiteralPath $evFile -Recurse -Force

    # 2. skill-event.ps1 end to end, from a scratch copy with a stub poster.
    $hk = Join-Path $scratch 'hooks'
    New-Item -ItemType Directory -Path $hk -Force | Out-Null
    Copy-Item (Join-Path $hooks 'skill-event.ps1') $hk
    Copy-Item (Join-Path $hooks 'collector_evidence.ps1') $hk
    $marker = Join-Path $scratch 'posted.txt'
    $stub = "param([string]`$Email, [string]`$Skill)`r`n[System.IO.File]::WriteAllText('$marker', `$Skill)`r`n"
    [System.IO.File]::WriteAllText((Join-Path $hk 'skill-post.ps1'), $stub, $utf8)
    $hookJson = '{"tool_name":"Skill","tool_input":{"skill":"nsls-builder-toolkit:gws"}}'

    function Invoke-SkillEvent {
        Remove-Item -LiteralPath $marker -Force -ErrorAction SilentlyContinue
        $hookJson | & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $hk 'skill-event.ps1') | Out-Null
        $rc = $LASTEXITCODE
        # The poster is detached; give it time to land before judging.
        for ($i = 0; $i -lt 40 -and -not (Test-Path -LiteralPath $marker); $i++) { Start-Sleep -Milliseconds 250 }
        return @{ rc = $rc; posted = (Test-Path -LiteralPath $marker) }
    }

    [System.IO.File]::WriteAllText($evFile, (Rec (Iso $now.AddHours(-1))), $utf8)
    $r = Invoke-SkillEvent
    Check 'skill-event.ps1 fresh evidence: exit 0' ($r.rc -eq 0) "$($r.rc)"
    Check 'skill-event.ps1 fresh evidence: skill-post.ps1 NOT launched' (-not $r.posted)

    [System.IO.File]::WriteAllText($evFile, (Rec (Iso $now.AddDays(-8))), $utf8)
    $r = Invoke-SkillEvent
    Check 'skill-event.ps1 8-day-old evidence: exit 0' ($r.rc -eq 0) "$($r.rc)"
    Check 'skill-event.ps1 8-day-old evidence: skill-post.ps1 launched' $r.posted
    if ($r.posted) {
        $posted = [System.IO.File]::ReadAllText($marker)
        Check 'skill-event.ps1 strips our prefix' ($posted -eq 'gws') $posted
    }

    Remove-Item -LiteralPath $evFile -Force
    $r = Invoke-SkillEvent
    Check 'skill-event.ps1 no evidence: skill-post.ps1 launched' $r.posted

    # 3. session-ping.ps1 still carries the flag wiring (it POSTs, so it is
    # checked by AST rather than run).
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $hooks 'session-ping.ps1'), [ref]$tokens, [ref]$errors)
    $text = $ast.Extent.Text
    Check 'session-ping.ps1 parses' ($errors.Count -eq 0)
    Check 'session-ping.ps1 sets collector_backed when fresh' (($text -match 'Test-CollectorEvidenceFresh') -and ($text -match 'collector_backed\s*=\s*\$true'))

    # ASCII only, no BOM, in every file this change touches.
    foreach ($f in @('collector_evidence.ps1', 'skill-event.ps1', 'session-ping.ps1')) {
        $bytes = [System.IO.File]::ReadAllBytes((Join-Path $hooks $f))
        $nonAscii = @($bytes | Where-Object { $_ -gt 127 }).Count
        Check "$f is ASCII-only with no BOM" ($nonAscii -eq 0) "$nonAscii non-ASCII bytes"
    }
} finally {
    $env:CLAUDE_CONFIG_DIR = $oldCfg
    $env:USERPROFILE = $oldProfile
    Remove-Item -LiteralPath $scratch -Recurse -Force -ErrorAction SilentlyContinue
}

if ($script:failures -gt 0) {
    Write-Host "$($script:failures) check(s) failed"
    exit 1
}
Write-Host 'collector evidence: all checks passed'
