#!/usr/bin/env python3
"""Session start installs the Claude usage collector once, quietly, in the
background, and never costs the session anything.

Popen is always stubbed: nothing here ever runs the real installer.
Plain stdlib; runs under pytest or `python3 hooks/tests/test_collector_bootstrap.py`.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS))

_spec = importlib.util.spec_from_file_location(
    "collector_bootstrap", HOOKS / "collector_bootstrap.py")
cb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cb)

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)


class FakePopen:
    """Records launches; optionally checks the marker already exists."""

    def __init__(self, marker=None, raise_exc=None):
        self.calls = []
        self.marker = marker
        self.marker_seen_at_launch = None
        self.raise_exc = raise_exc

    def __call__(self, args, **kw):
        if self.raise_exc:
            raise self.raise_exc
        if self.marker is not None:
            self.marker_seen_at_launch = (
                json.loads(self.marker.read_text()) if self.marker.exists() else None)
        out = kw.get("stdout")
        try:
            import os
            kw = dict(kw, stdout_ino=os.fstat(out.fileno()).st_ino)
        except Exception:
            pass
        self.calls.append((args, kw))
        return object()


class Box:
    """A scratch HOME + config dir and the kwargs maybe_bootstrap takes."""

    def __init__(self, platform="darwin", env=None):
        self.root = Path(tempfile.mkdtemp(prefix="cb-test-"))
        self.home = self.root / "home"
        self.cfg = self.root / "claude"
        self.home.mkdir()
        self.cfg.mkdir()
        self.platform = platform
        self.env = {"HOME": str(self.home),
                    "LOCALAPPDATA": str(self.root / "LocalAppData")}
        self.env.update(env or {})
        self.marker = self.cfg / ".nsls-collector" / "bootstrap.json"
        self.popen = FakePopen(marker=self.marker)

    def collector_home(self):
        return cb.collector_home(self.platform, self.env)

    def run(self, now=NOW, popen=None):
        return cb.maybe_bootstrap(config_dir=self.cfg, platform=self.platform,
                                  env=self.env, now=now,
                                  popen=popen or self.popen)

    def seed_marker(self, **data):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(json.dumps(data))

    def state(self):
        return json.loads(self.marker.read_text())

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def boxed(fn):
    def wrapper():
        box = Box()
        try:
            fn(box)
        finally:
            box.cleanup()
    wrapper.__name__ = fn.__name__
    return wrapper


# ── where the collector lives ─────────────────────────────────────────────


def test_collector_home_per_platform():
    env = {"HOME": "/Users/x", "LOCALAPPDATA": r"C:\Users\x\AppData\Local"}
    assert cb.collector_home("darwin", env) == Path(
        "/Users/x/Library/Application Support/nsls-collector")
    assert cb.collector_home("win32", env) == Path(
        r"C:\Users\x\AppData\Local") / "nsls-collector"
    assert cb.collector_home("linux", env) is None
    assert cb.collector_home("win32", {"HOME": "/x"}) is None  # no LOCALAPPDATA


# ── when it launches ──────────────────────────────────────────────────────


@boxed
def test_not_installed_launches_detached_with_marker_written_first(box):
    assert box.run() == "launched"
    assert len(box.popen.calls) == 1
    args, kw = box.popen.calls[0]
    assert args[:2] == ["/bin/bash", "-c"], args
    assert args[2] == ("curl -fsSL https://signal.nsls.org/api/collector/dist/install.sh"
                       " | bash"), args
    assert kw.get("start_new_session") is True
    assert kw.get("stdin") is subprocess.DEVNULL
    assert kw.get("stdout") is not None and kw.get("stderr") is not None
    seen = box.popen.marker_seen_at_launch
    assert seen == {"attempted_at": iso(NOW), "attempts": 1}, seen
    assert box.state() == {"attempted_at": iso(NOW), "attempts": 1}


def _plist(box):
    agents = box.home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "org.nsls.collector.plist").write_text("<plist/>")


def _launchd_says(loaded):
    real = cb._launch_agent_loaded
    cb._launch_agent_loaded = lambda: loaded
    return real


@boxed
def test_installed_config_and_schedule_mean_no_launch(box):
    home = box.collector_home()
    home.mkdir(parents=True)
    (home / "config.json").write_text("{}")
    _plist(box)
    real = _launchd_says(True)
    try:
        assert box.run() == "installed"
    finally:
        cb._launch_agent_loaded = real
    assert box.popen.calls == []
    assert not box.marker.exists()


@boxed
def test_plist_that_launchd_has_not_loaded_is_a_half_install(box):
    home = box.collector_home()
    home.mkdir(parents=True)
    (home / "config.json").write_text("{}")
    _plist(box)
    real = _launchd_says(False)
    try:
        assert box.run() == "launched"
    finally:
        cb._launch_agent_loaded = real


def test_mac_schedule_check_asks_launchd_for_this_user():
    import os
    calls = []

    def fake_run(code=None, exc=None):
        def run(args, **kw):
            calls.append(args)
            if exc:
                raise exc
            return subprocess.CompletedProcess(args, code)
        return run

    real = cb.subprocess.run
    try:
        cb.subprocess.run = fake_run(code=0)
        assert cb._launch_agent_loaded() is True
        assert calls[-1] == ["launchctl", "print", f"gui/{os.getuid()}/org.nsls.collector"]
        cb.subprocess.run = fake_run(code=113)
        assert cb._launch_agent_loaded() is False
        cb.subprocess.run = fake_run(exc=subprocess.TimeoutExpired("launchctl", 1))
        assert cb._launch_agent_loaded() is True
    finally:
        cb.subprocess.run = real


@boxed
def test_config_without_schedule_is_a_half_install_and_is_repaired(box):
    home = box.collector_home()
    home.mkdir(parents=True)
    (home / "config.json").write_text("{}")
    assert box.run() == "launched"
    assert len(box.popen.calls) == 1


def test_windows_schedule_check_asks_schtasks_and_trusts_it_only_when_it_answers():
    calls = []

    def fake_run(code=None, exc=None):
        def run(args, **kw):
            calls.append(args)
            if exc:
                raise exc
            return subprocess.CompletedProcess(args, code)
        return run

    real = cb.subprocess.run
    try:
        cb.subprocess.run = fake_run(code=0)
        assert cb.schedule_registered("win32", {}) is True
        assert calls[-1] == ["schtasks", "/Query", "/TN", "NSLS Collector"]
        cb.subprocess.run = fake_run(code=1)
        assert cb.schedule_registered("win32", {}) is False
        cb.subprocess.run = fake_run(exc=FileNotFoundError("schtasks"))
        assert cb.schedule_registered("win32", {}) is True
        cb.subprocess.run = fake_run(exc=subprocess.TimeoutExpired("schtasks", 1))
        assert cb.schedule_registered("win32", {}) is True
    finally:
        cb.subprocess.run = real


@boxed
def test_fresh_evidence_counts_as_installed(box):
    d = box.cfg / ".nsls-collector"
    d.mkdir(parents=True)
    (d / "reported.json").write_text(json.dumps(
        {"machine_id": "m", "at": iso(datetime.now(timezone.utc) - timedelta(hours=1)),
         "version": "1"}))
    assert box.run() == "installed"
    assert box.popen.calls == []


def test_opt_out_never_launches_or_writes():
    for value in ("1", "true", "YES"):
        box = Box(env={"NSLS_COLLECTOR_OPTOUT": value})
        try:
            assert box.run() == "optout", value
            assert box.popen.calls == []
            assert not box.marker.exists()
        finally:
            box.cleanup()
    box = Box(env={"NSLS_COLLECTOR_OPTOUT": "0"})
    try:
        assert box.run() == "launched"
    finally:
        box.cleanup()


def test_linux_is_skipped():
    box = Box(platform="linux")
    try:
        assert box.run() == "unsupported"
        assert box.popen.calls == []
        assert not box.marker.exists()
    finally:
        box.cleanup()


def test_windows_command_matches_the_self_serve_link():
    box = Box(platform="win32")
    try:
        assert box.run() == "launched"
        args, kw = box.popen.calls[0]
        assert args == [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command",
            "iwr -UseBasicParsing https://signal.nsls.org/api/collector/dist/install.ps1 | iex",
        ], args
    finally:
        box.cleanup()


def test_base_url_override_and_rejection():
    box = Box(env={"NSLS_COLLECTOR_BASE_URL": "http://127.0.0.1:8787/"})
    try:
        assert box.run() == "launched"
        assert box.popen.calls[0][0][2] == (
            "curl -fsSL http://127.0.0.1:8787/api/collector/dist/install.sh | bash")
    finally:
        box.cleanup()
    # Anything that is not a plain http(s) origin is refused, never shelled.
    for bad in ("https://x.org; rm -rf ~", "file:///etc/passwd", "https://a b",
                "https://x.org/$(id)"):
        box = Box(env={"NSLS_COLLECTOR_BASE_URL": bad})
        try:
            assert box.run() == "error", bad
            assert box.popen.calls == [], bad
        finally:
            box.cleanup()


# ── throttle and cap ──────────────────────────────────────────────────────


@boxed
def test_at_most_one_attempt_per_24h(box):
    assert box.run(now=NOW) == "launched"
    assert box.run(now=NOW + timedelta(hours=23)) == "throttled"
    assert len(box.popen.calls) == 1
    assert box.run(now=NOW + timedelta(hours=24, minutes=1)) == "launched"
    assert box.state()["attempts"] == 2


@boxed
def test_stops_after_five_attempts_and_logs_it_once(box):
    t = NOW
    for i in range(5):
        assert box.run(now=t) == "launched", i
        t += timedelta(days=1, minutes=1)
    assert box.run(now=t) == "capped"
    assert box.run(now=t + timedelta(days=3)) == "capped"
    assert len(box.popen.calls) == 5
    assert box.state()["attempts"] == 5
    log = (box.cfg / ".nsls-collector" / "bootstrap.log").read_text()
    assert log.count("giving up") == 1, log


@boxed
def test_future_marker_does_not_block_forever(box):
    box.seed_marker(attempted_at=iso(NOW + timedelta(days=30)), attempts=1)
    assert box.run() == "launched"


@boxed
def test_corrupt_marker_is_treated_as_no_attempts(box):
    box.marker.parent.mkdir(parents=True)
    box.marker.write_text("{not json")
    assert box.run() == "launched"
    assert box.state()["attempts"] == 1


@boxed
def test_concurrent_session_holding_the_lock_skips(box):
    box.marker.parent.mkdir(parents=True)
    (box.marker.parent / "bootstrap.lock").write_text("")
    assert box.run() == "busy"
    assert box.popen.calls == []


@boxed
def test_stale_lock_is_taken_over(box):
    box.marker.parent.mkdir(parents=True)
    lock = box.marker.parent / "bootstrap.lock"
    lock.write_text("")
    import os
    import time
    old = time.time() - 1800  # the lock's age is judged by the real clock
    os.utime(lock, (old, old))
    assert box.run() == "launched"
    assert not lock.exists()


def _in_subprocess(box, timeout=15):
    """Run maybe_bootstrap in a child with Popen stubbed; a hang fails the test."""
    code = (
        "import importlib.util,sys,json;"
        f"sys.path.insert(0,{str(HOOKS)!r});"
        f"s=importlib.util.spec_from_file_location('cb',{str(HOOKS / 'collector_bootstrap.py')!r});"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "from datetime import datetime,timezone;"
        f"print(m.run(config_dir={str(box.cfg)!r},platform='darwin',"
        f"env={box.env!r},popen=lambda *a,**k: None))"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, timeout=timeout)
    return proc.stdout.strip()


@boxed
def test_fifo_marker_does_not_hang(box):
    import os
    if not hasattr(os, "mkfifo"):
        return
    box.marker.parent.mkdir(parents=True)
    os.mkfifo(box.marker)
    assert _in_subprocess(box) in ("launched", "error")


@boxed
def test_fifo_log_does_not_hang(box):
    import os
    if not hasattr(os, "mkfifo"):
        return
    box.marker.parent.mkdir(parents=True)
    os.mkfifo(box.marker.parent / "bootstrap.log")
    assert _in_subprocess(box) == "launched"


@boxed
def test_stale_lock_takeover_never_steals_a_live_lock(box):
    """Session B judged the old lock stale; session A has since replaced it
    with a live one. B's takeover must notice and stand down, leaving A's."""
    import os
    import time
    box.marker.parent.mkdir(parents=True)
    lock = box.marker.parent / "bootstrap.lock"
    lock.write_text("")
    old = time.time() - 1800
    os.utime(lock, (old, old))
    stale_ino = lock.stat().st_ino
    # A replaces it with a live lock between B's stat and B's rename. Written
    # beside the old one and renamed over it, so both exist at once and the
    # filesystem can't hand the stale inode straight back: Linux reuses a freed
    # inode immediately (macOS happens not to), which failed this on CI.
    live = lock.with_name("bootstrap.lock.new")
    live.write_text("")
    os.replace(live, lock)
    assert lock.stat().st_ino != stale_ino
    assert cb._take_over_stale(lock, stale_ino) is False
    assert lock.exists(), "the live lock must be put back"


# ── logging ───────────────────────────────────────────────────────────────


@boxed
def test_logs_to_collector_home_when_it_exists(box):
    home = box.collector_home()
    home.mkdir(parents=True)  # exists, but no config.json yet
    assert box.run() == "launched"
    _, kw = box.popen.calls[0]
    assert kw["stdout_ino"] == (home / "bootstrap.log").stat().st_ino


@boxed
def test_logs_to_marker_dir_otherwise(box):
    assert box.run() == "launched"
    _, kw = box.popen.calls[0]
    assert kw["stdout_ino"] == (box.cfg / ".nsls-collector" / "bootstrap.log").stat().st_ino


# ── never costs the session anything ─────────────────────────────────────


@boxed
def test_popen_failure_is_swallowed(box):
    assert box.run(popen=FakePopen(raise_exc=OSError("no bash"))) == "error"
    # The attempt still counts, so a machine that cannot launch stops at five.
    assert box.state()["attempts"] == 1


def test_unwritable_config_dir_is_swallowed():
    box = Box()
    try:
        box.cfg.rmdir()
        box.cfg.write_text("a file, not a directory")
        assert box.run() == "error"
        assert box.popen.calls == []
    finally:
        box.cleanup()


def test_run_never_raises_and_prints_nothing():
    import io
    from contextlib import redirect_stdout, redirect_stderr
    box = Box()
    try:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cb.run(config_dir=box.cfg, platform=box.platform, env=box.env,
                   popen=box.popen)
            cb.run(config_dir=box.cfg, platform=box.platform, env=box.env,
                   popen=FakePopen(raise_exc=RuntimeError("boom")))
            shutil.rmtree(box.cfg)
            box.cfg.write_text("not a directory")
            cb.run(config_dir=box.cfg, platform=box.platform, env=box.env,
                   popen=box.popen)
        assert out.getvalue() == "" and err.getvalue() == ""
    finally:
        box.cleanup()


# ── wiring ────────────────────────────────────────────────────────────────


def test_session_start_main_calls_bootstrap_last():
    """main() runs the bootstrap after everything else, inside its own guard."""
    spec = importlib.util.spec_from_file_location("ss_cb", HOOKS / "session-start.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    order = []
    names = ["git_pull", "run_plugin_migration", "ensure_plugin_fresh",
             "sync_pointers", "emit_guardrails_context", "replay_failed_ping",
             "session_ping", "bootstrap_collector"]
    saved = {n: getattr(hook, n) for n in names}
    try:
        for n in names:
            setattr(hook, n, (lambda n: lambda *a, **k: order.append(n))(n))
        hook.main()
    finally:
        for n, f in saved.items():
            setattr(hook, n, f)
    assert order[-1] == "bootstrap_collector", order

    # And the real wrapper swallows anything the module throws.
    saved_mod = sys.modules.get("collector_bootstrap")
    try:
        class Boom:
            @staticmethod
            def run(**k):
                raise RuntimeError("boom")
        sys.modules["collector_bootstrap"] = Boom
        hook.bootstrap_collector()
    finally:
        if saved_mod is None:
            sys.modules.pop("collector_bootstrap", None)
        else:
            sys.modules["collector_bootstrap"] = saved_mod


# test_guardrail_gate_untouched() removed here — the second copy of it.
#
# It asserts guardrail-gate.py is byte-identical to the merge base with main.
# That is a true statement about the collector change that shipped it and a
# false one about this repository: committed to the suite, it asserts it of
# every branch from now on, so any PR that legitimately edits the gate fails a
# test about somebody else's scope. The same assertion arrived in
# test_collector_evidence.py and is removed there for the same reason.
#
# The intent is worth keeping and belongs in the PR, where a reviewer seeing a
# collector diff touch the gate can ask why.


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
            print(f"FAIL {name}: {exc!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
