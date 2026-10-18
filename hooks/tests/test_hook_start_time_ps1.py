#!/usr/bin/env python3
# windows-ci: python | session-start.ps1 keeps a re-run inside the first run's 90 seconds
"""session-start.ps1's start time, which Python's plugin update budgets against.

Plain stdlib: `python3 hooks/tests/test_hook_start_time_ps1.py`.

The .ps1 re-runs itself after pulling an update to itself, and the re-run
inherits its environment. Stamping a fresh start time there would give the
re-run a second 90 seconds the hook does not have, and the update and heal
could be killed midway (Macroscope, #272). So a start time from the last 90
seconds is kept and anything else is replaced. The block between the
"hook start time" markers runs under every PowerShell found (`powershell` and
`pwsh` on the Windows runner; NSLS_TEST_PWSH or `pwsh` elsewhere).
"""
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PS1 = Path(__file__).resolve().parents[1] / "session-start.ps1"
failures = []


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label} {'' if cond else detail}")
    if not cond:
        failures.append(label)


text = PS1.read_text(encoding="utf-8")
m = re.search(r"# --- hook start time ---\n(.*?)# --- end hook start time ---", text, re.S)
check("the start-time block is marked in session-start.ps1", m is not None)
block = m.group(1) if m else ""
check("it is set before anything else runs", m is not None and text.index(m.group(0)) < text.index("$ClaudeDir"))

shells = [p for p in (shutil.which("powershell"), shutil.which("pwsh")) if p]
if os.environ.get("NSLS_TEST_PWSH") and Path(os.environ["NSLS_TEST_PWSH"]).exists():
    shells.append(os.environ["NSLS_TEST_PWSH"])
if os.name != "nt":
    shells = [s for s in shells if Path(s).name.lower().startswith("pwsh")]
shells = list(dict.fromkeys(shells))
if not shells:
    print("skip: no PowerShell on this machine (set NSLS_TEST_PWSH to run it)")

for shell in shells:
    label = Path(shell).name
    now = int(time.time())
    cases = {
        "unset": (None, "fresh"),
        "inherited from a re-run 30s ago": (str(now - 30), str(now - 30)),
        "stale (10 minutes old)": (str(now - 600), "fresh"),
        "in the future": (str(now + 600), "fresh"),
        "not a number": ("12,5", "fresh"),
    }
    for name, (given, expect) in cases.items():
        env = dict(os.environ)
        env.pop("NSLS_HOOK_STARTED_AT", None)
        if given is not None:
            env["NSLS_HOOK_STARTED_AT"] = given
        script = block + '\nWrite-Output "STARTED=$env:NSLS_HOOK_STARTED_AT"\n'
        r = subprocess.run([shell, "-NoProfile", "-Command", script], capture_output=True, text=True,
                           env=env, timeout=60)
        got = re.search(r"STARTED=(\S*)", r.stdout)
        got = got.group(1) if got else ""
        if expect == "fresh":
            ok = got.isdigit() and abs(int(got) - int(time.time())) <= 5
        else:
            ok = got == expect
        check(f"{label}: {name}", ok, f"got {got!r} {r.stderr[-300:]}")

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
