#!/usr/bin/env python3
# windows-ci: python | The plugin hooks speak UTF-8 on a Windows pipe (policy printed whole, gate reads accented paths and emoji)
"""The plugin's Python hooks speak UTF-8 on a Windows pipe.

Plain stdlib: `python3 hooks/tests/test_plugin_hooks_utf8.py`.

Windows Python reads and writes a pipe in the ANSI code page (cp1252), and
strictly. The settings.json shim set PYTHONIOENCODING for its child; the
plugin's own hooks (hooks.json -> run-hook.sh -> python) never did. So on a PC:
  * session-start printed the guardrail policy, whose arrows cp1252 cannot
    encode, and died there: no policy, no credit ping. Its beacon was recorded
    first, so stage B still retired the shim that worked (Fable review,
    2026-10-04);
  * the gate decoded its payload as cp1252: an accented user folder came out
    garbled and the repo lookup missed, and an emoji raised; both allowed.
PYTHONIOENCODING=cp1252 reproduces a Windows pipe on any OS.
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "hooks"
failures = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {'' if cond else detail}")
    if not cond:
        failures.append(name)


EMIT = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location("ss", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
if sys.argv[2] == "fixed":
    m._utf8_stdio()
m.emit_guardrails_context()
"""

print("session-start's policy on a cp1252 pipe")
with tempfile.TemporaryDirectory() as tmp:
    env = dict(os.environ, CLAUDE_CONFIG_DIR=tmp, NSLS_NO_PLUGIN_MIGRATION="1", PYTHONIOENCODING="cp1252")
    env.pop("PYTHONUTF8", None)
    run = lambda mode: subprocess.run([sys.executable, "-c", EMIT, str(HOOKS / "session-start.py"), mode],
                                      capture_output=True, env=env, timeout=60)
    bare = run("bare")
    check("without the fix the print dies (the bug, reproduced)",
          bare.returncode != 0 and b"UnicodeEncodeError" in bare.stderr, f"({bare.returncode} {bare.stderr[-200:]!r})")
    fixed = run("fixed")
    out = fixed.stdout.decode("utf-8", "strict") if fixed.returncode == 0 else ""
    check("with it the whole policy goes out, as UTF-8",
          "## Builder Guardrails" in out and "\u2192" in out, f"({fixed.returncode} {fixed.stderr[-300:]!r})")

tree = ast.parse((HOOKS / "session-start.py").read_text(encoding="utf-8"))
main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
calls = [n.value.func.id for n in main.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
         and isinstance(n.value.func, ast.Name)]
check("main() switches to UTF-8 before anything prints", calls[:1] == ["_utf8_stdio"], f"({calls})")
check("and records its beacon last, so a copy that crashes proves nothing",
      calls[-1:] == ["_record_beacon"] and calls.count("_record_beacon") == 1, f"({calls})")
src = (HOOKS / "session-start.py").read_text(encoding="utf-8")
guard = src[src.index('if __name__ == "__guardrails__":'):]
check("the shim's entry point switches too", "_utf8_stdio()" in guard[:guard.index("emit_guardrails_context()")])

runner = (HOOKS / "run-hook.sh").read_text(encoding="utf-8")
check("run-hook.sh puts both hooks in UTF-8 mode before running Python",
      "export PYTHONUTF8=1 PYTHONIOENCODING=utf-8" in runner
      and runner.index("export PYTHONUTF8=1") < runner.index('"$py" "$script"'))

print("\nthe gate's payload on a cp1252 pipe")


def denied(stdout):
    try:
        return json.loads(stdout or "{}").get("hookSpecificOutput", {}).get("permissionDecision") == "deny"
    except Exception:
        return False


with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "Jos\u00e9" / "dashboard"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# NSLS chapter dashboard\nInternal NSLS tool.\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/someones-personal-account/dash.git"],
                   cwd=repo, capture_output=True)
    cfg = Path(tmp) / "claude"
    cfg.mkdir()
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(cfg), PYTHONIOENCODING="cp1252",
               NSLS_GUARDRAIL_EVENT_LOG=str(Path(tmp) / "ev.jsonl"))
    for k in ("PYTHONUTF8", "NSLS_GUARDRAILS_DISABLED"):
        env.pop(k, None)

    def gate(command, n, description=""):
        payload = {"tool_name": "Bash", "tool_input": {"command": command, "description": description},
                   "tool_use_id": f"toolu_utf8_{n}"}
        return subprocess.run([sys.executable, str(HOOKS / "guardrail-gate.py")],
                              input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                              capture_output=True, cwd=tmp, env=env, timeout=30)

    r = gate(f'git -C "{repo}" push origin main', 1)
    check("a push from an accented user folder is still judged", denied(r.stdout), f"({r.stdout[:200]!r})")
    r = gate(f'git -C "{repo}" push origin main', 2, description="ship it \U0001F44D")
    check("an emoji in the payload no longer makes the gate allow", denied(r.stdout), f"({r.stdout[:200]!r})")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
