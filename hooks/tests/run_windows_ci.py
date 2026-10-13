#!/usr/bin/env python3
"""Run every test marked for the Windows CI, as one step of windows-hooks.yml.

A test opts in with one comment line near its top:

    # windows-ci: python | What the test proves          (a Python test)
    # windows-ci: ps51 ps7 | What the script proves      (a PowerShell script, once per shell)

Each test used to be its own step in windows-hooks.yml, so every PR that added a
test appended a step to the end of the same file, and two open PRs conflicted as
soon as one merged (a merge train dropped two PRs that way on 2026-10-13). A test
now marks itself, and adding one never touches the workflow.

PowerShell scripts run the way a `shell: powershell` or `shell: pwsh` step runs
them: $ErrorActionPreference = 'stop', the script, then exit with $LASTEXITCODE.
Every test runs even after one fails; the step fails if any did.

    python hooks/tests/run_windows_ci.py                 # run everything marked
    python hooks/tests/run_windows_ci.py --list          # show what would run
    python hooks/tests/run_windows_ci.py --lane python   # one lane only
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIRS = ("hooks/tests", "hooks/tests/windows", "test")
MARK = re.compile(r"^#\s*windows-ci:\s*(?P<lanes>[a-z0-9 ]+?)\s*\|\s*(?P<title>\S.*?)\s*$")
# Anything that looks like a marker but isn't one fails the step, so a typo'd
# marker can't leave a test out of CI unnoticed.
NEAR_MARK = re.compile(r"^#\s*windows[\s_-]*ci\b", re.IGNORECASE)
SHELLS = {"ps51": ("powershell", "Windows PowerShell 5.1"), "ps7": ("pwsh", "PowerShell 7")}
LANES = ("python",) + tuple(SHELLS)
HEAD_LINES = 40


def scan(root=ROOT):
    """([(path, lanes, title)] for every test that opts in, [(path, line)] for every
    marker that doesn't parse), in a stable order.

    The marker must start its line: the indented examples above don't count, and
    this runner never runs itself."""
    found, malformed = [], []
    for d in DIRS:
        folder = root / d
        if not folder.is_dir():
            continue
        for f in sorted(folder.iterdir()):
            if not f.is_file() or f.suffix not in (".py", ".ps1") or f.resolve() == Path(__file__).resolve():
                continue
            with open(f, encoding="utf-8-sig", errors="replace") as fh:
                for _, line in zip(range(HEAD_LINES), fh):
                    line = line.rstrip()
                    m = MARK.match(line)
                    if m:
                        found.append((f, m.group("lanes").split(), m.group("title")))
                        break
                    if NEAR_MARK.match(line):
                        malformed.append((f, line))
                        break
    return found, malformed


def marked(root=ROOT):
    """(path, lanes, title) for every test that opts in, in a stable order."""
    return scan(root)[0]


def lanes(name, root=ROOT):
    """The lanes a test file (by file name) is marked for; empty if it isn't marked."""
    return next((ls for f, ls, _ in marked(root) if f.name == name), [])


def run_one(path, lane, root=ROOT):
    """The test's exit code, or None when this machine has no such shell."""
    rel = path.relative_to(root).as_posix()
    if lane == "python":
        return subprocess.run([sys.executable, rel], cwd=root).returncode
    exe = shutil.which(SHELLS[lane][0])
    if exe is None:
        return None
    # The same wrapper GitHub Actions puts around a `shell: powershell` / `pwsh` step.
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as tmp:
        tmp.write(f"$ErrorActionPreference = 'stop'\n./{rel}\n"
                  "if ((Test-Path -LiteralPath variable:\\LASTEXITCODE)) { exit $LASTEXITCODE }\n")
    try:
        return subprocess.run([exe, "-command", f". '{tmp.name}'"], cwd=root).returncode
    finally:
        os.unlink(tmp.name)


def main(argv=None, root=ROOT):
    ap = argparse.ArgumentParser(description="Run every test marked for the Windows CI.")
    ap.add_argument("--list", action="store_true", help="show what would run, and run nothing")
    ap.add_argument("--lane", choices=LANES, help="run one lane only")
    a = ap.parse_args(argv)
    tests, malformed = scan(root)
    unknown = [(f, lane) for f, ls, _ in tests for lane in ls if lane not in LANES]
    unknown += [(f, line) for f, line in malformed]
    for f, lane in unknown:
        what = (f"has a marker that doesn't parse: {lane!r}. The form is "
                "`# windows-ci: <lanes> | <what it proves>`" if lane.startswith("#")
                else f"names an unknown lane {lane!r}")
        print(f"windows-ci: {f.relative_to(root).as_posix()} {what} (lanes: {', '.join(LANES)})")
    jobs = [(f, lane, title) for f, ls, title in tests for lane in ls
            if lane in LANES and (not a.lane or lane == a.lane)]
    if not jobs:
        print("windows-ci: no marked tests found. Is the `# windows-ci:` marker intact?")
        return 1
    if a.list:
        for f, lane, title in jobs:
            print(f"{lane:7} {f.relative_to(root).as_posix()}  {title}")
        return 1 if unknown else 0
    actions = bool(os.environ.get("GITHUB_ACTIONS"))
    failed, skipped, passed = [], [], 0
    for f, lane, title in jobs:
        label = f"{title} ({SHELLS[lane][1]})" if lane in SHELLS else title
        print(f"::group::{label}" if actions else f"\n=== {label}", flush=True)
        code = run_one(f, lane, root)
        if actions:
            print("::endgroup::", flush=True)
        if code is None and os.name != "nt":
            skipped.append(label)  # no Windows PowerShell on a Mac; CI runs it
        elif code != 0:
            failed.append(f"{label} ({'no ' + SHELLS[lane][0] + ' found' if code is None else 'exit ' + str(code)})")
            if actions:
                print(f"::error title=Windows CI test failed::{failed[-1]}", flush=True)
        else:
            passed += 1
    print(f"\nwindows-ci: {passed} passed, {len(failed)} failed, {len(skipped)} skipped (no such shell here)")
    for line in failed:
        print(f"  FAILED: {line}")
    return 1 if failed or unknown else 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
