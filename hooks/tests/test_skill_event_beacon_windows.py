#!/usr/bin/env python3
# windows-ci: python | The skill-event hook proves itself from a Windows path, so stage B can finish on a PC
"""skill-event.sh earns its beacon on a PC, and fired() can read it.

Plain stdlib: `python3 hooks/tests/test_skill_event_beacon_windows.py`.
Runs on macOS/Linux and on windows-latest; the Windows-shaped invocations only
run where Git Bash is the shell, because that is the only place they exist.

Claude Code hands bash the script as C:\\Users\\...\\3.16.2/hooks/skill-event.sh.
The hook's `*/plugins/cache/*` test needs forward slashes, so on a PC it never
matched and the skill-event beacon was never written. Stage B then never
finished on any PC: the skill-event shim stayed (two POSTs per Skill call) and
the signal CLI check ran every session (Fable review, 2026-10-04). And had it
matched, `pwd` would have recorded /c/Users/..., which Windows Python reads as
C:\\c\\Users\\... .
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"
failures = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {'' if cond else detail}")
    if not cond:
        failures.append(name)


def git_bash():
    """Git for Windows' bash on a PC (never WSL's System32 bash); bash elsewhere."""
    if not WINDOWS:
        return "bash"
    git = shutil.which("git")
    for cand in ([Path(git).resolve().parents[1] / "bin" / "bash.exe"] if git else []) + [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"]:
        if cand.is_file():
            return str(cand)
    return None


def fired(cfg):
    code = "import sys; sys.path.insert(0, sys.argv[1]); import plugin_beacon; print(plugin_beacon.fired('skill-event'))"
    out = subprocess.run([sys.executable, "-c", code, str(HOOKS)], capture_output=True, text=True,
                         env=dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg)), timeout=30)
    return out.stdout.strip() == "True", out.stderr[-200:]


sys.path.insert(0, str(HOOKS))
import plugin_beacon  # noqa: E402

print("fired() reads a Git Bash path the way Windows spells it")
check("/c/Users/x becomes C:/Users/x on Windows",
      plugin_beacon._native("/c/Users/x/.claude", windows=True) == "C:/Users/x/.claude")
check("a bare drive works too", plugin_beacon._native("/d", windows=True) == "D:/")
check("and a POSIX path is left alone off Windows",
      plugin_beacon._native("/c/Users/x", windows=False) == "/c/Users/x")
check("as is a longer first segment", plugin_beacon._native("/cache/x", windows=True) == "/cache/x")

bash = git_bash()
print(f"\nthe hook, run the way Claude Code runs it ({bash})")
if not bash:
    check("Git Bash is present on this PC", False, "(not found)")
else:
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path(tmp) / ".claude"
        root = cfg / "plugins" / "cache" / "nsls-toolkit" / "nsls-builder-toolkit" / "3.16.11"
        (root / "hooks").mkdir(parents=True)
        shutil.copy(HOOKS / "skill-event.sh", root / "hooks" / "skill-event.sh")
        (cfg / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"plugins": {"nsls-builder-toolkit@nsls-toolkit": [{"installPath": str(root)}]}}))
        beacon = cfg / ".nsls-plugin-beacons" / "skill-event.json"

        shapes = [("forward slashes", root.as_posix() + "/hooks/skill-event.sh")]
        if WINDOWS:
            shapes += [("Claude Code's shape, C:\\...\\3.16.11/hooks/skill-event.sh",
                        str(root) + "/hooks/skill-event.sh"),
                       ("all backslashes", str(root / "hooks" / "skill-event.sh"))]
        for label, script in shapes:
            if beacon.exists():
                beacon.unlink()
            env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg), HOME=tmp, NSLS_SKILL_EVENT_DRYRUN="1")
            subprocess.run([bash, script], input="{}", capture_output=True, text=True, env=env, timeout=30)
            check(f"{label}: the beacon is written", beacon.exists())
            if beacon.exists():
                rec = json.loads(beacon.read_text(encoding="utf-8"))
                check(f"{label}: it names the plugin root, not an MSYS path",
                      not rec.get("root", "").startswith("/c/") and rec.get("version") == "3.16.11",
                      f"({rec})")
                ok, err = fired(cfg)
                check(f"{label}: and fired() accepts it", ok, err)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
