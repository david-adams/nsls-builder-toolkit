#!/usr/bin/env python3
"""
Tests for the guardrail event emitter.

Run: python3 test/test_guardrail_emit.py

Written as a plain script rather than pytest to match run_hook_scenarios.py --
the toolkit ships to machines with no dev dependencies installed.

These tests exist because the reporting path failed silently twice, in the two
ways silent failure happens:

  * Nothing emitted the seven soft labels at all, so three of Signal's four
    tiles could never move. The suite that should have caught it was 42 green
    scenarios about *blocking*, and reporting was nobody's assertion.
  * `guardrail_disputed` was emitted by nothing AND absent from Signal's label
    filter, so even once wired it would have written a real Airtable row that
    the dashboard dropped on the floor. That is worse than not reporting,
    because it looks like it worked.

So the assertions here are mostly about the contract at the seams: which labels
are legal, what the payload looks like on the wire, and that a failure to
deliver never costs the builder anything -- nor silently eats the retry.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hooks"))

failures = []


def check(label, ok, detail=""):
    if ok:
        print(f"  [ ok ] {label}")
    else:
        failures.append(label)
        print(f"  [FAIL] {label}" + (f"\n         {detail}" if detail else ""))


# ── a tracker that records what it was sent ───────────────────────────────
received = []


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        received.append((self.path, json.loads(raw.decode())))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"success": true}')

    def log_message(self, *a):
        pass


def start_server():
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def fresh(tmp, url):
    """Reload the emitter against a throwaway state dir and a given tracker."""
    os.environ["NSLS_TRACKER_URL"] = url
    for mod in ("guardrail_emit",):
        sys.modules.pop(mod, None)
    import guardrail_emit
    guardrail_emit.SEEN_FILE = tmp / "seen.json"
    return guardrail_emit


def main():
    import tempfile

    srv = start_server()
    good = f"http://127.0.0.1:{srv.server_address[1]}"
    dead = "http://127.0.0.1:9"  # discard port: refuses instantly

    tmp = Path(tempfile.mkdtemp())
    g = fresh(tmp, good)

    print("\nThe label set is closed")
    # Signal's isGuardrailLabel drops anything not in its own list, so an
    # invented label writes an Airtable row the dashboard ignores.
    check("an unknown label is refused, not sent",
          "unknown" in g.emit("noticed", "x") and not received,
          f"received={received}")
    # The exploration log's three labels are a second list in Signal
    # (EXPLORATION_LOG_LABELS), kept out of its guardrail tiles on purpose.
    LOGIT = {"guardrail_logit_offered", "guardrail_logit_accepted", "guardrail_logit_declined"}
    check("Signal's eight guardrail labels plus the exploration log's three, nothing else",
          set(g.LABELS) == {
              "guardrail_flagged", "guardrail_registered", "guardrail_mentor",
              "guardrail_migrated", "guardrail_proceeded", "guardrail_blocked",
              "guardrail_authorized", "guardrail_disputed",
          } | LOGIT,
          f"got {sorted(g.LABELS)}")

    # Cross-repo contract. Skipped when Signal isn't checked out here, but when
    # it is, drift between the two lists is the exact bug that lost disputes.
    signal = Path(os.environ.get("NSLS_SIGNAL_PATH")
                  or "/Volumes/SSK Data/Developer/Projects/employee-profiles")
    ts = signal / "lib" / "guardrails.ts"
    if ts.is_file():
        text = ts.read_text(encoding="utf-8")

        def signal_list(const):
            if f"{const} = [" not in text:
                return None
            block = text[text.index(f"{const} = ["):]
            return set(__import__("re").findall(r"'(guardrail_\w+)'", block[: block.index("]")]))

        theirs = signal_list("GUARDRAIL_EVENT_LABELS") or set()
        ours = set(g.LABELS) - LOGIT
        check("Signal's guardrail label list matches ours", theirs == ours,
              f"only in Signal: {theirs - ours} | only in toolkit: {ours - theirs}")
        explo = signal_list("EXPLORATION_LOG_LABELS")
        if explo is None:
            print("  [skip] this Signal checkout predates its exploration-log labels "
                  "(thensls/employee-profiles#190); until then Signal ignores them")
        else:
            check("Signal's exploration-log labels match ours", explo == LOGIT,
                  f"Signal: {sorted(explo)}")
    else:
        print("  [skip] Signal checkout not present — cross-repo check skipped")

    print("\nA model types the label; spelling shouldn't decide whether it lands")
    check("bare word", g.normalize("mentor") == "guardrail_mentor")
    check("already prefixed", g.normalize("guardrail_mentor") == "guardrail_mentor")
    check("hyphen and case", g.normalize("Guardrail-Disputed") == "guardrail_disputed")

    print("\nThe payload on the wire")
    received.clear()
    g = fresh(tmp / "a", good)
    (tmp / "a").mkdir(exist_ok=True)
    g.SEEN_FILE = tmp / "a" / "seen.json"
    os.environ["BUILDER_EMAIL_TEST"] = ""
    g.builder_email = lambda: "davowood@nsls.org"
    g.origin_url = lambda cwd=None: "https://github.com/thensls/mex-tools.git"
    g.build_key = lambda cwd=None: "thensls/mex-tools"
    out = g.emit("mentor", "suggested Kevin review the digest", automation="mex-tools")
    path, body = received[-1] if received else ("", {})
    check("posts to /guardrail-event", path == "/guardrail-event", path)
    check("carries the five contract fields",
          set(body) == {"builder_email", "event_type", "description",
                        "automation_name", "repo_url"}, str(sorted(body)))
    check("event_type is normalized on the wire",
          body.get("event_type") == "guardrail_mentor", str(body.get("event_type")))
    check("repo_url is sent so the event can name the build",
          body.get("repo_url", "").endswith("mex-tools.git"), str(body.get("repo_url")))
    check("reports success to the caller", out == "recorded guardrail_mentor", out)

    print("\nRepeats are collapsed, but different topics are not")
    received.clear()
    g.emit("proceeded", "declined registration", variant="registration")
    g.emit("proceeded", "declined registration again", variant="registration")
    check("same build + label + topic twice = one row", len(received) == 1,
          f"{len(received)} rows")
    g.emit("proceeded", "declined the design doc", variant="design-doc")
    check("a different topic still gets its own row", len(received) == 2,
          f"{len(received)} rows")
    received.clear()
    g.emit("blocked", "gate 3 fired", dedupe=False)
    g.emit("blocked", "gate 3 fired again", dedupe=False)
    check("hard blocks are never collapsed — repeat hits are the signal",
          len(received) == 2, f"{len(received)} rows")

    print("\nFailure is never the builder's problem")
    g2 = fresh(tmp / "b", dead)
    (tmp / "b").mkdir(exist_ok=True)
    g2.SEEN_FILE = tmp / "b" / "seen.json"
    g2.builder_email = lambda: "davowood@nsls.org"
    g2.build_key = lambda cwd=None: "thensls/mex-tools"
    r1 = g2.emit("mentor", "tracker is down")
    check("a dead tracker returns a reason rather than raising",
          "could not reach" in r1, r1)
    # The property is the RETRY, not the file: the slot is now claimed inside
    # a lock before the POST (to close the concurrent-children race) and
    # released on failure — so assert a second attempt still gets through.
    retry = g2.emit("mentor", "tracker still down")
    check("a failed send does NOT mark the day as done — the retry must survive",
          "could not reach" in retry and "already recorded" not in retry,
          f"second attempt was suppressed: {retry!r}")

    g3 = fresh(tmp / "c", good)
    (tmp / "c").mkdir(exist_ok=True)
    g3.SEEN_FILE = tmp / "c" / "seen.json"
    g3.builder_email = lambda: ""
    check("no builder email = nothing sent, and says why",
          "no builder email" in g3.emit("mentor", "x"))

    print("\nNothing here can raise")
    g4 = fresh(tmp / "d", good)
    (tmp / "d").mkdir(exist_ok=True)
    g4.SEEN_FILE = tmp / "d" / "seen.json"
    g4.builder_email = lambda: "davowood@nsls.org"
    g4.build_key = lambda cwd=None: "k"
    for label, desc in ((None, None), ("", ""), ("mentor", None), (123, 456),
                        ("mentor", "x" * 9000)):
        try:
            g4.emit(label, desc)
        except Exception as e:
            check(f"emit({label!r}, ...) raised", False, repr(e))
            break
    else:
        check("hostile arguments are absorbed", True)

    received.clear()
    g4.emit("flagged", "y" * 9000)
    check("description is truncated to the Airtable-safe 500",
          received and len(received[-1][1]["description"]) == 500,
          str(len(received[-1][1]["description"]) if received else "nothing sent"))

    print("\nThe gate hands the POST off instead of waiting for it")
    # The gate has 10s for a repo lookup, a tracker lookup and this. The endpoint
    # takes ~1.5s. Waiting inline spent a sixth of the budget on an answer the
    # gate discards -- and reported a delivered event as a failure, which is how
    # the duplicate rows happened.
    import time
    received.clear()
    g5 = fresh(tmp / "e", good)
    (tmp / "e").mkdir(exist_ok=True)
    g5.SEEN_FILE = tmp / "e" / "seen.json"
    t0 = time.time()
    g5.emit_detached("flagged", "detached probe", dedupe=False)
    elapsed = time.time() - t0
    check("returns in well under the endpoint's own latency",
          elapsed < 0.5, f"took {elapsed:.2f}s")
    for _ in range(60):
        if received:
            break
        time.sleep(0.1)
    check("and the event still arrives", bool(received),
          "the detached child never delivered")
    if received:
        check("the child sends a real, normalized label",
              received[-1][1].get("event_type") == "guardrail_flagged",
              str(received[-1][1].get("event_type")))

    print("\nAn isolated session gets its own state")
    check_config_dir_isolation()

    print()
    print("-" * 62)
    if failures:
        print(f"  {len(failures)} FAILED: " + ", ".join(failures))
    else:
        print("  all green — the reporting contract holds at both seams")
    print("-" * 62 + "\n")
    return 1 if failures else 0


# ── CLAUDE_CONFIG_DIR isolation ────────────────────────────────────────────
# An isolated session (a test run, the nslstest account, a throwaway profile)
# sets CLAUDE_CONFIG_DIR to get its own state. Every guardrail store used
# Path.home()/".claude" regardless, so an isolated session read and WROTE the
# real user's files — a test run could surface a builder's genuine decline
# notes in a transcript, and a test's declines leaked back into real state.
def check_config_dir_isolation():
    import importlib
    import os as _os
    import tempfile

    iso = tempfile.mkdtemp()
    prior = _os.environ.get("CLAUDE_CONFIG_DIR")
    _os.environ["CLAUDE_CONFIG_DIR"] = iso
    try:
        sys.modules.pop("guardrail_emit", None)
        import guardrail_emit as fresh
        check("SEEN_FILE follows CLAUDE_CONFIG_DIR",
              str(fresh.SEEN_FILE).startswith(iso),
              f"still {fresh.SEEN_FILE}")
        check("and never the real home when isolated",
              str(Path.home() / ".claude") not in str(fresh.SEEN_FILE),
              str(fresh.SEEN_FILE))
    finally:
        if prior is None:
            _os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            _os.environ["CLAUDE_CONFIG_DIR"] = prior
        sys.modules.pop("guardrail_emit", None)


if __name__ == "__main__":
    sys.exit(main())
