# session-ping.ps1 - Windows session ping (run DETACHED by session-start.ps1).
# Windows counterpart to the session_ping() in session-start.py. POSTs to the
# tracker /session-ping so merged-PR credits, stage advancement, and the daily
# session point are recorded. Launched detached so the proxy's cold-start
# latency (15-27s) never delays session start. Fails silently; logs any
# credit/stage messages to nsls-session-ping.log (not shown inline).
$ErrorActionPreference = 'SilentlyContinue'
$ProxyUrl = if ($env:NSLS_TRACKER_URL) { $env:NSLS_TRACKER_URL } else { 'https://web-production-6281e.up.railway.app' }
$LogFile  = Join-Path $env:USERPROFILE '.claude\hooks\nsls-session-ping.log'

function Read-EnvValue {
    param([string]$EnvPath, [string]$Key)
    if (-not (Test-Path $EnvPath)) { return '' }
    foreach ($line in Get-Content $EnvPath -Encoding UTF8) {
        if ($line -match ('^' + [regex]::Escape($Key) + '=(.*)$')) { return $Matches[1].Trim().Trim('"') }
    }
    return ''
}

$envFile = Join-Path $env:USERPROFILE '.claude\local-plugins\nsls-personal-toolkit\.env'
$email = Read-EnvValue -EnvPath $envFile -Key 'BUILDER_EMAIL'
if (-not $email) { try { $email = (& git config user.email) } catch {} }
$email = "$email".Trim()
if ([string]::IsNullOrWhiteSpace($email)) { exit 0 }
# Without a GitHub username the tracker skips the PR-credit check entirely, so a
# builder who never ran /personal-setup earns nothing for merged PRs and gets no
# error saying so. Fall back to the gh CLI when it's available and authenticated.
$github = Read-EnvValue -EnvPath $envFile -Key 'GITHUB_USERNAME'
if ([string]::IsNullOrWhiteSpace($github)) {
    try { $github = (& gh api user --jq .login 2>$null) } catch {}
}
$github = "$github".Trim()
$builderDir = Join-Path $env:USERPROFILE '.claude\local-plugins\nsls-builder-toolkit'
$toolkit = if (Test-Path $builderDir) { 'both' } else { 'personal' }

$body = @{ builder_email = $email; toolkit = $toolkit; github_username = $github; platform = 'windows' }
# Parity with session_ping() in session-start.py: while the NSLS usage
# collector's evidence file is fresh, the collector already reports this
# machine's daily session, so the tracker skips ONLY that credit. PR credit,
# stage advancement and announcements still run off this ping.
$evidenceHelper = Join-Path $PSScriptRoot 'collector_evidence.ps1'
if (Test-Path $evidenceHelper) {
    . $evidenceHelper
    if (Test-CollectorEvidenceFresh) { $body.collector_backed = $true }
}
# Which toolkit version this copy is, so the tracker can show who is behind
# main. Only a value shaped like a version is sent.
try {
    $manifest = Join-Path (Split-Path $PSScriptRoot -Parent) '.claude-plugin\plugin.json'
    $toolkitVersion = [string]((Get-Content $manifest -Raw -Encoding UTF8 | ConvertFrom-Json).version)
    if ($toolkitVersion -match '^\d{1,5}\.\d{1,5}\.\d{1,5}$') { $body.toolkit_version = $toolkitVersion }
} catch { }
$payload = $body | ConvertTo-Json -Compress

# Retry marker (parity with session-start.py A-4). When a ping can't be
# delivered, stash the payload here and replay it at the next session start so a
# queued credit/announcement isn't lost. Deleted once delivery succeeds.
$MarkerFile = if (Test-Path $builderDir) {
    Join-Path $builderDir '.last-ping-failed'
} else {
    Join-Path (Join-Path $env:USERPROFILE '.claude') '.last-ping-failed'
}

# Pending-announcements hand-off. This script runs DETACHED (launched by
# session-start.ps1), so its stdout can never reach session context the way
# session-start.py's synchronous print does on Mac. Instead we dismiss each
# announcement here and stash its text in this file; the next (synchronous,
# fast) session-start.ps1 reads it, surfaces it, and deletes it. Net effect:
# announcements show one session late on Windows instead of never.
$AnnounceFile = if (Test-Path $builderDir) {
    Join-Path $builderDir '.pending-announcements'
} else {
    Join-Path (Join-Path $env:USERPROFILE '.claude') '.pending-announcements'
}

# Replay a previously failed ping BEFORE the live one. Delete the marker on
# success; keep it if delivery still fails. We don't process the replayed
# response - the live ping right after fetches fresh announcements.
if (Test-Path $MarkerFile) {
    try {
        $saved = Get-Content $MarkerFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($saved.payload_json) {
            Invoke-RestMethod -Uri "$ProxyUrl/session-ping" -Method Post -Body $saved.payload_json -ContentType 'application/json' -TimeoutSec 30 -ErrorAction Stop | Out-Null
        }
        Remove-Item $MarkerFile -Force -ErrorAction SilentlyContinue
    } catch { }  # still unreachable - keep the marker for the next attempt
}

try {
    $resp = Invoke-RestMethod -Uri "$ProxyUrl/session-ping" -Method Post -Body $payload -ContentType 'application/json' -TimeoutSec 30 -ErrorAction Stop
} catch {
    # Delivery failed - stash for replay next session. Best-effort, never throw.
    try {
        $dir = Split-Path $MarkerFile -Parent
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $marker = @{ payload_json = $payload; attempted_at = (Get-Date).ToUniversalTime().ToString('o') } | ConvertTo-Json -Compress
        # BOM-less write: PS 5.1 `Set-Content -Encoding utf8` emits a BOM, which
        # breaks the ConvertFrom-Json replay path (and any other JSON consumer).
        [System.IO.File]::WriteAllText($MarkerFile, $marker, (New-Object System.Text.UTF8Encoding $false))
    } catch { }
    exit 0
}

# Delivered - clear any stale failure marker from a prior session.
Remove-Item $MarkerFile -Force -ErrorAction SilentlyContinue

$out = @()
$prCount = @($resp.new_pr_credits | Where-Object { $_ }).Count
foreach ($pr in @($resp.new_pr_credits)) { if ($pr) { $out += "Your PR #$($pr.pr) to $($pr.repo) was merged." } }
if ($resp.stage_advanced) { $out += "You advanced to $($resp.stage_advanced.to) on the builder path." }
if ($out.Count -gt 0) {
    Add-Content -Path $LogFile -Value ("[" + (Get-Date).ToString('s') + "] " + ($out -join ' '))
    # Surface these inline too (parity with session-start.py, which prints
    # credits synchronously on Mac). This script runs detached, so the lines
    # ride the same pending-announcements hand-off the announcements use and
    # show at the NEXT session start - previously they went ONLY to the log
    # file, so a Windows builder never saw their merged-PR credit at all.
    foreach ($line in $out) { Add-Content -Path $AnnounceFile -Value $line -Encoding UTF8 }
    if ($prCount -gt 0) {
        Add-Content -Path $AnnounceFile -Value 'Shipped something new with that PR (a skill, tool, or service others will use - not just a fix)? Run /register-automation so it counts on the org tracker.' -Encoding UTF8
    }
}

# Announcements - parity with session-start.py. Dismiss each so the server
# stops sending it, and append its text to the pending file for the next
# session-start.ps1 to surface. Append (not overwrite) so a queued
# announcement from an earlier ping isn't lost before it's shown; dismissal
# guarantees the same announcement is never fetched - hence never appended -
# twice, so appending can't duplicate.
foreach ($ann in @($resp.announcements)) {
    if (-not $ann) { continue }
    Add-Content -Path $AnnounceFile -Value "$($ann.title): $($ann.body)" -Encoding UTF8
    if ($ann.id) {
        try {
            $dismiss = @{ announcement_id = $ann.id; builder_email = $email } | ConvertTo-Json -Compress
            Invoke-RestMethod -Uri "$ProxyUrl/dismiss-announcement" -Method Post -Body $dismiss -ContentType 'application/json' -TimeoutSec 5 -ErrorAction Stop | Out-Null
        } catch { }  # best-effort; text is already stashed, it'll surface regardless
    }
}
exit 0
