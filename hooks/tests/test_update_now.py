#!/usr/bin/env python3
""""Update my NSLS toolkit": the on-request update behind the update-toolkit skill.

Plain stdlib, no pytest: run with `python3 hooks/tests/test_update_now.py`.

The skill runs `run-hook.sh update`, which runs session-start.py with
--update-now. That is the session-start update without the session-start
limits (no 10-minute throttle, 120 seconds instead of 20), and its output is
plain sentences Claude passes on. A fake `claude` on PATH records each call and
moves the registry the way the real CLI does. No network, no real install.
"""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]
PLUGIN_KEY = "nsls-builder-toolkit@nsls-toolkit"
OLD = "58b5a0227d37a72f3baaf911226e7b318ef4c497"

failures = []


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label} {'' if cond else detail}")
    if not cond:
        failures.append(label)


FAKE_CLAUDE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    from pathlib import Path
    args = sys.argv[1:]
    Path(os.environ["FAKE_LOG"]).open("a").write(" ".join(args) + "\\n")
    if args[:2] == ["plugin", "update"]:
        if os.environ.get("FAKE_UPDATE_FAILS"):
            sys.exit(1)
        if os.environ.get("FAKE_DROP_RECORD"):
            reg = Path(os.environ["FAKE_REGISTRY"])
            reg.write_text(json.dumps({"version": 2, "plugins": {"nsls-builder-toolkit@nsls-toolkit": []}}))
            sys.exit(0)
        new_path = os.environ.get("FAKE_NEW_PATH")
        if new_path:
            for name in os.environ["FAKE_NEW_SKILLS"].split(","):
                d = Path(new_path) / "skills" / name
                d.mkdir(parents=True, exist_ok=True)
                (d / "SKILL.md").write_text(f"---\\nname: {name}\\n---\\n")
            reg = Path(os.environ["FAKE_REGISTRY"])
            d = json.loads(reg.read_text())
            d["plugins"]["nsls-builder-toolkit@nsls-toolkit"] = [{
                "scope": "user", "version": "3.7.0", "installPath": new_path,
                "gitCommitSha": os.environ["FAKE_HEAD"]}]
            reg.write_text(json.dumps(d))
    sys.exit(0)
''')


def machine(tmp, release=None, installed=True, cli=True, update_fails=False):
    """A fake ~/.claude with 3.6.0 installed (skills alpha, beta) and a
    marketplace clone. `release`: skill names the fake update installs as 3.7.0."""
    root = Path(tmp)
    cfg = root / "cfg"
    cache = cfg / "plugins" / "cache" / "nsls-toolkit" / "nsls-builder-toolkit"
    old_path = cache / "3.6.0"
    for name in ("alpha", "beta"):
        (old_path / "skills" / name).mkdir(parents=True)
        (old_path / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\n---\n")
    market = cfg / "plugins" / "marketplaces" / "nsls-toolkit"
    market.mkdir(parents=True)
    env_git = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@example.test"],
                 ["config", "user.name", "t"], ["commit", "-q", "--allow-empty", "-m", "c"]):
        subprocess.run(["git", "-C", str(market), *args], check=True, env=env_git, capture_output=True)
    head = subprocess.run(["git", "-C", str(market), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()
    registry = cfg / "plugins" / "installed_plugins.json"
    sha = OLD if release else head  # nothing to release: already on the clone's HEAD
    registry.write_text(json.dumps({"version": 2, "plugins": {PLUGIN_KEY: [{
        "scope": "user", "version": "3.6.0", "installPath": str(old_path),
        "gitCommitSha": sha}]}} if installed else {"version": 2, "plugins": {}}))
    (cfg / "settings.json").write_text(json.dumps({"enabledPlugins": {PLUGIN_KEY: True}}))
    bin_dir = root / "bin"
    bin_dir.mkdir()
    if cli:
        (bin_dir / "claude").write_text(FAKE_CLAUDE)
        (bin_dir / "claude").chmod(0o755)
    log = root / "calls.log"
    log.write_text("")
    env = {**os.environ,
           "CLAUDE_CONFIG_DIR": str(cfg), "HOME": str(root / "home"),
           "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
           "FAKE_LOG": str(log), "FAKE_REGISTRY": str(registry), "FAKE_HEAD": head}
    if release:
        env["FAKE_NEW_PATH"] = str(cache / "3.7.0")
        env["FAKE_NEW_SKILLS"] = ",".join(release)
    if update_fails:
        env["FAKE_UPDATE_FAILS"] = "1"
    return cfg, log, env


def run(env, via_launcher=False):
    if via_launcher:
        cmd = ["bash", str(HOOKS / "run-hook.sh"), "update"]
        env = {**env, "CLAUDE_PLUGIN_ROOT": str(HOOKS.parent)}
    else:
        cmd = [sys.executable, str(HOOKS / "session-start.py"), "--update-now"]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=120)
    return r.stdout


with tempfile.TemporaryDirectory() as tmp:
    cfg, log, env = machine(tmp, release=["alpha", "beta", "quicknote"])
    out = run(env)
    check("release: one update call, nothing else", log.read_text().split("\n")[:-1] == [f"plugin update {PLUGIN_KEY}"],
          log.read_text())
    check("release: says it updated, from and to", "Updated the NSLS toolkit from version 3.6.0 to version 3.7.0." in out, out)
    check("release: names only the new skill", "New skills: /quicknote." in out and "/alpha" not in out, out)
    check("release: says to open a new session", "open a new session" in out, out)
    check("release: the session-start check is not repeated straight away",
          (cfg / ".nsls-plugin-fresh-check").exists())

with tempfile.TemporaryDirectory() as tmp:
    cfg, log, env = machine(tmp, release=["alpha", "beta", "quicknote"])
    out = run(env, via_launcher=True)
    check("launcher: `run-hook.sh update` reaches the same code", "Updated the NSLS toolkit" in out, out)

with tempfile.TemporaryDirectory() as tmp:
    cfg, log, env = machine(tmp)
    out = run(env)
    check("up to date: says so, with the version", "already up to date (version 3.6.0)" in out, out)
    check("up to date: no reinstall", "uninstall" not in log.read_text())

with tempfile.TemporaryDirectory() as tmp:
    cfg, log, env = machine(tmp, release=["quicknote"], update_fails=True)
    out = run(env)
    check("update fails: says it did not finish", "did not finish" in out, out)

with tempfile.TemporaryDirectory() as tmp:
    cfg, log, env = machine(tmp, release=["quicknote"])
    (cfg / ".nsls-plugin-heal.lock").write_text("123")
    out = run(env)
    check("another session updating: says so, no update call", "Another Claude session" in out and log.read_text() == "", out)

with tempfile.TemporaryDirectory() as tmp:
    cfg, log, env = machine(tmp, release=["quicknote"])
    env["FAKE_DROP_RECORD"] = "1"
    out = run(env)
    check("record unreadable after the update: never 'up to date'",
          "already up to date" not in out and "could not be checked" in out, out)

with tempfile.TemporaryDirectory() as tmp:
    # A project-scope install only: the plugin is there, but no user-scope
    # record to compare against before the update.
    cfg, log, env = machine(tmp, release=["alpha", "beta", "quicknote"])
    reg = cfg / "plugins" / "installed_plugins.json"
    d = json.loads(reg.read_text())
    d["plugins"][PLUGIN_KEY][0]["scope"] = "project"
    reg.write_text(json.dumps(d))
    out = run(env)
    check("no record before the update: reports the new version, not 'up to date'",
          "now on version 3.7.0" in out and "already up to date" not in out, out)

with tempfile.TemporaryDirectory() as tmp:
    # Nothing released, but the registry carries no commit and the clone no
    # version: "up to date" would be a guess.
    cfg, log, env = machine(tmp)
    reg = cfg / "plugins" / "installed_plugins.json"
    d = json.loads(reg.read_text())
    d["plugins"][PLUGIN_KEY][0].pop("gitCommitSha")
    reg.write_text(json.dumps(d))
    out = run(env)
    check("unverifiable commit: never 'up to date'",
          "already up to date" not in out and "could not be verified" in out, out)

with tempfile.TemporaryDirectory() as tmp:
    # Same, but the clone carries the installed version: that IS evidence.
    cfg, log, env = machine(tmp)
    reg = cfg / "plugins" / "installed_plugins.json"
    d = json.loads(reg.read_text())
    d["plugins"][PLUGIN_KEY][0].pop("gitCommitSha")
    reg.write_text(json.dumps(d))
    market = cfg / "plugins" / "marketplaces" / "nsls-toolkit"
    (market / ".claude-plugin").mkdir()
    (market / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "3.6.0"}))
    out = run(env)
    check("no commit but the clone carries the installed version: up to date", "already up to date" in out, out)

src = (HOOKS / "session-start.py").read_text(encoding="utf-8")
import re as _re
stale = int(_re.search(r"^HEAL_LOCK_STALE_S = (\d+)", src, _re.M).group(1))
upd = int(_re.search(r"^UPDATE_NOW_TIMEOUT_S = (\d+)", src, _re.M).group(1))
check("the lock outlives the longest holder (update now + its 60s heal)", stale > upd + 60)

with tempfile.TemporaryDirectory() as tmp:
    cfg, log, env = machine(tmp, installed=False)
    out = run(env)
    check("not a plugin install: says there is nothing to update", "not installed as a Claude Code plugin" in out, out)

with tempfile.TemporaryDirectory() as tmp:
    cfg, log, env = machine(tmp, release=["quicknote"], cli=False)
    out = run(env)
    check("no CLI: says it could not update", "claude command was not found" in out, out)

with tempfile.TemporaryDirectory() as tmp:
    # Without the flag the file is still the session-start hook, not the updater.
    cfg, log, env = machine(tmp)
    env["NSLS_TRACKER_URL"] = "http://127.0.0.1:9"
    env["NSLS_COLLECTOR_OPTOUT"] = "1"
    r = subprocess.run([sys.executable, str(HOOKS / "session-start.py")], capture_output=True, text=True,
                       env=env, timeout=120)
    check("no flag: the hook does not run the on-request updater", "already up to date" not in r.stdout)

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
