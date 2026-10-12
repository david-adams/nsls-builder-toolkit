#!/usr/bin/env python3
"""Print the Builder Guardrails policy as session context. Nothing else.

Registered in hooks.json as its own SessionStart hook (startup, resume, clear,
compact) and launched through run-hook.sh like every other toolkit hook.

Why a hook of its own. Claude Code caps each hook's output at 10,000
characters; past that, Claude gets the first 2,000 and a file path it is never
asked to open. Printed from session-start.py, the policy shared one budget with
every notice and announcement that hook makes, and from 2026-09-20 the section
alone was over the cap: Claude saw the tier table and the hard-gate list, never
the decline and event commands or the exploration log. Its own hook gives it
its own budget, and a matcher that includes compact and clear means it is back
after every compaction instead of fading into the summary.

The section extraction and path resolution stay in session-start.py
(`emit_guardrails_context`, reached through the `__policy__` run name) so they
exist once. That run name pulls nothing and pings nothing: this hook fires
several times a session, and a second credit ping is the duplicate-credit race.
"""
import runpy
from pathlib import Path

try:
    runpy.run_path(str(Path(__file__).resolve().with_name("session-start.py")),
                   run_name="__policy__")
except Exception:
    pass  # no policy is the status quo, never a reason to break a session
