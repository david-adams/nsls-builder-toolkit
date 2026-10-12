"""The exploration log's checkpoint: shared state and rules.

Used by logit-checkpoint.py (the UserPromptSubmit and Stop hooks) and log-it.py
(the helper Claude runs to answer). Plain stdlib, fails open everywhere.

WHY A CHECKPOINT. "Log it" asked Claude to notice, unprompted and mid-task,
the moment a build became worth credit. Nothing triggered it, nothing checked
it happened, and from 2026-09-20 Claude could not even see the instruction (the
session-start policy outgrew Claude Code's hook-output cap). It was offered
zero times, org-wide. So the moment now comes from a hook:

  * the hook decides WHEN to ask, from how much real work the session has done
    (changes made, builder messages, a return to the same build);
  * Claude decides WHETHER the work is worth credit or might collide with a
    colleague's, because only Claude has the conversation. Volume is never
    the trigger for an offer, only for the question.

The question goes in at the builder's next message, where they never see it.
Claude answers by running the helper: an offer (shown to the builder), or "not
yet" (shown to no one). An unanswered question gets one reminder at the end of
the turn, then is recorded as ignored. Every outcome is recorded somewhere, so
a skipped question is never a silent one.

BUILD IDENTITY. A git remote is a build: one answer settles it. A plain folder
can hold several builds, so it is never settled wholesale: the question lists
what has already been settled there and Claude judges whether today's work is
one of those.

MEMORY IS LOCAL. Declines and logs live in this config dir, like every other
guardrail memory. A second machine or a fresh Cowork VM may ask once more.
"""
import base64
import json
import os
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# When a session becomes eligible to be asked (not when an offer is made).
MIN_CHANGES = 6          # edits, commands, connector writes in this session
MIN_PROMPTS = 3          # builder messages in this session
RETURN_CHANGES = 1       # a later session on a build that already had a real one
NOT_YET_COOLDOWN_DAYS = 7
NOT_YET_LIMIT = 3        # per build in NOT_YET_WINDOW_DAYS; after that, stop asking
NOT_YET_WINDOW_DAYS = 30
FOLDER_QUIET_DAYS = 1      # a folder is quiet this long after a not-yet there
MAX_TRANSCRIPT_BYTES = 64 * 1024 * 1024
# Read at most this much new transcript per prompt; a resumed session with a
# huge transcript catches up over a few messages instead of stalling one.
SCAN_BUDGET_BYTES = 4 * 1024 * 1024
SESSION_TTL_DAYS = 14
MAX_BUILDS = 300

CHANGE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash", "PowerShell"})
# Connector tools that change something. Matched on the tool's own name (the
# part after the server), so read-only calls (list, get, search, query, read,
# fetch) never count as work.
_WRITE_VERB = re.compile(
    r"(^|_)(create|update|insert|send|post|publish|upsert|delete|write|edit|append|add|set|"
    r"move|batch|replace|submit|upload|share|merge|apply|draft|schedule|complete)(_|$)",
    re.I,
)


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def state_path():
    return config_dir() / ".nsls-logit-state.json"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ── state ───────────────────────────────────────────────────────────────────

def load():
    p = state_path()
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return {"sessions": {}, "builds": {}, "pending": []}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            data.setdefault("sessions", {})
            data.setdefault("builds", {})
            data.setdefault("pending", [])
            return data
    except Exception:
        pass
    # Unreadable: keep it for diagnosis rather than silently overwriting the
    # declines and queued logs it may still hold.
    try:
        p.replace(p.with_name(p.name + f".corrupt-{int(time.time())}"))
    except Exception:
        pass
    return {"sessions": {}, "builds": {}, "pending": []}


def save(data):
    try:
        _prune(data)
        p = state_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def _prune(data):
    cutoff = datetime.now(timezone.utc) - timedelta(days=SESSION_TTL_DAYS)
    for sid in [s for s, v in data["sessions"].items()
                if (_parse(v.get("updated", "")) or cutoff) < cutoff]:
        data["sessions"].pop(sid, None)
    if len(data["builds"]) > MAX_BUILDS:
        def newest(k):
            b = data["builds"][k]
            stamps = [n.get("at", "") for n in b.get("names", {}).values()] + [b.get("touched", "")]
            return max(stamps or [""])
        for stale in sorted(data["builds"], key=newest)[: len(data["builds"]) - MAX_BUILDS]:
            data["builds"].pop(stale, None)


class locked:
    """Exclusive lock around load-modify-save, on every OS.

    flock where it exists; elsewhere (Windows) a lock file created with
    O_CREAT|O_EXCL, which is atomic everywhere, taken over when stale. If the
    lock cannot be had within LOCK_WAIT it falls open: a lost update re-asks a
    question once, the cheaper wrong than a hook that hangs.
    """
    LOCK_WAIT = 3.0
    STALE = 15.0

    def __enter__(self):
        self.fh = None
        self.excl = None
        lock = state_path().with_suffix(".lock")
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            import fcntl
            self.fh = open(lock, "w")
            deadline = time.monotonic() + self.LOCK_WAIT
            while True:
                try:
                    fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        self.fh.close()
                        self.fh = None  # fall open rather than hold up the prompt
                        break
                    time.sleep(0.05)
        except ImportError:
            deadline = time.monotonic() + self.LOCK_WAIT
            excl = lock.with_suffix(".lockx")
            while time.monotonic() < deadline:
                try:
                    os.close(os.open(str(excl), os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                    self.excl = excl
                    break
                except FileExistsError:
                    try:
                        if time.time() - excl.stat().st_mtime > self.STALE:
                            excl.unlink()
                            continue
                    except Exception:
                        pass
                    time.sleep(0.05)
                except Exception:
                    break
        except Exception:
            pass
        self.data = load()
        return self.data

    def __exit__(self, *exc):
        save(self.data)
        for release in (lambda: self.fh and self.fh.close(),
                        lambda: self.excl and self.excl.unlink()):
            try:
                release()
            except Exception:
                pass
        return False


def session(data, sid):
    s = data["sessions"].setdefault(sid, {})
    s.setdefault("offset", 0)
    s.setdefault("changes", 0)
    s.setdefault("prompts", 0)
    s.setdefault("asked_at", "")
    s.setdefault("answer", "")
    s.setdefault("reminded", False)
    s.setdefault("counted", False)
    s.setdefault("changes_at_answer", 0)
    s["updated"] = now_iso()
    return s


def build(data, key):
    b = data["builds"].setdefault(key, {})
    b.setdefault("names", {})
    b.setdefault("work_sessions", 0)
    b.setdefault("not_yet", [])
    b["touched"] = now_iso()
    return b


# ── build identity ──────────────────────────────────────────────────────────

def _git(args, cwd):
    try:
        out = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=2)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def build_key(cwd):
    """`git:<host>/<owner>/<repo>` for a repo with a remote, `local:<root>` for
    one without, `dir:<folder>` otherwise. Same remote parsing as guardrail-memory.

    Only `git:` keys are remote slugs. A local root used to share the prefix,
    and on Windows `git:C:/work/project` became the repo link
    `https://C:/work/project`."""
    cwd = cwd or os.getcwd()
    root = _git(["rev-parse", "--show-toplevel"], cwd)
    if root:
        remote = _git(["remote", "get-url", "origin"], root)
        m = (re.search(r"(?:@|://)([^/:@]+)(?::\d+)?[:/]+(.+?)(?:\.git)?/?$", remote)
             if remote else None)
        if m:
            return f"git:{m.group(1).lower()}/{m.group(2).lower()}"
        return "local:" + Path(root).resolve().as_posix()
    try:
        return "dir:" + Path(cwd).resolve().as_posix()
    except Exception:
        return "dir:" + str(cwd)


def is_folder(key):
    return key.startswith("dir:")


def label_for(key):
    return key.split(":", 1)[1] if ":" in key else key


# ── work in this session, from the transcript ───────────────────────────────

def is_change(tool_name):
    name = tool_name or ""
    if name in CHANGE_TOOLS:
        return True
    if name.startswith("mcp__"):
        return bool(_WRITE_VERB.search(name.rsplit("__", 1)[-1]))
    return False


def _is_builder_message(entry):
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content.strip())
    if isinstance(content, list):
        kinds = {c.get("type") for c in content if isinstance(c, dict)}
        return "text" in kinds and "tool_result" not in kinds
    return False


def scan(sess, transcript_path):
    """Count new changes and builder messages since the last scan. Reads only the
    bytes appended since then; a shorter file means a new transcript."""
    try:
        p = Path(transcript_path)
        size = p.stat().st_size
    except Exception:
        return
    if size > MAX_TRANSCRIPT_BYTES:
        return
    if size < sess.get("offset", 0):
        sess.update(offset=0, changes=0, prompts=0)
    try:
        with open(p, "rb") as fh:
            fh.seek(sess.get("offset", 0))
            chunk = fh.read(SCAN_BUDGET_BYTES)
    except Exception:
        return
    end = chunk.rfind(b"\n")
    if end < 0:
        # One record bigger than the whole budget (a huge tool result, say):
        # step past it, or every later prompt rereads the same bytes and the
        # count never moves again. A shorter chunk is a line still being
        # written, so wait for it.
        if len(chunk) >= SCAN_BUDGET_BYTES:
            sess["offset"] = sess.get("offset", 0) + len(chunk)
        return
    for raw in chunk[: end + 1].splitlines():
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception:
            continue
        if entry.get("type") == "assistant" and not entry.get("isSidechain"):
            for c in (entry.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use" and is_change(c.get("name")):
                    sess["changes"] += 1
        elif _is_builder_message(entry):
            sess["prompts"] += 1
    sess["offset"] = sess.get("offset", 0) + end + 1


# ── when to ask ─────────────────────────────────────────────────────────────

SETTLED = ("declined", "logged", "logged-pending", "settled")


def settled_names(b):
    return {n: v for n, v in b.get("names", {}).items() if v.get("outcome") in SETTLED}


def recent_not_yets(b, days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return [t for t in b.get("not_yet", []) if (_parse(t) or cutoff) >= cutoff]


def due(sess, b, key):
    """Should the question be asked at this message?"""
    if sess.get("asked_at"):
        # At most once per session. Re-asking after a "not yet" every few
        # changes looked reasonable until a replay of 30 days of real sessions
        # put 50 questions into one long one; a later session asks again.
        return False
    if len(recent_not_yets(b, NOT_YET_WINDOW_DAYS)) >= NOT_YET_LIMIT and not is_folder(key):
        return False
    # A folder hosts a new task most sessions, so a "not yet" there quiets the
    # folder for a day rather than three-strikes the folder for a month.
    if is_folder(key) and recent_not_yets(b, FOLDER_QUIET_DAYS):
        return False
    if not is_folder(key) and settled_names(b):
        return False  # a repo is one build, and it has been answered
    if sess["changes"] >= MIN_CHANGES and sess["prompts"] >= MIN_PROMPTS:
        return True
    # Coming back to a build is a signal only where a key IS one build: a repo.
    # A folder like ~/Documents/Claude hosts a different task every session, and
    # replaying 30 days of real sessions showed this rule asking at nearly every
    # session there. Only EARLIER sessions count: this session's own stretch of
    # work is already in work_sessions once note_work() has seen it.
    earlier = b.get("work_sessions", 0) - (1 if sess.get("counted") else 0)
    returning = (not is_folder(key) and earlier >= 1
                 and not recent_not_yets(b, NOT_YET_COOLDOWN_DAYS))
    return returning and sess["changes"] >= RETURN_CHANGES and sess["prompts"] >= 1


def note_work(sess, b):
    """A session that reached a real stretch of work counts once toward 'they
    came back to it'."""
    if not sess.get("counted") and sess["changes"] >= MIN_CHANGES:
        sess["counted"] = True
        b["work_sessions"] = b.get("work_sessions", 0) + 1


# ── what Claude is told ─────────────────────────────────────────────────────

def launcher():
    return (Path(__file__).resolve().parent / "run-hook.sh").as_posix()


# Characters a shell would act on inside double quotes, in Git Bash or
# PowerShell: $ and ` expand or substitute, " ends the string, \ escapes, and a
# newline ends the command. Values printed into a command Claude will copy come
# partly from the tracker (a parked idea's name, from /similar), which anyone
# can write to, so a name like x$(curl ...) must reach the shell as text.
_UNSAFE = str.maketrans({c: " " for c in '$`"\\\r\n\t'})


def _q(value):
    """Double-quote a value for a command Claude copies, inert in any shell."""
    return '"' + " ".join(str(value).translate(_UNSAFE).split()) + '"'


def b64e(value):
    """Lossless and inert in any shell: [A-Za-z0-9_-] only."""
    return base64.urlsafe_b64encode(str(value).encode("utf-8")).decode("ascii").rstrip("=")


def b64d(token):
    token = str(token or "").strip()
    return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode("utf-8")


def command(sub, sid, key, *rest):
    """A helper command for Claude to copy. Values the toolkit supplies (the
    build key here; names from the tracker in log-it) travel base64-encoded:
    stripping shell characters instead lost them ("Budget $ Forecast" came
    back as a different name and made a twin), and quoting that survives both
    Git Bash and PowerShell does not exist."""
    parts = ["bash", _q(launcher()), "logit", sub, "--session", _q(sid), "--key-b64", b64e(key)]
    return " ".join(parts + list(rest))


def checkpoint_text(sess, b, key, sid):
    where = label_for(key)
    lines = [
        "[NSLS Builder Toolkit — exploration log checkpoint]",
        f"This session has made {sess['changes']} changes across {sess['prompts']} builder messages in {where}. "
        "The toolkit asks once per build whether work like this is turning into something worth builder credit, "
        "or something a colleague at NSLS may also be building. Answering is one step, after you respond to the "
        "builder's message, and it never holds up their request.",
    ]
    settled = settled_names(b)
    if settled:
        listed = "; ".join(
            f"\"{n}\" ({v['outcome']} {v.get('at', '')[:10]}{' — ' + v['note'] if v.get('note') else ''})"
            for n, v in sorted(settled.items()))
        lines.append(f"Already settled here: {listed}. If today's work is one of those, record that and say nothing: "
                     + command("settled", sid, key, "--name", '"<that name>"'))
    else:
        lines.append("If this is an existing tool that is already on the tracker, or part of one, it is settled: "
                     + command("settled", sid, key, "--name", '"<that tool>"') + " (nothing is shown to the builder).")
    lines += [
        "If it is worth offering, this prints the offer to show the builder (with any neighbouring build) and the "
        "commands for their answer: " + command("offer", sid, key, "--name", '"<short name>"',
                                                "--sentence", '"<what it does, one plain sentence>"'),
        "If it is not, or not yet, record why; nothing is shown to the builder: "
        + command("not-yet", sid, key, "--reason", '"<a few words>"'),
    ]
    return "\n".join(lines)


def reminder_text(sid, key):
    return ("[NSLS Builder Toolkit] The exploration log checkpoint from the builder's last message is still "
            "unanswered. It takes one step: " + command("offer", sid, key, "--name", '"<short name>"',
                                                       "--sentence", '"<one sentence>"')
            + " and add what it prints to your reply, or "
            + command("not-yet", sid, key, "--reason", '"<a few words>"')
            + ". This is the only reminder.")
