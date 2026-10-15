#!/usr/bin/env python3
"""Re-running an installer must not undo the plugin migration.

Plain stdlib: `python3 hooks/tests/test_installers_respect_migration.py`.

PC test 4 (2026-10-03): every install.ps1 run re-registered the settings.json
shims and re-wrote 69 org pointer files, and the next session start's migration
removed them again ("removed 69 legacy skill pointers, 2 legacy hook entries").
In between, every toolkit skill was listed twice. install.sh did the same.

Both installers now ask plugin_beacon.py which hooks the plugin has already been
seen running here, and skip exactly those shims, and the org pointers once
session-start is proven: the same evidence the migration uses to retire them.
The same goes for the user-scope signal MCP entry: once the plugin is installed it
registers its own servers, and the installers and /signal-setup no longer add a
second one.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOKS = REPO / "hooks"
failures = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {'' if cond else detail}")
    if not cond:
        failures.append(name)


def machine(tmp, prove, installed=True):
    """A config dir with the toolkit checkout's hooks and beacons for `prove`."""
    cfg = Path(tmp) / ".claude"
    hooks = cfg / "local-plugins" / "nsls-builder-toolkit" / "hooks"
    hooks.mkdir(parents=True)
    shutil.copy(HOOKS / "plugin_beacon.py", hooks / "plugin_beacon.py")
    cache = cfg / "plugins" / "cache" / "nsls-toolkit" / "nsls-builder-toolkit" / "3.16.5" / "hooks"
    cache.mkdir(parents=True)
    (cfg / "settings.json").write_text(json.dumps({"enabledPlugins": {}, "hooks": {}}))
    if installed:
        (cache.parent / ".mcp.json").write_text('{"mcpServers": {}}')
        (cfg / "plugins" / "installed_plugins.json").write_text(json.dumps(
            {"plugins": {"nsls-builder-toolkit@nsls-toolkit": [{"installPath": str(cache.parent)}]}}))
    if prove:
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import plugin_beacon\n"
                "from pathlib import Path\n"
                "for h in sys.argv[3].split(','):\n"
                "    f = Path(sys.argv[2]) / (h + '-copy.py'); f.write_text('#')\n"
                "    assert plugin_beacon.record(h, f)\n")
        subprocess.run([sys.executable, "-c", code, str(HOOKS), str(cache), ",".join(prove)],
                       check=True, env=dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg)))
    return cfg


print("plugin_beacon.py answers the installers")
with tempfile.TemporaryDirectory() as tmp:
    cfg = machine(tmp, ["session-start", "guardrail-gate"])
    out = subprocess.run([sys.executable, str(HOOKS / "plugin_beacon.py"), "--proven"],
                         capture_output=True, text=True, env=dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg)))
    check("--proven lists exactly the hooks with evidence",
          out.stdout.split() == ["session-start", "guardrail-gate"], f"({out.stdout!r} {out.stderr[-200:]!r})")
with tempfile.TemporaryDirectory() as tmp:
    cfg = machine(tmp, [])
    out = subprocess.run([sys.executable, str(HOOKS / "plugin_beacon.py"), "--proven"],
                         capture_output=True, text=True, env=dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg)))
    check("and nothing on a machine with no evidence", out.stdout.strip() == "", f"({out.stdout!r})")



def ask(cfg, flag):
    return subprocess.run([sys.executable, str(HOOKS / "plugin_beacon.py"), flag], capture_output=True,
                          text=True, env=dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg))).stdout.split()


def disable(cfg):
    (cfg / "settings.json").write_text(json.dumps(
        {"enabledPlugins": {"nsls-builder-toolkit@nsls-toolkit": False}, "hooks": {}}))


print("\nevidence only counts while the plugin is installed and enabled")
with tempfile.TemporaryDirectory() as tmp:
    cfg = machine(tmp, ["session-start", "skill-event", "guardrail-gate"])
    disable(cfg)
    check("a disabled plugin proves nothing", ask(cfg, "--proven") == [], f"({ask(cfg, '--proven')})")
    check("and is not counted as installed", ask(cfg, "--installed") == [])
with tempfile.TemporaryDirectory() as tmp:
    cfg = machine(tmp, ["session-start", "skill-event", "guardrail-gate"], installed=False)
    check("beacons left behind by an uninstalled plugin prove nothing", ask(cfg, "--proven") == [],
          f"({ask(cfg, '--proven')})")
with tempfile.TemporaryDirectory() as tmp:
    cfg = machine(tmp, ["session-start"])
    (cfg / "settings.json").write_text("{not json")
    check("unreadable settings prove nothing", ask(cfg, "--proven") == [])
with tempfile.TemporaryDirectory() as tmp:
    cfg = machine(tmp, ["session-start"])
    root = cfg / "plugins" / "cache" / "nsls-toolkit" / "nsls-builder-toolkit" / "3.16.5"
    (root / ".mcp.json").unlink()
    check("a plugin copy without its .mcp.json is not counted as serving the MCP servers",
          ask(cfg, "--installed") == [])
    shutil.rmtree(root)
    check("a registry entry whose copy is gone proves nothing", ask(cfg, "--proven") == [] and
          ask(cfg, "--installed") == [])

print("\ninstall.sh's settings step, run for real")
lines = (REPO / "install.sh").read_text().splitlines()
start = next(i for i, l in enumerate(lines) if 'python3 -c "' in l and "CONFIG_DIR=" in l)
end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith('" 2>/dev/null'))


def run_block(cfg, tmp):
    block = Path(tmp) / "block.sh"
    block.write_text('CONFIG_DIR="$1"\n' + "\n".join(lines[start:end + 1]) + "\n")
    proc = subprocess.run(["bash", str(block), str(cfg)], capture_output=True, text=True, timeout=60)
    written = json.loads((cfg / "settings.json").read_text())
    return proc, [h["command"] for g in written.get("hooks", {}).values() for e in g for h in e.get("hooks", [])]


with tempfile.TemporaryDirectory() as tmp:
    proc, cmds = run_block(machine(tmp, ["session-start", "skill-event", "guardrail-gate"]), tmp)
    check("a fully migrated machine gets no shims back", cmds == [], f"({cmds} {proc.stderr[-300:]!r})")
    check("and is told why", "toolkit plugin already runs it here" in proc.stdout, f"({proc.stdout[-300:]!r})")
with tempfile.TemporaryDirectory() as tmp:
    proc, cmds = run_block(machine(tmp, ["session-start"]), tmp)
    check("a half-migrated machine gets back only the unproven shims",
          len(cmds) == 2 and not any("session-start" in c for c in cmds), f"({cmds})")
with tempfile.TemporaryDirectory() as tmp:
    proc, cmds = run_block(machine(tmp, []), tmp)
    check("a machine with no evidence still gets all three", len(cmds) == 3, f"({cmds})")
with tempfile.TemporaryDirectory() as tmp:
    cfg = machine(tmp, ["session-start", "skill-event", "guardrail-gate"])
    disable(cfg)
    proc, cmds = run_block(cfg, tmp)
    check("a machine whose plugin was disabled gets all three back", len(cmds) == 3, f"({cmds})")

print("\ninstall.sh's pointer step")
sh = (REPO / "install.sh").read_text()
check("asks plugin_beacon.py before writing pointers",
      'python3 "$PLUGIN_DIR/hooks/plugin_beacon.py" --proven' in sh and "grep -qx 'session-start'" in sh)
check("and the pointer loop stops when the plugin serves the skills",
      '[ -n "$PLUGIN_SERVES_SKILLS" ] && break' in sh)

print("\ninstall.ps1 (no PowerShell here; Windows CI parses it)")
ps = (REPO / "install.ps1").read_text(encoding="utf-8")
fn = ps[ps.index("function Get-BeaconAnswer"):ps.index("$Proven = @(Get-BeaconAnswer '--proven')")]
check("asks plugin_beacon.py, as a file, never an inline -c",
      "$beaconArgs = @($beaconPy, $Flag)" in fn and " -c " not in fn)
check("and uses it for the proven hooks", "$Proven = @(Get-BeaconAnswer '--proven')" in ps)
for hook in ("session-start", "skill-event", "guardrail-gate"):
    check(f"skips the {hook} shim when proven", f"if ($Proven -notcontains '{hook}')" in ps)
check("skips the org pointers once session-start is proven",
      "$pointerSources = if ($Proven -contains 'session-start') { @() }" in ps)
check("restores CLAUDE_CONFIG_DIR after asking", "finally { $env:CLAUDE_CONFIG_DIR = $savedCfg }" in ps)
import run_windows_ci  # noqa: E402  (beside this file; reads the `# windows-ci:` markers)
check("and Windows CI runs Get-BeaconAnswer for real under PS 5.1", "ps51" in run_windows_ci.lanes("install-beacon-answer.ps1"))
check("skips user-scope MCP registration once the plugin is installed",
      "$PluginInstalled = (@(Get-BeaconAnswer '--installed') -contains 'installed')" in ps
      and "if ($PluginInstalled) {" in ps)

print("\nthe user-scope signal duplicate")
with tempfile.TemporaryDirectory() as tmp:
    cfg = machine(tmp, [], installed=False)
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg))
    ask = lambda: subprocess.run([sys.executable, str(HOOKS / "plugin_beacon.py"), "--installed"],
                                 capture_output=True, text=True, env=env).stdout.strip()
    check("--installed says nothing without the plugin", ask() == "")
    inst = cfg / "plugins" / "cache" / "nsls-toolkit" / "nsls-builder-toolkit" / "3.16.5"
    (cfg / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {"nsls-builder-toolkit@nsls-toolkit": [{"installPath": str(inst)}]}}))
    (inst / ".mcp.json").write_text('{"mcpServers": {}}')
    check("and says installed with it", ask() == "installed")
check("install.sh skips its MCP step once the plugin is installed",
      'python3 "$PLUGIN_DIR/hooks/plugin_beacon.py" --installed' in sh)
skill = (REPO / "skills" / "signal-setup" / "SKILL.md").read_text()
check("/signal-setup no longer adds a user-scope signal beside the plugin's",
      "If it is enabled, the plugin registers signal itself: do **not** add a user-scope entry" in skill)
check("but a disabled plugin is not treated as registering it",
      "If it is installed but disabled, it registers nothing" in skill)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
