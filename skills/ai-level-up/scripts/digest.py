#!/usr/bin/env python3
"""ai-level-up: read your own Claude Code and Cowork sessions on this computer,
reduce them to a masked digest the skill can judge, and render the report.

Standard library only, Python 3.8+. Everything is written under the private
state folder (~/.ai-level-up, or AI_LEVEL_UP_DIR). The one optional network
call reads your own Builder stage from the NSLS tracker.

  digest.py scan [--since-last] [--days N] [--max-per-surface 30] [--no-tracker]
  digest.py render REPORT.json [--no-record] [--open]
  digest.py config [frequency=weekly|monthly|never|ask] [tracker_lookup=true|false] [email=you@nsls.org]

A plain scan is a fresh check-in: it looks back 7 days and widens to 14, then 30,
until it finds 30 sessions the person drove. --since-last reads only what is new
since the last check-in: new sessions first, then new turns in older sessions.
"""
import argparse
import datetime as dt
import glob
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

SKILL_NAME = "ai-level-up"
SCHEDULED_PREFIX = "Scheduled check-in: run the ai-level-up skill"
TRACKER = "https://web-production-6281e.up.railway.app/builder-stats/"
NSLS_DOMAIN = "@nsls.org"
RAW_CAP = 20000           # longest human turn we parse; the rest is ignored
TURN_CHARS = 260          # kept per human turn in a digest
TURNS_HEAD, TURNS_TAIL = 12, 6
SKIM_MAX = 60             # one-line summaries of sessions not read closely
SAMPLE_SIZE = 30          # sessions read closely per surface
WIDEN_DAYS = (7, 14, 30)  # a fresh check-in widens its window until it finds SAMPLE_SIZE sessions
TASKS_MAX = 60            # judged sessions the report draws (two surfaces at most)
QUOTE_WORDS = 15
STAGES = ("Explorer", "Maker", "Builder", "Architect", "Steward")
FREQUENCIES = ("weekly", "monthly", "never", "ask")
DIRECTIONS = ("up", "same+check", "down", "habit")
BEHAVIOURS = [  # (habit, share of claude.ai chats showing it in Anthropic's AI Fluency Index, Jan 2026)
    ("Iterates and refines", 85.7), ("Shows an example of good", 41.1), ("Specifies format", 30.0),
    ("Sets how the AI should work", 30.0), ("Gives tone and style", 22.7), ("Names who it's for", 17.6),
    ("States the goal first", 51.1), ("Asks for the approach first", 10.1), ("Fills in missing context", 20.3),
    ("Pushes back on reasoning", 15.8), ("Checks facts that matter", 8.7),
]
HABIT_HELP = [  # (what it means, a quick example), in BEHAVIOURS order
    ("You go back and forth to improve the answer instead of taking the first draft.",
     "After a first draft: “Shorter, and put the ask in the first line.”"),
    ("You show what good looks like: a past email, a template, a sample row.",
     "Paste last term's welcome email and say “match this tone and length”."),
    ("You say what shape the answer should take.",
     "“A table with three columns: chapter, contact, next step.”"),
    ("You tell it how to work with you: ask first, flag doubts, don't guess.",
     "“Ask before you change anything, and flag anything you're unsure of.”"),
    ("You say how it should sound.",
     "“Warm and short, for busy chapter advisers.”"),
    ("You say who will read it.",
     "“This is for first-year members who've never heard of us.”"),
    ("You open with what you want to get done, before the details.",
     "“Goal: a one-page brief Kevin can approve in five minutes.”"),
    ("You ask for the plan before it starts the work.",
     "“Before you start, tell me how you'd approach this.”"),
    ("You notice what it doesn't know and give it the missing piece.",
     "“You're missing that the event moved to Zoom. Redo the invite.”"),
    ("You question its reasoning when something doesn't add up.",
     "“Why did you drop the March numbers? Show me your working.”"),
    ("You check the facts that matter before relying on them.",
     "“Which source says 40% of members renewed? Link it.”"),
]
NEAR = 10                 # points either side of typical that count as "about typical"
BIG_GAP = 15              # points below typical that get flagged as a big gap (top 3 at most)
PLACEHOLDER = re.compile(r"<[^<>\n]{1,60}>|\{[^{}\n]{1,60}\}|\[(?!(?:email|ssn|phone|id|number|REDACTED)\b)[a-z][^\[\]\n]{1,60}\]")


# ---------------------------------------------------------------- locations and private files

def _private_dir(path):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def state_dir():
    return _private_dir(os.environ.get("AI_LEVEL_UP_DIR") or os.path.join(os.path.expanduser("~"), ".ai-level-up"))


def reports_dir():
    return _private_dir(os.environ.get("AI_LEVEL_UP_REPORTS") or os.path.join(state_dir(), "reports"))


def claude_home():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def code_root():
    return os.path.join(claude_home(), "projects")


def cowork_root():
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, "Library", "Application Support", "Claude", "local-agent-mode-sessions")]
    if os.environ.get("APPDATA"):
        candidates.append(os.path.join(os.environ["APPDATA"], "Claude", "local-agent-mode-sessions"))
    candidates.append(os.path.join(home, ".config", "Claude", "local-agent-mode-sessions"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return None


def write_private(path, text, append=False):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "a" if append else "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def config_path():
    return os.path.join(state_dir(), "config.json")


def history_path():
    return os.path.join(state_dir(), "history.jsonl")


def digest_path():
    return os.path.join(state_dir(), "digest-latest.json")


# ---------------------------------------------------------------- masking: the privacy boundary

EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
SECRET_PATTERNS = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----(?:[\s\S]{0,8000}?-----END [A-Z ]{0,40}PRIVATE KEY-----|[\s\S]*)")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("api_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghs|ghu)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{16,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("credential", re.compile(r"(?i)[\"']?\b(?:api[_\- ]?key|secret|token|password|passwd|pwd|client[_\- ]?secret|access[_\- ]?key)\b[\"']?\s*[:=]\s*(?:\"[^\"\n]{1,300}\"|'[^'\n]{1,300}'|[^\s\"',;}]{6,})")),
]
PII_PATTERNS = [
    ("[email]", EMAIL),
    ("[ssn]", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("[phone]", re.compile(r"(?:\+?1[\s.\-]?)?\(?\b\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b")),
    ("[id]", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")),
    ("[number]", re.compile(r"\b\d{7,}\b")),
]
TOKEN = re.compile(r"[A-Za-z0-9_\-]{25,}")


def _long_id(m):
    t = m.group(0)
    return "[id]" if re.search(r"\d", t) and re.search(r"[A-Za-z]", t) else t


def mask(text, flags=None):
    """Mask secrets, contact details and identifiers. Every string that leaves
    this script (digest, history, report) goes through here."""
    text = str(text)[:RAW_CAP]
    for name, pat in SECRET_PATTERNS:
        if pat.search(text):
            if flags is not None:
                flags.add(name)
            text = pat.sub("[REDACTED:%s]" % name, text)
    for label, pat in PII_PATTERNS:
        text = pat.sub(label, text)
    return TOKEN.sub(_long_id, text)


def clip(text, n=TURN_CHARS):
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) <= n:
        return text
    cut = text[: n - 1]
    if " " in cut[n // 2:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:·-") + "…"


def safe(text, n):
    return clip(mask(text), n)


# ---------------------------------------------------------------- turn parsing (find-based, linear)

def strip_blocks(text, open_tag, close_tag):
    """Remove <tag ...>…</tag ...> blocks; return (kept, total_removed_chars).
    An unterminated block swallows the rest of the text (fail closed)."""
    kept, removed, i = [], 0, 0
    while True:
        j = text.find(open_tag, i)
        if j < 0:
            kept.append(text[i:])
            break
        kept.append(text[i:j])
        end = text.find(close_tag, j)
        if end < 0:
            removed += len(text) - j
            break
        close_end = text.find(">", end)
        stop = len(text) if close_end < 0 else close_end + 1
        removed += stop - j
        i = stop
    return "".join(kept), removed


def tag_value(text, tag):
    start = text.find("<%s>" % tag)
    if start < 0:
        return None
    start += len(tag) + 2
    end = text.find("</%s>" % tag, start)
    return text[start:end if end >= 0 else len(text)].strip()


SKIP_PREFIXES = (
    "Base directory for this skill",
    "<task-notification",
    "<local-command-stdout",
    "<local-command-stderr",
    "<local-command-caveat",
    "Caveat: The messages below were generated",
    "This session is being continued from a previous conversation",
    "The app was quit while you were working",
)


def human_text(content):
    """Text a person typed, from a message's content; None if it holds none."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p) or None
    return None


def classify_turn(text):
    """Return (kind, cleaned): kind is 'skip', 'interrupt', 'command' or 'typed'."""
    text, _ = strip_blocks(str(text)[:RAW_CAP], "<system-reminder", "</system-reminder")
    t = text.strip()
    if not t:
        return "skip", ""
    cmd = tag_value(t, "command-name")
    if cmd is not None:
        args = tag_value(t, "command-args") or ""
        return "command", (cmd + (" " + args if args else "")).strip()
    if t.startswith("[Request interrupted"):
        return "interrupt", ""
    if t.startswith(SKIP_PREFIXES):
        return "skip", ""
    return "typed", t


def is_self_command(cmd):
    name = cmd.split(" ", 1)[0].lstrip("/")
    return name == SKILL_NAME or name.endswith(":" + SKILL_NAME)


def parse_ts(value):
    """Aware UTC datetime from ISO text or epoch seconds/milliseconds; None if unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if isinstance(value, (int, float)):
            secs = value / 1000.0 if value > 1e11 else float(value)
            return dt.datetime.fromtimestamp(secs, tz=dt.timezone.utc)
        s = str(value).strip().replace("Z", "+00:00")
        s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)
    except (ValueError, OverflowError, OSError, TypeError):
        return None


# ---------------------------------------------------------------- tool signals

SHIP = [
    ("git_push", re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?push\b")),
    ("pr_create", re.compile(r"\bgh\s+pr\s+create\b")),
    ("pr_merge", re.compile(r"\bgh\s+pr\s+merge\b")),
    ("commit", re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?commit\b")),
    ("deploy", re.compile(r"\b(?:railway\s+up|netlify\s+deploy|vercel(?:\s+--prod)?\b|gcloud\s+run\s+deploy)")),
    ("tests", re.compile(r"\b(?:pytest|npm\s+(?:run\s+)?test|jest|vitest|unittest)\b")),
]
SCHEDULE_TOOLS = {"ScheduleWakeup", "CronCreate", "RemoteTrigger"}
APP_PLUMBING = ("ccd_", "terminal", "visualize", "mcp-registry", "pdf-viewer", "scheduled-tasks",
                "Claude_Code_iOS", "plugin_")
BROWSER = ("claude-in-chrome", "Control_Chrome", "Claude_Browser")
FAMILIES = [  # keyword in the tool name -> plain connector name (order matters)
    ("Slack", r"slack"), ("Fathom", r"meeting|transcript|recording"), ("Calendar", r"event|calendar|suggest_time"),
    ("Gmail", r"draft|thread|gmail|label|spam|forward|reply"), ("Google Drive", r"file|drive|folder|permission"),
    ("Airtable", r"record|table|base|field|automation|interface|page"), ("Asana", r"task|project|portfolio|goal|agent"),
    ("HubSpot", r"crm|hubspot|campaign|aeo|segment|landing|blog"),
]


def connector_label(server, tool):
    """Plain name for an MCP server, or None when it is the app's own plumbing."""
    if server.startswith(APP_PLUMBING):
        return None
    if server.startswith(BROWSER):
        return "browser control"
    if server.startswith("Control_your_Mac"):
        return "computer control"
    if re.fullmatch(r"[0-9a-f\-]{20,}", server):  # claude.ai connectors arrive as UUIDs
        for label, pat in FAMILIES:
            if re.search(pat, tool, re.I):
                return label
        return "other connector"
    return safe(server, 40)


def new_signals():
    return {
        "tools": {}, "skills": {}, "connectors": {}, "subagents": 0, "workflows": 0,
        "plan_mode": 0, "files_written": 0, "web_research": 0, "asked_user": 0,
        "artifacts_published": 0, "schedules_created": 0, "messages_to_sessions": 0,
        "ship": {k: 0 for k, _ in SHIP},
    }


def bump(d, k, n=1):
    d[k] = d.get(k, 0) + n


def read_tool_use(block, sig):
    name = str(block.get("name") or "?")
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    bump(sig["tools"], name)
    if name in ("Agent", "Task"):
        sig["subagents"] += 1
    elif name == "Workflow":
        sig["workflows"] += 1
    elif name == "Skill":
        bump(sig["skills"], safe(inp.get("skill") or inp.get("name") or "?", 60))
    elif name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        sig["files_written"] += 1
    elif name in ("WebSearch", "WebFetch"):
        sig["web_research"] += 1
    elif name in ("EnterPlanMode", "ExitPlanMode"):
        sig["plan_mode"] += 1
    elif name == "AskUserQuestion":
        sig["asked_user"] += 1
    elif name == "Artifact":
        sig["artifacts_published"] += 1
    elif name == "SendMessage":
        sig["messages_to_sessions"] += 1
    elif name in SCHEDULE_TOOLS:
        sig["schedules_created"] += 1
    elif name == "Bash":
        cmd = str(inp.get("command") or "")[:RAW_CAP]
        for key, pat in SHIP:
            if pat.search(cmd):
                sig["ship"][key] += 1
    if name.startswith("mcp__"):
        parts = name.split("__")
        server, tool = (parts[1] if len(parts) > 1 else "?"), parts[-1]
        if "scheduled" in server and tool.startswith("create"):
            sig["schedules_created"] += 1
        if "session_mgmt" in server and tool == "send_message":
            sig["messages_to_sessions"] += 1
        label = connector_label(server, tool)
        if label:
            bump(sig["connectors"], label)


# ---------------------------------------------------------------- sessions

SLOT_SECONDS = 1800


def blank_session(surface, sid):
    return {
        "surface": surface, "id": sid, "title": None, "project": None,
        "start": None, "turns": [], "commands": [], "interruptions": 0,
        "pasted_blocks": 0, "pasted_chars": 0, "images": 0, "permission_modes": {},
        "peer_messages_in": 0, "untimed_skipped": 0, "automated": False,
        "automation_reason": None, "secret_flags": set(), "signals": new_signals(),
        "is_self": False, "slots": set(), "earlier": False, "copies": 0,
    }


def ingest_line(s, o, since, seen=None):
    """Fold one JSONL record into session s. Only timestamped records inside the window count.
    seen holds record ids already read: moving a session to another folder copies its history
    into a new file with the same ids, and each record should count once."""
    if not isinstance(o, dict):
        return
    typ = o.get("type")
    if typ == "summary" and o.get("summary") and not s["title"]:
        s["title"] = safe(o["summary"], 90)
        return
    if typ not in ("user", "assistant") or o.get("isSidechain") or o.get("isReplay"):
        return
    ts = parse_ts(o.get("timestamp") or o.get("_audit_timestamp"))
    if ts is None:
        s["untimed_skipped"] += 1
        return
    if ts < since:
        s["earlier"] = True  # the session started before this window: only its new part is read
        return
    if seen is not None and o.get("uuid"):
        if o["uuid"] in seen:
            s["copies"] += 1
            return
        seen.add(o["uuid"])
    if s["project"] is None and o.get("cwd"):
        s["project"] = safe(os.path.basename(str(o["cwd"]).rstrip("/\\")), 60) or None
    msg = o.get("message") if isinstance(o.get("message"), dict) else {}
    if typ == "assistant":
        content = msg.get("content")
        for b in content if isinstance(content, list) else []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                read_tool_use(b, s["signals"])
                if b.get("name") == "Skill":
                    sk = str((b.get("input") or {}).get("skill") or "")
                    if sk == SKILL_NAME or sk.endswith(":" + SKILL_NAME):
                        s["is_self"] = True
        return
    if o.get("isMeta") or o.get("isCompactSummary") or o.get("isVisibleInTranscriptOnly"):
        return
    origin = o.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        if origin.get("kind") == "peer":
            s["peer_messages_in"] += 1
        return
    content = msg.get("content")
    text = human_text(content)
    if text is None:
        return  # tool results only
    kind, clean = classify_turn(text)
    if kind == "skip":
        return
    if s["start"] is None or ts < s["start"]:
        s["start"] = ts
    s["slots"].add(int(ts.timestamp() // SLOT_SECONDS))
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "image" for b in content):
        s["images"] += 1
    if o.get("permissionMode"):
        bump(s["permission_modes"], str(o["permissionMode"])[:30])
    if kind == "interrupt":
        s["interruptions"] += 1
        return
    if kind == "command":
        if is_self_command(clean):
            s["is_self"] = True
        s["commands"].append(clip(mask(clean, s["secret_flags"]), 120))
    elif not s["turns"]:
        head = clean[:4000]
        if head.startswith(SCHEDULED_PREFIX) or (head.startswith("<scheduled-task") and SCHEDULED_PREFIX in head):
            s["is_self"] = True  # a scheduled check-in, wrapped or not
        elif head.startswith("<scheduled-task"):  # the desktop app wraps every scheduled run in this tag
            s["automated"], s["automation_reason"] = True, "scheduled task"
    own, removed = strip_blocks(clean, "<pasted_content", "</pasted_content")
    turn = {"t": ts.isoformat(), "kind": kind, "text": clip(mask(own, s["secret_flags"]))}
    if removed:
        # pasted text may be someone else's words: only its size is kept
        s["pasted_blocks"] += 1
        s["pasted_chars"] += removed
        turn["pasted_chars"] = removed
    s["turns"].append(turn)


def read_jsonl(path, s, since):
    """Fold a transcript into s; False if the file could not be read."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    ingest_line(s, json.loads(line), since)
                except ValueError:
                    continue
        return True
    except OSError:
        return False


def finish(s):
    if not s["title"] and s["turns"]:
        s["title"] = clip(s["turns"][0]["text"], 90)
    return s


def scheduled_prompts():
    prompts = []
    for f in glob.glob(os.path.join(claude_home(), "scheduled-tasks", "*", "SKILL.md")):
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                body = fh.read(RAW_CAP)
        except OSError:
            continue
        body = body.split("---", 2)[-1] if body.startswith("---") else body
        body = re.sub(r"\s+", " ", body).strip()
        if body:
            prompts.append(clip(mask(body), 60).rstrip("…"))
    return prompts


def scan_code(since, notes):
    root = code_root()
    if not os.path.isdir(root):
        return None, []
    cutoff = since.timestamp()
    sched = scheduled_prompts()
    out, unreadable, seen, files = [], 0, set(), []
    for f in glob.glob(os.path.join(root, "*", "*.jsonl")):
        try:
            if os.path.getmtime(f) < cutoff:  # transcripts are append-only
                continue
            files.append((os.path.getsize(f), f))
        except OSError:
            unreadable += 1
    for _, f in sorted(files, reverse=True):  # the fullest copy of a moved session claims its records
        s = blank_session("code", os.path.splitext(os.path.basename(f))[0])
        entry = None
        try:
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if entry is None and isinstance(o, dict) and o.get("entrypoint"):
                        entry = o["entrypoint"]
                    ingest_line(s, o, since, seen)
        except OSError:
            unreadable += 1
            continue
        if entry == "sdk-cli":
            s["automated"], s["automation_reason"] = True, "headless run"
        elif s["turns"] and any(p and s["turns"][0]["text"].startswith(p) for p in sched):
            s["automated"], s["automation_reason"] = True, "scheduled task"
        out.append(finish(s))
    if unreadable:
        notes.append("code: %d session files could not be read" % unreadable)
    copies = sum(s["copies"] for s in out)
    if copies:
        notes.append("code: %d records were copies of a moved session and were counted once" % copies)
    return root, [s for s in out if s["turns"] or s["signals"]["tools"] or not s["copies"]]


def known_email(cfg):
    """The person's NSLS work email: set in settings, or the signed-in Claude account if it's @nsls.org."""
    if cfg.get("email"):
        return cfg["email"]
    acct = load_json(os.path.join(os.path.expanduser("~"), ".claude.json"), {})
    acct = acct.get("oauthAccount") if isinstance(acct, dict) else None
    email = (acct or {}).get("emailAddress") if isinstance(acct, dict) else None
    if isinstance(email, str) and email.lower().endswith(NSLS_DOMAIN):
        return email
    return None


def scan_cowork(since, notes, email):
    root = cowork_root()
    if not root:
        return None, []
    metas = []
    for meta_path in glob.glob(os.path.join(root, "*", "*", "local_*.json")):
        meta = load_json(meta_path, None)
        if isinstance(meta, dict):
            metas.append((meta_path, meta))
    accounts = {str(m.get("emailAddress")).lower() for _, m in metas if m.get("emailAddress")}
    if accounts and (len(accounts) > 1 or email):
        if not email or email.lower() not in accounts:
            notes.append("cowork: %s, and none confirmed as yours; Cowork skipped (say 'my work email is …' to include it)"
                         % ("several Claude accounts on this computer" if len(accounts) > 1 else "the Cowork account here isn't your work email"))
            return root, []
        metas = [(p, m) for p, m in metas if str(m.get("emailAddress")).lower() == email.lower()]
    out, unreadable = [], 0
    for meta_path, meta in metas:
        last = parse_ts(meta.get("lastActivityAt") or meta.get("createdAt"))
        if last is not None and last < since:
            continue
        audit = os.path.join(os.path.splitext(meta_path)[0], "audit.jsonl")
        if not os.path.isfile(audit):
            continue
        s = blank_session("cowork", safe(meta.get("sessionId") or os.path.basename(meta_path), 60))
        s["title"] = safe(meta.get("title") or "", 90) or None
        folders = meta.get("userSelectedFolders") or []
        if isinstance(folders, list) and folders:
            s["project"] = safe(os.path.basename(str(folders[0]).rstrip("/\\")), 60)
        if meta.get("scheduledTaskId"):
            s["automated"], s["automation_reason"] = True, "scheduled task"
        if not read_jsonl(audit, s, since):
            unreadable += 1
            continue
        out.append(finish(s))
    if unreadable:
        notes.append("cowork: %d task files could not be read" % unreadable)
    return root, out


# ---------------------------------------------------------------- shaping

def overlap_max(sessions):
    """Most sessions the person typed into within the same half hour (multi-agent signal)."""
    per_slot = {}
    for s in sessions:
        for slot in s["slots"]:
            per_slot[slot] = per_slot.get(slot, 0) + 1
    return max(per_slot.values()) if per_slot else 0


def aggregate(sessions):
    agg = {
        "sessions": len(sessions), "automated_sessions": 0, "human_turns": 0, "days_active": 0,
        "max_concurrent_sessions": overlap_max(sessions), "permission_modes": {},
        "sessions_with": {"subagents": 0, "workflows": 0, "plan_mode": 0, "skills": 0,
                          "connectors": 0, "files_written": 0, "schedules_created": 0,
                          "messages_between_sessions": 0, "ship_push_or_pr": 0, "deploy": 0,
                          "tests": 0, "web_research": 0, "artifacts_published": 0},
        "top_skills": {}, "top_connectors": {}, "slash_commands": {},
        "secret_flags": {}, "interruptions": 0, "untimed_records_skipped": 0,
    }
    days = set()
    for s in sessions:
        sig = s["signals"]
        agg["automated_sessions"] += int(s["automated"])
        agg["human_turns"] += len(s["turns"])
        agg["interruptions"] += s["interruptions"]
        agg["untimed_records_skipped"] += s["untimed_skipped"]
        days.update(t["t"][:10] for t in s["turns"])
        for k, v in s["permission_modes"].items():
            bump(agg["permission_modes"], k, v)
        w = agg["sessions_with"]
        w["subagents"] += int(sig["subagents"] > 0)
        w["workflows"] += int(sig["workflows"] > 0)
        w["plan_mode"] += int(sig["plan_mode"] > 0)
        w["skills"] += int(bool(sig["skills"]))
        w["connectors"] += int(bool(sig["connectors"]))
        w["files_written"] += int(sig["files_written"] > 0)
        w["schedules_created"] += int(sig["schedules_created"] > 0)
        w["messages_between_sessions"] += int(sig["messages_to_sessions"] > 0 or s["peer_messages_in"] > 0)
        w["ship_push_or_pr"] += int(sig["ship"]["git_push"] + sig["ship"]["pr_create"] > 0)
        w["deploy"] += int(sig["ship"]["deploy"] > 0)
        w["tests"] += int(sig["ship"]["tests"] > 0)
        w["web_research"] += int(sig["web_research"] > 0)
        w["artifacts_published"] += int(sig["artifacts_published"] > 0)
        for k, v in sig["skills"].items():
            bump(agg["top_skills"], k, v)
        for k, v in sig["connectors"].items():
            bump(agg["top_connectors"], k, v)
        for c in s["commands"]:
            bump(agg["slash_commands"], c.split(" ")[0])
        for f in s["secret_flags"]:
            bump(agg["secret_flags"], f)
    agg["days_active"] = len(days)
    for k in ("top_skills", "top_connectors", "slash_commands"):
        agg[k] = dict(sorted(agg[k].items(), key=lambda kv: -kv[1])[:12])
    return agg


def driven(sessions):
    return [s for s in sessions if not s["automated"] and s["turns"]]


def readable(sessions):
    """Driven sessions worth reading closely: at least one turn that isn't a slash command."""
    return [s for s in driven(sessions) if any(t["kind"] != "command" for t in s["turns"])]


def sample(sessions, k, prefer_new=False):
    """Pick up to k sessions a person actually drove, most substantial first, one per project
    before any project gets a second. A since-last check-in reads new sessions before older
    ones; a session that is only slash commands (/login, /model) is never read closely."""
    pool = readable(sessions)
    pool.sort(key=lambda s: (s["earlier"] if prefer_new else False, -len(s["turns"]), -sum(s["signals"]["tools"].values()), s["id"]))
    picked, projects = [], set()
    for s in pool:
        if len(picked) < k and s["project"] not in projects:
            picked.append(s)
            projects.add(s["project"])
    picked += [s for s in pool if s not in picked][:k - len(picked)]
    picked.sort(key=lambda s: s["start"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    return picked


def shape(s, num):
    turns = s["turns"]
    if len(turns) > TURNS_HEAD + TURNS_TAIL:
        gap = {"t": None, "kind": "gap", "text": "… %d more turns …" % (len(turns) - TURNS_HEAD - TURNS_TAIL)}
        turns = turns[:TURNS_HEAD] + [gap] + turns[-TURNS_TAIL:]
    sig = s["signals"]
    return {
        "ref": "%s-%d" % (s["surface"], num),
        "surface": s["surface"],
        "title": s["title"], "project": s["project"],
        "start": s["start"].isoformat() if s["start"] else None,
        "continued": s["earlier"],
        "active_days": len({t["t"][:10] for t in s["turns"]}),
        "human_turns": len(s["turns"]),
        "interruptions": s["interruptions"], "pasted_blocks": s["pasted_blocks"], "images": s["images"],
        "permission_modes": s["permission_modes"],
        "secret_flags": sorted(s["secret_flags"]),
        "signals": {
            "skills": sig["skills"], "connectors": sig["connectors"], "subagents": sig["subagents"],
            "workflows": sig["workflows"], "plan_mode": sig["plan_mode"], "files_written": sig["files_written"],
            "web_research": sig["web_research"], "asked_user": sig["asked_user"],
            "schedules_created": sig["schedules_created"], "artifacts_published": sig["artifacts_published"],
            "messages_to_sessions": sig["messages_to_sessions"], "peer_messages_in": s["peer_messages_in"],
            "ship": {k: v for k, v in sig["ship"].items() if v},
            "tool_calls": sum(sig["tools"].values()),
        },
        "turns": turns,
    }


# ---------------------------------------------------------------- settings, stage, history

def load_config():
    cfg = load_json(config_path(), {})
    return cfg if isinstance(cfg, dict) else {}


def save_config(cfg):
    write_private(config_path(), json.dumps(cfg, indent=1))


def tracker_stage(cfg):
    email = known_email(cfg)
    cache = cfg.get("stage_cache") if isinstance(cfg.get("stage_cache"), dict) else None
    result = None
    if cfg.get("tracker_lookup") is False:
        result = {"stage": None, "source": "tracker lookup turned off"}
    elif not email:
        result = {"stage": None, "source": "no NSLS work email known"}
    else:
        try:
            url = TRACKER + urllib.parse.quote(email)
            with urllib.request.urlopen(url, timeout=15) as r:
                data = json.load(r)
            stage = (data.get("builder") or {}).get("stage") if isinstance(data, dict) else None
            if stage in STAGES:
                cfg["stage_cache"] = {"stage": stage, "checked_at": dt.date.today().isoformat()}
                save_config(cfg)
                return {"stage": stage, "source": "tracker"}
            result = {"stage": None, "source": "not on the tracker"}
        except Exception as e:  # offline, sandboxed or blocked: never fatal
            result = {"stage": None, "source": "tracker unreachable (%s)" % type(e).__name__}
    if cache and cache.get("stage") in STAGES and result["source"].startswith("tracker unreachable"):
        return {"stage": cache["stage"], "source": "tracker, last checked %s" % cache.get("checked_at"), "stale": True}
    return result


def clean_run(run):
    """History keeps an allowlisted summary only: never chat text."""
    run = run if isinstance(run, dict) else {}
    date = str(run.get("date") or "")
    out = {"date": date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) else dt.date.today().isoformat()}
    wd = run.get("window_days")
    out["window_days"] = wd if isinstance(wd, int) and 1 <= wd <= 366 else None
    covered = parse_ts(run.get("covered_to"))
    out["covered_to"] = covered.isoformat() if covered else None
    out["stage"] = run.get("stage") if run.get("stage") in STAGES else None
    seen = run.get("seen_counts") if isinstance(run.get("seen_counts"), dict) else run.get("seen")
    seen = seen if isinstance(seen, dict) else {}  # a saved history line keeps them under "seen"
    out["seen"] = {k: seen[k] for k in ("code", "cowork", "chat") if isinstance(seen.get(k), int)}
    levels = run.get("levels") if isinstance(run.get("levels"), dict) else {}
    out["levels"] = {safe(k, 40): safe(v, 80) for k, v in list(levels.items())[:4]}
    beh = run.get("behaviours") if isinstance(run.get("behaviours"), dict) else {}
    out["behaviours"] = {}
    for i in range(1, 12):
        v = str(beh.get(str(i), ""))
        m = re.fullmatch(r"\s*(\d{1,3})\s*(?:/|of)\s*(\d{1,3})\s*", v)
        if m:
            out["behaviours"][str(i)] = "%s/%s" % m.groups()
    exps = run.get("experiments") if isinstance(run.get("experiments"), list) else []
    out["experiments"] = [
        {"title": safe(e.get("title"), 100), "direction": e.get("direction") if e.get("direction") in DIRECTIONS else "same+check"}
        for e in exps[:3] if isinstance(e, dict) and e.get("title")
    ]
    fix = run.get("fix")
    out["fix"] = safe(fix.get("line") if isinstance(fix, dict) else fix, 200) if fix else None
    out["frequency"] = run.get("frequency") if run.get("frequency") in FREQUENCIES else None
    sc = run.get("scores") if isinstance(run.get("scores"), dict) else {}
    out["scores"] = {k: "%d/%d" % ratio(sc[k]) for k in ("right_level", "checks", "habits_ahead") if ratio(sc.get(k))}
    return out


def last_run():
    last = None
    try:
        with open(history_path(), encoding="utf-8") as fh:
            for line in fh:
                try:
                    last = json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return None
    return clean_run(last) if last else None


# ---------------------------------------------------------------- report rendering

def esc(v):
    return html.escape(str(v if v is not None else ""), quote=True)


def text(v, n=600):
    return esc(safe(v, n))


def quote(q, ref):
    if not q:
        return ""
    words = safe(q, 400).split()
    if len(words) > QUOTE_WORDS:
        words = words[:QUOTE_WORDS] + ["…"]
    return '<blockquote>"%s" — %s</blockquote>' % (esc(" ".join(words)), text(ref, 60))


LEVELS = [
    ("Chatbot", "You ask, it answers"),
    ("Copilot", "AI works inside the file you're in"),
    ("Agent", "Works step by step across your tools; you approve"),
    ("Autopilot", "You hand over the task and review the result"),
    ("Workflows", "A repeatable process with checks built in"),
    ("Assistant", "Runs on a schedule without being asked"),
    ("Multi-agent", "Several agents, each with its own job"),
    ("Orchestrator", "A lead agent runs a team for you"),
]
FITS = {"right": "Fit the stakes", "higher": "Could have handed over more", "lower": "Handed over too much for the stakes", "other": "Not judged"}
DIRECTION_LABEL = {"up": "Try the next level", "same+check": "Same way, add a check", "down": "Try one level lower", "habit": "Build a habit"}

ICON = {
    "chev": '<svg class="chev" width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 4l5 5-5 5"/></svg>',
    "check": '<svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 9.5l3.5 3.5 7.5-8"/></svg>',
    "flag": '<svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 18V3"/><path d="M5 3.5h9l-2 3.5 2 3.5H5"/></svg>',
    "stairs": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 18.5h4.5v-4h4v-4h4v-4H19"/></svg>',
    "chart": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.5 3.5v15h15"/><path d="M7 7.5h9M7 11.5h5.5M7 15.5h10.5"/></svg>',
    "scale": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 3.5v15M7.5 18.5h7M4.5 6.5h13"/><path d="M4.5 6.5l-2.3 5.2h4.6zM17.5 6.5l-2.3 5.2h4.6z"/></svg>',
    "pin": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8 3.5h6M9.3 3.5v5.2L6.5 12h9l-2.8-3.3V3.5M11 12v6.5"/></svg>',
    "screens": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="4" width="17" height="11" rx="2"/><path d="M8 18.5h6"/></svg>',
    "info": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="8.5"/><path d="M11 10v5.5M11 6.6v.2"/></svg>',
    "i": '<svg class="i" width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="8" cy="8" r="6.3"/><path d="M8 7.2v4M8 4.9v.1"/></svg>',
}


def ratio(v):
    """'7 of 8' or '7/8' -> (7, 8); None if not a usable ratio."""
    m = re.fullmatch(r"\s*(\d{1,3})\s*(?:/|of)\s*(\d{1,3})\s*", str(v or ""))
    if not m or int(m.group(2)) == 0 or int(m.group(1)) > int(m.group(2)):
        return None
    return int(m.group(1)), int(m.group(2))


def level_index(v):
    s = str(v or "").lower()
    hits = [(s.find(name.lower()), i) for i, (name, _) in enumerate(LEVELS) if name.lower() in s]
    return min(hits)[1] if hits else None


def fit_of(task):
    f = str(task.get("fit") or "").lower()
    v = str(task.get("verdict") or "").lower()
    if any(k in f for k in ("lower", "trusting")):
        return "lower"
    if any(k in f for k in ("higher", "cautious")):
        return "higher"
    if f.startswith("right") or f.startswith("fit"):
        return "right"
    if not f:  # older reports carried the verdict only
        if "trusting" in v:
            return "lower"
        if "cautious" in v:
            return "higher"
        if v.startswith("right"):
            return "right"
    return "other"


def sentence(v):
    v = str(v or "").strip()
    return v[:1].upper() + v[1:]


def fold(icon, title, sub, body, open_=False, anchor=None):
    return ('<details class="fold"%s%s><summary>%s<span class="t"><strong>%s</strong><span>%s</span></span>'
            '<span class="toggle" aria-hidden="true">%s</span></summary><div class="fold-body">%s</div></details>') % (
        ' id="%s"' % anchor if anchor else "", " open" if open_ else "", ICON[icon], esc(title), sub, ICON["chev"], body)


def stage_track(r):
    stage = r.get("stage") if r.get("stage") in STAGES else None
    cur = STAGES.index(stage) if stage else -1
    items = []
    for i, name in enumerate(STAGES):
        cls = "passed" if i < cur else "here" if i == cur else ""
        inner = ICON["check"] if i < cur else str(i + 1)
        tag = '<span class="tag">You\'re here</span>' if i == cur else ""
        items.append('<li class="%s"><span class="dot">%s</span><span>%s</span>%s</li>' % (cls, inner, esc(name), tag))
    head = '<span class="eyebrow">Builder stage · on record</span>'
    if not stage:
        return ('<section aria-label="Builder stage on record">%s<ol class="stages unknown">%s</ol>'
                '<p class="soft">I couldn\'t check your Builder stage this time.</p></section>') % (head, "".join(items))
    prev = (r.get("previous") or {}).get("stage") if isinstance(r.get("previous"), dict) else None
    moved = ""
    if prev in STAGES and prev != stage:
        moved = '<p><strong>%s since last time</strong> (was %s).</p>' % (
            "Up a stage" if STAGES.index(prev) < cur else "Changed", esc(prev))
    unlock = ""
    if r.get("unlock") or r.get("toward_unlock"):
        unlock = '<div class="unlock">%s<div>%s%s%s</div></div>' % (
            ICON["flag"],
            (("<strong>%s is the top stage.</strong> %s" % (esc(stage), text(r.get("unlock"), 300))) if cur == len(STAGES) - 1
             else "<strong>Next unlock:</strong> %s" % text(r.get("unlock"), 300)) if r.get("unlock") else "",
            ("<p>%s</p>" % text(r["toward_unlock"], 600)) if r.get("toward_unlock") else "", moved)
    return '<section aria-label="Builder stage on record">%s<ol class="stages">%s</ol>%s</section>' % (head, "".join(items), unlock)


def habit_rows(r):
    beh = r.get("behaviours") if isinstance(r.get("behaviours"), dict) else {}
    rows = []
    for i, (name, typ) in enumerate(BEHAVIOURS, 1):
        got = ratio(beh.get(str(i)))
        if got:
            you = 100.0 * got[0] / got[1]
            rows.append({"i": i, "name": name, "typ": typ, "got": got, "you": you, "diff": you - typ})
    return rows


def habit_groups(rows):
    """(ahead, to improve, about typical), each sorted biggest difference first."""
    ahead = sorted((x for x in rows if x["diff"] >= NEAR), key=lambda x: -x["diff"])
    grow = sorted((x for x in rows if x["diff"] <= -NEAR), key=lambda x: x["diff"])
    par = sorted((x for x in rows if -NEAR < x["diff"] < NEAR), key=lambda x: x["diff"])
    return ahead, grow, par


AXIS = ('<div class="hb-axis" aria-hidden="true"><span></span><div class="hb-scale">%s</div></div>' % "".join(
    '<span style="left:%d%%">%d%s</span>' % (v, v, "%" if v == 100 else "") for v in (0, 25, 50, 75, 100)))


def habit_row(x, prev, notes, big):
    i = x["i"]
    was = ratio(prev.get(str(i)))
    tip = "You: %d of %d sessions" % x["got"] + (" (last time %d of %d)" % was if was else "")
    what, example = HABIT_HELP[i - 1]
    mine = safe(notes.get(str(i)) or "", 300)
    pop = ('<div id="habit-%d" popover class="pop"><h4>%s</h4><p>%s</p><p><strong>Example:</strong> %s</p>%s'
           '<button type="button" class="pop-close" popovertarget="habit-%d" popovertargetaction="hide">Close</button></div>') % (
        i, esc(x["name"]), esc(what), esc(example),
        '<p><strong>From your week:</strong> %s</p>' % esc(mine) if mine else "", i)
    return ('<div class="hb-row%s"><div class="hb-name"><button type="button" class="hb-btn" popovertarget="habit-%d">%s%s</button>%s</div>'
            '<div class="hb-bars"><div class="hb-line you" tabindex="0" aria-label="%s">'
            '<span class="hb-trk"><span class="fill you" style="width:%.1f%%"></span><span class="tip" style="left:%.0f%%">%s</span></span>'
            '<span class="hb-pct num">%.0f%%</span></div>'
            '<div class="hb-line typ"><span class="hb-trk thin"><span class="fill typ" style="width:%.1f%%"></span></span>'
            '<span class="hb-pct num soft">typical %.0f%%</span></div></div></div>%s') % (
        " big" if big else "", i, esc(x["name"]), ICON["i"], '<span class="badge">Big gap</span>' if big else "",
        esc(tip), x["you"], min(x["you"], 60), esc(tip), x["you"], x["typ"], x["typ"], pop)


def habits_section(r, n_tasks):
    rows = habit_rows(r)
    ahead, grow, par = habit_groups(rows)
    prev = (r.get("previous") or {}).get("behaviours") if isinstance(r.get("previous"), dict) else {}
    prev = prev if isinstance(prev, dict) else {}
    notes = r.get("habit_notes") if isinstance(r.get("habit_notes"), dict) else {}
    big = {x["i"] for x in [x for x in grow if -x["diff"] >= BIG_GAP][:3]}  # grow is biggest gap first

    def group(title, sub, items):
        if not items:
            return ""
        return '<div class="hb-group"><h4>%s <span class="soft">· %d</span></h4>%s%s%s</div>' % (
            title, len(items), '<p class="soft hb-sub">%s</p>' % sub if sub else "", AXIS,
            "".join(habit_row(x, prev, notes, x["i"] in big) for x in items))
    legend = ('<div class="legend"><span><span class="key" style="background:var(--you)"></span>You</span>'
              '<span><span class="key" style="background:var(--typical)"></span>Typical: share of chats showing the habit in Anthropic\'s study</span></div>'
              '<p class="soft hb-how">Click a habit to see what it means, with an example. Hover over your bar to see the count.</p>')
    body = (legend + group("Ahead of typical", "", ahead)
            + group("Habits to improve", "Below typical by %d points or more, biggest gap first." % NEAR, grow)
            + group("About typical", "Within %d points of typical." % NEAR, par)
            + ('<p class="soft">With fewer than 8 sessions, read these as rough.</p>' if 0 < n_tasks < 8 else ""))
    sub = " · ".join(p for p in ("%d ahead of typical" % len(ahead) if ahead else "",
                                 "%d to improve" % len(grow) if grow else "",
                                 "%d about typical" % len(par) if par else "") if p)
    return sub, body


FIT_WORDS = {"right": "Right level for the stakes", "higher": "Could have handed over more",
             "lower": "Handed over too much for the stakes", "other": "Not judged"}
FIT_GLYPH = {"right": "✓", "higher": "↑", "lower": "↓", "other": "·"}


def lvl(i):
    return "%d (%s)" % (i + 1, LEVELS[i][0])


def and_join(items):
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def levels_section(tasks, scores, background=0):
    """One bar per level: length = sessions there, colour = whether the level fit the stakes.
    Each count opens the sessions behind it, right under the bar. Sessions are never numbered."""
    placed = [(t, level_index(t.get("level"))) for t in tasks]
    counts = [sum(1 for _, li in placed if li == i) for i in range(len(LEVELS))]
    top = max(counts + [1])
    rows = []
    for i, (name, what) in enumerate(LEVELS):
        group = [t for t, li in placed if li == i]
        if i == 4:
            rows.append('<div class="lv-divide" aria-hidden="true">Levels 5–8: usually set up with a technical colleague</div>')
        segs = "".join('<span class="seg %s" style="width:%.1f%%"></span>' % (f, 100.0 * k / top)
                       for f, k in ((f, sum(fit_of(t) == f for t in group)) for f in ("right", "higher", "lower", "other")) if k)
        label = '<span class="lv-name"><span class="lv-num">Level %d</span>%s<small>%s</small></span>' % (i + 1, esc(name), esc(what))
        track = '<span class="lv-track">%s</span>' % segs
        if not group:
            rows.append('<div class="lv-row empty"><div class="lv-grid">%s%s<span class="lv-val num"><span class="none">0</span></span></div></div>'
                        % (label, track))
            continue
        items = "".join(
            '<li><span class="fit %s" aria-hidden="true">%s</span><div><strong>%s</strong><br>'
            '<span class="fit-word">%s%s.</span> <span class="soft">%s</span>%s</div></li>' % (
                fit_of(t), FIT_GLYPH[fit_of(t)], text(t.get("session"), 60),
                esc(FIT_WORDS[fit_of(t)]),
                (" (better: %s)" % esc(LEVELS[level_index(t.get("better_level"))][0]))
                if fit_of(t) in ("higher", "lower") and level_index(t.get("better_level")) is not None else "",
                text(t.get("why") or t.get("verdict") or "", 240), checked_line(t)) for t in group)
        rows.append('<details class="lv-row"><summary><span class="lv-grid">%s%s<span class="lv-val num">'
                    '<span class="lv-count">%d session%s</span>%s</span></span></summary><ul class="lv-sessions">%s</ul></details>' % (
                        label, track, len(group), "" if len(group) == 1 else "s", ICON["chev"], items))
    head = ""
    if "right_level" in scores:
        head = '<p class="lv-head">%d of %d sessions were at the right level for what was at stake.</p>' % scores["right_level"]
    used = [li for _, li in placed if li is not None]
    facts = ""
    if used:
        lo, hi = min(used), max(used)
        if lo == hi:
            facts = "<li><strong>All at level %s</strong></li>" % esc(lvl(lo))
        else:
            mostly = [i for i, c in enumerate(counts) if c == max(counts)]
            if len(mostly) > 2:
                verdict = "Spread evenly across %d levels" % len(mostly)
            else:
                verdict = "Mostly level%s %s" % ("s" if len(mostly) > 1 else "", and_join([lvl(i) for i in mostly]))
                above = sorted({li for t, li in placed if li is not None and li > max(mostly) and fit_of(t) == "right"})
                if above:
                    verdict += ", with the beginnings of level%s %s" % ("s" if len(above) > 1 else "", and_join([lvl(i) for i in above]))
            facts = "<li><strong>Range: level %s to level %s</strong></li><li>%s.</li>" % (esc(lvl(lo)), esc(lvl(hi)), esc(verdict))
    note = ('<p class="lv-note"><strong>Higher isn\'t better.</strong> Each task needs the level that fits what\'s at stake. '
            'For most staff, levels 1–4 are the sweet spot.</p>')
    present = {fit_of(t) for t, _ in placed}
    none = lambda f: "" if f in present else ' <span class="soft">(none this time)</span>'
    legend = ('<div class="legend"><span><span class="key seg-key right"></span>Right level for the stakes%s</span>'
              '<span><span class="key seg-key higher"></span>Could have handed over more%s</span>'
              '<span><span class="key seg-key lower"></span>Handed over too much%s</span></div>') % (none("right"), none("higher"), none("lower"))
    return (head + ('<ul class="lv-facts">%s</ul>' % facts if facts else "") + note + legend
            + '<div class="lv-chart">%s</div><p class="soft lv-hint">Click a count to see the sessions behind it.%s</p>' % ("".join(rows), (
                " Not in the bars: %d scheduled run%s that worked without you, which is level 6 (Assistant) work."
                % (background, "" if background == 1 else "s")) if background else ""))


def checked_line(t):
    if not isinstance(t.get("checked"), bool):
        return ""
    if t["checked"]:
        return '<br><span class="chk yes">Checked the work: yes</span>'
    note = safe(t.get("check_note") or "", 160)
    return '<br><span class="chk no">Checked the work: no%s</span>' % (" · " + esc(note) if note else "")


SCOPE = {
    "local": ("Claude Code: every project on this computer, plus Cowork tasks saved here.", None),
    "chat": ("Your claude.ai chats outside projects.",
             "This doesn't look inside your projects. To check one, run this check-in inside that project."),
    "project": ("This project's chats only.",
                "For your chats outside projects, run this check-in outside any project. For another project, run it inside that one."),
}
WHERE_TO_RUN = ("In Claude Code, a check-in covers all your Claude Code work on that computer. In claude.ai, it covers "
                "your chats outside projects; inside a project, it covers that project only.")


def short_date(v):
    d = parse_ts(v) if v else None
    return "%d %s" % (d.day, d.strftime("%b")) if d else None


def scope_card(r):
    mode = r.get("mode") if r.get("mode") in SCOPE else "local"
    covered, hint = SCOPE[mode]
    read = len([t for t in (r.get("tasks") or []) if isinstance(t, dict)][:TASKS_MAX])
    unit = "session" if mode == "local" else "chat"
    since = short_date(r.get("since_date")) if r.get("basis") == "since-last" else None
    days = r.get("window_days") if isinstance(r.get("window_days"), int) else None
    if since:
        how = ": everything new since your last check-in on %s." % esc(since)
    elif days:
        how = " from the last %d days, picked to cover different projects and kinds of work." % days
    else:
        how = ", picked to cover different projects and kinds of work."
    sample = ('<p class="scope-lead"><strong>%d %s%s read closely</strong>%s</p>' % (read, unit, "" if read == 1 else "s", how)) if read else ""
    note = '<p class="scope-hint">%s</p>' % esc(hint) if hint else ""
    return ('<section class="scope" aria-label="Scope"><span class="eyebrow">What this check-in covered</span>'
            '<p class="scope-what"><span class="sc" aria-hidden="true">✓</span>%s</p>%s%s</section>' % (esc(covered), sample, note))


TILES = [  # (score key, eyebrow, what the number counts)
    ("right_level", "Ways of working", "sessions at the right level for what was at stake"),
    ("checks", "Checked the work", "sessions where you said what done looks like upfront, reviewed the plan, or checked the result"),
    ("habits_ahead", "Habits", "habits ahead of typical"),
]


def compute_scores(r):
    tasks = [t for t in (r.get("tasks") or []) if isinstance(t, dict)][:TASKS_MAX]
    judged = [t for t in tasks if fit_of(t) != "other"]
    checked = [t for t in tasks if isinstance(t.get("checked"), bool)]
    rows = habit_rows(r)
    scores = {}
    if judged:
        scores["right_level"] = (sum(fit_of(t) == "right" for t in judged), len(judged))
    if checked:
        scores["checks"] = (sum(t["checked"] for t in checked), len(checked))
    if rows:
        scores["habits_ahead"] = (len(habit_groups(rows)[0]), len(rows))
    return scores


def tiles(r):
    scores = compute_scores(r)
    prev = (r.get("previous") or {}).get("scores") if isinstance(r.get("previous"), dict) else None
    prev = prev if isinstance(prev, dict) else {}
    out = []
    if not scores:
        return ""
    for key, eyebrow, what in TILES:
        if key not in scores:
            why_not = "needs 8 or more sessions read closely to compare" if key == "habits_ahead" else "nothing to judge this time"
            out.append('<div class="tile quiet"><span class="eyebrow">%s</span><div class="big num">–</div><div class="what">%s</div></div>'
                       % (esc(eyebrow), why_not))
            continue
        x, y = scores[key]
        was = ratio(prev.get(key))
        change = ""
        if was:
            now_share, then_share = x / float(y), was[0] / float(was[1])
            mark = "▲" if now_share > then_share + 0.01 else "▼" if now_share < then_share - 0.01 else "="
            change = '<div class="was">%s from %d of %d last time</div>' % (mark, was[0], was[1])
        more = ""
        if key == "habits_ahead" and y > x:
            more = '<a class="tile-link" href="#habits">See the %d you could improve →</a>' % (y - x)
        out.append('<div class="tile"><span class="eyebrow">%s</span><div class="big num">%d<small> of %d</small></div>'
                   '<div class="what">%s</div>%s%s</div>' % (esc(eyebrow), x, y, esc(what), change, more))
    return '<div class="tiles">%s</div>' % "".join(out) if out else ""


def is_template(v):
    return bool(PLACEHOLDER.search(str(v or "")))


def report_problems(r):
    """What the skill must fix before the page is drawn: every experiment names the real session it
    replays, and a template prompt (one with blanks) comes with an example filled in from that session."""
    probs = []
    for n, x in enumerate([x for x in (r.get("experiments") or []) if isinstance(x, dict)][:3], 1):
        if not str(x.get("from") or "").strip():
            probs.append('experiment %d: add "from", the real session it replays (e.g. "7 Oct · meeting agenda")' % n)
        if is_template(x.get("prompt")):
            example = str(x.get("example") or "").strip()
            if not example:
                probs.append('experiment %d: the prompt is a template (it has blanks), so add "example": '
                             'the same prompt filled in for the session in "from"' % n)
            elif is_template(example):
                probs.append("experiment %d: the example still has blanks; fill each one in from the real session" % n)
    return probs


def experiment_card(i, x):
    d = x.get("direction") if x.get("direction") in DIRECTIONS else "same+check"
    src = safe(x.get("from") or "", 80)
    template = is_template(x.get("prompt"))
    example = ""
    if x.get("example"):
        example = '<span class="pr-label">%s</span><div class="prompt example">%s</div>' % (
            esc("Filled in for " + src if src else "An example from your week"), esc(mask(x["example"])[:1500]))
    return ('<li class="step"><span class="marker">%d</span><div class="step-card"><div class="step-body">'
            '<span class="dir">%s</span><h3>%s</h3><p class="soft">%s</p>%s'
            '<span class="pr-label">%s</span><div class="prompt">%s</div>%s'
            '<p class="teach">Not sure how? Say &ldquo;walk me through %d&rdquo; and we\'ll practise it on a real task.</p></div>'
            '<div class="done-strip">%s<p><span class="done-label">Your check</span>%s</p></div></div></li>') % (
        i, esc(DIRECTION_LABEL[d]), text(x.get("title"), 120), text(x.get("why"), 500),
        '<p class="from">From your week: <strong>%s</strong></p>' % esc(src) if src else "",
        "Template: fill in the blanks" if template else "Copy this", esc(mask(x.get("prompt") or "")[:1500]), example,
        i, ICON["check"], text(sentence(x.get("check")), 300))


def about_section(r):
    items = [("Where to run it.", WHERE_TO_RUN)]
    if (r.get("mode") or "local") == "local":
        items.append(("Fresh or since last time.", "Ask any time for a fresh check-in on up to %d recent sessions. "
                      "Scheduled check-ins read only what's new since the last one." % SAMPLE_SIZE))
    items += [("Where “typical” comes from.", "Anthropic's study of claude.ai chats in January 2026. Your sessions are "
               "different work, so treat the comparison as a guide, not a grade."),
              ("Private.", "Made from your own sessions. Nobody else sees it unless you share it.")]
    return '<ul class="about">%s</ul>' % "".join("<li><strong>%s</strong> %s</li>" % (esc(a), esc(b)) for a, b in items)


def render_body(r):
    parts = []
    since = short_date(r.get("since_date")) if r.get("basis") == "since-last" else None
    span = ("since %s" % since) if since else ("last %s days" % safe(r.get("window_days"), 5))
    parts.append('<header><span class="eyebrow">AI check-in · %s · %s</span><h1>How you\'re working with AI</h1></header>'
                 % (text(r.get("date"), 20), esc(span)))
    parts.append(scope_card(r))
    parts.append(stage_track(r))
    tasks = [t for t in (r.get("tasks") or []) if isinstance(t, dict)][:TASKS_MAX]
    scores = compute_scores(r)
    parts.append(tiles(r))

    since_items = [x for x in (r.get("since_last") or []) if x][:4]
    if since_items:
        parts.append('<section class="card since"><span class="eyebrow">Since last time</span><ul>%s</ul></section>'
                     % "".join("<li>%s</li>" % text(x, 300) for x in since_items))

    exps = [x for x in (r.get("experiments") or []) if isinstance(x, dict)][:3]
    if exps:
        parts.append('<section><span class="eyebrow">Try this week</span><h2>%d thing%s to try</h2><ol class="trail">%s</ol></section>'
                     % (len(exps), "" if len(exps) == 1 else "s", "".join(experiment_card(i, x) for i, x in enumerate(exps, 1))))

    folds = []
    if tasks:
        top = max(range(len(LEVELS)), key=lambda i: sum(level_index(t.get("level")) == i for t in tasks))
        sub = ("%d of %d sessions at the right level for what was at stake" % scores["right_level"]
               if "right_level" in scores else "Mostly level %d, %s" % (top + 1, LEVELS[top][0]))
        runs = r.get("scheduled_runs") if isinstance(r.get("scheduled_runs"), int) and not isinstance(r.get("scheduled_runs"), bool) else 0
        folds.append(fold("stairs", "Ways of working", esc(sub), levels_section(tasks, scores, max(0, min(runs, 9999))), open_=True))
    if habit_rows(r):
        sub, body = habits_section(r, len(tasks))
        folds.append(fold("chart", "Your habits vs typical", esc(sub), body, open_=True, anchor="habits"))
    else:
        folds.append(fold("chart", "Your habits vs typical", "Too few sessions to compare this time",
                          '<p class="soft">Come back after a busier week.</p>', anchor="habits"))
    strengths = [x for x in (r.get("strengths") or []) if isinstance(x, dict)][:2]
    edges = [x for x in (r.get("edges") or []) if isinstance(x, dict)][:2]
    if strengths or edges:
        col = lambda items, label, cls: "".join('<div class="note %s"><span class="eyebrow">%s</span>%s%s</div>' % (
            cls, label, text(x.get("text"), 400), quote(x.get("quote"), x.get("ref"))) for x in items)
        folds.append(fold("scale", "Strengths and gaps", "%d strength%s · %d to grow" % (
            len(strengths), "" if len(strengths) == 1 else "s", len(edges)),
            '<div class="pair"><div>%s</div><div>%s</div></div>' % (col(strengths, "Strong", ""), col(edges, "Worth growing", "grow"))))
    fix = r.get("fix") if isinstance(r.get("fix"), dict) else None
    if fix and fix.get("line"):
        folds.append(fold("pin", "Make one fix stick", text(fix.get("summary") or fix.get("pattern"), 90),
                          '<p>%s</p><div class="prompt">%s</div><p class="soft">Where it goes: %s. Nothing in your files has been changed. To add it, tell Claude "add that fix".</p>' % (
                              text(fix.get("pattern"), 600), text(fix.get("line"), 300), text(str(fix.get("where") or "").rstrip(". "), 300))))
    surfaces = [s for s in (r.get("surfaces") or []) if isinstance(s, dict)][:3]
    if surfaces:
        body = []
        for s in surfaces:
            body.append('<h3>%s</h3><p>%s</p>' % (text(s.get("name"), 40), text(s.get("summary"), 900)))
            for p in (s.get("points") or [])[:3]:
                if isinstance(p, dict):
                    body.append("<p><strong>%s</strong> %s</p>%s" % (text(p.get("label"), 40), text(p.get("text"), 400), quote(p.get("quote"), p.get("ref"))))
        folds.append(fold("screens", "Where you work", esc(", ".join(safe(s.get("name"), 40) for s in surfaces)), "".join(body)))
    folds.append(fold("info", "About this check-in", "What it can see, and where to run it", about_section(r)))
    parts.append('<section><span class="eyebrow">Dig deeper</span>%s</section>' % "".join(folds))

    sched = text(r.get("schedule_line") or "", 300)
    parts.append("<footer>%s<p>Sources: your Builder stage comes from the NSLS Automation Tracker (Kevin's rubric); "
                 "“ways of working” are adapted from Every's eight levels of AI adoption; the habits come from "
                 "Anthropic's AI Fluency Index.</p><p class=\"contact\">Questions or ideas about this check-in? Send them to Davo Wood.</p></footer>"
                 % ("<p>%s</p>" % sched if sched else ""))
    return "\n".join(parts)


def template_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "report-template.html")


def cmd_render(args):
    report = load_json(args.file, None)
    if not isinstance(report, dict):
        sys.exit("could not read a JSON object from %s" % args.file)
    try:
        with open(template_path(), encoding="utf-8") as fh:
            shell = fh.read()
    except OSError:
        sys.exit("report template missing: %s" % template_path())
    if shell.count("{{BODY}}") != 1:
        sys.exit("report template must contain the body slot exactly once")
    problems = report_problems(report)
    if problems:
        sys.exit("fix needed before the report is drawn:\n" + "\n".join("  - " + p for p in problems))
    head, _, tail = shell.partition("{{BODY}}")
    if not report.get("covered_to"):  # where this check-in's reading stopped, for the next --since-last
        latest = load_json(digest_path(), {})
        made = parse_ts(latest.get("generated")) if isinstance(latest, dict) else None
        try:
            drafted = dt.datetime.fromtimestamp(os.path.getmtime(args.file), tz=dt.timezone.utc)
        except OSError:
            drafted = None
        # only a scan made before this draft was written can be the one it came from
        if made and drafted and made <= drafted and drafted - made < dt.timedelta(days=3):
            report["covered_to"] = (latest.get("window") or {}).get("to")
    date = str(report.get("date") or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        date = dt.date.today().isoformat()
        report["date"] = date
    report["scores"] = {k: "%d/%d" % v for k, v in compute_scores(report).items()}
    out = os.path.join(reports_dir(), "%s.html" % date)
    write_private(out, head + render_body(report) + tail)
    print("report: %s" % out)
    if not args.no_record:
        write_private(history_path(), json.dumps(clean_run(report)) + "\n", append=True)
        print("history: recorded %s" % date)
    if args.open:
        open_file(out)


def open_file(path):
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False, timeout=15)
        elif os.name == "nt":
            os.startfile(path)  # noqa: available on Windows only
        else:
            subprocess.run(["xdg-open", path], check=False, timeout=15)
    except (OSError, subprocess.SubprocessError, AttributeError):
        print("open it yourself: %s" % path)


# ---------------------------------------------------------------- commands

def gather(since, email, notes):
    """{surface: (root or None, sessions not made by this skill)}; self-runs noted."""
    found = {}
    for name in ("code", "cowork"):
        root, sessions = scan_code(since, notes) if name == "code" else scan_cowork(since, notes, email)
        mine = [s for s in sessions if not s["is_self"]]
        if len(sessions) != len(mine):
            notes.append("%s: set aside %d of this skill's own runs" % (name, len(sessions) - len(mine)))
        found[name] = (root, mine)
    return found


def cmd_scan(args):
    now = dt.datetime.now(dt.timezone.utc)
    cfg = load_config()
    email = known_email(cfg)
    previous = last_run()
    notes, surfaces, digests, skims = [], {}, [], []
    basis, since_checkin, found, since = "fresh", None, None, None
    if args.since_last:
        cut = parse_ts((previous or {}).get("covered_to"))
        if cut and cut < now:
            basis, since, since_checkin = "since-last", cut, cut.date().isoformat()
            found = gather(since, email, notes)
        else:
            notes.append("no earlier check-in on record, so this is a fresh check-in")
    if found is None:  # fresh: widen the window until there are enough sessions to read
        windows = (args.days,) if args.days else WIDEN_DAYS
        for days in windows:
            notes_now = []
            since = now - dt.timedelta(days=days)
            found = gather(since, email, notes_now)
            if sum(len(readable(ss)) for _, ss in found.values()) >= args.max_per_surface:
                break
        notes.extend(notes_now)
    days = max(1, -(-int((now - since).total_seconds()) // 86400))
    for name, (root, mine) in found.items():
        if root is None:
            surfaces[name] = {"found": False}
            continue
        active = [s for s in mine if s["turns"] or s["signals"]["tools"]]
        picked = sample(active, args.max_per_surface, prefer_new=basis == "since-last")
        surfaces[name] = {"found": True, "aggregate": aggregate(active), "sampled": len(picked),
                          "carried_over": sum(1 for s in picked if s["earlier"])}
        digests.extend(shape(s, i) for i, s in enumerate(picked, 1))
        rest = sorted((s for s in active if s not in picked and not s["automated"]),
                      key=lambda s: s["start"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        skims.extend({"surface": name, "title": s["title"], "project": s["project"], "human_turns": len(s["turns"]),
                      "signals": {k: v for k, v in (("skills", sorted(s["signals"]["skills"])[:4]),
                                                    ("connectors", sorted(s["signals"]["connectors"])[:4]),
                                                    ("subagents", s["signals"]["subagents"]),
                                                    ("ship", sum(s["signals"]["ship"].values()))) if v}}
                     for s in rest[-SKIM_MAX:])
    stage = {"stage": None, "source": "not checked (--no-tracker)"} if args.no_tracker else tracker_stage(cfg)
    result = {
        "generated": now.isoformat(),
        "window": {"from": since.isoformat(), "to": now.isoformat(), "days": days,
                   "basis": basis, "since_checkin": since_checkin},
        "stage_on_record": stage,
        "settings": {k: cfg.get(k) for k in ("frequency", "tracker_lookup") if k in cfg},
        "previous_run": previous,
        "surfaces": surfaces,
        "chat": {"found": False, "note": "claude.ai chats are not on this computer; run the skill inside claude.ai to include them"},
        "sessions": digests,
        "skimmed": skims,
        "notes": notes,
    }
    write_private(digest_path(), json.dumps(result, indent=1, default=str))
    seen = ", ".join("%s %d sessions" % (k, v["aggregate"]["sessions"]) for k, v in surfaces.items() if v.get("found"))
    print("digest: %s" % digest_path())
    print("window: %s | %s | read closely %d | stage on record: %s (%s)" % (
        "since the check-in on %s" % since_checkin if basis == "since-last" else "last %d days" % days,
        seen or "no sessions found", len(digests), stage["stage"], stage["source"]))
    for n in notes:
        print("note: %s" % n)


def cmd_config(args):
    cfg = load_config()
    for pair in args.pairs:
        if "=" not in pair:
            sys.exit("expected key=value, got %r" % pair)
        k, v = pair.split("=", 1)
        if k == "frequency" and v in FREQUENCIES:
            cfg[k] = v
        elif k == "tracker_lookup" and v.lower() in ("true", "false"):
            cfg[k] = v.lower() == "true"
        elif k == "email" and EMAIL.fullmatch(v):
            cfg[k] = v
        else:
            sys.exit("not a setting I keep: %s (frequency=%s, tracker_lookup=true|false, email=you@nsls.org)"
                     % (pair, "|".join(FREQUENCIES)))
    if args.pairs:
        save_config(cfg)
    shown = {k: v for k, v in cfg.items() if k != "email"}
    shown["email"] = "set" if cfg.get("email") else "not set"
    print(json.dumps(shown, indent=1))


def main(argv=None):
    p = argparse.ArgumentParser(prog="digest.py")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("scan")
    s.add_argument("--days", type=int, default=None, help="fix the window instead of widening it")
    s.add_argument("--since-last", action="store_true", help="only what is new since the last check-in")
    s.add_argument("--max-per-surface", type=int, default=SAMPLE_SIZE)
    s.add_argument("--no-tracker", action="store_true")
    r = sub.add_parser("render")
    r.add_argument("file")
    r.add_argument("--no-record", action="store_true")
    r.add_argument("--open", action="store_true")
    c = sub.add_parser("config")
    c.add_argument("pairs", nargs="*")
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("-"):
        argv = ["scan"] + list(argv)
    args = p.parse_args(argv)
    {"scan": cmd_scan, "render": cmd_render, "config": cmd_config}[args.cmd](args)


if __name__ == "__main__":
    main()
