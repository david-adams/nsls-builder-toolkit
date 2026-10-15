#!/usr/bin/env python3
"""install.ps1 gives settings.json back as it found it.

Plain stdlib: `python3 hooks/tests/test_install_ps1_settings.py`. No PowerShell
here: hooks/tests/windows/install-settings-roundtrip.ps1 runs the real
functions under Windows PowerShell 5.1 in CI. This pins the wiring.

Fable's review (2026-10-04): 5.1 read the BOM-less file as ANSI and wrote any
non-ASCII back garbled, with no copy and an in-place write; and the hook filter
dropped a whole matcher group, a builder's own hook included, if one of our
entries was in it.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ps = (REPO / "install.ps1").read_text(encoding="utf-8")
failures = []


def check(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


check("settings.json is read as UTF-8", "Get-Content -LiteralPath $Path -Raw -Encoding UTF8" in ps)
check("through one function, used for the hooks step", "$cfg = Read-SettingsJson $Settings" in ps)
check("no read of settings.json is left without an encoding",
      "Get-Content $Settings -Raw | ConvertFrom-Json" not in ps)
check("a copy is kept before the rewrite", '"$Path.pre-nsls-install"' in ps)
check("writes go to a temp file and are swapped into place, keeping the file's permissions",
      "[System.IO.File]::Replace($tmp, $Path, [NullString]::Value)" in ps
      and "Move-Item -Force -LiteralPath $tmp -Destination $Path" in ps)
check("each write uses its own temp name and cleans it up",
      "$tmp = \"$Path.$([guid]::NewGuid().ToString('N')).nsls-tmp\"" in ps and "Remove-Item -Force -LiteralPath $tmp" in ps)
fn_w = ps[ps.index("function Write-TextNoBom"):]
fn_w = fn_w[:fn_w.index("\n}\n")]
check("a failed swap that took the original away puts the new copy in place, never deletes it",
      fn_w.index("if (-not (Test-Path -LiteralPath $Path))") < fn_w.index("Remove-Item -Force -LiteralPath $tmp"))
check("the copy gets the original's permissions", "Set-Acl -LiteralPath $bak -AclObject (Get-Acl -LiteralPath $Path)" in ps)
fn = ps[ps.index("function Without-Matching"):]
fn = fn[:fn.index("\n}\n")]
check("the hook filter works entry by entry", "$group.hooks = $rest" in fn and "-join" not in fn)
import run_windows_ci  # noqa: E402  (beside this file; reads the `# windows-ci:` markers)
check("Windows CI runs the real functions under PS 5.1", "ps51" in run_windows_ci.lanes("install-settings-roundtrip.ps1"))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
