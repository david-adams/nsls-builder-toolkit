#!/usr/bin/env python3
"""The exploration log's checkpoint hook: UserPromptSubmit and Stop.

Registered in hooks.json for both events and launched through run-hook.sh.
The rules live in logit_core.py; this file only reads the hook payload and
answers it.

  UserPromptSubmit  count the session's work from the transcript; once it is
                    due, hand Claude the question (plain stdout on this event
                    reaches Claude and is not shown to the builder).
  Stop              if the question went unanswered this turn, continue Claude
                    once with a reminder (additionalContext, a plain-facts
                    system reminder); if the reminder went unanswered too,
                    record it as ignored and stop asking this session.

Never blocks a tool call, never exits non-zero, prints nothing it does not
mean Claude to read. A broken checkpoint is a missing question, not a broken
session.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import logit_core as core  # noqa: E402


def retry_pending_detached():
    """Logs that could not reach the tracker are retried off the hook's path:
    a network call here would sit between the builder and their prompt."""
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().with_name("log-it.py")), "retry-pending"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def on_prompt(payload):
    sid = str(payload.get("session_id") or "")
    if not sid:
        return
    key = core.build_key(payload.get("cwd") or os.getcwd())
    pending = False
    out = ""
    tx = core.locked()  # Busy (another session writing) ends the hook quietly: no ask this time
    with tx as data:
        sess = core.session(data, sid)
        sess["key"] = key
        core.scan(sess, payload.get("transcript_path") or "")
        b = core.build(data, key)
        core.note_work(sess, b)
        # A scheduled task runs with nobody at the keyboard to answer an offer.
        automated = str(payload.get("user_prompt") or payload.get("prompt") or "").lstrip().startswith(
            "<scheduled-task")
        if not automated and core.due(sess, b, key):
            sess["asked_at"] = core.now_iso()
            sess["answer"] = ""
            sess["reminded"] = False
            out = core.checkpoint_text(sess, b, key, sid)
        pending = bool(data.get("pending"))
    if pending:
        retry_pending_detached()
    # An ask that wasn't saved would be asked again at the next message.
    if out and tx.saved:
        print(out)


def on_stop(payload):
    sid = str(payload.get("session_id") or "")
    if not sid:
        return
    out = None
    tx = core.locked()
    with tx as data:
        sess = data["sessions"].get(sid)
        if not sess or not sess.get("asked_at") or sess.get("answer"):
            return
        if not sess.get("reminded"):
            sess["reminded"] = True
            out = core.reminder_text(sid, sess.get("key") or core.build_key(payload.get("cwd")))
        else:
            sess["answer"] = "ignored"
            sess["changes_at_answer"] = sess.get("changes", 0)
            b = core.build(data, sess.get("key") or core.build_key(payload.get("cwd")))
            b["ignored"] = b.get("ignored", 0) + 1
    if out and tx.saved:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": out}}))


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    event = payload.get("hook_event_name")
    if event == "UserPromptSubmit":
        on_prompt(payload)
    elif event == "Stop":
        on_stop(payload)


if __name__ == "__main__":
    try:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        main()
    except Exception:
        pass
    sys.exit(0)
