# windows-ci: ps51 | Once the plugin is active the hook stops writing org pointers but still notices the builder's own skills
# Once the toolkit is an active plugin, the Windows hook stops writing org
# pointer files (the plugin delivers those skills) but still notices a
# builder's own skill that sits where a pointer would. The functions are lifted
# out of session-start.ps1 by AST, so this never tests a copy. Run under
# Windows PowerShell 5.1, the version the hook runs under.
$ErrorActionPreference = 'Continue'
$hooks = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $hooks 'session-start.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw "session-start.ps1 does not parse: $($errors[0].Message)" }
$want = @('Parse-Frontmatter', 'Test-OwnPointer', 'Test-OrgPluginActive', 'Sync-Pointers')
$fns = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $want -contains $n.Name }, $true)
if ($fns.Count -ne $want.Count) { throw "expected $($want.Count) functions, found $($fns.Count)" }
foreach ($f in $fns) { Invoke-Expression $f.Extent.Text }

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}

$root = Join-Path ([IO.Path]::GetTempPath()) ('ptrs-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
try {
    $ClaudeDir = Join-Path $root '.claude'
    $SkillsDir = Join-Path $ClaudeDir 'skills'
    $tk = Join-Path $ClaudeDir 'local-plugins\nsls-builder-toolkit'
    foreach ($n in 'slack', 'gws') {
        New-Item -ItemType Directory -Path (Join-Path $tk "skills\$n") -Force | Out-Null
        Set-Content -Path (Join-Path $tk "skills\$n\SKILL.md") -Value "---`nname: $n`ndescription: the $n skill`n---`nbody`n" -Encoding ASCII
    }
    # The builder's own gws.
    New-Item -ItemType Directory -Path (Join-Path $SkillsDir 'gws') -Force | Out-Null
    Set-Content -Path (Join-Path $SkillsDir 'gws\SKILL.md') -Value "---`nname: gws`ndescription: mine`n---`nmy own`n" -Encoding ASCII
    New-Item -ItemType Directory -Path (Join-Path $ClaudeDir 'plugins') -Force | Out-Null
    $PointerPrecedence = @('nsls-personal-toolkit', 'nsls-builder-toolkit')

    function Run([bool]$Active) {
        $script:PointerWritten = @{}; $script:OwnSkills = @{}
        $n = Sync-Pointers -PluginDir $tk -NoteOnly:$Active
        return @{ n = $n; own = @($script:OwnSkills.Keys) }
    }

    Check 'no plugin installed: not active' (-not (Test-OrgPluginActive))
    $r = Run (Test-OrgPluginActive)
    Check 'without the plugin, the slack pointer is written' (Test-Path (Join-Path $SkillsDir 'slack\SKILL.md'))
    Check "and the builder's gws is noted, not touched" (($r.own -contains 'gws') -and ((Get-Content (Join-Path $SkillsDir 'gws\SKILL.md') -Raw) -match 'my own'))

    Remove-Item -Recurse -Force (Join-Path $SkillsDir 'slack')
    Set-Content -Path (Join-Path $ClaudeDir 'plugins\installed_plugins.json') -Value '{"plugins": {"nsls-builder-toolkit@nsls-toolkit": [{"installPath": "x"}]}}' -Encoding ASCII
    Check 'plugin installed: active' (Test-OrgPluginActive)
    $r = Run (Test-OrgPluginActive)
    Check 'with the plugin active, no pointer is written' ((-not (Test-Path (Join-Path $SkillsDir 'slack'))) -and $r.n -eq 0) "n=$($r.n)"
    Check "but the builder's gws is still noted for the notice" ($r.own -contains 'gws')

    Set-Content -Path (Join-Path $ClaudeDir 'settings.json') -Value '{"enabledPlugins": {"nsls-builder-toolkit@nsls-toolkit": false}}' -Encoding ASCII
    Check 'plugin switched off by the builder: not active, so pointers come back' (-not (Test-OrgPluginActive))
} finally {
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
}
if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) failed"; exit 1 }
Write-Host 'all checks passed'
