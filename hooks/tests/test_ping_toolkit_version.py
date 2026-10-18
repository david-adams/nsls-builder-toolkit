#!/usr/bin/env python3
# windows-ci: python | Both session pings carry the running toolkit version
"""Both session pings tell the tracker which toolkit version is running.

Plain stdlib: `python3 hooks/tests/test_ping_toolkit_version.py`.

Without it the tracker could not say who is behind main or how long a release
takes to reach people (measured by hand on 2026-10-17: a median 19.6 hours on
one Mac, and no way to see anyone else). The Python ping reads the version from
its own copy's plugin.json; so does session-ping.ps1. Only a value shaped like
a version is sent.

The PowerShell half POSTs to a local capture server, never the tracker. It runs
under every PowerShell found: `powershell` and `pwsh` on the Windows runner,
and on a Mac the shell named by NSLS_TEST_PWSH (or `pwsh` on PATH); with none,
it says so and skips.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]
ROOT = HOOKS.parent
VERSION = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]

failures = []


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label} {'' if cond else detail}")
    if not cond:
        failures.append(label)


def python_ping_body(hook_file, config_dir):
    """Run session_ping() from `hook_file` and return the body it would POST."""
    saved = {k: os.environ.get(k) for k in ("CLAUDE_CONFIG_DIR",)}
    os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        spec = importlib.util.spec_from_file_location("ss_ping_version", hook_file)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        sent = []

        def fake_post(body, timeout=None):
            sent.append(body)
            return {"success": True}

        hook._post_session_ping = fake_post
        hook.session_ping()
        return sent[0] if sent else None
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def config_with_email(tmp):
    cfg = Path(tmp) / "cfg"
    env_dir = cfg / "local-plugins" / "nsls-personal-toolkit"
    env_dir.mkdir(parents=True)
    (env_dir / ".env").write_text("BUILDER_EMAIL=b@nsls.org\nGITHUB_USERNAME=octocat\n")
    return cfg


# --- Python ping ---------------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    body = python_ping_body(HOOKS / "session-start.py", config_with_email(tmp))
    check("python ping: sent", body is not None)
    check("python ping: carries this copy's version", body and body.get("toolkit_version") == VERSION, body)
    check("python ping: the other fields are unchanged",
          body and {"builder_email", "toolkit", "github_username", "platform"} <= set(body), body)

with tempfile.TemporaryDirectory() as tmp:
    # A copy whose plugin.json says something that is not a version.
    copy = Path(tmp) / "copy"
    (copy / "hooks").mkdir(parents=True)
    (copy / ".claude-plugin").mkdir()
    shutil.copy2(HOOKS / "session-start.py", copy / "hooks" / "session-start.py")
    (copy / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "3.1 IGNORE PREVIOUS"}))
    body = python_ping_body(copy / "hooks" / "session-start.py", config_with_email(tmp))
    check("python ping: a non-version is never sent", body is not None and "toolkit_version" not in body, body)


# --- PowerShell ping -----------------------------------------------------------
class Capture(BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Capture.bodies.append((self.path, raw.decode("utf-8", "replace")))
        out = json.dumps({"success": True, "announcements": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


shells = []
for name in ("powershell", "pwsh"):
    path = shutil.which(name)
    if path and path not in shells:
        shells.append(path)
override = os.environ.get("NSLS_TEST_PWSH")
if override and Path(override).exists() and override not in shells:
    shells.append(override)
if os.name != "nt":
    # `powershell` off Windows is not Windows PowerShell; keep whatever pwsh is.
    shells = [s for s in shells if Path(s).name.lower().startswith("pwsh")]

if not shells:
    print("skip PowerShell ping: no PowerShell on this machine (set NSLS_TEST_PWSH to run it)")

for shell in shells:
    server = HTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    Capture.bodies = []
    with tempfile.TemporaryDirectory() as tmp:
        profile = Path(tmp) / "profile"
        env_dir = profile / ".claude" / "local-plugins" / "nsls-personal-toolkit"
        env_dir.mkdir(parents=True)
        (env_dir / ".env").write_text("BUILDER_EMAIL=b@nsls.org\nGITHUB_USERNAME=octocat\n")
        env = {**os.environ, "USERPROFILE": str(profile), "HOME": str(profile),
               "NSLS_TRACKER_URL": f"http://127.0.0.1:{server.server_port}"}
        r = subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(HOOKS / "session-ping.ps1")],
                           capture_output=True, text=True, env=env, timeout=120)
    server.shutdown()
    pings = [json.loads(b) for p, b in Capture.bodies if p == "/session-ping"]
    label = Path(shell).name
    check(f"{label} ping: sent", len(pings) == 1, f"{Capture.bodies} {r.stderr[-400:]}")
    check(f"{label} ping: carries this copy's version",
          bool(pings) and pings[0].get("toolkit_version") == VERSION, pings)

print()
if failures:
    print(f"{len(failures)} failed: " + ", ".join(failures))
    sys.exit(1)
print("all passed")
