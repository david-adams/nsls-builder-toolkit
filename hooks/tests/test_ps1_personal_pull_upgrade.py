#!/usr/bin/env python3
"""The Windows hook upgrades the personal toolkit's old pull entry, and only that.

Plain stdlib. There is no PowerShell here; hooks/tests/windows/personal-pull-upgrade.ps1
runs the real thing under Windows PowerShell 5.1 in CI. This pins where it runs,
which file and path it is pointed at, and that the command it writes is the one
the personal toolkit's install.ps1 writes (thensls/nsls-personal-toolkit#86).
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PS1 = (ROOT / "hooks" / "session-start.ps1").read_text(encoding="utf-8")
failures = []

# What the personal install.ps1 puts ahead of its pull, and the unset form it
# also upgrades, in full.
GUARD = ("Remove-Item Env:GIT_ALTERNATE_OBJECT_DIRECTORIES,Env:GIT_CONFIG,Env:GIT_CONFIG_PARAMETERS,"
         "Env:GIT_CONFIG_COUNT,Env:GIT_OBJECT_DIRECTORY,Env:GIT_DIR,Env:GIT_WORK_TREE,Env:GIT_IMPLICIT_WORK_TREE,"
         "Env:GIT_GRAFT_FILE,Env:GIT_INDEX_FILE,Env:GIT_NO_REPLACE_OBJECTS,Env:GIT_REPLACE_REF_BASE,Env:GIT_PREFIX,"
         "Env:GIT_INTERNAL_SUPER_PREFIX,Env:GIT_SHALLOW_FILE,Env:GIT_COMMON_DIR -ErrorAction Ignore; ")
UNSET = ("unset GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_CONFIG GIT_CONFIG_PARAMETERS GIT_CONFIG_COUNT "
         "GIT_OBJECT_DIRECTORY GIT_DIR GIT_WORK_TREE GIT_IMPLICIT_WORK_TREE GIT_GRAFT_FILE GIT_INDEX_FILE "
         "GIT_NO_REPLACE_OBJECTS GIT_REPLACE_REF_BASE GIT_PREFIX GIT_INTERNAL_SUPER_PREFIX GIT_SHALLOW_FILE GIT_COMMON_DIR; ")


def check(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


names = re.findall(r"'(GIT_[A-Z_]+)'", PS1[PS1.index("function Get-GitRepoEnv {"):PS1.index("function Clear-GitRepoEnv {")])
check("the guard built from Get-GitRepoEnv is the one install.ps1 writes",
      "Remove-Item " + ",".join("Env:" + n for n in names) + " -ErrorAction Ignore; " == GUARD)
check("so is the unset form it upgrades", "unset " + " ".join(names) + "; " == UNSET)
check("the hook clears that same list for its own git calls", "foreach ($v in (Get-GitRepoEnv))" in PS1)

fn = PS1.index("function Update-PersonalPullHook {")
call = PS1.index("\nUpdate-PersonalPullHook -Settings")
body = PS1[fn:call]
check("defined once, called once",
      PS1.count("function Update-PersonalPullHook") == 1 and PS1.count("\nUpdate-PersonalPullHook ") == 1)
check("it runs after the re-run, so only the newest copy of the hook writes",
      PS1.index("if ($ownBefore -and -not $isRerun)") < fn < call < PS1.index("# --- 1b. personal-toolkit forks"))
line = PS1[call:PS1.index("\n", call + 1)]
check("it edits the settings file Claude Code reads", "-Settings (Join-Path $ClaudeDir 'settings.json')" in line)
check("and looks for the path install.ps1 builds, from $HOME",
      "-PluginDir (Join-Path $HOME '.claude\\local-plugins\\nsls-personal-toolkit')" in line)
check("the old command is built exactly as install.ps1 builds it",
      "$legacy = 'git -C \"' + $PluginDir + '\" pull --ff-only --quiet'" in body)
check("and the new one from the same pieces",
      "$guard  = 'Remove-Item ' + (($vars | ForEach-Object { 'Env:' + $_ }) -join ',') + ' -ErrorAction Ignore; '" in body
      and "$unset  = 'unset ' + ($vars -join ' ') + '; '" in body)
check("matching is exact and case-sensitive",
      "if ($val -ceq $legacy)" in body and "$val -ceq ($unset + $legacy)" in body)
check('it adds "shell": "powershell"', "'\"shell\"' + $sep + '\"powershell\"'" in body)
check("a hook with a shell of its own is never a match", "$_.Name -ieq 'shell'" in body)
check("it writes only after the re-parse proof", body.index("ConvertTo-Json -Depth 100") < body.index("[System.IO.File]::Open($tmp"))
hold = body.index("$hold = [System.IO.File]::Open($Settings")
check("a file changed since it was read wins, checked through the handle that holds it",
      hold < body.index("$hold.CopyTo($ms)") < body.index("[System.IO.File]::Replace($tmp, $Settings") < body.index("$hold.Dispose()"))
check("that handle lets others read and the swap delete, but nobody write",
      "([System.IO.FileShare]::Read -bor [System.IO.FileShare]::Delete)" in body[hold:hold + 300]
      and "FileShare]::Write" not in body and "FileShare]::ReadWrite" not in body)
check("a BOM is kept", "if ($bom) { $fs.Write($data, 0, 3) }" in body)
check("a linked settings.json is left alone", "[System.IO.FileAttributes]::ReparsePoint" in body)
check("nothing reaches stdout, the model's context", not re.search(r"Write-(Output|Host)|\becho\b", body, re.I))
check("session-start.ps1 is ASCII-only", all(ord(c) < 128 for c in PS1))
import run_windows_ci  # noqa: E402  (beside this file; reads the `# windows-ci:` markers)
check("CI runs the real thing under Windows PowerShell 5.1", "ps51" in run_windows_ci.lanes("personal-pull-upgrade.ps1"))

if failures:
    print(f"FAILED: {len(failures)}"); sys.exit(1)
print("all checks passed")
