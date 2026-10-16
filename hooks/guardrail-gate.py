#!/usr/bin/env python3
"""
guardrail-gate.py — PreToolUse hook implementing the four NSLS hard gates.

Skills can't block. They're description-matched, so a builder who never types a
trigger phrase never meets one. This hook is deterministic: it sees every Bash,
Write and Edit call regardless of what the builder said.

The four gates (see CLAUDE.md § Builder Guardrails):
  1. NSLS work in a personal repo        — git remote isn't an NSLS org,
                                           and no .personal-repo file
  2. Tier 3 ship with no tracker record  — deploying member-facing, unowned
  3. Production write at scale           — bulk writes, no reviewer, no rollback
  4. Off-platform at Tier 2+             — non-Anthropic SDK on a shared build

DESIGN RULES, in priority order:

*   **Fail open, always.** Every failure path allows the action. A guardrail
    that bricks someone's session costs more trust than the risk it averts.
    Unparseable input, no network, no git, missing config — allow.
*   **False positives are the failure mode.** A gate that fires when it
    shouldn't teaches builders to route around the toolkit, and then it
    protects nobody. Every pattern here is deliberately narrow. When in doubt,
    stay silent.
*   **Never a flat no.** Each block states the policy AND the way through,
    including the authorization route. See _shared/references/guardrail-voice.md.
*   **Escape hatch.** NSLS_GUARDRAILS_DISABLED=1 turns everything off. A gate
    with no off-switch is a gate that gets uninstalled.
"""

import glob
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _config_dir() -> Path:
    """The Claude config directory, resolved without ever raising.

    `Path.home()` raises when neither HOME nor a password-database entry
    resolves — a bare launchd job, a stripped container, some CI images. At
    module scope that kills the hook during import, BEFORE main()'s fail-open
    handler exists, so every gate is silently off and nothing says why. Each
    hook resolves this for itself rather than sharing a helper: these scripts
    are launched directly by the hook runner, and an import that can fail is
    the same outage one level up.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw)
    try:
        return Path.home() / ".claude"
    except Exception:
        return Path(tempfile.gettempdir()) / "nsls-claude-config"

TRACKER_URL = os.environ.get(
    "NSLS_TRACKER_URL", "https://web-production-6281e.up.railway.app"
)
NSLS_ORGS = ("thensls",)

# A repo declares itself personal with this file at its top level, and gate 1
# then leaves it alone. Before it, a builder's own tool was blocked on every
# push for as long as its README said "NSLS", even to say it was not NSLS work,
# and a repo Kevin had authorized to stay put was blocked again on the next
# push, because nothing remembered the authorization.
PERSONAL_MARKER = ".personal-repo"

# The hook's total budget is 10s (hooks.json). Every subprocess and socket here
# has to fit inside it *cumulatively*, because a gate can chain several: repo
# lookup, then a tracker lookup, then the event POST on the way out. Blowing the
# budget means the harness kills the hook mid-decision and the block is lost —
# the gate silently fails to fire. These numbers are picked so the worst path
# (gate 2: repo_root + tracker_get + emit) lands near 6s, leaving headroom on a
# slow network. Raise them and redo that arithmetic.
GIT_TIMEOUT = 2
NET_TIMEOUT = 3
EMIT_TIMEOUT = 1.5


# Appended to every block. Some gates will misfire in situations we could not
# simulate, and a builder who hits a wrong block with no way to say so loses
# trust in the whole toolkit. This is the only channel through which a false
# positive becomes visible: it emits guardrail_disputed, which surfaces in
# Signal's guardrail report where Davo will actually see it.
FEEDBACK = (
    "\n\n---\n"
    # Two clauses here are load-bearing and were lost in the first length trim.
    # "why you think it misfired" is the reason the dispute event is worth
    # emitting at all -- without it the report says a gate fired and nothing
    # about whether it should have. "not a complaint form" is what makes a
    # builder willing to use it; people who think they're filing a complaint
    # against the tooling mostly just don't.
    "If this block looks wrong, say so — I'll log what you were doing and why "
    "you think it misfired, straight to Davo. Genuinely useful, not a "
    "complaint form: getting these wrong is worse than not having them."
)


def builder_email():
    """Same precedence as session-start.py and skill-event.sh."""
    try:
        cfg = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
        env_file = cfg / "local-plugins" / "nsls-personal-toolkit" / ".env"
        if env_file.is_file():
            for line in env_file.read_text(errors="ignore").splitlines():
                if line.startswith("BUILDER_EMAIL="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return git("config", "user.email")


# Reporting comes from the shared emitter so the payload contract with
# POST /guardrail-event lives in exactly one file (guardrail_emit.py). It used
# to be inlined here, which is how the gate ended up the only thing in the
# toolkit that emitted anything at all, and how the docstring describing the
# payload drifted out of date.
#
# Imported defensively: fail open is the first design rule above, and a hook
# that cannot import its reporting module must still make the decision. A gate
# that fires without recording is worth far more than one that does not fire.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import plugin_beacon as _beacon
except Exception:  # pragma: no cover - a missing beacon never blocks a decision
    class _beacon:  # type: ignore
        @staticmethod
        def record(*a, **k):
            return False

try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from guardrail_emit import emit_detached as _emit
except Exception:  # pragma: no cover - reporting is optional, deciding is not
    def _emit(*a, **k):
        return ""


def emit(event_type: str, description: str, automation: str = ""):
    """Fire-and-forget guardrail event. Never raises, never affects the decision.

    Deduplication is off here. Every hard block is a distinct event worth a row
    even when the same gate stops the same build twice in a day -- a builder
    hitting a wall repeatedly is the signal, and collapsing it would hide the
    gate most in need of a second look.

    Detached, because the endpoint takes ~1.5s and this hook has 10s for the
    whole decision. The previous version waited inline on a 1.5s timeout, which
    means the block events we believed were being recorded were landing about
    half the time. EMIT_TIMEOUT in guardrail_emit.py has the measurements.
    """
    log = os.environ.get("NSLS_GUARDRAIL_EVENT_LOG")
    if log:
        # Test seam. Never set in normal use: the tests need to count events
        # per decision (a double-fire writes TWO guardrail_blocked rows, which
        # would corrupt the very metric the gates are measured by) and that
        # cannot be asserted against a detached network POST.
        try:
            with open(log, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": event_type,
                                     "description": description,
                                     "automation": automation}) + "\n")
        except Exception:
            pass
        return
    try:
        _emit(event_type, description, automation=automation, dedupe=False)
    except Exception:
        pass  # reporting is never worth failing or delaying a decision over


def allow():
    """Exit silently, permitting the tool call. Every error path lands here."""
    sys.exit(0)


def block(reason: str, gate: str = "", automation: str = ""):
    # Every block names a fix the builder can apply in the next minute —
    # assign the reviewer, register the automation. Serving them cached
    # evidence afterwards would block them again for having done what they
    # were asked. The cache exists to spare a repeated round trip inside one
    # burst of calls, never to outlive the state it describes.
    _tracker_cache_clear()
    emit("guardrail_blocked", f"{gate}: {reason.splitlines()[0]}", automation)
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason + FEEDBACK,
                }
            }
        )
    )
    sys.exit(0)


# ---------------------------------------------------------------- helpers


_GIT_CACHE = {}


def git(*args, cwd=None):
    """Memoised per process. Three gates ask for the repo root independently;
    without the cache that's three subprocesses against the same 10s budget."""
    key = (args, cwd)
    if key in _GIT_CACHE:
        return _GIT_CACHE[key]
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=GIT_TIMEOUT,
            cwd=cwd,
        )
        val = out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        val = ""
    _GIT_CACHE[key] = val
    return val


def repo_root(start=None):
    return git("rev-parse", "--show-toplevel", cwd=start) or ""


def origin_url(cwd=None):
    return git("remote", "get-url", "origin", cwd=cwd)


def is_nsls_remote(url: str) -> bool:
    """True only for a confidently-NSLS remote.

    Unknown hosts return True (allow) on purpose — this decides whether to
    BLOCK, so ambiguity must resolve to silence.
    """
    if not url:
        return True
    low = url.lower()
    if "github.com" not in low:
        return True  # not GitHub; not our call to make
    m = re.search(r"github\.com[:/]+([^/]+)/", low)
    if not m:
        return True
    return m.group(1) in NSLS_ORGS


def looks_like_nsls_work(root: str) -> bool:
    """Narrow test for 'this repo is NSLS work'.

    Requires positive evidence — an NSLS system named in tracked config or
    docs. A personal scratch repo with no NSLS fingerprints is none of our
    business, and treating it as ours is exactly the false positive that
    makes builders resent the toolkit.
    """
    if not root:
        return False
    # "nsls" alone is decisive. The SaaS names are not — plenty of people use
    # Airtable or PostHog for their own projects, and blocking someone's
    # weekend build is exactly the false positive that gets the toolkit
    # uninstalled. So they only count as evidence in pairs. (Tightened after
    # Codex review 2026-08-15 flagged the fingerprints as too broad.)
    decisive = ("nsls",)
    corroborating = (
        "hubspot",
        "customer.io",
        "customerio",
        "airtable",
        "feather",
        "posthog",
    )
    try:
        seen = set()
        for name in ("README.md", "CLAUDE.md", "DESIGN.md", "package.json",
                     "pyproject.toml", ".env.example", "requirements.txt"):
            p = Path(root) / name
            if not p.is_file():
                continue
            try:
                text = p.read_text(errors="ignore").lower()[:20000]
            except Exception:
                continue
            if any(n in text for n in decisive):
                return True
            seen.update(n for n in corroborating if n in text)
        # customer.io/customerio are the same system; don't let them pair up.
        if "customer.io" in seen and "customerio" in seen:
            seen.discard("customerio")
        return len(seen) >= 2
    except Exception:
        pass
    return False


MAX_BODY = 1 << 20  # 1 MiB — a tracker reply is a few KB; anything else is wrong

# Measured on this repo, 25 runs per shape, 2026-09-20: a plain Bash or Edit
# call costs 27 ms p50 (Python startup, regexes, no I/O) and a `git push` 52 ms.
# A deploy-shaped command costs 776 ms p50 / 831 ms p95 — all of it the tracker
# round trip, and all of it paid again on the next call, because the hook is a
# fresh process every time and the in-process cache dies with it. That is the
# only number worth a cache: a builder iterating on a deploy pays it per
# keystroke-turn, and thirty builders point it at one Railway instance. The git
# lookups were measured and deliberately left alone — 25 ms does not justify
# a staleness window on the answer to "whose repo is this".
_TRACKER_CACHE_TTL = 120
_TRACKER_CACHE_DIR = _config_dir() / ".nsls-gate-tracker-cache"


def _cache_key(path: str) -> str:
    import hashlib

    return hashlib.sha256(f"{TRACKER_URL}{path}".encode()).hexdigest()[:32]


def _tracker_cached(path: str):
    """A recent page for this path, or None. Only ever caches a real answer."""
    try:
        # Read the clear stamp either side of the read. An unlinked file stays
        # readable to whoever already opened it, so a reader that started
        # before block() cleared the cache still gets the page block just threw
        # away — and blocks the builder again for having complied. Comparing
        # the stamp afterwards catches exactly that overlap.
        cleared_before = _tracker_cleared_at()
        f = _TRACKER_CACHE_DIR / _cache_key(path)
        if time.time() - f.stat().st_mtime > _TRACKER_CACHE_TTL:
            return None
        page = json.loads(f.read_text(encoding="utf-8"))
        if _tracker_cleared_at() != cleared_before:
            return None
        if isinstance(page, dict) and isinstance(page.get("records"), list):
            return page
        return None
    except Exception:
        return None


def _text(value) -> str:
    """Coerce one tracker field to a string the gates can match on.

    The tracker proxies Airtable, and an Airtable single-select field does not
    come back as a string — it comes back as {"id", "name", "color"}. Calling
    .lower() on one raises AttributeError, main() catches it as "one broken
    gate never takes down the rest", and the action is allowed. A gate that
    fails open on a field shape is worse than no gate, because the empty
    events table reads identically either way.

    A string is itself; the Airtable object yields its name; anything else
    yields "", which every caller already treats as "no scope stated" and
    declines to act on.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        name = value.get("name")
        if isinstance(name, str):
            return name
    return ""


# The Automation Tracker's Scope single-select, as the policy's tiers (CLAUDE.md
# § Builder Guardrails). Its five choices, read from the live schema 2026-10-16:
# Personal, Department, Company-wide, Customer Facing: Advisors and Customer
# Facing: Members. Both gates that need a tier ask scope_tier() instead of
# keeping a substring list each, because both lists missed the Customer Facing
# choices: 9 builds were registered as Customer Facing: Members that day, Tier 3
# in the policy's own words ("members see it"), and neither the reviewer check
# nor the platform check ever looked at them. A new Scope choice needs a line
# here, or both gates read it as unknown. Highest tier first, so a scope that
# names two tiers is judged by the higher, as the old substring tests did.
_SCOPE_TIERS = (
    (3, ("company", "customer facing")),
    (2, ("department",)),
    (1, ("personal",)),
)


def scope_tier(value) -> int:
    """The policy tier a tracker Scope names: 1, 2 or 3, or 0 for none.

    0 means "I can't tell", and both gates treat it as silence: an empty,
    missing or unrecognised scope is not evidence of anything (Codex review
    2026-08-15). Takes the field as the tracker sent it, string or Airtable
    object. Hyphens, underscores and runs of spaces read as one space, so
    "customer-facing" and "Customer  Facing" are the same words.
    """
    scope = re.sub(r"[\s_-]+", " ", _text(value).lower())
    for tier, words in _SCOPE_TIERS:
        if any(w in scope for w in words):
            return tier
    return 0


_TRACKER_CLEAR_STAMP = ".cleared-at"


def _tracker_cleared_at() -> float:
    try:
        return (_TRACKER_CACHE_DIR / _TRACKER_CLEAR_STAMP).stat().st_mtime
    except Exception:
        return 0.0


def _tracker_cache_clear():
    """Drop EVERY cached page. Best-effort, and deliberately unbounded.

    Called from block(), where correctness depends on it finishing: a page
    left behind is stale evidence that outlives the block that invalidated it.
    A cap by iteration position would not even bound the work reliably — the
    directory is walked in filesystem order, so the cap drops an arbitrary
    subset rather than the cheapest one. The directory holds at most a couple
    of minutes of pages and this runs only on a block, which is rare.
    """
    try:
        for entry in _TRACKER_CACHE_DIR.iterdir():
            if entry.name == _TRACKER_CLEAR_STAMP:
                continue
            try:
                entry.unlink()
            except OSError:
                continue
    except Exception:
        pass
    # Record WHEN the clear happened. A sibling process can be mid-request
    # right now; without this its reply lands after the clear and the next
    # hook reads evidence the block just invalidated, so a builder who fixed
    # the tracker record is blocked again for up to the whole TTL.
    try:
        _TRACKER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_TRACKER_CACHE_DIR / _TRACKER_CLEAR_STAMP).touch()
    except Exception:
        pass


def _tracker_store(path: str, page, fetched_at: float = 0.0):
    # None means "I don't know" and must never be cached: it would turn one
    # tracker hiccup into two minutes of blind spots.
    if not isinstance(page, dict) or not isinstance(page.get("records"), list):
        return
    # A reply whose request began before the last clear is already stale, even
    # though it arrived after it. block() clears the cache precisely so the
    # builder who fixes the tracker record is not blocked again for complying;
    # letting a pre-clear reply land would reinstate the evidence the block
    # just threw away, for the rest of the TTL. Cheap and lock-free: the clear
    # leaves a timestamp, and a request older than it does not get to write.
    if fetched_at and fetched_at < _tracker_cleared_at():
        return
    try:
        _TRACKER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(_TRACKER_CACHE_DIR, 0o700)
        except OSError:
            pass
        f = _TRACKER_CACHE_DIR / _cache_key(path)
        # mkstemp, not a predictable name. write_text() on a fixed ".tmp" path
        # follows a symlink planted there first, so anything this process can
        # write becomes a target — and the 0700 on the directory lands after
        # the attacker's file already exists. mkstemp creates O_EXCL with 0600
        # and cannot follow a link.
        fd, tmp_name = tempfile.mkstemp(dir=str(_TRACKER_CACHE_DIR), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(page))
            os.replace(tmp_name, f)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except Exception:
        pass


def _http_get(url: str):
    """Raw bytes from the tracker, or None. A seam so the evidence rules below
    can be tested without a network."""
    try:
        import urllib.request

        with urllib.request.urlopen(urllib.request.Request(url),
                                    timeout=NET_TIMEOUT) as r:
            return r.read(MAX_BODY)
    except Exception:
        return None


def tracker_page(path: str):
    """One page of automation records: {"records": [...], "total": N|None}.

    None means "I don't know", and every caller must treat it as silence.

    The distinction is load-bearing and has been got wrong twice. Callers block
    on a positively empty answer and stay silent on None, so ANY reply we cannot
    confidently read — HTTP error, unreadable body, a 200 carrying an error
    object, an unexpected shape, an explicit success:false — has to be None.
    Codex review 2026-08-15 caught the original returning [] for a malformed
    200, which turned a tracker hiccup into a denied deploy. Codex review
    2026-09-20 caught {"success": false, "records": []} doing the same thing.
    """
    cached = _tracker_cached(path)
    if cached is not None:
        return cached

    started = time.time()
    raw = _http_get(f"{TRACKER_URL}{path}")
    if raw is None:
        return None
    try:
        data = json.loads(raw.decode(errors="replace"))
    except Exception:
        return None

    total = None
    if isinstance(data, list):
        recs = data
    elif isinstance(data, dict):
        # An explicit failure flag outranks whatever else the body carries.
        # `is not True`, not `not ...`. The string "false" is truthy, so the
        # old test read {"success": "false"} as a success, tracker_lookup
        # returned "absent", and gate 4 blocked a deploy on a malformed reply.
        # Same class as the scope/name coercion below: a field's TYPE is part
        # of the answer, and anything that is not the expected type means "I
        # cannot read this", which for this gate is fail-open.
        if "success" in data and data.get("success") is not True:
            return None
        # The tracker answers {"count": N, "records": [...], "success": true}.
        # This client only ever accepted an "automations" key, so every reply
        # parsed as None — the fail-open path — and gates 2 and 4 have therefore
        # been incapable of firing for as long as they have existed,
        # independently of the hooks.json wiring. Verified against the live
        # endpoint 2026-09-20. "automations" stays accepted in case the API is
        # ever corrected toward the name this client expected.
        if "records" in data:
            recs = data.get("records")
        elif "automations" in data:
            recs = data.get("automations")
        else:
            return None  # unrecognised shape — do not infer "none found"
        count = data.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            count = None
        total = count
    else:
        return None

    if not isinstance(recs, list):
        return None
    # A list with a non-dict inside is a half-garbled reply. Filtering the
    # garbage out silently turned "temporary error" strings into an empty
    # result — which callers read as "positively no record" and BLOCKED on.
    for r in recs:
        if not isinstance(r, dict):
            return None

    page = {"records": recs, "total": total}
    _tracker_store(path, page, fetched_at=started)
    return page


# The tracker returns at most this many records and honours no name, search or
# pagination parameter — every query comes back as the same first page of the
# table (151 rows on 2026-09-20; verified against the live endpoint with name=,
# search=, q=, limit= and offset=, all ignored). So a name missing from the
# reply means "not in the first 50", NOT "not registered", and blocking on that
# would be a false positive on two thirds of the tracker — the failure mode
# these gates care most about. Until the endpoint can answer the question,
# absence is treated as unknown and only a positive match is acted on.
_TRACKER_PAGE_CAP = 50


def tracker_lookup(name: str):
    """("found", record) | ("absent", None) | ("unknown", None).

    Callers must treat "unknown" as silence. Collapsing it into "absent" is what
    turns a tracker hiccup — or a paging limit — into a blocked deploy.
    """
    from urllib.parse import quote

    page = tracker_page(f"/automations?name={quote(name, safe='')}")
    if page is None:
        return "unknown", None
    recs = page["records"]
    total = page.get("total")
    # Check the reply is self-consistent BEFORE matching anything in it. A page
    # declaring fewer records than it carries — count 0 with a row in it — is
    # garbled, and "found" from a garbled page is enough to block a deploy. The
    # count is checked after the match below for the opposite reason: a page
    # smaller than the table cannot prove ABSENCE. Consistency has to be
    # established first, because it undermines both answers.
    if isinstance(total, int) and total < len(recs):
        return "unknown", None
    for r in recs:
        if _text(r.get("name")).lower() == name.lower():
            return "found", r
    if total is not None and total > len(recs):
        return "unknown", None  # the reply is one page of a larger table
    if len(recs) >= _TRACKER_PAGE_CAP:
        return "unknown", None  # a capped page proves nothing about absence
    return "absent", None


# ── Command segmentation ────────────────────────────────────────────────────
# Four Macroscope findings on PR #157 shared one root cause: the gates matched
# RAW COMMAND TEXT. So `echo 'git push origin main'` read as a push, a harmless
# `git push --help` in one segment vouched for a real push after the semicolon,
# `--dry-run` anywhere disabled the bulk-write gate for the whole line, and
# `cd elsewhere && railway up` was judged against the shell's cwd instead of
# the directory actually being deployed. The fix is one idea applied
# everywhere: split the command into the segments that actually execute, and
# judge each segment by its own tokens — where a quoted string is ONE token,
# so text inside quotes can never look like a command.

_SEG_BREAKS = frozenset({";", "&&", "||", "|", "&", "\n"})

# Which shell will run the command being judged. normalize_call presents a
# PowerShell call to the gates as Bash, because the command gates read one
# shape; this is the one fact that must survive that, and main() sets it.
_POWERSHELL = False


def _join_ps_continuations(cmd: str) -> str:
    """A trailing backtick continues a PowerShell line, so `git -C C:\\repo `
    then `push origin main` on the next line is one command.

    Never inside a here-string: its body is data, and a body line ending in a
    backtick joined onto the closing '@ hid that delimiter, so everything after
    it - a real push included - was read as here-string text (Macroscope).
    """
    out, here_end, pending = [], None, ""
    for line in cmd.splitlines():
        if here_end is not None:
            out.append(line)
            if line.lstrip().startswith(here_end):
                here_end = None
            continue
        line = pending + line
        pending = ""
        m = re.search(r"`[ \t]*$", _ps_code_part(line))
        if m:
            pending = line[:m.start()] + " "
            continue
        out.append(line)
        m_ps = re.search(r"""@(['"])\s*$""", line)
        if m_ps:
            here_end = m_ps.group(1) + "@"
    if pending:
        out.append(pending)
    return "\n".join(out)


def _ps_code_part(line: str) -> str:
    """The line up to a PowerShell `#` comment, quotes respected.

    A backtick at the end of a comment is comment text, not a continuation;
    joining on it swallowed the next line - a real push - into the comment
    (Macroscope). `#` starts a comment at the line start or after whitespace.
    """
    quote, escaped = None, False
    for i, c in enumerate(line):
        if escaped:  # a backtick escapes the next character, `" included
            escaped = False
            continue
        if c == "`" and quote != "'":  # single quotes are literal in PowerShell
            escaped = True
        elif quote:
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
        elif c == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def command_segments(cmd: str):
    """Token lists for each independently-executed segment, quotes resolved.

    Returns None when the command cannot be tokenized (unbalanced quotes,
    heredocs). Callers must then fall back to their old whole-string
    behaviour — degraded precision, never a crash.

    A backslash is an escape in bash and an ordinary character in PowerShell.
    Tokenizing a PowerShell command the bash way turned `git -C
    C:\\Users\\x\\repo push origin main` into `C:Usersxrepo`, repo_root()
    found nothing, and the gate let a real push through (PC test,
    2026-09-30). The same unquoted path through the Bash tool is mangled by
    Git Bash itself, so nothing is pushed there, and bash rules stay right
    for Bash.
    """
    # Newlines separate commands exactly like semicolons, but shlex eats them
    # as whitespace — so a two-line command folded into ONE segment, and an
    # `echo --dry-run` on line one excused the real write on line two.
    # Tokenize per line; a quoted string that spans lines fails that line's
    # parse, and one failed line fails the whole command into the callers'
    # conservative whole-string fallback.
    segments = []
    heredoc_end = None  # inside a heredoc: skip body lines until the delimiter
    ps_here_end = None  # inside a PowerShell here-string: the same, for @' '@
    if _POWERSHELL:
        cmd = _join_ps_continuations(cmd)
    for line in cmd.splitlines():
        if ps_here_end is not None:
            stripped = line.lstrip()
            if not stripped.startswith(ps_here_end):
                continue  # here-string body is data, like a heredoc's
            ps_here_end = None
            line = stripped[2:]  # after the '@ or "@: `'@ | Out-File x` still runs
        if heredoc_end is not None:
            if line.strip() == heredoc_end:
                heredoc_end = None
            continue  # heredoc BODY is data — tokenizing it as commands made
            # a heredoc that merely CONTAINED "git push origin main" block
        # The delimiter is a WORD, and bash words include -, ., + and more.
        # `\w+` truncated `END-1` to `END`, so the terminator line never
        # matched, heredoc mode never exited, and every command after it was
        # skipped — silently disabling all four gates for the rest of the
        # command. A gate that can be switched off by a hyphen is not a gate.
        if _POWERSHELL:
            m_ps = re.search(r"""@(['"])\s*$""", line)
            if m_ps:
                ps_here_end = m_ps.group(1) + "@"
                line = line[:m_ps.start()]  # `$x = @'` - the part before still counts
        m_here = None if _POWERSHELL else re.search(
            r"""<<-?\s*(?:'([^']+)'|"([^"]+)"|([A-Za-z0-9_.+-]+))""", line)
        if m_here:
            heredoc_end = m_here.group(1) or m_here.group(2) or m_here.group(3)
            line = line[:m_here.start()]  # the command part before << still counts
        if not line.strip():
            continue
        try:
            lex = shlex.shlex(line, posix=True, punctuation_chars=";|&")
            lex.whitespace_split = True
            if _POWERSHELL:
                lex.escape = "`"  # PowerShell escapes with a backtick, never "\"
            tokens = list(lex)
        except ValueError:
            return None
        current = []
        for tok in tokens:
            if tok and all(c in ";|&" for c in tok):
                if current:
                    segments.append(current)
                    current = []
            else:
                current.append(tok)
        if current:
            segments.append(current)
    return segments


_WRAPPERS = frozenset({"env", "command", "exec", "nohup", "nice", "time"})
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def strip_wrappers(seg):
    """Drop leading VAR=… assignments and transparent wrappers.

    `RAILWAY_TOKEN=$T railway up` and `command git push origin main` are the
    same actions with the executable not at seg[0]; anchoring there let both
    walk past the gates. `env -i` style flags after `env` are dropped too.
    Fail-open by construction: stripping can only EXPOSE an executable to the
    gates, never hide one.
    """
    i = 0
    while i < len(seg) and _ASSIGN_RE.match(seg[i]):
        i += 1
    while i < len(seg) and seg[i] in _WRAPPERS:
        i += 1
        while i < len(seg) and seg[i].startswith("-"):
            i += 1
        while i < len(seg) and _ASSIGN_RE.match(seg[i]):
            i += 1
    rest = seg[i:]
    return [_exe_name(rest[0])] + rest[1:] if rest else rest


def _exe_name(tok: str) -> str:
    """The command a token runs, as the gates compare it.

    `git`, `Git`, `git.exe` and `& "C:\\Program Files\\Git\\cmd\\git.exe"`
    all run git on Windows (and `Git` on a case-insensitive Mac disk too),
    but only the first matched `== "git"`, so the rest walked past every gate.
    """
    name = re.split(r"[\\/]", tok)[-1]
    # Fold case only where names are case-insensitive: PowerShell, and the
    # default Mac and Windows disks Git Bash and zsh resolve against. On a
    # case-sensitive Linux shell `Git` may be a different program (Macroscope).
    if _POWERSHELL or sys.platform in ("darwin", "win32"):
        name = name.lower()
    for ext in (".exe", ".cmd", ".bat"):
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


# PowerShell's ways of changing directory, beside plain cd (an alias there too).
_CD_COMMANDS = frozenset({"cd", "chdir", "set-location", "sl"})
_PUSH_COMMANDS = frozenset({"pushd", "push-location"})
_POP_COMMANDS = frozenset({"popd", "pop-location"})


def _location_args(args):
    """(target, literal, stack_name) for a location command's arguments.

    PowerShell spells the target `-Path X`, `-Path:X`, `-LiteralPath X` or
    positionally, with switches such as -PassThru anywhere; -StackName names a
    separate location stack. Taking the first token after a bare -Path read
    -PassThru as the destination (Macroscope).
    """
    target, literal, stack_name, i = None, False, "", 0
    while i < len(args):
        a, low = args[i], args[i].lower()
        name, _, attached = low.partition(":")
        if name in ("-path", "-literalpath", "-stackname"):
            value = a[len(name) + 1:] if attached else (args[i + 1] if i + 1 < len(args) else None)
            i += 1 if attached else 2
            if name == "-stackname":
                stack_name = value or ""
            elif target is None:
                target, literal = value, name == "-literalpath"
            continue
        if a.startswith("-") and a not in ("-", "+"):
            i += 1
            continue
        if target is None:
            target = a
        i += 1
    return target, literal, stack_name


def _resolve_dir(target, base, literal):
    """The directory a location command lands in, or None if it would fail.

    A failed cd leaves the shell where it was, so None means "unchanged".
    PowerShell expands wildcards in -Path (not -LiteralPath) and moves only
    when exactly one container matches; checking the literal `*` path left the
    gate judging the old directory (Macroscope).
    """
    target = os.path.expanduser(target)
    path = target if os.path.isabs(target) else os.path.join(base, target)
    if _POWERSHELL and not literal and re.search(r"[*?\[]", target):
        hits = [h for h in glob.glob(path) if os.path.isdir(h)]
        return os.path.normpath(hits[0]) if len(hits) == 1 else None
    path = os.path.normpath(path)
    return path if os.path.isdir(path) else None


def walk_segments(cmd: str):
    """Yield (effective_cwd, tokens) per segment, tracking `cd` between them.

    effective_cwd is None until a cd is seen (meaning: the hook's own cwd).
    Relative cd targets resolve against the previous effective cwd. `git -C
    <dir> …` is handled by the caller, since it scopes one invocation only.

    The gates judge the repository a command runs in, so every way the shell
    can move is followed: cd/Set-Location (wildcards included), the pushd/popd
    stack and PowerShell's named stacks, bash's bare pushd swap, and `cd -` /
    `cd +` history. A move the shell would refuse leaves the directory as it
    was, which is exactly what the shell does.
    """
    segs = command_segments(cmd)
    if segs is None:
        return None
    out, cwd = [], None
    stacks = {}            # PowerShell -StackName stacks; "" is the default
    back, fwd = [], []     # `cd -` / `cd +` history (bash keeps one OLDPWD)

    def move(new):
        nonlocal cwd
        back.append(cwd)
        fwd.clear()
        cwd = new

    for seg in segs:
        seg = strip_wrappers(seg)  # `MODE=prod cd dir` is still a cd
        if not seg:
            continue
        verb = seg[0]
        if verb in _POP_COMMANDS:
            _, _, name = _location_args(seg[1:])
            stack = stacks.get(name) or []
            # popd returns to where the matching pushd left from; an empty
            # stack is an error in both shells and leaves the directory alone.
            if stack:
                move(stack.pop())
            continue
        if verb not in _CD_COMMANDS and verb not in _PUSH_COMMANDS:
            out.append((cwd, seg))
            continue
        target, literal, name = _location_args(seg[1:])
        base = cwd or os.getcwd()
        if verb in _PUSH_COMMANDS:
            stack = stacks.setdefault(name, [])
            if target is None:
                # Bash's bare pushd swaps the top two directories; PowerShell's
                # only saves the current location.
                if not _POWERSHELL and stack:
                    top = stack.pop()
                    stack.append(cwd)
                    move(top)
                elif _POWERSHELL:
                    stack.append(cwd)
                continue
            new = _resolve_dir(target, base, literal)
            if new is not None:
                stack.append(cwd)
                move(new)
            continue
        if target is None:
            move(str(Path.home()))
        elif target == "-":
            if back:
                prev = back.pop()
                if _POWERSHELL:
                    fwd.append(cwd)
                    cwd = prev
                else:  # bash toggles with OLDPWD
                    back.append(cwd)
                    cwd = prev
        elif target == "+" and _POWERSHELL:
            if fwd:
                back.append(cwd)
                cwd = fwd.pop()
        else:
            # A cd to a directory that doesn't exist FAILS in the shell — the
            # next command runs in the OLD directory (`;`) or not at all
            # (`&&`). Tracking the bogus target instead made repo_root("")
            # come back empty and waved the push through.
            new = _resolve_dir(target, base, literal)
            if new is not None:
                move(new)
    return out


def _git_invocation(seg):
    """(is_git, effective_dir_flag, args_after_global_flags) for one segment."""
    seg = strip_wrappers(seg)
    if not seg or seg[0] != "git":
        return False, None, []
    i, gitdir = 1, None
    while i < len(seg):
        tok = seg[i]
        if tok == "-C" and i + 1 < len(seg):
            gitdir = seg[i + 1]
            i += 2
        elif tok.startswith("--work-tree="):
            gitdir = tok.split("=", 1)[1]
            i += 1
        elif tok == "--work-tree" and i + 1 < len(seg):
            gitdir = seg[i + 1]
            i += 2
        elif tok.startswith("--git-dir="):
            # the repo is the .git dir's parent — pushing via --git-dir was a
            # clean walk past a gate that only knew -C
            gitdir = gitdir or os.path.dirname(tok.split("=", 1)[1].rstrip("/")) or "."
            i += 1
        elif tok == "--git-dir" and i + 1 < len(seg):
            gitdir = gitdir or os.path.dirname(seg[i + 1].rstrip("/")) or "."
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            break
    return True, os.path.expanduser(gitdir) if gitdir else None, seg[i:]


# ---------------------------------------------------------------- gate 1

# Loose on purpose: `git -C <dir> push` and `git -c k=v push` put options
# between the words, and the old tight form returned before the tokenizer ever
# saw them. Precision lives in _git_invocation; this only has to not miss.
PUSH_RE = re.compile(r"\bgit\b[^|;&\n]*\bpush\b", re.I)  # `Git` runs git on Windows
# A push that publishes nothing isn't the moment we care about. Matched only
# within the push invocation itself (not across ; | &&) so an unrelated later
# command can't wave the gate through. Codex review 2026-08-15.
PUSH_HARMLESS_RE = re.compile(r"\bgit(?:\.exe)?\s+push\b[^|;&]*?(--dry-run|--help|\s-n\b)", re.I)


def gate_personal_repo(tool: str, ti: dict):
    """NSLS work in a personal repo. Fires on push, not on every edit —
    an edit is reversible, a push publishes the code to the wrong owner."""
    cwd = None
    push_remote = ""
    if tool == "Bash":
        cmd = ti.get("command") or ""
        if not PUSH_RE.search(cmd):
            return  # cheap pre-filter before any tokenizing
        walked = walk_segments(cmd)
        if walked is None:
            # Untokenizable (heredoc, unbalanced quote): old whole-string
            # behaviour rather than no gate at all.
            if PUSH_HARMLESS_RE.search(cmd):
                return
        else:
            push_cwd, found = None, False
            for seg_cwd, seg in walked:
                is_git, gitdir, args = _git_invocation(seg)
                if not is_git or "push" not in args:
                    continue  # `echo 'git push …'` is one quoted token, not git
                if any(a in ("--dry-run", "--help", "-n") for a in args):
                    continue  # harmless — but only for THIS segment
                # A relative -C/--work-tree resolves against the shell's
                # tracked cd, not the hook's own directory — `cd /x && git -C
                # service push` targets /x/service.
                if gitdir and not os.path.isabs(gitdir):
                    gitdir = os.path.normpath(
                        os.path.join(seg_cwd or os.getcwd(), gitdir))
                found, push_cwd = True, (gitdir or seg_cwd)
                # `git push <remote> …` names its destination; a second remote
                # pointing at a personal account slipped past a check that only
                # ever read origin.
                after = args[args.index("push") + 1:]
                push_remote = next((a for a in after if not a.startswith("-")), "")
                break
            if not found:
                return
            cwd = push_cwd
    elif tool in ("Write", "Edit"):
        return  # editing locally is fine; the push is the moment that matters
    else:
        return

    root = repo_root(cwd)
    if not root:
        return
    if (Path(root) / PERSONAL_MARKER).is_file():
        return  # the repo says it is personal; see PERSONAL_MARKER
    url = origin_url(root)
    if push_remote and push_remote != "origin" and not push_remote.startswith(
            ("http://", "https://", "git@")):
        named = git("remote", "get-url", push_remote, cwd=root)
        if named:
            url = named
    elif push_remote.startswith(("http://", "https://", "git@")):
        url = push_remote  # pushing straight to a URL names the destination itself
    if is_nsls_remote(url):
        return
    if not looks_like_nsls_work(root):
        return

    owner = "your personal account"
    m = re.search(r"github\.com[:/]+([^/]+)/([^/\s.]+)", url or "")
    if m:
        owner = f"{m.group(1)}/{m.group(2)}"

    block(
        f"Critical flag — this looks like an NSLS tool in a personal repo "
        f"({owner}). If you're away or you move on, no one else can open it.\n\n"
        f"Moving it to the NSLS org takes about a minute, keeps your full "
        f"history, and you stay the owner. Or Kevin can authorize it staying "
        f"put — I'll draft that note now if you'd rather.\n\n"
        f"Which one?\n\n"
        f"(If it's really a personal project, or Kevin has OK'd it staying put, "
        f"an empty `{PERSONAL_MARKER}` file at the top of the repo tells this "
        f"check to skip it from then on.)",
        gate="personal_repo",
        automation=Path(root).name,
    )


# ---------------------------------------------------------------- gate 2

DEPLOY_RE = re.compile(
    # (?:\.exe)? and [\s"']+: `railway.exe up` and `& "C:\...\railway.exe" up`
    # are how Windows spells the same deploy (Macroscope).
    r"\b(railway(?:\.exe)?[\s\"']+up|railway(?:\.exe)?[\s\"']+redeploy"
    r"|netlify(?:\.exe)?[\s\"']+deploy"
    r"|vercel(?:\.exe)?[\s\"']+(deploy\s+)?--prod"
    r"|fly(?:\.exe)?[\s\"']+deploy"
    r"|gcloud(?:\.exe)?[\s\"']+(run\s+deploy|functions\s+deploy)"
    r"|serverless(?:\.exe)?[\s\"']+deploy"
    r"|eb(?:\.exe)?[\s\"']+deploy)\b",
    re.I,  # executable names are case-insensitive on Windows
)
# The first token a deploy segment must start with. Anchoring here is what
# separates a command from a mention of one.
DEPLOY_BINARIES = frozenset(
    {"railway", "netlify", "vercel", "fly", "gcloud", "serverless", "eb"}
)

# Reading the docs, rehearsing, or shipping to a preview target isn't shipping
# to members. Codex review 2026-08-15 flagged `railway up --help` blocking.
DEPLOY_HARMLESS_RE = re.compile(
    r"--help|-h\b|--dry-run|--alias|--preview|\bnetlify\s+deploy(?!.*--prod)",
    re.I,
)


def gate_unregistered_ship(tool: str, ti: dict):
    """Tier 3 ship with no tracker record, or with one but no reviewer.

    Blocks a deploy only when the tracker positively reports no record, or when
    the record's scope is Tier 3 (scope_tier: Company-wide or Customer Facing)
    and no reviewer is assigned. Network failure, an unparseable response, an
    unnamed repo, or a scope that names no tier all fall through to allow.
    """
    if tool != "Bash":
        return
    cmd = ti.get("command") or ""
    if not DEPLOY_RE.search(cmd):
        return

    deploy_cwd = None
    walked = walk_segments(cmd)
    if walked is None:
        # Untokenizable: old whole-string behaviour.
        if DEPLOY_HARMLESS_RE.search(cmd):
            return
    else:
        found = False
        for seg_cwd, seg in walked:
            # Anchor on the segment's own binary so `echo 'railway up'` — one
            # quoted token under echo — can never read as a deploy, and a
            # `railway --help` segment can't vouch for the real deploy after
            # the semicolon.
            seg = strip_wrappers(seg)
            if not seg or seg[0] not in DEPLOY_BINARIES:
                continue
            seg_str = " ".join(seg)
            if not DEPLOY_RE.search(seg_str) or DEPLOY_HARMLESS_RE.search(seg_str):
                continue
            found, deploy_cwd = True, seg_cwd
            break
        if not found:
            return

    # Judge the repo the deploy actually runs in. `cd /path/to/service &&
    # railway up` deploys THAT service; the shell's own cwd is irrelevant and
    # judging it let an unregistered service ship from anywhere.
    root = repo_root(deploy_cwd)
    if not root:
        return
    name = Path(root).name
    if not name:
        return

    # Only NSLS work is our business. Without this, every personal side project
    # deployed from a laptop gets blocked for not being in the NSLS tracker —
    # which is both wrong and the fastest way to lose builders. Codex review
    # 2026-08-15.
    if not looks_like_nsls_work(root):
        return

    status, match = tracker_lookup(name)
    if status == "unknown":
        return  # unreachable, unparseable, or a capped page => allow

    if match:
        # Kept as the tracker wrote it: the block names the build's real scope.
        # It used to say "Company-wide" whatever the record said.
        scope = _text(match.get("scope")).strip()
        reviewer = match.get("reviewer")
        if scope_tier(scope) >= 3 and not reviewer:
            block(
                f"Critical flag — '{name}' is registered as {scope} but has "
                f"no reviewer assigned, and this deploys it.\n\n"
                f"Anything member-facing needs a second set of eyes before it "
                f"ships. Kevin covers member-facing and usually turns these round "
                f"inside a day. Want me to assign him and request review now? "
                f"If it's genuinely urgent he can authorize the deploy instead — "
                f"I'll draft that note.",
                gate="tier3_no_reviewer",
                automation=name,
            )
        return  # registered and reviewed, or lower tier — carry on

    block(
        f"Critical flag — this deploys '{name}', and there's no record of it in "
        f"the Automation Tracker. If it misfires at 2am nobody can tell what it "
        f"is or who owns it.\n\n"
        f"Two minutes and it's sorted: I can register it and get a reviewer "
        f"assigned, then we deploy properly. Want me to do that now?\n\n"
        f"If this is genuinely urgent, Kevin can authorize shipping first — say "
        f"the word and I'll draft the note.",
        gate="tier3_unregistered",
        automation=name,
    )


# ---------------------------------------------------------------- gate 3

BULK_WRITE_RE = re.compile(
    r"(api\.hubapi\.com|track\.customer\.io|api\.customer\.io|api\.airtable\.com)",
    re.I,
)
# -X, --request, and curl's data flags (which imply POST with no verb flag at
# all) — the tight -X-only form let `--request DELETE` and `-d @rows.json`
# bulk writes straight past the prefilter.
WRITE_VERB_RE = re.compile(
    r"(-X\s*|--request[\s=])(POST|PUT|PATCH|DELETE)\b"
    r"|(^|\s)(-d|--data(-\w+)?|--json)([\s=]|$)",
    re.I,
)
# The marker has to appear inside the URL, not anywhere in a compound command.
# That's what makes "import" safe to keep: `.../customers/import` is a real bulk
# endpoint, while `... && python import_data.py` sits outside any URL and no
# longer matches. Codex review 2026-08-15 flagged the unscoped version.
BATCH_RE = re.compile(r"(https?://)?\S*\b(batch|bulk|backfill|import|/records)\b", re.I)
DRYRUN_RE = re.compile(r"--dry[-_]?run|\bDRY_RUN=(1|true)\b", re.I)

# Airtable base IDs are "app" + 14 chars and appear directly in the URL path.
AIRTABLE_RE = re.compile(r"api\.airtable\.com", re.I)
AIRTABLE_BASE_RE = re.compile(r"\bapp[A-Za-z0-9]{14}\b")

# Bases the builder has told us are sandboxes. One ID per line, # for comments.
#
# Airtable has no separate sandbox host -- a test base and the real one are both
# api.airtable.com and differ only by base ID -- so the gate cannot tell them
# apart on its own. Before this list, rehearsing a bulk write against a copy got
# blocked, which punished exactly the careful behaviour we want people to have.
#
# An allowlist of DECLARED test bases rather than a list of known production
# ones: we don't have a reliable inventory of NSLS's production bases, and
# inverting it would silently un-protect every base nobody had got round to
# listing. This way the default stays "block", and a builder who hits it once
# says "that's my sandbox" and is never bothered about that base again.
# Overridable so the scenario suite can point at a throwaway file. The suite is
# hermetic by design (it already stubs the tracker over loopback); a test that
# rewrote the builder's real allowlist would be changing what the gate lets
# through on their machine.
TEST_BASES_FILE = Path(
    os.environ.get("NSLS_AIRTABLE_TEST_BASES_FILE")
    or (Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
        / ".nsls-airtable-test-bases")
)


def declared_test_bases():
    try:
        lines = TEST_BASES_FILE.read_text().splitlines()
    except Exception:
        return set()  # no file, unreadable => nothing declared => block as before
    return {
        ln.strip() for ln in lines
        if ln.strip() and not ln.strip().startswith("#")
    }


def only_hits_test_bases(cmd: str) -> bool:
    """True only when this command is Airtable-only AND every base in it is declared.

    Conservative on every axis. If the command also touches HubSpot or
    Customer.io, a declared Airtable base is irrelevant. If no base ID is
    visible -- the common case of `$AIRTABLE_BASE_ID` from the environment --
    we cannot know which base it is, so it does not qualify.
    """
    if not AIRTABLE_RE.search(cmd):
        return False
    if re.search(r"(api\.hubapi\.com|track\.customer\.io|api\.customer\.io)", cmd, re.I):
        return False
    # Only base IDs appearing in an Airtable REQUEST URL count. Scanning the
    # whole command let a declared sandbox ID sitting anywhere else -- a comment,
    # an unrelated argument, or the JSON payload -- vouch for a write whose real
    # target was a production base held in a shell variable. That turned the
    # "this is my sandbox" declaration into a way to switch the gate off.
    found = {
        base
        for url in re.findall(r"https?://\S+", cmd)
        if AIRTABLE_RE.search(url)
        for base in AIRTABLE_BASE_RE.findall(url)
    }
    if not found:
        return False
    return found <= declared_test_bases()


def _segment_is_bulk_write(seg) -> bool:
    """Does THIS segment perform an un-rehearsed bulk production write?

    Judged entirely on the segment's own tokens: the target hosts and batch
    markers must appear in tokens that ARE URLs (so a URL quoted inside an
    echo string or a JSON payload never counts — and a sandbox URL inside the
    payload can't vouch for the request either), the mutating verb must be a
    real curl-style flag token, and a dry-run flag only excuses the segment it
    is actually part of. `echo --dry-run; curl -X POST …` was disabling the
    gate for the whole command line.
    """
    # curl accepts scheme-less URLs and sends the same request, so a token
    # counts as a request URL if it carries a scheme OR is host-shaped for one
    # of the gated systems. Quoted payloads stay excluded: a JSON token starts
    # with '{', not a hostname.
    url_toks = [
        t for t in seg
        if t.lower().startswith(("http://", "https://"))
        or re.match(r"^(api\.airtable\.com|api\.hubapi\.com|track\.customer\.io"
                    r"|api\.customer\.io)/", t, re.I)
    ]
    if not url_toks:
        return False
    urls = " ".join(url_toks)
    if not (BULK_WRITE_RE.search(urls) and BATCH_RE.search(urls)):
        return False

    verb = False
    for i, t in enumerate(seg):
        if re.fullmatch(r"-X(POST|PUT|PATCH|DELETE)", t, re.I):
            verb = True
        elif t in ("-X", "--request") and i + 1 < len(seg) and re.fullmatch(
                r"POST|PUT|PATCH|DELETE", seg[i + 1], re.I):
            verb = True
        elif re.fullmatch(r"-d|--data(-\w+)?|--json", t):
            verb = True  # curl sends POST for data flags with no verb at all
    if not verb:
        return False

    # Everything after curl's `--` marker is a URL, not an option — so a
    # `--dry-run` sitting there does not make the POST a rehearsal, it just
    # names a path. Only tokens BEFORE the marker can excuse the write.
    option_tokens = seg[:seg.index("--")] if "--" in seg else seg
    if any(DRYRUN_RE.fullmatch(t) or DRYRUN_RE.search(t) and t.startswith("--")
           or re.fullmatch(r"DRY_RUN=(1|true)", t, re.I) for t in option_tokens):
        return False

    # Sandbox rehearsal: every base named in this segment's request URLs is a
    # declared test base, and none of the URLs reach HubSpot / Customer.io.
    if re.search(r"(api\.hubapi\.com|track\.customer\.io|api\.customer\.io)",
                 urls, re.I):
        return True
    if AIRTABLE_RE.search(urls):
        found = set(AIRTABLE_BASE_RE.findall(urls))
        if found and found <= declared_test_bases():
            return False
    return True


def gate_bulk_production_write(tool: str, ti: dict):
    """Production write at scale.

    Deliberately narrow: a system-of-record host AND a mutating verb AND a
    batch/bulk marker AND no dry-run flag. A single-record POST is normal work
    and must not trip this.
    """
    if tool != "Bash":
        return
    cmd = ti.get("command") or ""
    if not (
        BULK_WRITE_RE.search(cmd)
        and WRITE_VERB_RE.search(cmd)
        and BATCH_RE.search(cmd)
    ):
        return  # cheap pre-filter; precise judgement is per-segment below

    walked = walk_segments(cmd)
    if walked is None:
        # Untokenizable: the old whole-string checks.
        if DRYRUN_RE.search(cmd):
            return
        if only_hits_test_bases(cmd):
            return
    else:
        if not any(_segment_is_bulk_write(seg) for _, seg in walked):
            return

    block(
        "Critical flag — this writes to a production system of record in bulk, "
        "with no dry run and no rollback path I can see. I'm not worried about "
        "the code; I'm worried about the version of this that runs twice.\n\n"
        "A dry-run pass first shows what it would touch — want me to set that "
        "up? Kevin can also authorize it as-is, and I'll draft that note.\n\n"
        "If this is a test base, tell me and I'll remember it — you won't be "
        "stopped on it again.",
        gate="bulk_production_write",
    )


# ---------------------------------------------------------------- gate 4

OFF_PLATFORM_RE = re.compile(
    r"(from\s+openai\s+import|import\s+openai\b|require\(['\"]openai['\"]\)"
    r"|import\s+(\{[^}]{0,120}\}\s+from\s+)?['\"]openai['\"]"
    r"|from\s+['\"]openai['\"]|api\.openai\.com"
    r"|generativelanguage\.googleapis\.com|from\s+mistralai|import\s+cohere\b)",
    re.I,
)


def gate_off_platform(tool: str, ti: dict):
    """Off-platform at Tier 2+.

    Needs the build's scope, which lives in the tracker. Unknown scope => allow;
    we do not block a personal experiment for using another vendor.
    """
    if tool not in ("Write", "Edit"):
        return
    # Documentation quoting `from openai import OpenAI` is prose, not a
    # platform choice — a README example was getting the same block as real
    # code. Judge only files that execute.
    # Normalised to forward slashes first: a Windows Edit carries
    # C:\\repo\\docs\\example.py, which the "/docs/" test below never matched, so
    # documentation quoting an OpenAI import was judged as executable code —
    # a false positive, and this gate only started reaching Windows at all
    # with the parity change.
    path = str(ti.get("file_path") or "").lower().replace("\\", "/")
    name = path.rsplit("/", 1)[-1]
    if (path.endswith((".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc"))
            or "/docs/" in path
            or name.startswith(("readme", "changelog", "license", "contributing"))):
        return
    body = ti.get("content") or ti.get("new_string") or ""
    # Drop comment lines before matching: "# migrate from openai import later"
    # is a note, not an SDK. String literals stay matchable on purpose —
    # imports inside strings are usually codegen writing real code.
    body = "\n".join(
        l for l in body.splitlines()
        if not l.lstrip().startswith(("#", "//", "--", "*", "/*"))
    )
    if not OFF_PLATFORM_RE.search(body):
        return

    root = repo_root()
    if not root:
        return
    name = Path(root).name
    status, record = tracker_lookup(name)
    if status != "found":
        return  # this gate needs a positive scope to have anything to say

    scope = _text(record.get("scope")).strip()

    # Positive confirmation of Tier 2+ only. Previously any unrecognised scope
    # string ("", "n/a", "tbd") fell through to a block; Codex review
    # 2026-08-15 caught it. Unknown scope is not evidence of anything.
    if scope_tier(scope) < 2:
        return

    block(
        f"Pausing on this one — '{name}' is registered as {scope}, and this adds "
        f"a non-Anthropic AI platform to it.\n\n"
        f"NSLS's default is Anthropic; it isn't dogma, it's that security review, "
        f"spend tracking and support all point one direction, and splitting them "
        f"for something a whole team depends on costs more than it looks. Going "
        f"off-platform at this scope needs a short written why, which Kevin "
        f"then authorizes — the same authorization route as any other flag.\n\n"
        f"If there's a real reason it's the right call here — and sometimes there "
        f"is — tell me and I'll draft the memo with you now. It's a paragraph, "
        f"not a process.",
        gate="off_platform",
        automation=name,
    )


# ---------------------------------------------------------------- main

GATES = (
    gate_personal_repo,
    gate_unregistered_ship,
    gate_bulk_production_write,
    gate_off_platform,
)


_INFLIGHT_DIR = _config_dir() / ".nsls-gate-inflight"
# Comfortably above the hook's own 10s timeout, so a slow winner is never
# overtaken; comfortably below any interval at which a builder could produce a
# genuinely new call carrying the same id (Claude Code does not reuse them).
_INFLIGHT_TTL = 90
_TOOL_USE_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")


_SWEEP_STAMP_NAME = ".last-sweep"
# A tool_use_id can never be named this: _TOOL_USE_ID_RE forbids a leading dot.
_SWEEP_INTERVAL = 60


def _sweep_inflight():
    """Drop markers nothing will ever look at again.

    A FULL sweep, throttled to at most once a minute by a stamp file, rather
    than a partial sweep on every call. Stopping after a fixed number of
    entries looks like the cheap option and is in fact the broken one: the
    directory is walked in whatever order the filesystem hands back, so
    anything past the cut is never reached on any call and the directory grows
    without limit. Throttling bounds the cost per tool call just as tightly
    while still eventually removing everything.

    Best-effort throughout. A sweep that cannot run is a few stale empty files,
    never a missed decision.
    """
    try:
        now = time.time()
        stamp = _INFLIGHT_DIR / _SWEEP_STAMP_NAME
        try:
            if now - stamp.stat().st_mtime < _SWEEP_INTERVAL:
                return
        except OSError:
            pass  # no stamp yet, or unreadable — sweep and lay one down
        try:
            stamp.touch()  # claim it first, so parallel hooks do not all sweep
        except OSError:
            return
        cutoff = now - (_INFLIGHT_TTL * 4)
        for entry in _INFLIGHT_DIR.iterdir():
            if entry.name == _SWEEP_STAMP_NAME:
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                continue
    except Exception:
        pass


def already_deciding(tool_use_id) -> bool:
    """True when another copy of this hook already owns this exact tool call.

    A machine can carry two live registrations of this gate at once: the
    plugin's hooks.json entry and a settings.json entry the installer wrote
    before the migration stopped stripping it. The docs are explicit that a
    plugin's copy of a handler "stays separate" from a settings.json copy —
    both run, in parallel. Two decisions would be survivable; two
    `guardrail_blocked` rows per block would not, because emit() deliberately
    does not deduplicate and those rows are the only measure the gates have.

    The marker is deliberately NOT removed when the decision finishes. Removing
    it reopens the race it exists to close: the winner can finish before the
    loser has even started, and the loser would then claim a free marker and
    decide again. It ages out instead.

    Every failure path here returns False — deciding twice is a bad day;
    skipping a decision because a marker could not be written is a hole.
    """
    tid = tool_use_id if isinstance(tool_use_id, str) else ""
    if not _TOOL_USE_ID_RE.match(tid):
        return False  # no usable id — decide, rather than skip a decision
    marker = _INFLIGHT_DIR / tid
    try:
        _INFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(_INFLIGHT_DIR, 0o700)
        except OSError:
            pass
        for _ in range(2):
            try:
                os.close(os.open(str(marker),
                                 os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                _sweep_inflight()
                return False  # we own this call
            except FileExistsError:
                try:
                    if time.time() - marker.stat().st_mtime <= _INFLIGHT_TTL:
                        return True  # a live sibling owns it
                    marker.unlink()  # abandoned; try once more to claim it
                except OSError:
                    return True
    except Exception:
        return False
    return False


def normalize_call(tool: str, ti: dict):
    """Show the gates the shapes they were written against.

    The matcher admits six tool names; the gates read three shapes — a Bash
    command, a Write, an Edit.

    PowerShell is a first-class tool, not a synonym: Claude Code enables it
    automatically on Windows and its payload carries the command in the same
    `command` field Bash uses. Three of the four gates open with
    `if tool != "Bash": return`, so adding PowerShell to the matcher without
    this line would admit the call and then wave it through every command
    gate — the matcher would look fixed while nothing judged it.

    MultiEdit carries its text in edits[].new_string and NotebookEdit in
    new_source, so without this both are a silent bypass of gate 4 — the tool
    call is inspected, finds no `content` or `new_string`, and is waved through.
    """
    if tool == "PowerShell":
        return "Bash", ti
    if tool == "MultiEdit":
        parts = []
        edits = ti.get("edits")
        if isinstance(edits, list):
            for e in edits:
                if isinstance(e, dict) and isinstance(e.get("new_string"), str):
                    parts.append(e["new_string"])
        return "Edit", {"file_path": ti.get("file_path"),
                        "new_string": "\n".join(parts)}
    if tool == "NotebookEdit":
        source = ti.get("new_source")
        return "Edit", {"file_path": ti.get("notebook_path") or ti.get("file_path"),
                        "new_string": source if isinstance(source, str) else ""}
    return tool, ti


def main():
    # Before anything else, and regardless of the verdict: this is the only
    # record that the gate runs at all. Blocks are counted; evaluations were
    # not, which is why two weeks of no gate looked identical to two weeks of
    # nothing worth blocking.
    try:
        _beacon.record("guardrail-gate", __file__)
    except Exception:
        pass

    if os.environ.get("NSLS_GUARDRAILS_DISABLED") == "1":
        allow()

    try:
        # Bytes, decoded as UTF-8 here. json.load(sys.stdin) used the ANSI code
        # page on Windows: a user folder with an accented letter came out
        # garbled, so the repo lookup missed and the gate allowed; a byte
        # cp1252 has no character for (in an emoji, say) raised, and the gate
        # allowed.
        raw = getattr(sys.stdin, "buffer", None)
        raw = raw.read() if raw is not None else sys.stdin.read().encode("utf-8")
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        allow()

    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") or {}
    if not isinstance(ti, dict):
        allow()

    if already_deciding(payload.get("tool_use_id")):
        allow()

    global _POWERSHELL
    _POWERSHELL = tool == "PowerShell"
    if _POWERSHELL and isinstance(ti.get("command"), str):
        # Join backtick continuations before any gate looks, so the cheap
        # prefilters (PUSH_RE and friends) see `git ... push` on one line.
        ti = dict(ti, command=_join_ps_continuations(ti["command"]))
    tool, ti = normalize_call(tool, ti)

    for gate in GATES:
        try:
            gate(tool, ti)
        except SystemExit:
            raise
        except Exception:
            continue  # one broken gate never takes down the rest

    allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        allow()
