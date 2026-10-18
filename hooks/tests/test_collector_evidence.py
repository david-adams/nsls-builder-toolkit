#!/usr/bin/env python3
"""The collector's evidence file retires two toolkit credits on its machine.

When `<config dir>/.nsls-collector/reported.json` is fresh, the session ping
tells the tracker `collector_backed: true` (so the tracker skips only the
daily-session credit) and the skill hook posts nothing, because the collector
already reports both. On any other machine, including one whose collector went
quiet eight days ago, both behave exactly as before.

Plain stdlib, no pytest import: runs on a stock install with
`python3 hooks/tests/test_collector_evidence.py`, and pytest collects it too.
"""

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "collector_evidence", HOOKS / "collector_evidence.py")
evidence = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(evidence)

_spec = importlib.util.spec_from_file_location(
    "session_start_hook_ce", HOOKS / "session-start.py")
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def write_evidence(config_dir, content):
    d = Path(config_dir) / ".nsls-collector"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "reported.json"
    if isinstance(content, (dict, list)):
        content = json.dumps(content)
    if isinstance(content, bytes):
        p.write_bytes(content)
    else:
        p.write_text(content, encoding="utf-8")
    return p


def record(at, machine_id="m-123"):
    return {"machine_id": machine_id, "at": at, "version": "1.0.0"}


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def at1h():
    return iso(ago(hours=1))


# Each case: (name, file content or None for absent, expected fresh(), now).
# `now` pins the clock (None = real time); the bash run pins it with a stub
# `date` on PATH, so every implementation sees the same instant.
CASES = [
    ("fresh", lambda: record(at1h()), True, None),
    ("fresh_fractional_offset", lambda: record(
        ago(days=6).strftime("%Y-%m-%dT%H:%M:%S.123456+00:00")), True, None),
    ("slightly_future_clock_skew", lambda: record(iso(ago(hours=-12))), True, None),
    ("pretty_printed_any_key_order", lambda: json.dumps(
        {"version": "1.0.0", "at": at1h(), "machine_id": "m-1"}, indent=2), True, None),
    ("eight_days_old", lambda: record(iso(ago(days=8))), False, None),
    ("missing", None, False, None),
    ("malformed", lambda: '{"machine_id": "m-1", "at": ', False, None),
    ("trailing_comma", lambda: '{"machine_id":"m","at":"%s","version":"1",}' % at1h(),
     False, None),
    ("not_an_object", lambda: [record(at1h())], False, None),
    ("far_future", lambda: record(iso(ago(days=-2))), False, None),
    ("numeric_machine_id", lambda: record(at1h(), machine_id=123), False, None),
    ("empty_machine_id", lambda: record(at1h(), machine_id=""), False, None),
    ("missing_machine_id", lambda: {"at": at1h(), "version": "1"}, False, None),
    # KTD3: EXACTLY {machine_id, at, version}.
    ("missing_version", lambda: {"machine_id": "m", "at": at1h()}, False, None),
    ("numeric_version", lambda: {"machine_id": "m", "at": at1h(), "version": 1},
     False, None),
    ("extra_key", lambda: dict(record(at1h()), extra="x"), False, None),
    # Ambiguity fails closed: duplicate keys, nested decoys.
    ("duplicate_at_fresh_then_stale", lambda:
        '{"machine_id":"m","at":"%s","at":"%s","version":"1"}'
        % (at1h(), iso(ago(days=8))), False, None),
    ("duplicate_at_stale_then_fresh", lambda:
        '{"machine_id":"m","at":"%s","at":"%s","version":"1"}'
        % (iso(ago(days=8)), at1h()), False, None),
    ("nested_decoy", lambda: {"machine_id": "", "at": iso(ago(days=8)),
                              "version": {"machine_id": "m", "at": at1h()}},
     False, None),
    ("escaped_value", lambda: '{"machine_id":"m\\u0041","at":"%s","version":"1"}'
     % at1h(), False, None),
    ("non_ascii_value", lambda: '{"machine_id":"mé","at":"%s","version":"1"}'
     % at1h(), False, None),
    ("nul_byte", lambda: ('{"machine_id":"m\x00","at":"%s","version":"1"}'
                          % at1h()).encode(), False, None),
    ("non_utc_offset", lambda: record(
        ago(hours=1).strftime("%Y-%m-%dT%H:%M:%S-05:00")), False, None),
    ("date_only", lambda: record(ago(hours=1).strftime("%Y-%m-%d")), False, None),
    ("garbage_at", lambda: record("yesterday"), False, None),
    ("oversized", lambda: dict(record(at1h()), pad="x" * 5000), False, None),
    # Calendar validity, pinned so a rolled-over date would otherwise be fresh.
    ("feb_30", lambda: record("2026-02-30T12:00:00Z"), False, utc(2026, 3, 2, 12)),
    ("feb_29_non_leap_century", lambda: record("2100-02-29T12:00:00Z"), False,
     utc(2100, 3, 2, 12)),
    ("feb_29_leap_year", lambda: record("2028-02-29T12:00:00Z"), True,
     utc(2028, 3, 1, 12)),
    ("apr_31", lambda: record("2026-04-31T12:00:00Z"), False, utc(2026, 5, 2, 12)),
    ("hour_24", lambda: record("2026-05-01T24:00:00Z"), False, utc(2026, 5, 2, 12)),
    # Fractions are truncated, which moves `at` EARLIER and makes the file look
    # OLDER, so the 7-day window can only close early, never late. 0.1 s past
    # seven days must already read stale.
    ("fraction_just_past_seven_days", lambda: record("2026-09-01T12:00:00.900Z"),
     False, utc(2026, 9, 8, 12, 0, 1)),
    ("fraction_just_inside_seven_days", lambda: record("2026-09-01T12:00:00.900Z"),
     True, utc(2026, 9, 8, 12, 0, 0)),
]


def _with_case(content_fn):
    tmp = Path(tempfile.mkdtemp(prefix="ce-test-"))
    if content_fn is not None:
        write_evidence(tmp, content_fn())
    return tmp


# ── collector_evidence.fresh() ─────────────────────────────────────────────


def test_fresh_contract_cases():
    for name, content_fn, expected, now in CASES:
        tmp = _with_case(content_fn)
        try:
            got = evidence.fresh(config_dir=tmp, now=now)
            assert got is expected, f"{name}: fresh() -> {got!r}, want {expected!r}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_fresh_does_not_block_on_a_fifo():
    """A FIFO at reported.json must read as not fresh, not hang session start."""
    if not hasattr(os, "mkfifo"):
        return
    tmp = Path(tempfile.mkdtemp(prefix="ce-test-"))
    try:
        d = tmp / ".nsls-collector"
        d.mkdir()
        os.mkfifo(d / "reported.json")
        code = ("import importlib.util,sys;"
                f"s=importlib.util.spec_from_file_location('ce',{str(HOOKS / 'collector_evidence.py')!r});"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                f"print(m.fresh(config_dir={str(tmp)!r}))")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, timeout=10)
        assert proc.stdout.strip() == "False", proc.stdout + proc.stderr
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_fresh_honours_claude_config_dir():
    tmp = _with_case(lambda: record(iso(ago(hours=1))))
    old = os.environ.get("CLAUDE_CONFIG_DIR")
    try:
        os.environ["CLAUDE_CONFIG_DIR"] = str(tmp)
        assert evidence.fresh() is True
        assert evidence.evidence_path() == tmp / ".nsls-collector" / "reported.json"
    finally:
        if old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_fresh_never_raises_on_a_directory_or_binary():
    tmp = Path(tempfile.mkdtemp(prefix="ce-test-"))
    try:
        (tmp / ".nsls-collector" / "reported.json").mkdir(parents=True)
        assert evidence.fresh(config_dir=tmp) is False
        shutil.rmtree(tmp / ".nsls-collector")
        p = write_evidence(tmp, "")
        p.write_bytes(b"\xff\xfe\x00{garbage")
        assert evidence.fresh(config_dir=tmp) is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── session_ping payload ───────────────────────────────────────────────────


def _ping_payload(config_dir):
    """Run session_ping with the network and env stubbed; return the body sent."""
    sent = []
    saved = {k: getattr(hook, k) for k in
             ("CONFIG_DIR", "read_env", "_post_session_ping", "_retire_ping_payload")}
    try:
        hook.CONFIG_DIR = Path(config_dir)
        hook.read_env = lambda key: {"BUILDER_EMAIL": "builder@nsls.org",
                                     "GITHUB_USERNAME": "builder"}.get(key, "")
        hook._post_session_ping = lambda body, *a, **k: sent.append(dict(body)) or {}
        hook._retire_ping_payload = lambda **k: None
        hook.session_ping()
    finally:
        for k, v in saved.items():
            setattr(hook, k, v)
    assert len(sent) == 1, f"expected exactly one ping, got {len(sent)}"
    return sent[0]


def test_session_ping_flags_collector_backed_when_fresh():
    tmp = _with_case(lambda: record(iso(ago(hours=1))))
    try:
        body = _ping_payload(tmp)
        assert body.get("collector_backed") is True, body
        # Everything else in the ping is unchanged.
        assert body["builder_email"] == "builder@nsls.org"
        assert body["github_username"] == "builder"
        assert set(body) == {"builder_email", "toolkit", "github_username",
                             "platform", "toolkit_version", "collector_backed"}, body
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_session_ping_unflagged_when_stale_missing_or_malformed():
    for name in ("eight_days_old", "missing", "malformed", "far_future",
                 "numeric_machine_id"):
        content_fn = next(c for n, c, _, _ in CASES if n == name)
        tmp = _with_case(content_fn)
        try:
            body = _ping_payload(tmp)
            assert "collector_backed" not in body, f"{name}: {body}"
            assert set(body) == {"builder_email", "toolkit", "github_username",
                                 "platform", "toolkit_version"}, f"{name}: {body}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ── skill-event.sh ─────────────────────────────────────────────────────────


def _stub(bindir, name, body):
    p = bindir / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_skill_event(config_dir, now=None):
    """Run skill-event.sh with a stub curl on PATH. Returns (rc, curl_called).

    With `now`, a stub `date` answers `date -u +%s` with that instant."""
    bindir = Path(tempfile.mkdtemp(prefix="ce-bin-"))
    log = bindir / "curl.log"
    _stub(bindir, "curl", f'echo "$@" >> "{log}"\nexit 0\n')
    if now is not None:
        real_date = shutil.which("date") or "/bin/date"
        _stub(bindir, "date",
              f'if [ "$*" = "-u +%s" ]; then echo {int(now.timestamp())}; '
              f'else exec {real_date} "$@"; fi\n')
    envdir = Path(config_dir) / "local-plugins" / "nsls-personal-toolkit"
    envdir.mkdir(parents=True, exist_ok=True)
    (envdir / ".env").write_text("BUILDER_EMAIL=builder@nsls.org\n")
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env.get('PATH', '')}"
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    env["NSLS_TRACKER_URL"] = "http://127.0.0.1:9"
    env.pop("SKILL_EVENT_VERBOSE", None)
    try:
        proc = subprocess.run(
            ["bash", str(HOOKS / "skill-event.sh")],
            input='{"tool_name":"Skill","tool_input":{"skill":"nsls-builder-toolkit:gws"}}',
            capture_output=True, text=True, env=env, timeout=20)
        return proc.returncode, log.exists()
    finally:
        shutil.rmtree(bindir, ignore_errors=True)


def test_skill_event_sh_matches_fresh_for_every_case():
    """The bash check is a second implementation of the same contract, so it
    runs against every fixture the Python one does."""
    for name, content_fn, expected_fresh, now in CASES:
        tmp = _with_case(content_fn)
        try:
            rc, posted = _run_skill_event(tmp, now)
            assert rc == 0, f"{name}: exit {rc}"
            assert posted is (not expected_fresh), (
                f"{name}: curl {'called' if posted else 'not called'}, "
                f"evidence fresh={expected_fresh}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def test_skill_event_sh_does_not_block_on_a_fifo():
    if not hasattr(os, "mkfifo"):
        return
    tmp = Path(tempfile.mkdtemp(prefix="ce-test-"))
    try:
        (tmp / ".nsls-collector").mkdir()
        os.mkfifo(tmp / ".nsls-collector" / "reported.json")
        rc, posted = _run_skill_event(tmp)  # subprocess timeout=20 catches a hang
        assert rc == 0 and posted, f"rc={rc} posted={posted}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# test_guardrail_gate_untouched() was removed here, deliberately.
#
# It asserted that guardrail-gate.py is byte-identical to the merge base with
# main. That is a true statement about the collector change that introduced it
# (U6 had no business touching the gate) but it is not a property of this
# repository, and as a committed test it asserts it of every branch forever:
# any PR that legitimately edits the gate fails a test about somebody else's
# scope. This stack edits the gate on purpose, in four commits, so it is the
# first branch to hit it.
#
# The intent was worth keeping and belongs in the PR, not in the suite — a
# reviewer reading a collector diff that touches the gate should ask why. The
# other eight checks in this file are real contracts and are untouched.


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
