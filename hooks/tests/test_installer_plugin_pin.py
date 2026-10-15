#!/usr/bin/env python3
"""A branch install must install that branch's plugin, not main's.

Plain stdlib, no pytest: run with `python3 hooks/tests/test_installer_plugin_pin.py`.

Found 2026-10-10. With NSLS_TOOLKIT_BRANCH set, install.sh cloned the branch
but added the plugin's marketplace with the bare repo URL, and `marketplace add`
takes the repo's default branch. A machine that already had the toolkit was
worse: install_plugin saw the plugin registered and skipped it entirely. Either
way a branch test ran the branch's hooks (the clone) with main's skills (the
plugin), and nothing said so.

This runs the real "Toolkit plugin pin" block from install.sh against a
throwaway config dir and a fake `claude` that records what it was asked to do.
install.ps1 carries the same logic; its checks here are textual, because the
Mac has no PowerShell (windows-hooks CI parses it under 5.1 and 7).
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
failures = []

PROD = "https://github.com/thensls/nsls-builder-toolkit.git"
FORK = "https://github.com/someone/nsls-builder-toolkit.git"


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {'' if cond else detail}")
    if not cond:
        failures.append(name)


lines = (REPO / "install.sh").read_text(encoding="utf-8").splitlines()
start = next(i for i, l in enumerate(lines) if l.startswith("# --- Toolkit plugin pin (begin)"))
end = next(i for i, l in enumerate(lines) if l.startswith("# --- Toolkit plugin pin (end)"))
BLOCK = "\n".join(lines[start:end + 1])


def run(tmp, call, repo_url=PROD, branch="main", env=None, registered=None, installed_sha=None, clone_sha=None):
    """Run one function from the block; return (stdout, claude calls, INSTALL_FAILED)."""
    tmp = Path(tmp)
    cfg = tmp / "cfg"
    (cfg / "plugins").mkdir(parents=True, exist_ok=True)
    reg = cfg / "plugins" / "known_marketplaces.json"
    if registered is not None:
        reg.write_text(json.dumps({"nsls-toolkit": {"source": registered}}), encoding="utf-8")
    elif reg.exists():
        reg.unlink()
    inst = cfg / "plugins" / "installed_plugins.json"
    if installed_sha:
        inst.write_text(json.dumps({"version": 2, "plugins": {
            "nsls-builder-toolkit@nsls-toolkit": [{"scope": "user", "gitCommitSha": installed_sha}]}}),
            encoding="utf-8")
    elif inst.exists():
        inst.unlink()

    log = tmp / "claude.log"
    log.write_text("")
    fake = tmp / "claude"
    fake.write_text(f'#!/bin/bash\necho "$*" >> "{log}"\n')
    fake.chmod(0o755)

    # A real git repo stands in for the clone, so `rev-parse HEAD` is honest.
    plugin_dir = tmp / "clone"
    if not plugin_dir.exists():
        plugin_dir.mkdir()
        g = ["git", "-C", str(plugin_dir)]
        subprocess.run(g + ["init", "-q"], check=True)
        subprocess.run(g + ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                            "--allow-empty", "-m", "x"], check=True)
    head = subprocess.run(["git", "-C", str(plugin_dir), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    if installed_sha == "CLONE":
        inst.write_text(inst.read_text().replace("CLONE", head), encoding="utf-8")

    script = tmp / "run.sh"
    script.write_text(
        "set -euo pipefail\n"
        f'CONFIG_DIR="{cfg}"; PLUGIN_DIR="{plugin_dir}"; CLAUDE_BIN="{fake}"\n'
        f'REPO_URL="{repo_url}"; REPO_BRANCH="{branch}"; INSTALL_FAILED=""\n'
        + BLOCK + "\n" + call + '\necho "INSTALL_FAILED=$INSTALL_FAILED"\n',
        encoding="utf-8")
    e = {k: v for k, v in os.environ.items() if not k.startswith("NSLS_TOOLKIT_")}
    e.update(env or {})
    p = subprocess.run(["bash", str(script)], capture_output=True, text=True, env=e, timeout=60)
    out = p.stdout
    failed = out.rsplit("INSTALL_FAILED=", 1)[-1].strip() if "INSTALL_FAILED=" in out else "?"
    if p.returncode != 0:
        out += "\nSTDERR: " + p.stderr
    return out, log.read_text().splitlines(), failed


print("Which marketplace source the toolkit plugin is added from")
with tempfile.TemporaryDirectory() as t:
    out, _, _ = run(t, "toolkit_market")
    check("production, main: the bare URL (staff installs unchanged)", out.splitlines()[0] == PROD, out)
    out, _, _ = run(t, "toolkit_market", branch="fix/x")
    check("a branch is pinned onto the URL", out.splitlines()[0] == PROD + "#fix/x", out)
    out, _, _ = run(t, "toolkit_market", repo_url=FORK, branch="feat/y")
    check("a fork's branch is pinned too", out.splitlines()[0] == FORK + "#feat/y", out)
    out, _, _ = run(t, "toolkit_market", repo_url="/Users/x/worktree", branch="feat/y")
    check("a local folder is left as it is (it can't carry a pin)",
          out.splitlines()[0] == "/Users/x/worktree", out)

print("When an installed toolkit plugin is started over")
with tempfile.TemporaryDirectory() as t:
    _, calls, _ = run(t, "repoint_toolkit_marketplace", registered={"source": "git", "url": PROD})
    check("production, unpinned, plain run: left alone", calls == [], calls)

    _, calls, _ = run(t, "repoint_toolkit_marketplace",
                      registered={"source": "git", "url": "https://github.com/thensls/nsls-builder-toolkit/"})
    check("same repo written without .git: still left alone", calls == [], calls)

    _, calls, _ = run(t, "repoint_toolkit_marketplace",
                      registered={"source": "github", "repo": "thensls/nsls-builder-toolkit"})
    check("same repo in owner/repo form: still left alone", calls == [], calls)

    _, calls, _ = run(t, "repoint_toolkit_marketplace", branch="fix/x",
                      env={"NSLS_TOOLKIT_BRANCH": "fix/x"},
                      registered={"source": "git", "url": PROD})
    check("a branch test on a machine that has the toolkit: started over",
          calls == ["plugin marketplace remove nsls-toolkit"], calls)

    _, calls, _ = run(t, "repoint_toolkit_marketplace", branch="fix/x",
                      env={"NSLS_TOOLKIT_BRANCH": "fix/x"},
                      registered={"source": "git", "url": PROD, "ref": "fix/x"})
    check("re-testing the same branch: started over (new commits, same version)",
          calls == ["plugin marketplace remove nsls-toolkit"], calls)

    _, calls, _ = run(t, "repoint_toolkit_marketplace",
                      registered={"source": "git", "url": PROD, "ref": "fix/x"})
    check("a plain run after a test: back to main",
          calls == ["plugin marketplace remove nsls-toolkit"], calls)

    _, calls, _ = run(t, "repoint_toolkit_marketplace", registered={"source": "git", "url": FORK})
    check("a plain run after a fork test: back to production",
          calls == ["plugin marketplace remove nsls-toolkit"], calls)

    _, calls, _ = run(t, "repoint_toolkit_marketplace", registered=None)
    check("nothing registered yet: nothing to remove", calls == [], calls)

print("The plugin's half of the receipt")
with tempfile.TemporaryDirectory() as t:
    out, _, failed = run(t, "toolkit_plugin_receipt", installed_sha="CLONE", branch="fix/x",
                         env={"NSLS_TOOLKIT_BRANCH": "fix/x"})
    check("a matching plugin prints its commit and no warning",
          "Plugin commit:" in out and "[!]" not in out and failed == "", out)

    out, _, failed = run(t, "toolkit_plugin_receipt", installed_sha="f" * 40, branch="fix/x",
                         env={"NSLS_TOOLKIT_BRANCH": "fix/x"})
    check("a branch test whose plugin is another commit says so and fails",
          "[!]" in out and failed == "nsls-builder-toolkit", out)

    out, _, failed = run(t, "toolkit_plugin_receipt", installed_sha="f" * 40)
    check("a plain re-run (plugin updates daily, on its own) only prints the receipt",
          "Plugin commit: fffffff" in out and "[!]" not in out and failed == "", out)

    out, _, failed = run(t, "toolkit_plugin_receipt")
    check("no plugin recorded: says nothing", out.strip() == "INSTALL_FAILED=", out)

print("install.ps1 carries the same logic")
ps1 = (REPO / "install.ps1").read_text(encoding="utf-8")
check("it pins the branch onto a remote URL", '$ToolkitMarket = "$RepoUrl#$RepoBranch"' in ps1)
check("it installs the toolkit from the pinned source",
      "Install-Plugin 'nsls-builder-toolkit' 'nsls-builder-toolkit@nsls-toolkit' `\n        $ToolkitMarket" in ps1)
check("it starts an installed plugin over when it can't be what was asked for",
      "plugin marketplace remove nsls-toolkit" in ps1 and "$Repoint = $Testing -or [bool]$src.ref" in ps1)
check("it prints the plugin's commit", "Plugin commit:" in ps1)
check("a wrong-commit plugin counts as a failed install",
      "$InstallFailed += 'nsls-builder-toolkit (not the branch asked for)'" in ps1)
check("a toolkit plugin that didn't install counts as a failed install",
      "$ToolkitMarket)) { $InstallFailed += 'nsls-builder-toolkit' }" in ps1)
check("a failed install changes the banner and exits 1",
      "if ($InstallFailed.Count) { Write-Host" in ps1
      and "if ($InstallFailed.Count) { exit 1 } else { exit 0 }" in ps1)

print("install.sh never calls a failed install a success")
sh = (REPO / "install.sh").read_text(encoding="utf-8")
check("the banner names what did not install",
      'if [ -n "$INSTALL_FAILED" ]; then\n  echo "  NSLS Builder Toolkit set up, but this did NOT install' in sh)
check("and the installer exits 1", sh.rstrip().endswith('[ -z "$INSTALL_FAILED" ] || exit 1'))

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall passed")
