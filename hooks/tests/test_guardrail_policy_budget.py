#!/usr/bin/env python3
"""The guardrail policy reaches Claude whole, and stays small enough to.

Plain stdlib: `python3 hooks/tests/test_guardrail_policy_budget.py`.

Claude Code caps each hook's output at 10,000 characters. Past that, Claude gets
the first 2,000 and a file path it is never asked to open. The policy section
of CLAUDE.md passed the cap on 2026-09-20 (10,107 characters before any path was
expanded), so from then on every builder's Claude saw the tier table and the
hard-gate list and nothing after it: not how to record a "no", not how to report
a guardrail event, not the exploration log, which is why "log it" was never
offered once in the org.

This pins the fix:
  * the printed policy stays under BUDGET, with room left for long install
    paths and a decline list;
  * the commands in it run through run-hook.sh, never a bare python3, and name
    no unexpanded variable;
  * the policy is its own SessionStart hook that also fires on clear and compact,
    while the hook that sends the credit ping does not.

Every subprocess here runs with a dead tracker URL and an empty git identity:
`memory record` reports a decline to the tracker, and a test that forgets this
writes a real row under the developer's own name.
"""
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]
ROOT = HOOKS.parent
WINDOWS = os.name == "nt"
BUDGET = 6000          # printed characters; the hard cap is 10,000
PREVIEW = 2000         # what Claude sees of anything over the cap
failures = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {'' if cond else detail}")
    if not cond:
        failures.append(name)


def git_bash():
    """Git for Windows' bash on a PC (never WSL's System32 bash); bash elsewhere."""
    if not WINDOWS:
        return shutil.which("bash")
    git = shutil.which("git")
    for cand in ([Path(git).resolve().parents[1] / "bin" / "bash.exe"] if git else []) + [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"]:
        if cand.is_file():
            return str(cand)
    return None


def sandbox_env(tmp):
    """No real config, no real identity, no reachable tracker."""
    home = Path(tmp) / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    empty_gitconfig = Path(tmp) / "gitconfig"
    empty_gitconfig.write_text("", encoding="utf-8")
    return dict(
        os.environ,
        HOME=str(home), USERPROFILE=str(home),
        CLAUDE_CONFIG_DIR=str(home / ".claude"),
        CLAUDE_PLUGIN_ROOT=str(ROOT),
        GIT_CONFIG_GLOBAL=str(empty_gitconfig), GIT_CONFIG_NOSYSTEM="1",
        NSLS_TRACKER_URL="http://127.0.0.1:9",
        NSLS_NO_PLUGIN_MIGRATION="1",
        PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
    )


def run(argv, env, cwd, inp=""):
    # Always feed stdin: the gate reads its payload from it, and an inherited
    # terminal would leave this waiting for input that never comes.
    return subprocess.run(argv, input=inp, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, cwd=cwd, timeout=60)


with tempfile.TemporaryDirectory() as tmp:
    env = sandbox_env(tmp)
    cwd = Path(tmp) / "work"
    cwd.mkdir()

    print("the policy hook prints the policy, whole and under budget")
    r = run([sys.executable, str(HOOKS / "guardrail-policy.py")], env, cwd)
    out = r.stdout
    check("it exits 0", r.returncode == 0, f"({r.returncode}: {r.stderr[-300:]!r})")
    check("it prints the policy", "org guardrail policy" in out and "## Builder Guardrails" in out,
          f"({out[:200]!r})")
    check(f"it is at most {BUDGET} characters (cap 10,000)", len(out) <= BUDGET, f"({len(out)})")
    root_posix = ROOT.resolve().as_posix()
    n_paths = out.count(root_posix)
    worst = len(out) + n_paths * max(0, 200 - len(root_posix))
    check("and still fits the cap with a 200-character install path", worst <= 10000 - 1500,
          f"({worst} with {n_paths} paths)")
    check("no variable is left unexpanded", "${CLAUDE_PLUGIN_ROOT}" not in out)
    check("no command calls a bare python3 (absent on stock Windows)",
          not re.search(r"(^|[\s`])python3\s", out), "")
    decline = out.find('run-hook.sh" memory record')
    check("the decline command is there, inside the first 2,000 characters",
          0 <= decline < PREVIEW, f"(at {decline})")
    check("the event command is there", 'run-hook.sh" event ' in out)
    check("so is the exploration log", "exploration log" in out.lower())

    print("\nevery path it hands Claude opens, and every helper it names exists")
    for ref in ("guardrail-voice.md", "guardrail-playbook.md"):
        m = re.search(r"`([^`]*" + re.escape(ref) + r")`", out)
        check(f"{ref} is named by an absolute path", bool(m) and Path(m.group(1)).is_absolute(),
              f"({m.group(1) if m else None})")
        check(f"and that file exists", bool(m) and Path(m.group(1)).is_file())
    launcher = (HOOKS / "run-hook.sh").read_text(encoding="utf-8")
    for sub in sorted(set(re.findall(r'run-hook\.sh" (\w[\w-]*)', out))):
        check(f"run-hook.sh dispatches `{sub}`", re.search(rf"^\s*{re.escape(sub)}\)", launcher, re.M) is not None)
    playbook = (ROOT / "_shared" / "references" / "guardrail-playbook.md").read_text(encoding="utf-8")
    check("the playbook calls nothing as a bare python3", not re.search(r"(^|[\s`])python3\s", playbook))
    for sub in sorted(set(re.findall(r'run-hook\.sh" (\w[\w-]*)', playbook))):
        check(f"run-hook.sh dispatches the playbook's `{sub}`",
              re.search(rf"^\s*{re.escape(sub)}\)", launcher, re.M) is not None)

    print("\nonly the plugin's policy hook prints it; a clone or shim still does")
    g = runpy.run_path(str(HOOKS / "session-start.py"), run_name="__probe__")
    owns = g["_policy_hook_owns_policy"]
    saved = os.environ.get("CLAUDE_PLUGIN_ROOT")
    try:
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(ROOT)
        check("under the plugin, session-start leaves the policy to its own hook", owns() is True)
        os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        check("from a clone or shim, session-start prints it itself", owns() is False)
    finally:
        if saved is None:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        else:
            os.environ["CLAUDE_PLUGIN_ROOT"] = saved

    src = (HOOKS / "session-start.py").read_text(encoding="utf-8")
    main_src = src[src.index("\ndef main():"):src.index('\nif __name__ == "__guardrails__":')]
    check("main() prints the policy only behind that check",
          "if not _policy_hook_owns_policy():\n        emit_guardrails_context()" in main_src
          and main_src.count("emit_guardrails_context()") == 1)
    policy_block = src[src.index('if __name__ == "__policy__":'):src.index('if __name__ == "__main__":')]
    busy = [f for f in ("git_pull", "session_ping", "replay_failed_ping", "run_plugin_migration",
                        "ensure_plugin_fresh", "sync_pointers", "bootstrap_collector", "_record_beacon")
            if f + "(" in policy_block]
    check("the policy-only entry pulls, pings and migrates nothing", not busy, f"({busy})")

    print("\nhooks.json: the policy returns after clear and compact; the credit ping does not")
    starts = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"]

    def entries(sub):
        return [(grp.get("matcher", ""), h) for grp in starts for h in grp["hooks"]
                if h.get("command", "").rstrip().endswith(f'run-hook.sh" {sub}')]

    ss, pol = entries("session-start"), entries("policy")
    check("session-start is registered exactly once", len(ss) == 1, f"({len(ss)})")
    check("and never on clear or compact (one ping per session)",
          bool(ss) and not set(ss[0][0].split("|")) & {"clear", "compact"}, f"({ss})")
    check("the policy hook is registered exactly once", len(pol) == 1, f"({len(pol)})")
    want = {"startup", "resume", "clear", "compact"}
    check("on startup, resume, clear and compact",
          bool(pol) and set(pol[0][0].split("|")) == want, f"({pol})")
    check("through bash, like every toolkit hook",
          bool(pol) and pol[0][1].get("shell") == "bash" and pol[0][1]["command"].startswith("bash "))

    print("\nthe launcher passes arguments through to the helpers")
    bash = git_bash()
    if not bash:
        check("bash is available to run the launcher", False, "(no Git Bash found)")
    else:
        launcher_path = (HOOKS / "run-hook.sh").as_posix()
        r = run([bash, launcher_path, "event", "--list"], env, cwd)
        check("`event --list` reaches guardrail-event.py", "flagged" in r.stdout and r.returncode == 0,
              f"({r.returncode}: {r.stdout[:200]!r} {r.stderr[-200:]!r})")
        r = run([bash, launcher_path, "policy"], env, cwd)
        check("`policy` prints the same policy", "org guardrail policy" in r.stdout and len(r.stdout) <= BUDGET,
              f"({len(r.stdout)}: {r.stderr[-200:]!r})")
        repo = Path(tmp) / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], env=env, timeout=30)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "https://github.com/example/test-build.git"], env=env, timeout=30)
        run([bash, launcher_path, "memory", "record", "exploration-log", "--note", "just for me",
             "--cwd", str(repo)], env, cwd)
        r = run([bash, launcher_path, "memory", "list", "--cwd", str(repo)], env, cwd)
        check("a decline recorded through the launcher reads back",
              "exploration-log" in r.stdout and "just for me" in r.stdout,
              f"({r.stdout[:200]!r} {r.stderr[-200:]!r})")
        r = run([bash, launcher_path, "gate"], env, cwd, inp="{}")
        check("the gate still exits 0 with no payload", r.returncode == 0, f"({r.returncode})")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
