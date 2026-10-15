# windows-ci: ps51 | The hook makes the personal toolkit's old pull entry safe from a git hook's variables, and touches nothing else
# The Windows hook upgrades the personal toolkit's old pull entry in
# settings.json, and nothing else. Proves, under Windows PowerShell 5.1:
#   1. the exact old command, and its `unset` form, become the command the
#      personal install.ps1 now writes, with "shell": "powershell" right after
#      it, and every other byte stays: a BOM, CRLF, an accented home folder,
#      the layout 5.1's ConvertTo-Json writes;
#   2. anything else is left byte for byte: an edited command, a Git Bash
#      path, another user's path, a hook with a shell of its own, the string
#      under another event too, broken JSON, a linked settings.json, a rerun;
#   3. the file keeps its permissions, no temp file is left, and a file another
#      program has open for writing is left until it lets go;
#   4. the new entry, run by PowerShell with GIT_DIR and GIT_INDEX_FILE aimed at
#      a project (git sets both for its hooks), updates the toolkit and leaves
#      the project alone - and the old entry, the control, does not.
# The functions are lifted out of session-start.ps1 by AST, so this tests the
# real code. Run by .github/workflows/windows-hooks.yml. ASCII only.
$ErrorActionPreference = 'SilentlyContinue'   # the hook's own setting
$hooks = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $hooks 'session-start.ps1'), [ref]$tokens, [ref]$errors)
if ($errors.Count -gt 0) { throw "session-start.ps1 does not parse: $($errors[0].Message)" }
$want = @('Get-GitRepoEnv', 'Update-PersonalPullHook')
$fns = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $want -contains $n.Name }, $true)
if ($fns.Count -ne $want.Count) { throw "expected $($want.Count) functions in session-start.ps1, found $($fns.Count)" }
foreach ($f in $fns) { Invoke-Expression $f.Extent.Text }

$script:failures = 0
function Check([string]$Label, [bool]$Cond, [string]$Detail = '') {
    if ($Cond) { Write-Host "ok   $Label" } else { Write-Host "FAIL $Label"; if ($Detail) { Write-Host "     saw: $Detail" }; $script:failures++ }
}

# What the personal toolkit's install.ps1 writes (thensls/nsls-personal-toolkit#86),
# spelt out in full so a change on either side shows up here.
$Guard = 'Remove-Item Env:GIT_ALTERNATE_OBJECT_DIRECTORIES,Env:GIT_CONFIG,Env:GIT_CONFIG_PARAMETERS,' +
    'Env:GIT_CONFIG_COUNT,Env:GIT_OBJECT_DIRECTORY,Env:GIT_DIR,Env:GIT_WORK_TREE,Env:GIT_IMPLICIT_WORK_TREE,' +
    'Env:GIT_GRAFT_FILE,Env:GIT_INDEX_FILE,Env:GIT_NO_REPLACE_OBJECTS,Env:GIT_REPLACE_REF_BASE,Env:GIT_PREFIX,' +
    'Env:GIT_INTERNAL_SUPER_PREFIX,Env:GIT_SHALLOW_FILE,Env:GIT_COMMON_DIR -ErrorAction Ignore; '
$Unset = 'unset GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CONFIG GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT ' +
    'GIT_OBJECT_DIRECTORY GIT_DIR GIT_WORK_TREE GIT_IMPLICIT_WORK_TREE GIT_GRAFT_FILE GIT_INDEX_FILE ' +
    'GIT_NO_REPLACE_OBJECTS GIT_REPLACE_REF_BASE GIT_PREFIX GIT_INTERNAL_SUPER_PREFIX GIT_SHALLOW_FILE GIT_COMMON_DIR; '

$Utf8 = New-Object System.Text.UTF8Encoding $false
function J([string]$S) { return '"' + (($S -replace '\\', '\\') -replace '"', '\"') + '"' }
function Put([string]$Path, [string]$Text, [bool]$Bom = $false) {
    $b = $Utf8.GetBytes($Text)
    if ($Bom) { $b = [byte[]](@(0xEF, 0xBB, 0xBF) + $b) }
    [System.IO.File]::WriteAllBytes($Path, $b)
}
function Bytes([string]$Path) { return [System.Convert]::ToBase64String([System.IO.File]::ReadAllBytes($Path)) }
function Lf([string]$S) { return ($S -replace "`r`n", "`n") }
function NoTemp([string]$Dir) { return (@(Get-ChildItem -LiteralPath $Dir -Force -Filter '*.nsls-tmp').Count -eq 0) }

# Claude Code's own layout. @CMD@ is the pull entry's command.
$Tpl = Lf @'
{
  "permissions": {
    "allow": [
      "Bash(git status)"
    ]
  },
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup",
        "hooks": [
          {
            "type": "command",
            "command": "powershell -NoProfile -ExecutionPolicy Bypass -File \"C:\\x\\session-start.ps1\"",
            "timeout": 90
          }
        ]
      },
      {
        "matcher": "startup|resume",
        "hooks": [
          {
            "type": "command",
            "command": @CMD@,
            "timeout": 20,
            "statusMessage": "Updating personal toolkit..."
          }
        ]
      }
    ]
  }
}
'@
$TplUpgraded = $Tpl.Replace('"command": @CMD@,', ('"command": @CMD@,' + "`n" + '            "shell": "powershell",'))

$root = Join-Path ([System.IO.Path]::GetTempPath()) ('nsls-ppu-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
try {
    $dir = Join-Path $root '.claude'
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $settings = Join-Path $dir 'settings.json'
    $pd = Join-Path $root 'Users\Jose\.claude\local-plugins\nsls-personal-toolkit'
    $legacy = 'git -C "' + $pd + '" pull --ff-only --quiet'
    $newCmd = $Guard + $legacy

    # --- 1. the old entry becomes the new one; nothing else moves ---
    Put $settings $Tpl.Replace('@CMD@', (J $legacy))
    $me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $acl = Get-Acl -LiteralPath $settings
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { $null = $acl.RemoveAccessRule($rule) }
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($me, 'FullControl', 'Allow')))
    Set-Acl -LiteralPath $settings -AclObject $acl
    function Locked([string]$P) { $a = Get-Acl -LiteralPath $P; return ($a.AreAccessRulesProtected -and @($a.Access).Count -eq 1 -and "$(@($a.Access)[0].IdentityReference)" -eq $me) }
    Check 'setup: the file is locked down to this user' (Locked $settings) (Get-Acl -LiteralPath $settings).Sddl
    Update-PersonalPullHook -Settings $settings -PluginDir $pd
    $got = [System.IO.File]::ReadAllText($settings, $Utf8)
    Check 'the old command becomes the one install.ps1 writes, with shell right after it, and nothing else changes' ($got -ceq $TplUpgraded.Replace('@CMD@', (J $newCmd))) $got
    $h = @(($got | ConvertFrom-Json).hooks.SessionStart)[1].hooks[0]
    Check 'it parses back to that command and shell' (($h.command -ceq $newCmd) -and ($h.shell -ceq 'powershell')) "$($h.command) / $($h.shell)"
    Check 'the file keeps its locked-down permissions' (Locked $settings) (Get-Acl -LiteralPath $settings).Sddl
    Check 'no temp file is left' (NoTemp $dir)
    $once = Bytes $settings
    Update-PersonalPullHook -Settings $settings -PluginDir $pd
    Check 'a second run changes nothing' ((Bytes $settings) -eq $once)

    # Something holding settings.json open for writing (Node opens it sharing
    # everything) makes this session leave it; once that lets go, the next
    # session upgrades it.
    Put $settings $Tpl.Replace('@CMD@', (J $legacy))
    $was = Bytes $settings
    $writer = [System.IO.File]::Open($settings, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write, [System.IO.FileShare]::ReadWrite -bor [System.IO.FileShare]::Delete)
    try { Update-PersonalPullHook -Settings $settings -PluginDir $pd } finally { $writer.Dispose() }
    Check 'left alone while another program has it open for writing' (((Bytes $settings) -eq $was) -and (NoTemp $dir))
    Update-PersonalPullHook -Settings $settings -PluginDir $pd
    Check 'and upgraded once it lets go' ((Bytes $settings) -eq [System.Convert]::ToBase64String($Utf8.GetBytes($TplUpgraded.Replace('@CMD@', (J $newCmd)))))

    # BOM, CRLF, an accented home, and the unset form a Git Bash install leaves.
    $pdA = Join-Path $root ('Users\Jos' + [char]0x00E9 + '\.claude\local-plugins\nsls-personal-toolkit')
    $legacyA = 'git -C "' + $pdA + '" pull --ff-only --quiet'
    Put $settings ($Tpl.Replace('@CMD@', (J ($Unset + $legacyA))) -replace "`n", "`r`n") $true
    Update-PersonalPullHook -Settings $settings -PluginDir $pdA
    $want = [System.Convert]::ToBase64String([byte[]](@(0xEF, 0xBB, 0xBF) + $Utf8.GetBytes(($TplUpgraded.Replace('@CMD@', (J ($Guard + $legacyA))) -replace "`n", "`r`n"))))
    Check 'the unset form is upgraded too, keeping a BOM, CRLF and an accented path' ((Bytes $settings) -eq $want) ([System.IO.File]::ReadAllText($settings))

    # The layout the old personal install.ps1 itself wrote: 5.1's ConvertTo-Json.
    $cfg = [pscustomobject]@{
        enabledPlugins = [pscustomobject]@{ 'nsls-personal-toolkit@local' = $true }
        hooks = [pscustomobject]@{ SessionStart = @(@{ matcher = 'startup|resume'; hooks = @(@{ type = 'command'; command = $legacy;
                    timeout = 20; statusMessage = 'Updating personal toolkit...' }) }) }
    }
    $before = $cfg | ConvertTo-Json -Depth 12
    Put $settings $before
    Update-PersonalPullHook -Settings $settings -PluginDir $pd
    $got = [System.IO.File]::ReadAllText($settings, $Utf8)
    $h = @(($got | ConvertFrom-Json).hooks.SessionStart)[0].hooks[0]
    Check "5.1's own layout is upgraded" (($h.command -ceq $newCmd) -and ($h.shell -ceq 'powershell')) $got
    $b = @((Lf $before) -split "`n")
    $a = @((Lf $got) -split "`n")
    $k = -1
    for ($i = 0; $i -lt $b.Count; $i++) { if ($b[$i].Contains('pull --ff-only --quiet')) { $k = $i } }
    $same = ($k -ge 0 -and $a.Count -eq $b.Count + 1 -and $a[$k + 1] -match '^\s*"shell"\s*:\s*"powershell",?$')
    if ($same) {
        for ($i = 0; $i -lt $b.Count; $i++) {
            if ($i -ne $k -and $b[$i] -cne $a[$(if ($i -lt $k) { $i } else { $i + 1 })]) { $same = $false }
        }
    }
    Check 'and only the command line changes, with "shell" on a line of its own after it' $same $got

    # --- 2. anything else is left exactly as it was ---
    $untouched = [ordered]@{
        'an edited command (two spaces)'      = $Tpl.Replace('@CMD@', (J ('git -C "' + $pd + '"  pull --ff-only --quiet')))
        'a different spelling (-c, not -C)'   = $Tpl.Replace('@CMD@', (J ('git -c "' + $pd + '" pull --ff-only --quiet')))
        'a Git Bash path'                     = $Tpl.Replace('@CMD@', (J 'git -C "/c/Users/Jose/.claude/local-plugins/nsls-personal-toolkit" pull --ff-only --quiet'))
        "another user's path"                 = $Tpl.Replace('@CMD@', (J ('git -C "' + (Join-Path $root 'Users\Other\.claude\local-plugins\nsls-personal-toolkit') + '" pull --ff-only --quiet')))
        'a hook that has a shell already'     = $Tpl.Replace('"command": @CMD@,', ('"shell": "bash",' + "`n" + '            "command": ' + (J $legacy) + ','))
        'the string under another event too'  = $Tpl.Replace('@CMD@', (J $legacy)).Replace('"hooks": {', ('"hooks": {' + "`n" + '    "Stop": [{"hooks": [{"type": "command", "command": ' + (J $legacy) + '}]}],'))
        'broken JSON (cut short)'             = $Tpl.Replace('@CMD@', (J $legacy)).TrimEnd().TrimEnd('}')
        'the new command, already there'      = $TplUpgraded.Replace('@CMD@', (J $newCmd))
    }
    foreach ($label in $untouched.Keys) {
        Put $settings $untouched[$label]
        $was = Bytes $settings
        Update-PersonalPullHook -Settings $settings -PluginDir $pd
        Check "left alone: $label" (((Bytes $settings) -eq $was) -and (NoTemp $dir)) ([System.IO.File]::ReadAllText($settings))
    }

    # A linked settings.json (dotfiles): the link and its target both stay as
    # they are. A runner that cannot make links skips this one, saying so.
    $real = Join-Path $root 'dotfiles-settings.json'
    Put $real $Tpl.Replace('@CMD@', (J $legacy))
    $realWas = Bytes $real
    Remove-Item -LiteralPath $settings -Force
    $link = New-Item -ItemType SymbolicLink -Path $settings -Target $real -ErrorAction SilentlyContinue
    if ($link) {
        Update-PersonalPullHook -Settings $settings -PluginDir $pd
        $still = ((Get-Item -LiteralPath $settings -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint)
        Check 'left alone: a linked settings.json' ($still -and ((Bytes $real) -eq $realWas))
        Remove-Item -LiteralPath $settings -Force
    } else {
        Write-Host 'skip a linked settings.json (this runner cannot make a symbolic link)'
    }

    # --- 4. what the new entry does when it runs ---
    # git exports GIT_DIR and GIT_INDEX_FILE to its hooks, so a Claude session
    # started from one inherits them. -EncodedCommand: 5.1 strips the quotes
    # around the path from a plain -Command argument.
    foreach ($v in @(Get-GitRepoEnv)) { Remove-Item -Path "Env:$v" -ErrorAction SilentlyContinue }
    $e2e = Join-Path $root 'e2e'
    $up = Join-Path $e2e 'up.git'
    $w = Join-Path $e2e 'w'
    $tk = Join-Path $e2e 'toolkit'
    $proj = Join-Path $e2e 'project'
    function TGit {
        # Never name this Git: PowerShell would resolve `& git` inside it to itself.
        & git -c user.name=t -c user.email=t@example.com -c init.defaultBranch=main @args 2>$null | Out-Null
    }
    TGit init -q --bare $up
    TGit init -q $w
    Set-Content -LiteralPath (Join-Path $w 'a.txt') -Value 'one' -Encoding ASCII
    TGit -C $w add a.txt
    TGit -C $w commit -q -m one
    TGit -C $w push -q $up HEAD:refs/heads/main
    TGit -C $up symbolic-ref HEAD refs/heads/main
    TGit clone -q $up $tk
    Set-Content -LiteralPath (Join-Path $w 'a.txt') -Value 'two' -Encoding ASCII
    TGit -C $w commit -q -am two
    TGit -C $w push -q $up HEAD:refs/heads/main
    TGit init -q $proj
    Set-Content -LiteralPath (Join-Path $proj 'p.txt') -Value 'mine' -Encoding ASCII
    TGit -C $proj add p.txt
    TGit -C $proj commit -q -m mine
    $upHead = (& git -C $up rev-parse HEAD 2>$null | Out-String).Trim()
    $tkHead = (& git -C $tk rev-parse HEAD 2>$null | Out-String).Trim()
    $projHead = (& git -C $proj rev-parse HEAD 2>$null | Out-String).Trim()
    Check 'setup: the toolkit is one commit behind its upstream' ($upHead -and $tkHead -and $upHead -ne $tkHead)

    function RunAsHook([string]$Cmd) {
        $env:GIT_DIR = Join-Path $proj '.git'
        $env:GIT_INDEX_FILE = Join-Path $proj '.git\index'
        $enc = [System.Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($Cmd))
        & powershell.exe -NoProfile -NonInteractive -EncodedCommand $enc 2>$null | Out-Null
        Remove-Item Env:GIT_DIR, Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
    }
    $tkEntry = 'git -C "' + $tk + '" pull --ff-only --quiet'
    RunAsHook $tkEntry
    Check 'control: the old entry, run from inside a git hook, does not update the toolkit' (((& git -C $tk rev-parse HEAD 2>$null | Out-String).Trim()) -eq $tkHead)
    RunAsHook ($Guard + $tkEntry)
    Check 'the new entry, run the same way, updates the toolkit' (((& git -C $tk rev-parse HEAD 2>$null | Out-String).Trim()) -eq $upHead)
    $projStatus = (& git -C $proj status --porcelain 2>$null | Out-String).Trim()
    Check 'and leaves the project as it was' ((((& git -C $proj rev-parse HEAD 2>$null | Out-String).Trim()) -eq $projHead) -and -not $projStatus) $projStatus
} finally {
    Remove-Item Env:GIT_DIR, Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force $root -ErrorAction SilentlyContinue
}
if ($script:failures -gt 0) { Write-Host "$($script:failures) check(s) failed"; exit 1 }
Write-Host 'all checks passed'
