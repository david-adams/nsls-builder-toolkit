#!/usr/bin/env python3
"""Every Scope the tracker can send, judged by both gates that read one.

Plain stdlib, no pytest: run with `python3 hooks/tests/test_gate_scope_tiers.py`.

The Automation Tracker's Scope field has five choices: Personal, Department,
Company-wide, Customer Facing: Advisors and Customer Facing: Members. The policy
calls member-facing work Tier 3, yet gate 2 looked for a reviewer only when the
scope said "company", and gate 4 checked the platform only for "department" or
"company". So the 9 builds registered as Customer Facing: Members (2026-10-15)
were never reviewer-checked or platform-checked, and gate 2's block said
"Company-wide" whatever the record said. Both gates now ask scope_tier(); this
file runs every choice through it and through both gates, in both shapes a
choice can arrive in.

The last section runs the real hook as a subprocess against a loopback tracker,
so "a member-facing deploy with no reviewer is blocked" is proven through
main(), the tracker parse and the repo lookup, not only through stubs. Nothing
here touches the network or the builder's own config.
"""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HOOKS = Path(__file__).resolve().parents[1]
GATE = HOOKS / "guardrail-gate.py"
failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def load_gate(config_dir):
    os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
    spec = importlib.util.spec_from_file_location(
        f"gate_{config_dir.name}", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Every choice of the live Scope field (base appd5oK1wLVPYZeia, field
# fldutWlsSLKVEQcZJ, read 2026-10-16) and the tier the policy gives it.
CHOICES = (
    ("Personal", 1),
    ("Department", 2),
    ("Company-wide", 3),
    ("Customer Facing: Advisors", 3),
    ("Customer Facing: Members", 3),
)

# Scopes that name no tier. Both gates must stay silent on every one: an
# unknown scope is not evidence of anything.
UNKNOWN = (
    ("", "an empty scope"),
    (None, "no scope"),
    ("tbd", "an unrecognised scope"),
    (["Company-wide"], "a list"),
    ({"id": "selX", "color": "red"}, "an Airtable object with no name"),
)


def shapes(scope):
    """A choice as the proxy sends it (a string), and as Airtable's own
    {id, name, color} object, which _text() unwraps."""
    return ((scope, "string"),
            ({"id": "selTEST", "name": scope, "color": "blueLight2"},
             "Airtable object"))


def run_gate(fn, tool, ti):
    """(blocked, reason) for one call of one gate. A block prints its deny JSON
    to stdout and exits; an allow just returns."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            fn(tool, ti)
        except SystemExit:
            pass
    text = out.getvalue().strip()
    if not text:
        return False, ""
    return True, json.loads(text)["hookSpecificOutput"]["permissionDecisionReason"]


def both_ways_out(reason):
    """The policy: every hard block names the compliance route AND Kevin's
    authorization. A block without Kevin in it isn't finished."""
    return "Kevin" in reason and "authoriz" in reason.lower()


print("every Scope choice reads as the tier the policy gives it")
with tempfile.TemporaryDirectory() as tmp:
    gate = load_gate(Path(tmp))
    for scope, tier in CHOICES:
        for value, shape in shapes(scope):
            got = gate.scope_tier(value)
            check(f"{scope} ({shape}) is Tier {tier}", got == tier, f"(got {got})")
    for value, label in UNKNOWN + ((7, "a number"), ("n/a", "n/a")):
        got = gate.scope_tier(value)
        check(f"{label} is unknown (0)", got == 0, f"(got {got})")
    check("hyphens, underscores and spacing don't hide a choice",
          gate.scope_tier("customer-facing") == 3
          and gate.scope_tier("Customer_Facing") == 3
          and gate.scope_tier("  CUSTOMER   FACING: Members ") == 3)
    check("a scope naming two tiers is judged by the higher",
          gate.scope_tier("Department / Company-wide") == 3)

print("\ngate 2: a Tier 3 deploy with no reviewer is blocked, whichever Tier 3 it is")
with tempfile.TemporaryDirectory() as tmp:
    gate = load_gate(Path(tmp))
    events = []
    gate.emit = lambda *a, **k: events.append(a)
    gate.repo_root = lambda *a, **k: "/tmp/member-portal"
    gate.looks_like_nsls_work = lambda root: True

    def deploy(record):
        gate.tracker_lookup = lambda name: ("found", dict(record, name=name))
        events.clear()
        return run_gate(gate.gate_unregistered_ship, "Bash",
                        {"command": "railway up"})

    for scope, tier in CHOICES:
        want = tier == 3
        for value, shape in shapes(scope):
            # [] is what the proxy sends for an empty Reviewer field.
            blocked, reason = deploy({"scope": value, "reviewer": []})
            check(f"{scope} ({shape}), no reviewer: "
                  f"{'blocked' if want else 'allowed'}", blocked == want)
            if want:
                check(f"  the block names {scope}, as the tracker wrote it",
                      f"registered as {scope} but has no reviewer" in reason
                      and (scope == "Company-wide" or "Company-wide" not in reason),
                      f"({reason.splitlines()[0][:120]!r})")
                check("  and keeps both ways out", both_ways_out(reason))
                check("  and is recorded once, as tier3_no_reviewer, with the scope",
                      len(events) == 1
                      and events[0][1].startswith("tier3_no_reviewer: ")
                      and scope in events[0][1], f"({events!r})")
            blocked, _ = deploy({"scope": value, "reviewer": ["recREVIEWER000001"]})
            check(f"{scope} ({shape}) with a reviewer assigned: allowed", not blocked)
    for value, label in UNKNOWN:
        blocked, _ = deploy({"scope": value, "reviewer": []})
        check(f"{label}, no reviewer: allowed (unknown is silence)", not blocked)
    blocked, _ = deploy({"reviewer": []})
    check("a record with no scope field at all: allowed", not blocked)

print("\ngate 4: a non-Anthropic SDK in a Tier 2+ build is paused, member-facing included")
with tempfile.TemporaryDirectory() as tmp:
    gate = load_gate(Path(tmp))
    events = []
    gate.emit = lambda *a, **k: events.append(a)
    gate.repo_root = lambda *a, **k: "/tmp/member-portal"
    body = "import openai\nclient = openai.OpenAI()\n"

    def add_sdk(record):
        gate.tracker_lookup = lambda name: ("found", dict(record, name=name))
        events.clear()
        return run_gate(gate.gate_off_platform, "Edit",
                        {"file_path": "/tmp/member-portal/src/client.py",
                         "new_string": body})

    for scope, tier in CHOICES:
        want = tier >= 2
        for value, shape in shapes(scope):
            blocked, reason = add_sdk({"scope": value})
            check(f"{scope} ({shape}): {'paused' if want else 'allowed'}",
                  blocked == want)
            if want:
                check(f"  the pause names {scope}, as the tracker wrote it",
                      f"registered as {scope}, and this adds" in reason,
                      f"({reason.splitlines()[0][:120]!r})")
                check("  and is recorded once, as off_platform",
                      len(events) == 1 and events[0][1].startswith("off_platform: "),
                      f"({events!r})")
    for value, label in UNKNOWN:
        blocked, _ = add_sdk({"scope": value})
        check(f"{label}: allowed (unknown is silence)", not blocked)


print("\nthe real hook, end to end, against a loopback tracker")


class _Tracker(BaseHTTPRequestHandler):
    """Answers every lookup with one record named after the request, in the
    reply shape the live proxy uses: {"success", "count", "records"}."""
    record = {}

    def do_GET(self):
        name = (parse_qs(urlparse(self.path).query).get("name") or [""])[0]
        raw = json.dumps({"success": True, "count": 1,
                          "records": [dict(type(self).record, name=name)]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


def run_hook(payload, repo, url):
    """(blocked, reason, events) from one run of the real hook, with a fresh
    config dir each time so no cached tracker page carries over."""
    with tempfile.TemporaryDirectory() as cfg:
        log = Path(cfg) / "events.jsonl"
        env = {k: v for k, v in os.environ.items()
               if k != "NSLS_GUARDRAILS_DISABLED"}
        # Block events go to the log file, never the network (emit()'s seam).
        env.update(NSLS_TRACKER_URL=url, CLAUDE_CONFIG_DIR=cfg,
                   NSLS_GUARDRAIL_EVENT_LOG=str(log))
        proc = subprocess.run([sys.executable, str(GATE)],
                              input=json.dumps(payload), capture_output=True,
                              text=True, encoding="utf-8", cwd=str(repo),
                              env=env, timeout=30)
        events = []
        if log.exists():
            events = [json.loads(line) for line in
                      log.read_text(encoding="utf-8").splitlines() if line.strip()]
    out = proc.stdout.strip()
    if not out:
        return False, "", events
    return (True, json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"],
            events)


with tempfile.TemporaryDirectory() as tmp:
    repo = Path(tmp) / "member-portal"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True,
                   capture_output=True, timeout=30)
    # "nsls" in the README is what makes a repo NSLS work to gate 2.
    (repo / "README.md").write_text("# NSLS member portal\n", encoding="utf-8")
    srv = HTTPServer(("127.0.0.1", 0), _Tracker)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    try:
        deploy_call = {"tool_name": "Bash", "tool_input": {"command": "railway up"}}
        _Tracker.record = {"scope": "Customer Facing: Members", "reviewer": []}
        blocked, reason, events = run_hook(deploy_call, repo, url)
        check("a member-facing deploy with no reviewer is denied", blocked)
        check("  naming the record's scope",
              "registered as Customer Facing: Members but has no reviewer" in reason)
        check("  with both ways out", both_ways_out(reason))
        check("  and exactly one guardrail_blocked row, tier3_no_reviewer",
              [e["type"] for e in events] == ["guardrail_blocked"]
              and events[0]["description"].startswith("tier3_no_reviewer: "),
              f"({events!r})")
        _Tracker.record = {"scope": "Customer Facing: Members",
                           "reviewer": ["recREVIEWER000001"]}
        blocked, _, events = run_hook(deploy_call, repo, url)
        check("the same deploy with a reviewer assigned goes through",
              not blocked and not events)

        sdk_call = {"tool_name": "Write", "tool_input": {
            "file_path": str(repo / "src" / "client.py"),
            "content": "import openai\nclient = openai.OpenAI()\n"}}
        _Tracker.record = {"scope": "Customer Facing: Advisors"}
        blocked, reason, events = run_hook(sdk_call, repo, url)
        check("an advisor-facing build adding an OpenAI SDK is paused", blocked)
        check("  naming the record's scope",
              "registered as Customer Facing: Advisors, and this adds" in reason)
        check("  and exactly one guardrail_blocked row, off_platform",
              [e["type"] for e in events] == ["guardrail_blocked"]
              and events[0]["description"].startswith("off_platform: "),
              f"({events!r})")
        _Tracker.record = {"scope": "Personal"}
        blocked, _, _ = run_hook(sdk_call, repo, url)
        check("a Personal build adding the same SDK is left alone", not blocked)
    finally:
        srv.shutdown()
        srv.server_close()

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
