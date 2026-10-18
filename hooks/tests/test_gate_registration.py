#!/usr/bin/env python3
"""The gate is wired in hooks.json, and wiring it twice still decides once.

Plain stdlib, no pytest: run with `python3 hooks/tests/test_gate_registration.py`.

The failures this exists to prevent:

1. 2026-09-06 to 2026-09-20 — the gate was registered ONLY by the installers,
   into settings.json, which is precisely the surface the plugin migration
   strips. Every Mac that finished migrating ran no gate at all for two weeks
   and nobody could see it, because "no blocks" and "no gate" produce the same
   empty table. The hooks.json assertions below are that outage's regression
   test.

2. The fix reintroduces a second registration on any machine that still
   carries the installer's settings.json entry (the migration's done-marker is
   permanent, so it never goes back to strip it). The docs are explicit that a
   plugin's copy of a handler "stays separate" from a settings.json copy: both
   run, in parallel. Two decisions are survivable. Two `guardrail_blocked`
   rows per block are not — emit() deliberately does not deduplicate, and
   those rows are the only measure the gates have.

3. The matcher used to be the unanchored "Bash|PowerShell|Write|Edit", which
   admitted BashOutput and KillBash (a Python process per poll) and reached
   MultiEdit only as a substring accident — while the gates themselves read
   `new_string`/`content`, which a MultiEdit payload does not have. Anchoring
   it fixes both. PowerShell stays in the list deliberately: it is a real tool,
   enabled automatically on Windows, and the gates only judge it because
   normalize_call presents it as Bash.

No network: NSLS_GUARDRAIL_EVENT_LOG redirects the event emitter to a file.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "hooks" / "guardrail-gate.py"
HOOKS_JSON = ROOT / "hooks" / "hooks.json"
LAUNCHER = ROOT / "hooks" / "run-hook.sh"

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


# ------------------------------------------------------------------ wiring

print("hooks.json registration")
cfg = json.loads(HOOKS_JSON.read_text())
pre = cfg.get("hooks", {}).get("PreToolUse", [])
gate_entries = [
    g for g in pre
    if any("run-hook.sh" in h.get("command", "") and "gate" in h.get("command", "")
           for h in g.get("hooks", []))
]
check("the plugin registers the gate", len(gate_entries) == 1,
      f"(found {len(gate_entries)})")

if gate_entries:
    entry = gate_entries[0]
    hook = entry["hooks"][0]
    matcher = entry.get("matcher", "")
    check("matcher is anchored", matcher.startswith("^") and matcher.endswith("$"),
          f"({matcher!r})")
    rx = re.compile(matcher)
    for tool in ("Bash", "PowerShell", "Write", "Edit", "MultiEdit",
                 "NotebookEdit"):
        check(f"matcher admits {tool}", bool(rx.search(tool)))
    for tool in ("BashOutput", "KillBash", "Skill", "Read", "WebFetch"):
        check(f"matcher ignores {tool}", not rx.search(tool))
    check("shell is named explicitly", hook.get("shell") == "bash",
          f"({hook.get('shell')!r})")
    check("timeout is the budget the gate is written against",
          hook.get("timeout") == 10, f"({hook.get('timeout')})")
    check("status message says what is happening",
          bool(hook.get("statusMessage")))
    check("launcher is executable", os.access(LAUNCHER, os.X_OK))

# --------------------------------------------------------------- fixtures


def personal_repo(tmp):
    """A repo that gate 1 must block a push from: personal remote, NSLS work."""
    repo = Path(tmp) / "nsls-thing"
    repo.mkdir()
    (repo / "README.md").write_text("# NSLS chapter dashboard\nInternal NSLS tool.\n")
    run = lambda *a: subprocess.run(a, cwd=repo, capture_output=True, text=True)
    run("git", "init", "-q")
    run("git", "remote", "add", "origin",
        "https://github.com/someones-personal-account/nsls-thing.git")
    return repo


def call(repo, env, payload):
    return subprocess.run(
        [sys.executable, str(GATE)], input=json.dumps(payload),
        capture_output=True, text=True, cwd=repo, env=env, timeout=30,
    )


def denied(result):
    try:
        out = json.loads(result.stdout or "{}")
    except Exception:
        return False
    return (out.get("hookSpecificOutput", {}).get("permissionDecision")
            == "deny")


def environment(config_dir, log):
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["NSLS_GUARDRAIL_EVENT_LOG"] = str(log)
    env.pop("NSLS_GUARDRAILS_DISABLED", None)
    return env


PUSH = {"tool_name": "Bash", "tool_input": {"command": "git push origin main"}}

with tempfile.TemporaryDirectory() as tmp:
    repo = personal_repo(tmp)
    config_dir = Path(tmp) / "claude"
    config_dir.mkdir()
    log = Path(tmp) / "events.jsonl"
    env = environment(config_dir, log)

    print("\nthe gate still fires at all")
    r = call(repo, env, dict(PUSH, tool_use_id="toolu_solo"))
    check("a push from a personal repo is denied", denied(r), r.stdout[:200])
    check("one event recorded", log.read_text().count("\n") == 1)

    print("\ntwo registrations, one tool call")
    # Exactly the two-copy shape a machine has when the installer's
    # settings.json entry survived the migration and the plugin now registers
    # the gate too: same tool_use_id, both copies launched together.
    log.write_text("")
    results = {}

    def one(tag):
        results[tag] = call(repo, env, dict(PUSH, tool_use_id="toolu_same"))

    threads = [threading.Thread(target=one, args=(t,)) for t in ("plugin", "settings")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    denies = [t for t, r in results.items() if denied(r)]
    check("exactly one copy decides", len(denies) == 1, f"(denied: {denies})")
    check("exactly one guardrail_blocked row",
          log.read_text().count("guardrail_blocked") == 1,
          f"({log.read_text().count('guardrail_blocked')} rows)")
    silent = [t for t, r in results.items() if t not in denies]
    check("the other copy is silent", all(not results[t].stdout.strip()
                                          for t in silent))
    check("neither copy errors", all(r.returncode == 0 for r in results.values()))

    print("\nthe guard does not over-suppress")
    log.write_text("")
    a = call(repo, env, dict(PUSH, tool_use_id="toolu_first"))
    b = call(repo, env, dict(PUSH, tool_use_id="toolu_second"))
    check("two distinct calls both decide", denied(a) and denied(b))
    check("two events", log.read_text().count("guardrail_blocked") == 2)

    log.write_text("")
    c = call(repo, env, PUSH)  # no tool_use_id at all
    check("a call with no id is still decided", denied(c))
    log.write_text("")
    d = call(repo, env, dict(PUSH, tool_use_id="../../etc/passwd"))
    check("an unusable id is decided, not skipped", denied(d))

    print("\nMultiEdit and NotebookEdit reach gate 4")
    log.write_text("")
    openai_edit = "import openai\nclient = openai.OpenAI()\n"
    # gate 4 needs a registered Tier-2+ automation to name; with no tracker
    # reachable it cannot fire, so this asserts the payload is SEEN — the
    # normalisation step — rather than the verdict.
    sys.path.insert(0, str(ROOT / "hooks"))
    import importlib.util
    spec = importlib.util.spec_from_file_location("gate_mod", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tool, ti = mod.normalize_call("MultiEdit", {
        "file_path": "/tmp/x.py",
        "edits": [{"old_string": "a", "new_string": openai_edit},
                  {"old_string": "b", "new_string": "more"}],
    })
    check("MultiEdit becomes an Edit the gates read", tool == "Edit")
    check("MultiEdit text survives normalisation", "import openai" in ti["new_string"])
    tool, ti = mod.normalize_call("NotebookEdit", {
        "notebook_path": "/tmp/x.ipynb", "new_source": openai_edit})
    check("NotebookEdit becomes an Edit the gates read", tool == "Edit")
    check("NotebookEdit text survives normalisation",
          "import openai" in ti["new_string"])
    tool, ti = mod.normalize_call("PowerShell", {"command": "git push"})
    check("PowerShell is judged as a Bash command", tool == "Bash")
    check("PowerShell command survives normalisation", ti["command"] == "git push")
    tool, ti = mod.normalize_call("Bash", {"command": "ls"})
    check("other tools pass through untouched",
          tool == "Bash" and ti == {"command": "ls"})

    print("\nthe launcher refuses an interpreter that runs nothing")
    # The Windows Store alias for `python3` exits 0 without executing. Modelled
    # here with a stub, because the failure it causes is invisible: the hook
    # "succeeds", decides nothing, and leaves no trace.
    import shutil, stat
    fake_bin = Path(tmp) / "fakebin"
    fake_bin.mkdir()
    for name in ("python3", "python"):
        alias = fake_bin / name
        alias.write_text("#!/bin/sh\nexit 0\n")
        alias.chmod(alias.stat().st_mode | stat.S_IEXEC)
    launcher_cfg = Path(tmp) / "launcher-cfg"
    launcher_cfg.mkdir()
    real_python3 = shutil.which("python3") or sys.executable
    lenv = dict(os.environ)
    # The stub aliases come FIRST, so a probe that accepts exit 0 picks one.
    # The suite's own git comes before /usr/bin. Without it, on a macOS runner
    # `git` became the Xcode shim in /usr/bin, a different git from every other
    # check in this file, and every git call the gate makes has a 2-second cap
    # and fails open. This check flaked on macOS CI ten times in a week
    # (2026-10-11 to 10-18) while the interpreter it is about was always the
    # real one (the cache check below passed each time).
    real_git = shutil.which("git")
    lenv["PATH"] = os.pathsep.join(
        [str(fake_bin), os.path.dirname(real_python3)]
        + ([os.path.dirname(real_git)] if real_git else [])
        + ["/usr/bin", "/bin"])
    # Evidence for the line above, printed every run: which git each PATH
    # finds and how long a first call to it takes on this machine.
    for label, path in (("suite PATH", os.environ.get("PATH", "")),
                        ("old test PATH", os.pathsep.join([os.path.dirname(real_python3),
                                                           "/usr/bin", "/bin"]))):
        found = shutil.which("git", path=path)
        started = time.monotonic()
        try:
            subprocess.run([found or "git", "rev-parse", "--show-toplevel"], cwd=repo,
                           capture_output=True, timeout=30)
        except Exception:
            pass
        print(f"  info git on the {label}: {found} ({time.monotonic() - started:.2f}s)")
    lenv["CLAUDE_CONFIG_DIR"] = str(launcher_cfg)
    lenv["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
    lenv["NSLS_GUARDRAIL_EVENT_LOG"] = str(Path(tmp) / "launcher-ev")
    lenv.pop("NSLS_GUARDRAILS_DISABLED", None)
    r = subprocess.run(["bash", str(LAUNCHER), "gate"],
                       input=json.dumps(dict(PUSH, tool_use_id="toolu_alias")),
                       capture_output=True, text=True, cwd=repo, env=lenv, timeout=60)
    check("the launcher exits 0", r.returncode == 0, f"(exit {r.returncode})")
    check("a silent alias is not used to decide", denied(r),
          "(the gate did not run — an alias was selected)")
    cached = (launcher_cfg / ".nsls-hook-python")
    if cached.exists():
        chosen = cached.read_text().strip()
        check("the cache records a real interpreter, not the alias",
              str(fake_bin) not in chosen, f"({chosen})")
        check("the cache is format-tagged so a change invalidates it",
              chosen.startswith("v3"), f"({chosen})")
        check("the cache is one field per line, not delimiter-packed",
              "|" not in chosen.splitlines()[0], f"({chosen})")

    print("\nan interpreter path containing the old delimiter still works")
    # The launcher used to hand `path|flag` back through stdout and split it
    # again. A path containing that character lost everything after it, and the
    # launcher then ran something that does not exist -- silently, because this
    # file fails open. The answer travels in globals now, and the cache is
    # line-based, so there is no delimiter left to trip over.
    odd = Path(tmp) / "py|dir"
    odd.mkdir()
    (odd / "python3").symlink_to(real_python3)
    odd_cfg = Path(tmp) / "odd-cfg"
    odd_cfg.mkdir()
    oenv = dict(lenv)
    oenv["PATH"] = os.pathsep.join([str(odd), "/usr/bin", "/bin"])
    oenv["CLAUDE_CONFIG_DIR"] = str(odd_cfg)
    oenv["NSLS_GUARDRAIL_EVENT_LOG"] = str(Path(tmp) / "odd-ev")
    r = subprocess.run(["bash", str(LAUNCHER), "gate"],
                       input=json.dumps(dict(PUSH, tool_use_id="toolu_pipepath")),
                       capture_output=True, text=True, cwd=repo, env=oenv, timeout=60)
    check("the launcher still decides from a path containing '|'", denied(r),
          f"(exit {r.returncode}; stderr {r.stderr[:120]!r})")
    odd_cache = odd_cfg / ".nsls-hook-python"
    if odd_cache.exists():
        lines = odd_cache.read_text().splitlines()
        check("the cached path survives the round trip whole",
              len(lines) >= 2 and lines[1] == str(odd / "python3"),
              f"({lines})")

    print("\nthe hook survives a machine with no resolvable home")
    ninja = dict(os.environ)
    for k in ("HOME", "CLAUDE_CONFIG_DIR", "USERPROFILE"):
        ninja.pop(k, None)
    r = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util,sys;"
         f"spec=importlib.util.spec_from_file_location('g', r'{GATE}');"
         "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
         "print('IMPORTED', m._config_dir() is not None)"],
        capture_output=True, text=True, env=ninja, timeout=60)
    check("guardrail-gate.py imports with no HOME at all",
          "IMPORTED True" in r.stdout,
          f"(exit {r.returncode}; {r.stderr.strip()[-160:]!r})")

    print("\nthe single-flight guard's three awkward cases")
    # Delayed loser: the winner has already finished. The marker is retained
    # precisely so a copy that starts late is still suppressed.
    log.write_text("")
    a = call(repo, env, dict(PUSH, tool_use_id="toolu_delayed"))
    b = call(repo, env, dict(PUSH, tool_use_id="toolu_delayed"))
    check("a copy starting after the winner finished is still suppressed",
          denied(a) and not denied(b))
    check("still exactly one event", log.read_text().count("guardrail_blocked") == 1)

    # Killed winner: a marker with no decision behind it. Deliberate fail-open —
    # the alternative is deciding twice on every hook the harness times out.
    inflight = config_dir / ".nsls-gate-inflight"
    inflight.mkdir(parents=True, exist_ok=True)
    (inflight / "toolu_killed").write_text("")
    log.write_text("")
    r = call(repo, env, dict(PUSH, tool_use_id="toolu_killed"))
    check("a killed winner's marker suppresses the sibling (documented fail-open)",
          not denied(r) and not r.stdout.strip())

    # Stale marker: older than the TTL, so it is reclaimed and the call decided.
    stale = inflight / "toolu_stale"
    stale.write_text("")
    old_time = time.time() - 600
    os.utime(stale, (old_time, old_time))
    log.write_text("")
    r = call(repo, env, dict(PUSH, tool_use_id="toolu_stale"))
    check("a stale marker is reclaimed and the call is decided", denied(r))

    print("\nthe off switch still wins")
    off = dict(env, NSLS_GUARDRAILS_DISABLED="1")
    r = call(repo, off, dict(PUSH, tool_use_id="toolu_off"))
    check("NSLS_GUARDRAILS_DISABLED=1 allows", not denied(r))

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
