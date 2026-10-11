#!/usr/bin/env python3
"""ai-level-up: read your own Claude Code and Cowork sessions on this computer,
reduce them to a masked digest the skill can judge, and render the report.

Standard library only, Python 3.8+. Everything is written under the private
state folder (~/.ai-level-up, or AI_LEVEL_UP_DIR). The one optional network
call reads your own Builder stage from the NSLS tracker.

  digest.py scan [--days 7] [--max-per-surface 8] [--no-tracker]
  digest.py render REPORT.json [--no-record] [--open]
  digest.py config [frequency=weekly|monthly|never|ask] [tracker_lookup=true|false] [email=you@nsls.org]
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
QUOTE_WORDS = 15
STAGES = ("Explorer", "Maker", "Builder", "Architect", "Steward")
FREQUENCIES = ("weekly", "monthly", "never", "ask")
DIRECTIONS = ("up", "same+check", "down", "habit")
BEHAVIOURS = [  # (habit, share of claude.ai chats showing it in Anthropic's AI Fluency Index, Jan 2026, D)
    ("Iterates and refines", 85.7, "Description"), ("Shows an example of good", 41.1, "Description"),
    ("Specifies format", 30.0, "Description"), ("Sets how the AI should work", 30.0, "Delegation"),
    ("Gives tone and style", 22.7, "Description"), ("Names who it's for", 17.6, "Description"),
    ("States the goal first", 51.1, "Delegation"), ("Asks for the approach first", 10.1, "Delegation"),
    ("Fills in missing context", 20.3, "Discernment"), ("Pushes back on reasoning", 15.8, "Discernment"),
    ("Checks facts that matter", 8.7, "Discernment"),
]


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
    ("credential", re.compile(r"(?i)[\"']?\b(?:api[_\- ]?key|secret|token|password|passwd|pwd|client[_\- ]?secret|access[_\- ]?key)\b[\"']?\s*[:=]\s*[\"']?[^\s\"',;}]{6,}")),
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
        "is_self": False, "slots": set(),
    }


def ingest_line(s, o, since):
    """Fold one JSONL record into session s. Only timestamped records inside the window count."""
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
        return
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
    elif not s["turns"] and clean.startswith(SCHEDULED_PREFIX):
        s["is_self"] = True
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
    out, unreadable = [], 0
    for f in glob.glob(os.path.join(root, "*", "*.jsonl")):
        try:
            if os.path.getmtime(f) < cutoff:  # transcripts are append-only
                continue
        except OSError:
            unreadable += 1
            continue
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
                    ingest_line(s, o, since)
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
    return root, out


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
    if len(accounts) > 1:
        if not email or email.lower() not in accounts:
            notes.append("cowork: several Claude accounts on this computer and none confirmed as yours; "
                         "Cowork skipped (say 'my work email is …' to include it)")
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


def sample(sessions, k):
    """Pick up to k sessions a person actually drove: varied in project and length."""
    pool = [s for s in sessions if not s["automated"] and len(s["turns"]) >= 3]
    if len(pool) < k:
        pool = [s for s in sessions if not s["automated"] and s["turns"]]
    pool.sort(key=lambda s: (-len(s["turns"]), s["id"]))
    picked, projects = [], set()
    for s in pool:  # one per project first, longest first
        if len(picked) < k and s["project"] not in projects:
            picked.append(s)
            projects.add(s["project"])
    rest = [s for s in pool if s not in picked]
    while len(picked) < k and rest:  # then fill, alternating long and short
        picked.append(rest.pop(0 if len(picked) % 2 else -1))
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
    out["stage"] = run.get("stage") if run.get("stage") in STAGES else None
    seen = run.get("seen_counts") if isinstance(run.get("seen_counts"), dict) else {}
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
    "ladder": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M7 2v18M15 2v18M7 6h8M7 11h8M7 16h8"/></svg>',
    "sliders": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M4 6h14M4 11h14M4 16h14"/><circle cx="8" cy="6" r="1.8" fill="currentColor"/><circle cx="14" cy="11" r="1.8" fill="currentColor"/><circle cx="10" cy="16" r="1.8" fill="currentColor"/></svg>',
    "bars": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M3 5h12M3 11h16M3 17h8"/></svg>',
    "spark": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M11 3v4M11 15v4M3 11h4M15 11h4M6 6l2 2M14 14l2 2M16 6l-2 2M8 14l-2 2"/></svg>',
    "wrench": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.5 4.5a4 4 0 00-5 5L3 15l2 2 5.5-5.5a4 4 0 005-5l-2.5 2.5-2-2z"/></svg>',
    "screens": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><rect x="2.5" y="4" width="17" height="11" rx="2"/><path d="M8 18.5h6"/></svg>',
    "list": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="M8 6h11M8 11h11M8 16h11"/><circle cx="4" cy="6" r=".8"/><circle cx="4" cy="11" r=".8"/><circle cx="4" cy="16" r=".8"/></svg>',
    "info": '<svg class="sym" width="22" height="22" viewBox="0 0 22 22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="8.5"/><path d="M11 10v5.5M11 6.6v.2"/></svg>',
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


def fold(icon, title, sub, body, open_=False):
    return ('<details class="fold"%s><summary>%s<span class="t"><strong>%s</strong><span>%s</span></span>%s</summary>'
            '<div class="fold-body">%s</div></details>') % (" open" if open_ else "", ICON[icon], esc(title), sub, ICON["chev"], body)


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


def behaviour_rows(r):
    beh = r.get("behaviours") if isinstance(r.get("behaviours"), dict) else {}
    prev = (r.get("previous") or {}).get("behaviours") if isinstance(r.get("previous"), dict) else {}
    prev = prev if isinstance(prev, dict) else {}
    rows, ahead, counted = [], 0, 0
    for i, (name, typ, d) in enumerate(BEHAVIOURS, 1):
        got = ratio(beh.get(str(i)))
        if not got:
            continue
        counted += 1
        you = 100.0 * got[0] / got[1]
        diff = you - typ
        ahead += diff >= 5
        word = "ahead" if diff >= 5 else "behind" if diff <= -5 else "about typical"
        tick, was = "", ratio(prev.get(str(i)))
        if was:
            tick = '<span class="prev" style="left:%.1f%%" title="Last check-in: %d of %d"></span>' % (100.0 * was[0] / was[1], was[0], was[1])
        rows.append(
            '<div class="bar-row"><div class="bar-label">%s<small>%s</small></div>'
            '<div><div class="tracks" title="You: %d of %d (%.0f%%) · Typical: %.0f%%">'
            '<div class="trk"><div class="fill you" style="width:%.1f%%"></div>%s</div>'
            '<div class="trk thin"><div class="fill typ" style="width:%.1f%%"></div></div></div>'
            '<div class="vals num"><span>You %d of %d · %.0f%%</span><span>Typical %.0f%%</span></div></div>'
            '<div class="delta num">%s %s%d<small>%s</small></div></div>' % (
                esc(name), esc(d), got[0], got[1], you, typ, you, tick, typ, got[0], got[1], you, typ,
                "▲" if diff >= 5 else "▼" if diff <= -5 else "≈", "+" if diff > 0 else "−" if diff < 0 else "",
                abs(round(diff)), word))
    return rows, ahead, counted


FIT_WORDS = {"right": "Right level for the stakes", "higher": "Could have handed over more",
             "lower": "Handed over too much for the stakes", "other": "Not judged"}
FIT_GLYPH = {"right": "✓", "higher": "↑", "lower": "↓", "other": "·"}


def levels_section(tasks, scores):
    """Plain bar chart: one row per level, bar length = sessions there, colour = whether the level fit.
    The count links to the sessions behind it. Sessions are never numbered, so only levels carry numbers."""
    placed = [(t, level_index(t.get("level"))) for t in tasks]
    counts = [sum(1 for _, li in placed if li == i) for i in range(len(LEVELS))]
    top = max(counts + [1])
    rows, groups = [], []
    for i, (name, what) in enumerate(LEVELS):
        group = [t for t, li in placed if li == i]
        if i == 4:
            rows.append('<div class="lv-divide" aria-hidden="true">Levels 5–8: usually set up with a technical colleague</div>')
        segs = "".join('<span class="seg %s" style="width:%.1f%%" title="%d: %s"></span>' % (
            f, 100.0 * k / top, k, FIT_WORDS[f])
            for f, k in ((f, sum(fit_of(t) == f for t in group)) for f in ("right", "higher", "lower", "other")) if k)
        value = ('<a href="#level-%d">%d session%s</a>' % (i + 1, len(group), "" if len(group) == 1 else "s")
                 if group else '<span class="none">0</span>')
        rows.append('<div class="lv-row%s"><div class="lv-name"><span class="lv-num">Level %d</span>%s<small>%s</small></div>'
                    '<div class="lv-track">%s</div><div class="lv-val num">%s</div></div>' % (
                        "" if group else " empty", i + 1, esc(name), esc(what), segs, value))
        if group:
            items = "".join(
                '<li><span class="fit %s" aria-hidden="true">%s</span><div><strong>%s</strong><br>'
                '<span class="fit-word">%s%s.</span> <span class="soft">%s</span>%s</div></li>' % (
                    fit_of(t), FIT_GLYPH[fit_of(t)], text(t.get("session"), 60),
                    esc(FIT_WORDS[fit_of(t)]),
                    (" (better: %s)" % esc(LEVELS[level_index(t.get("better_level"))][0]))
                    if fit_of(t) in ("higher", "lower") and level_index(t.get("better_level")) is not None else "",
                    text(t.get("why") or t.get("verdict") or "", 240), checked_line(t)) for t in group)
            groups.append('<div class="lv-group" id="level-%d"><h4>Level %d · %s: %d session%s</h4><ul>%s</ul></div>' % (
                i + 1, i + 1, esc(name), len(group), "" if len(group) == 1 else "s", items))
    head = ""
    if "right_level" in scores:
        x, y = scores["right_level"]
        head = '<p class="lv-head"><strong>%d of %d sessions</strong> were at the right level for what was at stake.</p>' % (x, y)
    used = [li for _, li in placed if li is not None]
    span = ""
    if used:
        lo, hi = min(used), max(used)
        mostly = max(set(used), key=lambda i: (used.count(i), -i))
        above = sorted({li for t, li in placed if li is not None and li > mostly and fit_of(t) == "right"})
        verdict = "Mostly level %d (%s)" % (mostly + 1, esc(LEVELS[mostly][0]))
        if above:
            verdict += ", with the beginnings of level %d (%s)" % (above[0] + 1, esc(LEVELS[above[0]][0]))
        span = ('<p class="lv-verdict"><strong>%s.</strong> Your range this time: level %d (%s) to level %d (%s).</p>'
                '<p class="soft">Higher isn\'t better: each task needs the level that fits what\'s at stake. '
                'For most staff, levels 1–4 are the sweet spot.</p>' % (verdict, lo + 1, esc(LEVELS[lo][0]), hi + 1, esc(LEVELS[hi][0])))
    present = {fit_of(t) for t, _ in placed}
    none = lambda f: "" if f in present else ' <span class="soft">(none this time)</span>'
    legend = ('<div class="legend"><span><span class="key seg-key right"></span>Right level for the stakes%s</span>'
              '<span><span class="key seg-key higher"></span>Could have handed over more%s</span>'
              '<span><span class="key seg-key lower"></span>Handed over too much%s</span></div>') % (none("right"), none("higher"), none("lower"))
    return (head + span + legend + '<div class="lv-chart">%s</div>' % "".join(rows)
            + '<h3 class="why-h">The sessions behind each bar</h3>%s' % "".join(groups))


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
WHERE_TO_RUN = ("Where to run your check-in: in Claude Code it covers all your Claude Code work on that computer. "
                "In claude.ai it covers your chats outside projects, and inside a project it covers that project.")


def scope_card(r):
    mode = r.get("mode") if r.get("mode") in SCOPE else "local"
    covered, hint = SCOPE[mode]
    read = len([t for t in (r.get("tasks") or []) if isinstance(t, dict)])
    unit = "session" if mode == "local" else "chat"
    sample = ("<p class=\"scope-lead\"><strong>%d %s%s read closely</strong>, picked to cover different projects and kinds of work.</p>"
              % (read, unit, "" if read == 1 else "s")) if read else ""
    note = '<p class="scope-hint">%s</p>' % esc(hint) if hint else ""
    return ('<section class="scope" aria-label="Scope"><span class="eyebrow">What this check-in covered</span>'
            '<p class="scope-what"><span class="sc" aria-hidden="true">✓</span>%s</p>%s%s</section>' % (esc(covered), sample, note))


TILES = [  # (score key, eyebrow, what the number counts)
    ("right_level", "Ways of working", "sessions at the right level for what was at stake"),
    ("checks", "Checked the work", "sessions where you said what done looks like upfront, reviewed the plan, or checked the result"),
    ("habits_ahead", "Habits", "habits ahead of typical"),
]


def compute_scores(r):
    tasks = [t for t in (r.get("tasks") or []) if isinstance(t, dict)][:12]
    judged = [t for t in tasks if fit_of(t) != "other"]
    checked = [t for t in tasks if isinstance(t.get("checked"), bool)]
    _, ahead, counted = behaviour_rows(r)
    scores = {}
    if judged:
        scores["right_level"] = (sum(fit_of(t) == "right" for t in judged), len(judged))
    if checked:
        scores["checks"] = (sum(t["checked"] for t in checked), len(checked))
    if counted:
        scores["habits_ahead"] = (ahead, counted)
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
        out.append('<div class="tile"><span class="eyebrow">%s</span><div class="big num">%d<small> of %d</small></div>'
                   '<div class="what">%s</div>%s</div>' % (esc(eyebrow), x, y, esc(what), change))
    return '<div class="tiles">%s</div>' % "".join(out) if out else ""


def render_body(r):
    parts = []
    parts.append('<header><span class="eyebrow">AI check-in · %s · last %s days</span><h1>How you\'re working with AI</h1></header>'
                 % (text(r.get("date"), 20), text(r.get("window_days"), 5)))
    parts.append(scope_card(r))
    parts.append(stage_track(r))
    tasks = [t for t in (r.get("tasks") or []) if isinstance(t, dict)][:12]
    scores = compute_scores(r)
    rows, ahead, counted = behaviour_rows(r)
    parts.append(tiles(r))

    since = [x for x in (r.get("since_last") or []) if x][:4]
    if since:
        parts.append('<section class="card since"><span class="eyebrow">Since last time</span><ul>%s</ul></section>'
                     % "".join("<li>%s</li>" % text(x, 300) for x in since))

    exps = [x for x in (r.get("experiments") or []) if isinstance(x, dict)][:3]
    if exps:
        steps = []
        for i, x in enumerate(exps, 1):
            d = x.get("direction") if x.get("direction") in DIRECTIONS else "same+check"
            steps.append(
                '<li class="step"><span class="marker">%d</span><div class="step-card"><div class="step-body">'
                '<span class="dir">%s</span><h3>%s</h3><p class="soft">%s</p><div class="prompt">%s</div>'
                '<p class="teach">Not sure how? Say &ldquo;walk me through %d&rdquo; and we\'ll practise it on a real task.</p></div>'
                '<div class="done-strip">%s<p><span class="done-label">Your check</span>%s</p></div></div></li>' % (
                    i, esc(DIRECTION_LABEL[d]), text(x.get("title"), 120), text(x.get("why"), 500),
                    esc(mask(x.get("prompt") or "")[:1500]), i, ICON["check"], text(sentence(x.get("check")), 300)))
        parts.append('<section><span class="eyebrow">Try this week</span><h2>%d thing%s to try</h2><ol class="trail">%s</ol></section>'
                     % (len(exps), "" if len(exps) == 1 else "s", "".join(steps)))

    folds = []
    if tasks:
        top = max(range(len(LEVELS)), key=lambda i: sum(level_index(t.get("level")) == i for t in tasks))
        sub = ("%d of %d sessions at the right level for what was at stake" % scores["right_level"]
               if "right_level" in scores else "Mostly level %d, %s" % (top + 1, LEVELS[top][0]))
        folds.append(fold("sliders", "Ways of working", esc(sub), levels_section(tasks, scores), open_=True))
    if counted:
        legend = '<div class="legend"><span><span class="key" style="background:var(--you)"></span>You</span><span><span class="key" style="background:var(--typical)"></span>Typical</span>%s</div>' % (
            '<span><span class="key" style="background:var(--ink);width:2px;border-radius:0"></span>Last check-in</span>' if isinstance(r.get("previous"), dict) and r["previous"].get("behaviours") else "")
        body = legend + '<div class="bars">%s</div><p class="soft">"Typical" is the share of claude.ai chats showing each habit in Anthropic\'s study.%s</p>' % (
            "".join(rows), " With fewer than 8 sessions, read these as rough." if 0 < len(tasks) < 8 else "")
        folds.append(fold("bars", "Your habits vs typical", "Ahead of typical on %d of %d" % (ahead, counted), body))
    else:
        folds.append(fold("bars", "Your habits vs typical", "Too few sessions to compare this week", '<p class="soft">Come back after a busier week.</p>'))
    strengths = [x for x in (r.get("strengths") or []) if isinstance(x, dict)][:2]
    edges = [x for x in (r.get("edges") or []) if isinstance(x, dict)][:2]
    if strengths or edges:
        col = lambda items, label, cls: "".join('<div class="note %s"><span class="eyebrow">%s</span>%s%s</div>' % (
            cls, label, text(x.get("text"), 400), quote(x.get("quote"), x.get("ref"))) for x in items)
        folds.append(fold("spark", "Strengths and gaps", "%d strength%s · %d to grow" % (
            len(strengths), "" if len(strengths) == 1 else "s", len(edges)),
            '<div class="pair"><div>%s</div><div>%s</div></div>' % (col(strengths, "Strong", ""), col(edges, "Worth growing", "grow"))))
    fix = r.get("fix") if isinstance(r.get("fix"), dict) else None
    if fix and fix.get("line"):
        folds.append(fold("wrench", "Make one fix stick", text(fix.get("summary") or fix.get("pattern"), 90),
                          '<p>%s</p><div class="prompt">%s</div><p class="soft">Where it goes: %s. Nothing in your files has been changed. To add it, tell Claude "add that fix".</p>' % (
                              text(fix.get("pattern"), 600), text(fix.get("line"), 300), text(fix.get("where"), 300))))
    surfaces = [s for s in (r.get("surfaces") or []) if isinstance(s, dict)][:3]
    if surfaces:
        body = []
        for s in surfaces:
            body.append('<h3>%s</h3><p>%s</p>' % (text(s.get("name"), 40), text(s.get("summary"), 900)))
            for p in (s.get("points") or [])[:3]:
                if isinstance(p, dict):
                    body.append("<p><strong>%s</strong> %s</p>%s" % (text(p.get("label"), 40), text(p.get("text"), 400), quote(p.get("quote"), p.get("ref"))))
        folds.append(fold("screens", "Where you work", esc(", ".join(safe(s.get("name"), 40) for s in surfaces)), "".join(body)))
    about = "".join("<p>%s</p>" % text(r[k], 800) for k in ("paradox", "method") if r.get(k))
    if about:
        folds.append(fold("info", "About this check-in", "What was read, and one pattern to watch", about))
    parts.append('<section><span class="eyebrow">Dig deeper</span>%s</section>' % "".join(folds))

    parts.append("<footer>Your Builder stage comes from the NSLS Automation Tracker (Kevin's rubric). "
                 "“Ways of working” are adapted from Every's eight levels of AI adoption, and the habits "
                 "from Anthropic's AI Fluency Index. This report was made from your own sessions. Nobody else "
                 "sees it unless you share it. %s<br>%s</footer>" % (text(r.get("schedule_line") or "", 300), esc(WHERE_TO_RUN)))
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
    head, _, tail = shell.partition("{{BODY}}")
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

def cmd_scan(args):
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=args.days)
    cfg = load_config()
    notes, surfaces, digests, skims = [], {}, [], []
    email = known_email(cfg)
    for name in ("code", "cowork"):
        root, sessions = scan_code(since, notes) if name == "code" else scan_cowork(since, notes, email)
        if root is None:
            surfaces[name] = {"found": False}
            continue
        mine = [s for s in sessions if not s["is_self"]]
        active = [s for s in mine if s["turns"] or s["signals"]["tools"]]
        picked = sample(active, args.max_per_surface)
        surfaces[name] = {"found": True, "aggregate": aggregate(active), "sampled": len(picked)}
        digests.extend(shape(s, i) for i, s in enumerate(picked, 1))
        rest = sorted((s for s in active if s not in picked and not s["automated"]),
                      key=lambda s: s["start"] or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
        skims.extend({"surface": name, "title": s["title"], "project": s["project"], "human_turns": len(s["turns"]),
                      "signals": {k: v for k, v in (("skills", sorted(s["signals"]["skills"])[:4]),
                                                    ("connectors", sorted(s["signals"]["connectors"])[:4]),
                                                    ("subagents", s["signals"]["subagents"]),
                                                    ("ship", sum(s["signals"]["ship"].values()))) if v}}
                     for s in rest[-SKIM_MAX:])
        if len(sessions) != len(mine):
            notes.append("%s: set aside %d of this skill's own runs" % (name, len(sessions) - len(mine)))
    stage = {"stage": None, "source": "not checked (--no-tracker)"} if args.no_tracker else tracker_stage(cfg)
    result = {
        "generated": now.isoformat(),
        "window": {"from": since.isoformat(), "to": now.isoformat(), "days": args.days},
        "stage_on_record": stage,
        "settings": {k: cfg.get(k) for k in ("frequency", "tracker_lookup") if k in cfg},
        "previous_run": last_run(),
        "surfaces": surfaces,
        "chat": {"found": False, "note": "claude.ai chats are not on this computer; run the skill inside claude.ai to include them"},
        "sessions": digests,
        "skimmed": skims,
        "notes": notes,
    }
    write_private(digest_path(), json.dumps(result, indent=1, default=str))
    seen = ", ".join("%s %d sessions" % (k, v["aggregate"]["sessions"]) for k, v in surfaces.items() if v.get("found"))
    print("digest: %s" % digest_path())
    print("window: last %d days | %s | sampled %d | stage on record: %s (%s)" % (
        args.days, seen or "no sessions found", len(digests), stage["stage"], stage["source"]))
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
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--max-per-surface", type=int, default=20)
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
