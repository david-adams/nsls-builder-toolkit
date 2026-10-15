#!/usr/bin/env python3
"""install.sh must register hook commands that can actually run.

Plain stdlib, no pytest: run with `python3 hooks/tests/test_installer_registration.py`.

The failure this exists to prevent, found 2026-09-20 on origin/main. The
installer writes settings.json from a Python program embedded in a shell
double-quoted string (`python3 -c "..."`). The guardrail gate's command was
built with unescaped double quotes, which closed that shell string early — so
the shell executed fragments of the Python source as words, and settings.json
received the literal text:

    python3  + os.path.join(CONFIG_DIR, local-plugins/.../guardrail-gate.py) +

as the hook command. It is not runnable. The gate therefore could not have
fired on any Mac the installer touched, even before the migration deleted the
entry, and every matching tool call would have printed a hook-error notice. The
session-start entry a few lines above escapes its quotes correctly; nobody
compared them, because nothing executed the installer's output.

This runs the real block from the real install.sh against a throwaway config
dir and then runs what it wrote.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "install.sh"
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


lines = INSTALL.read_text().splitlines()
start = next(i for i, l in enumerate(lines)
             if 'python3 -c "' in l and "CONFIG_DIR=" in l)
end = next(i for i in range(start + 1, len(lines))
           if lines[i].startswith('" 2>/dev/null'))

with tempfile.TemporaryDirectory() as tmp:
    cfg = Path(tmp) / ".claude"
    cfg.mkdir()
    (cfg / "settings.json").write_text(json.dumps({"enabledPlugins": {}, "hooks": {}}))
    block = Path(tmp) / "block.sh"
    block.write_text('CONFIG_DIR="$1"\n' + "\n".join(lines[start:end + 1]) + "\n")

    proc = subprocess.run(["bash", str(block), str(cfg)],
                          capture_output=True, text=True, timeout=60)
    noise = [l for l in proc.stderr.splitlines() if l.strip()]
    check("the block runs without the shell tripping over the program",
          not noise, f"({noise[:2]})")

    written = json.loads((cfg / "settings.json").read_text())
    commands = [h["command"]
                for groups in written.get("hooks", {}).values()
                for g in groups for h in g.get("hooks", [])]
    check("three hooks registered", len(commands) == 3, f"({len(commands)})")

    gate = next((c for c in commands if "guardrail-gate" in c), None)
    check("a gate command was written", gate is not None)

    if gate:
        check("it is not mangled Python source",
              "os.path.join" not in gate and " + " not in gate, f"({gate})")
        # The real test: run what the installer wrote, exactly as a shell would.
        script = Path(tmp) / "repo" / "hooks" / "guardrail-gate.py"
        script.parent.mkdir(parents=True)
        real = (REPO / "hooks" / "guardrail-gate.py").read_text()
        script.write_text(real)
        runnable = gate.replace(
            str(cfg / "local-plugins" / "nsls-builder-toolkit" / "hooks"
                / "guardrail-gate.py"),
            str(script))
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg),
                   NSLS_GUARDRAIL_EVENT_LOG=str(Path(tmp) / "ev"))
        env.pop("NSLS_GUARDRAILS_DISABLED", None)
        out = subprocess.run(runnable, shell=True, input='{"tool_name":"Bash",'
                             '"tool_input":{"command":"ls"},"tool_use_id":"t1"}',
                             capture_output=True, text=True, timeout=30, env=env)
        check("the command the installer wrote actually executes",
              out.returncode == 0, f"(exit {out.returncode}: {out.stderr[:160]})")
        check("and it is the gate that ran, not something else",
              "can't open file" not in out.stderr and "No such file" not in out.stderr,
              f"({out.stderr[:160]})")

    # The whole class, not just the instance. Everything between those quotes
    # is expanded by the shell before Python sees it -- including comments. A
    # backtick in a comment in this block once ran `claude plugin install` as a
    # command substitution during the install.
    import re as _re
    offenders = []
    for i in range(start + 1, end):
        line = lines[i]
        for kind, pat in (("backtick", r"(?<!\\)`"), ("quote", r'(?<!\\)"'),
                          ("expansion", r"(?<!\\)\$[({A-Za-z_]")):
            if _re.search(pat, line):
                offenders.append((i + 1, kind, line.strip()[:70]))
                break
    check("no unescaped shell metacharacters in the embedded program",
          not offenders, f"({offenders[:3]})")

    for c in commands:
        if "session-start" in c or "skill-event" in c:
            check(f"{'session-start' if 'session-start' in c else 'skill-event'}"
                  " command is well formed", "os.path.join" not in c, f"({c[:90]})")

print("\na plugin that did not install is not reported as installed")
# `claude plugin install ... | tail -1 || true` threw the exit status away
# twice over, so an unreachable marketplace and a failed install printed the
# same line a success prints. For the org toolkit that is the whole product
# silently absent behind an installer that said it worked.
ROOT_SH = Path(__file__).resolve().parents[2] / "install.sh"
src = ROOT_SH.read_text()
start = src.index("install_plugin() {")
end = src.index("\n}\n", start) + 3
func = src[start:end]

def run_install_plugin(stub_body, args, registry=None):
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp) / "claude"
        stub.write_text(stub_body)
        stub.chmod(0o755)
        if registry is not None:
            reg = Path(tmp) / "claude-cfg" / "plugins"
            reg.mkdir(parents=True)
            (reg / "installed_plugins.json").write_text(registry)
        script = (
            'set -uo pipefail\n'
            f'CLAUDE_BIN="{stub}"\n'
            f'CONFIG_DIR="{tmp}/claude-cfg"\n'
            'INSTALL_FAILED=""\n'
            f'{func}\n'
            f'install_plugin {args}\n'
            'echo "FAILED_LIST=[$INSTALL_FAILED]"\n'
        )
        return subprocess.run(["bash", "-c", script], capture_output=True,
                              text=True, timeout=30)

# invoked as `claude plugin list` / `claude plugin install ...`, so the verb is $2
never = '#!/bin/sh\ncase "$2" in list) exit 0;; *) exit 1;; esac\n'
# a same-named plugin from SOMEBODY ELSE's marketplace must not satisfy the
# check: the machine would end up with their copy and none of our hooks.
other = ('#!/bin/sh\ncase "$2" in list) echo nsls-builder-toolkit; exit 0;;'
         ' *) exit 1;; esac\n')
r = run_install_plugin(never, '"nsls-builder-toolkit" "nsls-builder-toolkit@nsls-toolkit" "" required')
check("a required plugin that never appears is called out",
      "did NOT install" in r.stdout, f"({r.stdout.strip()[:140]!r})")
check("and it is recorded for the end-of-run summary",
      "FAILED_LIST=[nsls-builder-toolkit]" in r.stdout,
      f"({r.stdout.strip()[-60:]!r})")
check("the installer does not abort on it",
      r.returncode == 0, f"(exit {r.returncode}: {r.stderr[:120]})")

optional = run_install_plugin(never, '"superpowers" "superpowers@x" "" ')
check("an optional plugin only warns",
      "[warn]" in optional.stdout and "did NOT install" not in optional.stdout,
      f"({optional.stdout.strip()[:120]!r})")
check("and is not recorded as a blocker",
      "FAILED_LIST=[]" in optional.stdout, f"({optional.stdout.strip()[-40:]!r})")

r = run_install_plugin(other, '"nsls-builder-toolkit" "nsls-builder-toolkit@nsls-toolkit" "" required')
check("a same-named plugin from another marketplace does not satisfy the check",
      "already installed" not in r.stdout, f"({r.stdout.strip()[:120]!r})")
check("and the missing org plugin is still reported",
      "did NOT install" in r.stdout, f"({r.stdout.strip()[:120]!r})")

# toolkit_market is $REPO_URL, plus the branch when one is being tested; its
# cases are run for real in test_installer_plugin_pin.py.
check("the org plugin install uses REPO_URL, not a hard-coded upstream",
      '"$(toolkit_market)" required' in ROOT_SH.read_text()
      and 'echo "$REPO_URL#$REPO_BRANCH"' in ROOT_SH.read_text(),
      "(fork testing would install the wrong revision)")

works = ('#!/bin/sh\ncase "$2" in list) echo nsls-builder-toolkit; exit 0;;'
         ' *) exit 0;; esac\n')
ok = run_install_plugin(
    works, '"nsls-builder-toolkit" "nsls-builder-toolkit@nsls-toolkit" "" required',
    registry='{"plugins": {"nsls-builder-toolkit@nsls-toolkit": {}}}')
check("a plugin that is present is reported as installed, with no alarm",
      "already installed" in ok.stdout and "did NOT install" not in ok.stdout,
      f"({ok.stdout.strip()[:120]!r})")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
