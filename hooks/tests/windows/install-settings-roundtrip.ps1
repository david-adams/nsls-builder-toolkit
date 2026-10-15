# windows-ci: ps51 | install.ps1 gives settings.json back as it found it (accents, a builder's own hooks, a copy, no torn write)
# install.ps1 rewrites settings.json; this proves, under Windows PowerShell 5.1,
# that it gives back what it found:
#   1. non-ASCII text survives a re-run (5.1 reads a BOM-less file as ANSI
#      unless told, which garbled it for good on every install);
#   2. a builder's own hook in the same matcher group as ours is kept (the old
#      filter dropped the whole group);
#   3. a copy of the file as it was is left beside it, and no temp file is.
# The functions are lifted out of install.ps1, so this tests the real code.
# Run by .github/workflows/windows-hooks.yml. ASCII only, PS 5.1 compatible.
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}

$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repo 'install.ps1'), [ref]$null, [ref]$null)
foreach ($name in 'Write-TextNoBom', 'Read-SettingsJson', 'Without-Matching') {
    $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
    if (-not $fn) { Write-Host "FAIL $name is not in install.ps1"; exit 1 }
    . ([scriptblock]::Create($fn.Extent.Text))
}
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false

$scratch = Join-Path ([System.IO.Path]::GetTempPath()) ('nsls-st-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $scratch -Force | Out-Null
$Settings = Join-Path $scratch 'settings.json'
$jose = 'Jos' + [char]0x00E9
$original = '{"permissions":{"additionalDirectories":["C:\\Users\\' + $jose + '\\work"]},' +
    '"hooks":{"SessionStart":[' +
    '{"matcher":"startup","hooks":[{"type":"command","command":"powershell -File C:\\x\\session-start.ps1"},' +
    '{"type":"command","command":"my-own-hook.cmd"}]},' +
    '{"matcher":"resume","hooks":[{"type":"command","command":"powershell -File C:\\x\\session-start.ps1"}]}]}}'
[System.IO.File]::WriteAllText($Settings, $original, $Utf8NoBom)
# Lock it down: no inherited permissions, the current user only.
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$acl = Get-Acl -LiteralPath $Settings
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) { $null = $acl.RemoveAccessRule($rule) }
$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($me, 'FullControl', 'Allow')))
Set-Acl -LiteralPath $Settings -AclObject $acl

$cfg = Read-SettingsJson $Settings
$ss = @(Without-Matching $cfg.hooks.SessionStart 'session-start.ps1')
$cfg.hooks | Add-Member -NotePropertyName SessionStart -NotePropertyValue $ss -Force
Write-TextNoBom $Settings ($cfg | ConvertTo-Json -Depth 12)

$after = [System.IO.File]::ReadAllText($Settings, (New-Object System.Text.UTF8Encoding $false))
$back = $after | ConvertFrom-Json
Check 'an accented folder name survives the rewrite' ($back.permissions.additionalDirectories[0] -eq "C:\Users\$jose\work") $back.permissions.additionalDirectories[0]
$cmds = @($back.hooks.SessionStart | ForEach-Object { @($_.hooks) } | ForEach-Object { $_.command })
Check "the builder's own hook in our matcher group is kept" ($cmds -contains 'my-own-hook.cmd') ($cmds -join ' | ')
Check 'our entries are gone' (-not ($cmds -like '*session-start.ps1*')) ($cmds -join ' | ')
Check 'a group left empty is dropped' (@($back.hooks.SessionStart).Count -eq 1) "$(@($back.hooks.SessionStart).Count) groups"
$bak = "$Settings.pre-nsls-install"
Check 'a copy of the file as it was is kept beside it' ((Test-Path $bak) -and ([System.IO.File]::ReadAllText($bak) -eq $original))
Check 'no temp file is left behind' (@(Get-ChildItem -LiteralPath $scratch -Filter '*.nsls-tmp').Count -eq 0)
function Locked([string]$P) { $a = Get-Acl -LiteralPath $P; return ($a.AreAccessRulesProtected -and @($a.Access).Count -eq 1 -and "$(@($a.Access)[0].IdentityReference)" -eq $me) }
Check 'the rewritten file keeps its locked-down permissions' (Locked $Settings) (Get-Acl -LiteralPath $Settings).Sddl
Check 'and so does the copy' (Locked $bak) (Get-Acl -LiteralPath $bak).Sddl
Check 'the file has no BOM' ([System.IO.File]::ReadAllBytes($Settings)[0] -eq [byte][char]'{')

$missing = Join-Path $scratch 'absent.json'
Check 'a missing settings file reads as empty, with no copy made' ((@((Read-SettingsJson $missing).PSObject.Properties).Count -eq 0) -and -not (Test-Path "$missing.pre-nsls-install"))

if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) failed"; exit 1 }
Write-Host 'all checks passed'
