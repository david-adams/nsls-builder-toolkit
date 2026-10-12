#!/usr/bin/env python3
"""
session-start.py — Cross-platform SessionStart hook for the NSLS Builder Toolkit.

Runs on every Claude Code session start. Does three things:
1. git pull the toolkit to get latest updates
2. Sync skill pointers from the plugin to ~/.claude/skills/
3. Ping the automation tracker for points, PR credits, and announcements

Must be fast and fail silently. Works on Mac, Linux, and Windows.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

HOME = Path.home()
# Respect CLAUDE_CONFIG_DIR (Claude Code passes it through to hook processes),
# falling back to ~/.claude. Keeps a --test install fully isolated: the test
# copy of this hook operates on the test config dir, never the real one.
CONFIG_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (HOME / ".claude"))
PLUGIN_DIR = CONFIG_DIR / "local-plugins" / "nsls-builder-toolkit"
SKILLS_DIR = CONFIG_DIR / "skills"
ENV_FILE = CONFIG_DIR / "local-plugins" / "nsls-personal-toolkit" / ".env"
PROXY_URL = os.environ.get("NSLS_TRACKER_URL", "https://web-production-6281e.up.railway.app")

# git reads these ahead of `-C`: with GIT_DIR or GIT_WORK_TREE set, `git -C <toolkit>`
# still works on the repository they name. Claude launched from inside a git hook
# inherits them (git exports GIT_DIR and GIT_INDEX_FILE to its hooks), so every git
# call here would read, fetch into, or fast-forward that repository instead of the
# toolkit. This is git's own list (`git rev-parse --local-env-vars`), the set it
# clears itself when it moves into another repository.
_GIT_REPO_ENV = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE", "GIT_GRAFT_FILE", "GIT_INDEX_FILE",
    "GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_REF_BASE", "GIT_PREFIX",
    "GIT_INTERNAL_SUPER_PREFIX", "GIT_SHALLOW_FILE", "GIT_COMMON_DIR",
)


def _git_env():
    """The environment for every git call aimed at a toolkit checkout."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REPO_ENV}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env

# When a session-ping can't be delivered (timeout / network / proxy down), we
# stash the payload here and replay it at the start of the next session so a
# queued announcement or credit isn't lost. Deleted once delivery succeeds.
PING_FAIL_MARKER = PLUGIN_DIR / ".last-ping-failed"
# was 8, then 20 — live measurement (2026-07-03): /session-ping takes ~8s WARM
# (server-side Airtable writes + announcement scan) and 15-27s on Railway cold
# start, so 20 was still marginal. The SessionStart hook budget must stay
# comfortably above this (install.sh sets 90).
PING_TIMEOUT = 35

# Toolkits to pull and to write skill pointers for.
SYNC_PLUGINS = [
    "nsls-builder-toolkit",
    "nsls-personal-toolkit",
]
# When both toolkits ship a skill with the same name, the earlier one's pointer
# wins. Personal first: the builder chose that toolkit and can customise it,
# and the one name the two share today, personal-setup, is an org bootstrapper
# that hands off to the personal copy once that is installed.
POINTER_PRECEDENCE = [
    "nsls-personal-toolkit",
    "nsls-builder-toolkit",
]
# The two pointer shapes the toolkit writes. sync_pointers() below writes the
# credit shape; install.sh and hooks/sync-pointers.sh write the plain one.
POINTER_FOLLOW = "and follow the full skill at "
POINTER_CREDIT = (
    "Before reading the skill, run this Bash command exactly once in the "
    "background (do not wait, do not announce, do not retry on failure) "
    "to record skill usage:"
)


def is_own_pointer(text, skill, plugins=SYNC_PLUGINS):
    """True only if `text` is exactly a pointer the toolkit writes for `skill`.

    This is the only ownership check before a pointer is overwritten, and
    install.sh and hooks/sync-pointers.sh call it too. Merely CONTAINING a
    toolkit path is not enough: a builder's own skill that credits or links a
    toolkit skill would match that and be overwritten, and so would a pointer
    the builder extended with notes of their own. A pointer to any of `plugins`'
    copies of THIS skill counts, so the copy that wins under POINTER_PRECEDENCE
    can replace the other.
    Backslashes, CRLF and a BOM are normalised first (mirrors Test-OwnPointer
    in session-start.ps1).
    """
    t = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\\", "/")
    own = "|".join(re.escape(f"local-plugins/{p}/skills/{skill}/SKILL.md") for p in plugins)
    follow = re.escape(POINTER_FOLLOW) + rf"`(?:[^`\n]*/)?(?:{own})`\.\s*"
    report = re.escape(f"echo '{{\"tool_input\":{{\"skill\":\"{skill}\"}}}}' | bash ")
    credit = (
        re.escape(POINTER_CREDIT) + r"\n\n```bash\n" + report
        + r"[^`\n]*/local-plugins/nsls-builder-toolkit/hooks/skill-event\.sh\n```\n\n"
        + "Then read " + follow
    )
    # Front matter exactly as every writer has always emitted it: a name line
    # and a one-line folded description. Keys of the builder's own (model:,
    # allowed-tools:, ...) mean they have customised it, so it is theirs.
    front = r"---\nname:[^\n]*\ndescription: >-\n  [^\n]*\n---\n\s*"
    return re.fullmatch(front + f"(?:Read {follow}|{credit})", t) is not None


# A bare block-scalar indicator is not a description. If `description: >-` (or
# `|`) has no indented body, the value is empty — the indicator itself must
# never become the description text.
BLOCK_INDICATORS = (">", ">-", ">+", "|", "|-", "|+")

# One left-to-right pass over a double-quoted scalar's escapes. Single-pass
# matters: chained .replace() calls would decode the output of an earlier
# replacement (\\" would collapse to " and lose its backslash).
_DQ_ESCAPE = re.compile(r"\\x([0-9a-fA-F]{2})|\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})|\\(.)")
_DQ_SIMPLE = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "n": "\n",
              "v": "\v", "f": "\f", "r": "\r", "e": "\x1b",
              # The four named Unicode escapes YAML defines beyond the C set:
              # next-line, non-breaking space, line separator, paragraph
              # separator. Decoded faithfully; the collapse below folds all four
              # to spaces (\x85 via _CONTROL, the rest because str.split treats
              # them as whitespace), which is what a YAML value folded onto a
              # single line should become.
              "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029"}

# Decoded control characters (NUL, BEL, ESC…) would make the generated pointer's
# frontmatter unparseable, so they're mapped to spaces and folded away by the
# whitespace collapse. \t \n \r are deliberately absent — they're whitespace and
# the collapse already handles them.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _dq_unescape(match):
    for group in (1, 2, 3):
        if match.group(group):
            return chr(int(match.group(group), 16))
    ch = match.group(4)
    # Anything else (\" \\ \/ \space) stands for itself.
    return _DQ_SIMPLE.get(ch, ch)


def unquote_scalar(value):
    """Turn a single-line YAML scalar into the string a YAML parser would give.

    The folded-block branch never sees quotes, but a description written as
    `description: "Brain dump…"` reaches the single-line fallback with its
    delimiters attached, and they end up verbatim in the generated pointer.
    We can't just call a YAML parser here — pyyaml isn't guaranteed on a fresh
    machine, which is why this extraction is hand-rolled in the first place.

    Whitespace is collapsed at the end because the caller embeds the result as
    a single indented line under `description: >-`. A decoded `\\n` left as a
    real newline would break the generated frontmatter, so folding it to a
    space is both safe and what the folded-block branch already does.
    """
    v = value.strip()
    if v in BLOCK_INDICATORS:
        return ""
    quote = v[:1]
    if len(v) >= 2 and v[-1:] == quote and quote in ('"', "'"):
        inner = v[1:-1]
        if quote == '"':
            inner = _DQ_ESCAPE.sub(_dq_unescape, inner)
        else:
            # Single-quoted YAML has exactly one escape: '' is a literal quote.
            inner = inner.replace("''", "'")
        v = inner
    return " ".join(_CONTROL.sub(" ", v).split())


# ff-only pull failures that mean the checkout ITSELF blocks updating (frozen),
# as opposed to being offline or having no upstream (both fine and quiet).
# Real incident: a single local commit froze a builder's toolkit for a month
# with zero signal — every session "synced" and nothing ever changed.
_FREEZE_SIGNS = (
    "fast-forward",            # ff-only refused: local commits diverge from upstream
    "would be overwritten",    # dirty working tree blocks the update
    "unmerged",                # conflicted / unfinished state
    "not concluded",           # "you have not concluded your merge"
)


IGNORED_BLOCKER = "ignored local file(s) that the update would replace"


def _checkout_blocks_update(plugin_dir, deadline=None):
    """Why this checkout itself blocks a fast-forward, or None if it doesn't.

    Asked BEFORE claiming a toolkit is frozen, because "local commits or edits"
    is a diagnosis and `_FREEZE_SIGNS` is only a substring match on git's
    output. The two came apart in practice: clean, up-to-date checkouts (0
    dirty files, 0 ahead of upstream, `pull --ff-only` returning 0 on the very
    next attempt) were reported as FROZEN every session, sending builders to
    back up local changes that did not exist.

    Returns a human phrase naming the real blocker so the warning can say which
    it is, since the repair differs: commits need a backup branch, a dirty tree
    needs a stash or a commit, and an ignored file the update would replace
    needs moving aside — the update refuses rather than overwrite it.

    Unable to tell (git failed, no upstream configured, out of time, anything
    unexpected) is reported as None — NOT as frozen. A guess is what this
    function exists to stop, and staying quiet costs at most one session's
    update, which the next session's update picks up anyway."""
    def git(*args):
        # Inside the caller's deadline: this runs within the 15s update envelope.
        timeout = 5
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError
        r = subprocess.run(
            ["git", "-C", str(plugin_dir), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=_git_env(),
        )
        return r.returncode, (r.stdout or "").strip()

    try:
        code, dirty = git("status", "--porcelain")
        if code != 0:
            return None
        # `@{u}` rather than a hardcoded origin/main: a supported fork checkout
        # (NSLS_PERSONAL_REPO / NSLS_PERSONAL_BRANCH) tracks something else, and
        # the pull this diagnoses follows the same upstream.
        code, ahead = git("rev-list", "--count", "@{u}..HEAD")
        if code != 0:
            # No upstream to compare against. A dirty tree still blocks a pull,
            # so that much is safe to report; divergence is unknowable here.
            return "uncommitted local edits" if dirty else None
        commits = int(ahead or 0)
        if commits and dirty:
            return f"{commits} local commit(s) and uncommitted edits"
        if commits:
            return f"{commits} local commit(s) not in its upstream"
        if dirty:
            return "uncommitted local edits"
        # Clean and level, yet refused: the update adds a path that already
        # exists here as a file git does not show (ignored, typically). The merge
        # runs with --no-overwrite-ignore, so it refuses where plain git would
        # silently replace the builder's file. Counted from the paths the update
        # adds, never from git's error text (see _warn_if_frozen).
        code, added = git("diff", "--name-only", "-z", "--no-renames",
                          "--diff-filter=A", "HEAD", "@{u}")
        if code != 0:
            return None
        hits = sum(1 for p in added.split("\0") if p and os.path.lexists(plugin_dir / p))
        return f"{hits} {IGNORED_BLOCKER}" if hits else None
    except Exception:
        return None


def _warn_if_frozen(plugin, plugin_dir, err, deadline=None):
    """Announce a self-update blocked by the checkout's own state. stdout of a
    SessionStart hook lands in the model's context, so Claude can tell the
    user and offer the repair — a frozen toolkit must never be silent.
    Network/offline failures stay quiet (a laptop on a plane is not an
    incident).

    Two gates, not one. `_FREEZE_SIGNS` says git's complaint LOOKS like a
    checkout-local refusal; `_checkout_blocks_update` then checks whether it
    actually is. Only the substring matched before, and it cried wolf every
    session on clean checkouts — which trains builders to ignore the one
    message that must never be ignored (the real incident this guards: a single
    local commit froze a toolkit for a month with zero signal).

    Which _FREEZE_SIGNS phrase matched is reported, but git's raw text is NOT.
    Everything this prints lands in the model's context, and git echoes content
    an attacker controls — `remote:` lines are printed verbatim from the server,
    and branch, ref and URL names appear in error text. Passing that through
    would let a hostile upstream write instructions straight into Claude's
    context wearing the toolkit's voice. The matched phrase is one of four of
    OUR OWN literals, so it carries the diagnostic value with none of the
    surface; the raw text stays in git's own output where a human can read it."""
    matched = next((s for s in _FREEZE_SIGNS if s in err), None)
    if matched is None:
        return
    blocker = _checkout_blocks_update(plugin_dir, deadline=deadline)
    if blocker is None:
        # Verified clean and level with upstream: the pull failed for some other
        # reason and this is not a frozen toolkit. Nothing to repair, so say
        # nothing rather than send Claude after a backup with no target.
        return
    if blocker.endswith(IGNORED_BLOCKER):
        # A backup branch would not hold these: git does not track them. Their
        # names are not printed (they are paths NSLS's update chose); a
        # fast-forward attempt lists them for Claude when it goes to look.
        fix = ("find those files (a fast-forward attempt names them), copy them "
               "somewhere safe and move them out of the way, then fast-forward "
               "the checkout to its upstream and help them keep whatever in "
               "those files was theirs")
    else:
        fix = ("preserve their local changes on a backup branch, then "
               "fast-forward the checkout to its upstream")
    print(
        f"WARNING - {plugin} could not self-update: the checkout at {plugin_dir} "
        f"has {blocker}, so automatic updates are FROZEN and this "
        f"toolkit is going stale. Tell the user at the first natural moment and "
        f"offer the fix: {fix}. (Skills in ~/.claude/skills "
        f"are the right place for personal edits and are unaffected.) "
        f"git refused with: {matched!r}"
    )


def _warn_operation_in_progress(plugin, plugin_dir):
    """Say so when the update was held back by an unfinished git operation.

    A checkout mid-bisect, mid-rebase or mid-cherry-pick can sit clean on its
    branch, and fast-forwarding that branch changes what the operation does, so
    the update does not run. Held back silently, an operation abandoned weeks ago
    would freeze the toolkit with no signal — the failure _warn_if_frozen exists
    for — so it is said out loud. Someone may genuinely be mid-operation in
    another session, which is why the wording leaves room for that."""
    print(
        f"WARNING - {plugin} did not self-update this session: the checkout at "
        f"{plugin_dir} has an unfinished git operation in progress (a merge, "
        f"rebase, cherry-pick, revert or bisect), and updating under it would "
        f"change what that operation does. If nobody is working in that checkout "
        f"right now, it was left behind and automatic updates stay FROZEN until "
        f"it is finished or abandoned. Tell the user at the first natural moment "
        f"and offer to look; finish or abort the operation only after they "
        f"confirm nothing of theirs depends on it."
    )


def _warn_unguardable_branch(plugin, plugin_dir):
    """Say so when the branch's NAME kept a behind checkout from updating.

    The update blanks the branch's mergeOptions through `-c`, which cannot carry
    a name containing "=" or anything outside plain ASCII (_CONFIG_SAFE_BRANCH),
    and merging without that guard can discard NSLS's changes. The name itself
    is not printed: it is text someone else can choose."""
    print(
        f"WARNING - {plugin} did not self-update: the checkout at {plugin_dir} is "
        f"on a branch whose name the automatic update cannot protect (it contains "
        f"'=' or characters outside plain ASCII) and is behind its upstream, so "
        f"automatic updates are FROZEN while it stays on that branch. Tell the "
        f"user at the first natural moment and offer the fix: rename the branch "
        f"using only letters, digits, '-', '_', '.' and '/', or put the checkout "
        f"back on main."
    )


def _git_out(plugin_dir, *args, deadline=None):
    """Run a git command, optionally bounded by a shared deadline.

    Callers used to check a deadline once and then make several of these, each
    free to burn the full 3s -- five calls behind one check is 15s against a 90s
    hook budget that still owes work to pointer-sync and the pings. Making the
    deadline an argument of the call itself is the only version that cannot
    drift: a caller past its budget gets "" without spawning anything, and a
    caller near it gets a timeout clamped to whatever is actually left.
    """
    timeout = 3
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        timeout = min(timeout, remaining)
    try:
        r = subprocess.run(
            ["git", "-C", str(plugin_dir), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
            stdin=subprocess.DEVNULL, env=_git_env(),
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


REPAIR_FALLBACK = (
    " The repair, if they want one, depends on what that branch is for: if the "
    "work on it is finished, merge or land it and put the checkout back on "
    "main; if it is still in progress, merging origin/main into it catches this "
    "checkout up without losing it."
)


def _warn_if_stale_by_configuration(plugin, plugin_dir, deadline=None):
    """Catch the freeze that _warn_if_frozen structurally cannot see.

    _warn_if_frozen only speaks when the pull FAILS. Two shapes of frozen
    checkout produce a pull that does not fail, and both went unreported for
    weeks on this machine:

      1. **Tracking a feature branch.** The builder toolkit sat on
         feat/builder-guardrails, whose upstream is that same branch on origin.
         Every session pulled, every pull said "already up to date", and main
         moved five commits ahead. Nothing was wrong with the pull; the pull was
         aimed somewhere that never changes.
      2. **A named branch with no upstream at all.** The personal toolkit sat on
         feat/completion-sweep, nine commits BEHIND main with five of its own. A
         bare pull exits non-zero with "no tracking information", which is not
         in _FREEZE_SIGNS -- deliberately, so a pinned fork stays quiet -- and
         so this said nothing either, forever.

    A detached HEAD stays quiet: that is the deliberate pin the git_pull
    docstring protects, and it is a different git state than a branch with
    nowhere to pull from.

    Costs no network. The update's bare `git fetch` fetches the whole remote by
    default, so origin/main is already current by the time this runs, and
    everything here is a local ref comparison.
    """
    # Four git calls at up to GIT_TIMEOUT each, twice over (two toolkits),
    # running AFTER git_pull's 15s and before two 35s ping attempts inside a
    # 90s hook budget. Overrunning kills the hook mid-flight and costs the
    # builder their session credit — a stale-branch NOTE is never worth that.
    if deadline is not None and time.monotonic() >= deadline:
        return
    branch = _git_out(plugin_dir, "rev-parse", "--abbrev-ref", "HEAD", deadline=deadline)
    if not branch or branch == "HEAD":
        return  # detached: pinned on purpose
    # A checkout tracking a FORK is supported (NSLS_PERSONAL_REPO), and its
    # branch legitimately trails thensls/main. Judging it against origin/main
    # produced a permanent false freeze warning — the same cry-wolf failure
    # PR #161 fixed in the other direction, and false alarms here are what
    # train people to ignore the one warning that matters.
    # A deliberately pinned fork branch and an accidentally-stranded feature
    # branch are STRUCTURALLY IDENTICAL in git: both track their own upstream,
    # both pull cleanly, both trail main. There is no state that separates
    # them, only intent — so an early return for "in step with its upstream"
    # silenced the exact case this function was written for
    # (feat/builder-guardrails tracked its own branch while main moved 5 ahead).
    # Both get told; the WORDING carries the ambiguity instead of the logic.
    upstream = _git_out(plugin_dir, "rev-parse", "--abbrev-ref",
                        "--symbolic-full-name", "@{u}", deadline=deadline)
    if upstream == "origin/main":
        return  # tracking main IS the healthy shape. A checkout on main that
        # has diverged locally is _warn_if_frozen's business (its ff-only pull
        # genuinely fails); saying "it tracks its own upstream rather than
        # main" about main itself is contradictory advice.
    has_upstream = bool(upstream)
    behind = _git_out(plugin_dir, "rev-list", "--count", "HEAD..origin/main", deadline=deadline)
    if not behind.isdigit() or int(behind) == 0:
        return  # nothing on main this checkout is missing
    # Everything printed here lands in the model's context, and a branch name
    # is text someone else can choose — a clone can arrive carrying one. Same
    # reasoning as PR #161's refusal to echo git's raw output: carry the
    # diagnostic value, not the injection surface. Conservative charset, capped.
    safe = re.sub(r"[^A-Za-z0-9._/-]", "", branch)[:60] or "(unnamed)"
    why = (f"it is on branch '{safe}', which tracks its own upstream rather "
           f"than main — so its pulls succeed while main moves on"
           if has_upstream else
           f"branch '{safe}' has no upstream, so there is nothing to pull from")
    # Reporting a problem without naming its fix is how this notice got ignored
    # for weeks: the builder is told they are stale and left to work out what to
    # do about it. Name the specific repair for the shape actually in front of
    # us. The two extra git calls are bought only now that we know we are going
    # to speak, and only if the budget allows -- running out of time costs the
    # ready-made offer, never the notice itself.
    offer = ""
    if deadline is None or time.monotonic() < deadline:
        ahead = _git_out(plugin_dir, "rev-list", "--count", "origin/main..HEAD", deadline=deadline)
        dirty = bool(_git_out(plugin_dir, "status", "--porcelain", deadline=deadline))
        if dirty and not (ahead.isdigit() and int(ahead) > 0):
            offer = (" There are uncommitted edits in this checkout, so nothing "
                     "should move until those are dealt with — say that is what "
                     "is in the way.")
        elif ahead == "0":
            # Deliberately NOT "loses nothing". `status --porcelain` omits
            # ignored files, and git will silently clobber an ignored file that
            # is tracked on main -- so a clean tree here does not entitle us to
            # promise a lossless switch. Adding --ignored is not the fix either:
            # __pycache__ and .toolkit-state.json exist in nearly every
            # checkout, so it would report dirty always and the branch would
            # never fire. The honest shape is to offer the repair and check for
            # overwrites at the moment of acting, where it can be done properly.
            offer = (" The branch carries nothing main does not already have "
                     "and has no uncommitted edits, so putting the checkout "
                     "back on main is the repair. Offer exactly that; if they "
                     "say yes, confirm nothing untracked or ignored would be "
                     "overwritten before switching.")
        elif ahead.isdigit() and int(ahead) > 0:
            edits = " plus uncommitted edits" if dirty else ""
            offer = (f" This branch carries {ahead} commit(s) of its own{edits}, "
                     f"so the repair that risks nothing is to merge origin/main "
                     f"into the branch — it catches the checkout up without "
                     f"losing their work. Offer exactly that, and act only if "
                     f"they say yes.")
    print(
        f"NOTE - {plugin} is {behind} commit(s) behind main: "
        f"{why}. The checkout at {plugin_dir} reports a clean pull every session "
        f"while going stale, which is why this needs saying out loud. Tell the "
        f"user at the first natural moment — and say plainly that if this "
        f"branch is a deliberate choice, nothing is wrong and they can ignore "
        f"it.{offer or REPAIR_FALLBACK} Do not "
        f"switch branches on the user's behalf — a live plugin checkout is what "
        f"their current session is running."
    )


# --- personal-toolkit forks: measured against NSLS, not against their own copy ---
# Everything above judges a checkout against ITS OWN upstream, and for a GitHub
# fork that upstream is the fork — which the builder is usually perfectly in
# sync with. So a fork pulled cleanly every session, passed every check, and
# received nothing NSLS shipped: a real builder's copy sat 196 commits behind
# (verified 2026-09-09) with every session reporting healthy.
#
# The personal toolkit's own hook carries this same check (its PR #72), but that
# hook is a file INSIDE the fork, so no fork can ever receive it. This hook
# self-updates from NSLS on every machine, which makes it the one channel that
# reaches a fork at all — which is why the check lives here too. The two are
# deliberately interchangeable: same private ref, same stamp, same lock, same
# wording, so whichever fires first speaks and the other stays quiet.
PERSONAL_PLUGIN = "nsls-personal-toolkit"
PERSONAL_UPSTREAM_URL = "https://github.com/thensls/nsls-personal-toolkit.git"
# Where the fetch lands: a private ref, not a remote. A fork may already have an
# `upstream` — or any other name — aimed at something else entirely; fetching
# that reported a stranger's commits as NSLS's, and re-pointing it was found to
# be its own small vandalism. Fetching NSLS by URL into a ref of our own touches
# neither, and gives the notice a stable name to merge. (FETCH_HEAD would do,
# but the concurrent pulls in this same hook can overwrite it mid-check.) The
# refspec names refs/heads/main explicitly so a tag called main cannot stand in.
PERSONAL_UPSTREAM_REF = "refs/nsls/upstream-main"
PERSONAL_UPSTREAM_REFSPEC = f"+refs/heads/main:{PERSONAL_UPSTREAM_REF}"
PERSONAL_UPSTREAM_STAMP = CONFIG_DIR / ".nsls-personal-upstream-check"
# The stamp is a throttle, not a claim: two hooks can both read it as stale
# before either writes it. The lock is the claim — an OS-level exclusive lock
# on a file that is never deleted, so exactly one of them fetches and speaks.
PERSONAL_UPSTREAM_LOCK = CONFIG_DIR / ".nsls-personal-upstream-check.flock"
# The lock file of the protocol this one replaced (created exclusively, a token
# inside, treated as stale after 120 s). A machine may briefly run one old copy
# of this check beside one new — the plugin cache and the personal checkout
# update on different schedules — so while the OS lock is held this copy ALSO
# claims under that protocol: it creates the old file exclusively and leaves it
# empty (see _shadow_legacy_claim); a fresh, non-empty token there means an old
# hook is mid-check and this one yields. Two files, so
# nothing the old protocol renames or deletes can ever be the inode locked here.
PERSONAL_UPSTREAM_LEGACY_LOCK = CONFIG_DIR / ".nsls-personal-upstream-check.lock"
PERSONAL_UPSTREAM_CHECK_EVERY_H = 12
PERSONAL_FETCH_TIMEOUT = 6
# The fast-forward that catches up a fork with nothing of its own. It is a local
# write, so it is never started without this much of the deadline left: git
# killed half-way through a checkout leaves a half-updated folder and a stale
# index.lock that blocks every git command after it. A write we might have to
# kill is a write we should not begin.
PERSONAL_FF_MIN_LEFT_S = 5
# The longest session start waits for the fast-forward to report, and never past
# the caller's deadline: that is the 15s pull envelope the 90s hook budget is
# built on. It is never killed: a merge still running when the wait ends is left
# to finish on its own, and the builder is told it may still be completing. A local fast-forward takes well under a
# second, with hooks and background maintenance switched off for it.
PERSONAL_FF_WAIT_S = 20
# git's markers for an operation in progress. A checkout mid-bisect, mid-rebase,
# mid-am or mid-revert can look clean and sit on its branch, and moving that
# branch under it changes what the operation does. git merge itself only refuses
# some of these. Both unattended fast-forwards check them: the daily update and
# the clean-fork catch-up.
GIT_OP_STATE = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_START",
                "BISECT_LOG", "rebase-merge", "rebase-apply", "sequencer")
# The one status that counts everything: untracked files even where
# status.showUntrackedFiles=no hides them, and submodules even where configured away.
PERSONAL_STATUS_ARGS = ("status", "--porcelain=v1", "--untracked-files=all", "--ignore-submodules=none")

# NSLS's own repository, in any spelling git accepts — compared as host + path,
# never as a substring: a mirror on another host, or a longer-named repo under
# the same owner, both CONTAIN the canonical path. Schemes are an allow-list:
# `file://github.com/...` or `evil://github.com/...` need never touch GitHub.
_CANONICAL_HOST = "github.com"
_CANONICAL_PATH = "thensls/nsls-personal-toolkit"
_CANONICAL_SCHEMES = ("https", "http", "ssh", "git", "git+ssh", "ssh+git")
_URL_FORMS = (
    # scheme://[user[:secret]@]host[:port]/path
    re.compile(r"^([a-z][a-z0-9+.-]*)://(?:[^@/]*@)?([^/:]+)(?::\d+)?/(.*)$", re.IGNORECASE),
    # scp-like [user@]host:path — no scheme, and `host://...` is not this form
    re.compile(r"^(?:[^@/:]+@)?([^/:]+):(?!//)/?(.*)$"),
)


def _is_canonical_origin(url):
    """True only for NSLS's own repository on github.com.

    Accepts https, ssh://, and scp-like spellings, optional user info and port,
    `www.`, a trailing slash, `.git`, and any letter case (GitHub owner and repo
    names are case-insensitive). Anything else — another host, another owner,
    a longer repo name, a local path, an unexpected scheme — is not canonical.
    """
    u = (url or "").strip()
    m = _URL_FORMS[0].match(u)
    if m:
        if m.group(1).lower() not in _CANONICAL_SCHEMES:
            return False
        host, path = m.group(2).lower(), m.group(3)
    else:
        m = _URL_FORMS[1].match(u)
        if not m:
            return False
        host, path = m.group(1).lower(), m.group(2)
    if host.startswith("www."):
        host = host[4:]
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return host == _CANONICAL_HOST and path.rstrip("/").lower() == _CANONICAL_PATH


def _safe_text(value, limit=200):
    """A path for the notice: printable ASCII only, bounded. Everything printed
    here is model context, and a path is text someone else can choose."""
    return re.sub(r"[^\x20-\x7e]", "?", str(value))[:limit]


def _git_rc(plugin_dir, *args, timeout=3):
    """(returncode, stdout) — never raises; -1 when git timed out or could not run.

    git runs in its own session so a timeout kills the whole process TREE — a
    fetch's HTTPS remote helper is a child that killing git alone would leave
    running the network operation after we had released the lock."""
    try:
        proc = subprocess.Popen(
            ["git", "-C", str(plugin_dir), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
            env=_git_env(),
        )
    except Exception:
        return -1, ""
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, (out or "").strip()
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.communicate(timeout=2)
        except Exception:
            pass
        return -1, ""
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return -1, ""


def _pull_source_url(git):
    """URL of the remote this checkout's branch actually pulls from — the same
    source a bare `git pull` uses — falling back to origin. A canonical `origin`
    beside a branch that tracks a personal fork is a fork in every way that
    matters; a fork whose remote is not called origin is still a fork. Empty
    when neither resolves."""
    # From config, not by splitting `@{u}` on "/": a remote may itself be
    # named with a slash (`personal/fork`), and the split kept only `personal`.
    remote = ""
    rc, branch = git("symbolic-ref", "--short", "HEAD")  # fails when detached
    seen = set()
    while rc == 0 and branch and branch not in seen:
        seen.add(branch)  # stop only on a cycle, not at an arbitrary hop count
        rc, configured = git("config", "--get", f"branch.{branch}.remote")
        if rc != 0 or not configured:
            break
        if configured != ".":
            remote = configured
            break
        # "." means the branch pulls from a LOCAL branch, not a remote. Follow
        # that chain to the remote it ends at, rather than pretending it is
        # origin — but never give up on the checkout: its remote of record is
        # still the honest fallback, and silence on a fork is the failure here.
        rc, merge = git("config", "--get", f"branch.{branch}.merge")
        if rc != 0 or not merge.startswith("refs/heads/"):
            break
        branch = merge[len("refs/heads/"):]
    remote = remote or "origin"
    rc, url = git("remote", "get-url", remote)
    if (rc != 0 or not url) and remote != "origin":
        rc, url = git("remote", "get-url", "origin")
    return url if rc == 0 else ""


def _stamp_is_fresh(stamp):
    """Bounded at BOTH ends: a stamp dated in the future (clock skew, a restored
    backup, a synced home directory) yields a negative age, which a bare `<`
    would read as freshly checked and could silence the check for days."""
    try:
        if stamp.exists():
            age_h = (time.time() - stamp.stat().st_mtime) / 3600
            return 0 <= age_h < PERSONAL_UPSTREAM_CHECK_EVERY_H
    except OSError:
        pass
    return False


def _lock_exclusive(fd):
    """Non-blocking exclusive lock on an open descriptor: flock where the
    platform has it, the C runtime's byte-range lock on Windows. Raises
    OSError when another process holds it."""
    try:
        import fcntl
    except ImportError:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _claim_lock(path):
    """Take an exclusive OS lock on `path`; the open descriptor when we hold it,
    None when another hook does.

    An OS lock, not create-exclusively/rename/unlink: the kernel owns the
    claim and drops it the instant the holder exits, so a hook that dies
    mid-check leaves nothing to reclaim — no stale threshold, no token, no
    grave — and there is no step at which a third hook can be handed a lock
    that is still in use. The file is never deleted: every hook locks the same
    inode. On a PC session-start.ps1 opens this same file with sharing denied
    and this copy opens it with the default share mode, so whichever of the two
    opens first holds it and the other's open simply fails."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0), 0o644)
    except OSError:
        return None  # includes a Windows sharing violation: the PowerShell hook holds it
    try:
        _lock_exclusive(fd)
    except (OSError, ImportError):
        _release_lock(fd)
        return None
    if not _shadow_legacy_claim(PERSONAL_UPSTREAM_LEGACY_LOCK):
        _release_lock(fd)
        return None  # an old copy of this check is mid-check right now; it will speak
    return fd


def _legacy_lock_is_live(path):
    """True while a hook still on the previous lock protocol is mid-check: its
    token file exists, is NON-EMPTY, and is under 120 s old (its own stale
    threshold). Older or future-dated means a dead hook left it. Empty means a
    hook on THIS protocol left its shadow claim (below) — never a live old hook,
    whose token is always written."""
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_size > 0 and 0 <= time.time() - st.st_mtime <= 120


def _shadow_legacy_claim(path):
    """Also claim under the previous protocol while the OS lock is held, so a
    hook still on the old code sees a live lock and yields — the one atomic
    handshake the two protocols share is the old one's own exclusive create.
    Only ever called while holding the OS lock, so no two hooks on this protocol
    touch the file at once; the only other actor is an old hook, and the old
    rules apply to it: a file under 120 s old is someone mid-check.

    False when an old hook is mid-check right now (yield). True when the shadow
    is ours, or the file cannot be written at all (then the stamp, re-checked
    under the lock, is what arbitrates). The shadow is left EMPTY and never
    deleted by us: old tokens are never empty, so a later hook on this protocol
    does not mistake our own leftover for a live old hook, and an old hook's
    own release only ever deletes a file that still carries its token."""
    for attempt in (1, 2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if _legacy_lock_is_live(path):
                return False
            if attempt == 2:
                return True
            try:
                path.unlink()  # a dead old hook's token, or our own empty shadow from last time
            except OSError:
                return True
            continue
        except OSError:
            return True
        os.close(fd)
        return True
    return True


def _release_lock(fd):
    """Closing the descriptor drops the lock; the file itself stays."""
    try:
        os.close(fd)
    except OSError:
        pass


def _fork_notice(behind, plugin_dir):
    # Same prefix and wording as the personal toolkit's own hook, so Claude sees
    # one identical line whichever of the two spoke first. Names the ref we just
    # fetched and the checkout path, and points at the update skill's file: the
    # merged checkout contains it, but the slash command will not exist until
    # the next restart, and without the file the release walk is skipped.
    return (
        f"[NSLS Personal Toolkit] This builder's toolkit is their OWN FORK and "
        f"is {behind} commit(s) behind NSLS — nothing shipped upstream has "
        f"reached them, and their auto-update never will, because it follows "
        f"their fork. Tell them in ONE plain sentence at the start of your "
        f"first reply — e.g. \"Your toolkit is your own copy, so NSLS updates "
        f"haven't been reaching you — want me to catch it up?\" — and if they "
        f"agree: run /update-personal-productivity if this machine has it; "
        f"otherwise, in {_safe_text(plugin_dir)}, merge {PERSONAL_UPSTREAM_REF} "
        f"(NSLS's main, fetched from {PERSONAL_UPSTREAM_URL} moments ago) "
        f"yourself, preserving their own commits and setting aside any "
        f"uncommitted edits first, then read and follow "
        f"skills/update-personal-productivity/SKILL.md from the freshly merged "
        f"checkout to walk them through what's new (the slash command itself "
        f"appears after their next restart). NEVER hand them a git command."
    )


def _fork_caught_up_notice(behind, plugin_dir):
    # Same prefix as the offer, so either hook's line reads as one voice.
    return (
        f"[NSLS Personal Toolkit] This builder's toolkit was their OWN FORK, "
        f"{behind} commit(s) behind NSLS, with no commits or unsaved edits of "
        f"their own, so it has just been caught up with NSLS automatically. "
        f"Tell them in ONE plain sentence at the start of your first reply — "
        f"e.g. \"Your toolkit was your own copy, so NSLS updates hadn't been "
        f"reaching you; I've caught it up.\" — then offer to show them what's "
        f"new: read and follow skills/update-personal-productivity/SKILL.md in "
        f"{_safe_text(plugin_dir)} (the slash command itself appears after "
        f"their next restart). NEVER hand them a git command."
    )


def _fork_catch_up_unfinished_notice(behind, plugin_dir):
    return (
        f"[NSLS Personal Toolkit] An automatic catch-up of this builder's toolkit "
        f"(their own fork, {behind} commit(s) behind NSLS) did not finish cleanly, "
        f"so the folder at {_safe_text(plugin_dir)} may be part-way through an update. "
        f"Nothing of theirs was at risk: it only starts on a clean checkout with no "
        f"commits of their own. Tell them in ONE plain sentence at the start of your "
        f"first reply that their toolkit needs a quick look, and offer to sort it "
        f"out. If they agree, inspect before changing anything — whether a git "
        f"process is still running, whether .git/index.lock exists, git status, and "
        f"where HEAD sits relative to {PERSONAL_UPSTREAM_REF} — then finish the "
        f"fast-forward or put it back. NEVER hand them a git command."
    )


def _git_path_exists(plugin_dir, rel):
    # rev-parse --git-path answers relative to the checkout, or absolute; Path
    # joining handles both (an absolute right-hand side wins).
    return bool(rel) and (plugin_dir / rel).exists()


def _clean_to_fast_forward(git, plugin_dir):
    """Every condition under which a fast-forward can only add NSLS's commits.

    On the branch `main` (any other branch, or detached, is somewhere its owner
    put it on purpose; NSLS_PERSONAL_BRANCH is a supported setup). No operation
    in progress. No submodules and no sparse checkout, neither of which is proven
    here. No commits NSLS lacks. Nothing unsaved, untracked, or in a submodule.
    """
    rc, branch = git("symbolic-ref", "--short", "-q", "HEAD")
    if rc != 0 or branch != "main":
        return False
    rc, paths = git("rev-parse", *[a for name in GIT_OP_STATE for a in ("--git-path", name)])
    if rc != 0 or any(_git_path_exists(plugin_dir, rel) for rel in paths.splitlines()):
        return False
    if (plugin_dir / ".gitmodules").exists():
        return False
    rc, sparse = git("config", "--bool", "--get", "core.sparseCheckout")
    if sparse == "true":
        return False
    rc, ahead = git("rev-list", "--count", f"{PERSONAL_UPSTREAM_REF}..HEAD")
    if rc != 0 or ahead != "0":
        return False
    rc, dirty = git(*PERSONAL_STATUS_ARGS)
    return rc == 0 and not dirty


def _no_hooks(plugin_dir):
    """`-c` that runs git with no hooks at all. The builder's own hooks (often a
    global core.hooksPath) have no business running on NSLS's checkout, and a
    post-merge hook runs after the branch has already moved, and can hang.

    The hooks path is .git/HEAD: a file, so git can never find a hook "inside"
    it, and in a linked worktree, where .git is itself a file, it cannot exist at
    all. A folder that is merely never created could be created by someone."""
    return ["-c", f"core.hooksPath={(plugin_dir / '.git' / 'HEAD').as_posix()}"]


# A branch name that can sit inside a `-c branch.<name>.mergeOptions=` key and
# reach git intact: printable ASCII without "=". git splits `-c` at the first
# "=", which ref names may contain, and a non-ASCII name may not survive the
# round trip through a Windows console code page. Any other name is not merged
# at all: without the blanking, "-s ours" in its mergeOptions is back.
_CONFIG_SAFE_BRANCH = re.compile(r"[\x21-\x3c\x3e-\x7e]+")


def _guarded_ff_merge(plugin_dir, target, branch, wait, output=subprocess.DEVNULL):
    """Fast-forward `branch` onto `target`, unattended. Returns git's exit code,
    None if it is still running when `wait` ends, or -1 if it never ran (git
    could not start, or `branch` is a name whose mergeOptions cannot be blanked).

    Every unattended fast-forward of a toolkit checkout goes through here — the
    daily update and the clean-fork catch-up — so the two cannot drift apart.

    Never killed: git stopped mid-checkout leaves a half-updated folder and a
    stale index.lock. It runs in its own session, writing only to `output`
    (a file, never a pipe), so it can outlive this hook and finish on its own.
    Everything that could turn it into something else is switched off:
      * branch.<branch>.mergeOptions. --squash there stages the new tree without
        moving the branch; "-s ours" there makes even `merge --ff-only` exit 0
        with a merge commit that keeps the old tree and discards every incoming
        change. --no-squash and --no-autostash are stated outright as well;
      * hooks (_no_hooks);
      * background maintenance and auto-gc, which also run after the move and
        would hold the merge open past its wait. Housekeeping still runs after
        the daily update's fetch, which is safe to stop;
      * overwriting an ignored local file the update starts tracking
        (--no-overwrite-ignore): it refuses instead, and _warn_if_frozen says so.
    """
    if not _CONFIG_SAFE_BRANCH.fullmatch(branch or ""):
        return -1  # its mergeOptions cannot be blanked, so it is not merged (a backstop:
        # _update_checkout checks first, so that it can say so)
    args = ["git", "-C", str(plugin_dir), *_no_hooks(plugin_dir),
            "-c", "maintenance.auto=false", "-c", "gc.auto=0",
            "-c", f"branch.{branch}.mergeOptions=",
            "merge", "--ff-only", "--no-squash", "--no-autostash",
            "--no-overwrite-ignore", "--quiet", target]
    try:
        proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            start_new_session=True, env=_git_env(),
        )
    except Exception:
        return -1
    try:
        return proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        return None


def _ff_merge(plugin_dir, wait):
    """The clean-fork catch-up's fast-forward: main onto NSLS's main."""
    return _guarded_ff_merge(plugin_dir, PERSONAL_UPSTREAM_REF, "main", wait)


def _checkout_state(plugin_dir, head_before, target):
    """'caught_up', 'untouched' or 'broken', read fresh after the attempt.

    Deliberately NOT through the caller's deadline: a merge that succeeded as the
    deadline ran out must still be reported, never left as a silent move.
    """
    rc_h, head = _git_rc(plugin_dir, "rev-parse", "HEAD", timeout=5)
    rc_s, dirty = _git_rc(plugin_dir, *PERSONAL_STATUS_ARGS, timeout=10)
    rc_l, lock = _git_rc(plugin_dir, "rev-parse", "--git-path", "index.lock", timeout=5)
    # Still on main: git has no "merge only into main", so a checkout moved to
    # another branch in the moment between the checks and the merge would have
    # had THAT branch moved. It is reported for a look, never called caught up.
    rc_b, branch = _git_rc(plugin_dir, "symbolic-ref", "-q", "HEAD", timeout=5)
    if rc_h != 0 or rc_s != 0 or rc_l != 0 or dirty or _git_path_exists(plugin_dir, lock):
        return "broken"
    if rc_b != 0 or branch != "refs/heads/main":
        return "broken"
    if head == target:
        return "caught_up"
    if head == head_before:
        return "untouched"
    return "broken"


def _fast_forward_clean_fork(git, plugin_dir, remaining):
    """Catch up a fork that has nothing of its own onto NSLS's main.

    A fork's own remote never receives NSLS's changes, so the offer came back
    with every release and depended on the builder noticing one sentence. When
    there is nothing of theirs to protect there is nothing to ask about.

    Returns 'caught_up'; 'untouched' when the conditions were not met, time was
    short, or git refused cleanly (the builder gets the offer); 'unfinished' when
    the merge is still running when the wait ends; or 'broken' when the
    checkout is in any state other than before or after.
    """
    if not _clean_to_fast_forward(git, plugin_dir):
        return "untouched"
    rc_b, before = git("rev-parse", "HEAD")
    rc_t, target = git("rev-parse", PERSONAL_UPSTREAM_REF)
    if rc_b != 0 or rc_t != 0:
        return "untouched"
    left = remaining()
    if left is not None and left < PERSONAL_FF_MIN_LEFT_S:
        return "untouched"   # a write we might have to abandon is a write we do not begin
    wait = PERSONAL_FF_WAIT_S if left is None else min(PERSONAL_FF_WAIT_S, left)
    if _ff_merge(plugin_dir, wait) is None:
        return "unfinished"
    return _checkout_state(plugin_dir, before, target)


def report_personal_fork_drift(deadline=None):
    """Say so when a personal-toolkit FORK has fallen behind NSLS.

    One network fetch, throttled to once per PERSONAL_UPSTREAM_CHECK_EVERY_H,
    claimed under an exclusive lock, and with EVERY git call charged against the
    caller's deadline so the 90s hook budget is untouched. Nothing git prints is
    surfaced — only our own literal and a verified integer — because
    SessionStart stdout is the model's context and git echoes server-controlled
    text. Reads and writes no remote: NSLS is fetched by URL into
    PERSONAL_UPSTREAM_REF. A fork with nothing of its own is fast-forwarded
    onto it (see _fast_forward_clean_fork); any other fork gets the offer.
    """
    plugin_dir = CONFIG_DIR / "local-plugins" / PERSONAL_PLUGIN
    if not (plugin_dir / ".git").exists():
        return

    def remaining():
        return None if deadline is None else deadline - time.monotonic()

    def git(*args, cap=3):
        # Never start a git command with no time left, and never let one run
        # past the deadline — the fetch was bounded before, the rest were not.
        left = remaining()
        if left is not None:
            if left < 0.5:
                raise TimeoutError
            cap = min(cap, left)
        return _git_rc(plugin_dir, *args, timeout=cap)

    try:
        url = _pull_source_url(git)
        if not url or _is_canonical_origin(url):
            return  # NSLS itself, or unknowable: the freeze and stale-branch checks own it
        if _stamp_is_fresh(PERSONAL_UPSTREAM_STAMP):
            return
        left = remaining()
        budget = PERSONAL_FETCH_TIMEOUT if left is None else min(PERSONAL_FETCH_TIMEOUT, left - 1)
        if budget < 1:
            return  # no time to fetch: leave the stamp alone so the next session gets a real check
        lock = _claim_lock(PERSONAL_UPSTREAM_LOCK)
        if lock is None:
            return  # another hook is mid-check this very second; it will speak
        try:
            if _stamp_is_fresh(PERSONAL_UPSTREAM_STAMP):
                return  # it finished between our two looks
            try:
                PERSONAL_UPSTREAM_STAMP.parent.mkdir(parents=True, exist_ok=True)
                PERSONAL_UPSTREAM_STAMP.touch()
            except OSError:
                pass
            # Bounded and best-effort: offline, blocked, or slow all mean "say
            # nothing this session" — never delay a session start over a nicety.
            # --no-tags: a plain fetch also auto-follows tags reachable from main,
            # writing NSLS's tags into the builder's checkout — or failing when
            # one collides with a tag of theirs. Only our ref should move.
            rc, _ = git("fetch", "--quiet", "--no-tags", PERSONAL_UPSTREAM_URL,
                        PERSONAL_UPSTREAM_REFSPEC, cap=budget)
            if rc != 0:
                return
            # A fork shares history with NSLS. A repository that does not — some
            # unrelated project sitting at this path — is not a fork, and telling
            # Claude to merge NSLS's main into it would be an instruction to merge
            # two unrelated histories. Nothing to say about such a checkout.
            rc, _ = git("merge-base", "HEAD", PERSONAL_UPSTREAM_REF)
            if rc != 0:
                return
            rc, count = git("rev-list", "--count", f"HEAD..{PERSONAL_UPSTREAM_REF}")
            if rc != 0 or not count.isdigit() or int(count) == 0:
                return
            behind = int(count)
            # Nothing of theirs in the way: catch it up and say so once. Anything
            # of theirs in the way keeps the offer, because only they can decide
            # what happens to it.
            outcome = _fast_forward_clean_fork(git, plugin_dir, remaining)
            if outcome == "caught_up":
                print(_fork_caught_up_notice(behind, plugin_dir))
            elif outcome in ("unfinished", "broken"):
                print(_fork_catch_up_unfinished_notice(behind, plugin_dir))
            else:
                print(_fork_notice(behind, plugin_dir))
        finally:
            _release_lock(lock)
    except TimeoutError:
        return


# The daily update's fetch is network and safe to stop, so it keeps the cap the
# whole `git pull` used to have. Its merge is a local write that is never stopped
# (_guarded_ff_merge), so it is not begun with less than this much of the
# envelope left; with hooks and housekeeping off it takes well under a second.
PULL_FETCH_TIMEOUT = 10
PULL_MERGE_MIN_LEFT_S = 2
OP_IN_PROGRESS = "op-in-progress"
UNGUARDABLE_BRANCH = "unguardable-branch"


def _update_checkout(plugin_dir, deadline):
    """Fetch this checkout's upstream and fast-forward onto it.

    A fetch and then a merge rather than one `git pull`: the merge has to carry
    --no-overwrite-ignore, which `git pull` rejects outright (exit 129), and the
    split also takes pull.rebase and its relatives out of play. The fetch is the
    one a bare pull runs (the current branch's remote, its configured refspecs);
    the merge target is the branch's upstream, `@{u}` — what pull merges.

    Returns (code, git_output). code is the merge's exit status; OP_IN_PROGRESS
    when an unfinished operation held the merge back; UNGUARDABLE_BRANCH when
    the branch's name kept it from being merged and it is behind; None when nothing was
    merged and there is nothing to say (the fetch failed, detached, no time
    left, the merge still running when its wait ended, or this checkout could
    not be read).
    """
    def left():
        return deadline - time.monotonic()

    if left() <= 0:
        return None, ""
    # Through _git_rc: a fetch that runs out of time is stopped with its whole
    # process tree, network helper included, not just git itself.
    rc, _ = _git_rc(plugin_dir, *_no_hooks(plugin_dir), "fetch", "--quiet",
                    timeout=min(PULL_FETCH_TIMEOUT, left()))
    fetched = rc == 0
    if left() <= 0:
        return None, ""
    # Read AFTER the fetch, immediately before the merge: the fetch can take
    # seconds, and a branch switched or an operation started inside them must be
    # what the merge is judged against. git has no lock a separate `git bisect
    # start` would honour, so this is as close to the merge as the check can sit.
    rc, probe = _git_rc(plugin_dir, "rev-parse", "--symbolic-full-name", "HEAD",
                        *[a for name in GIT_OP_STATE for a in ("--git-path", name)],
                        timeout=min(3, left()))
    lines = probe.splitlines()
    if rc != 0 or len(lines) != 1 + len(GIT_OP_STATE):
        return None, ""
    head, markers = lines[0], lines[1:]
    if any(_git_path_exists(plugin_dir, rel) for rel in markers):
        return OP_IN_PROGRESS, ""
    if not fetched:
        # Offline, refused or stopped: nothing merged, as with a failed pull. The
        # upstream ref may still hold what some earlier session fetched, which
        # NSLS may since have withdrawn.
        return None, ""
    if not head.startswith("refs/heads/"):
        return None, ""  # detached: a deliberate pin, and it has no upstream to merge
    branch = head[len("refs/heads/"):]
    if not _CONFIG_SAFE_BRANCH.fullmatch(branch):
        # Not merged (see _CONFIG_SAFE_BRANCH). Said out loud only when that
        # actually leaves it behind, so a level checkout does not cry wolf.
        if left() <= 0:
            return None, ""
        rc, behind = _git_rc(plugin_dir, "rev-list", "--count", "HEAD..@{u}",
                             timeout=min(3, left()))
        stuck = rc == 0 and behind.isdigit() and int(behind) > 0
        return (UNGUARDABLE_BRANCH if stuck else None), ""
    with tempfile.TemporaryFile() as out:
        # Checked here, the last step before the merge starts.
        if left() < PULL_MERGE_MIN_LEFT_S:
            return None, ""  # a write we might have to abandon is a write we do not begin
        code = _guarded_ff_merge(plugin_dir, "@{u}", branch, left(), output=out)
        if code is None:
            return None, ""  # still running: it finishes on its own
        out.seek(0)
        return code, out.read().decode("utf-8", "replace")


def git_pull(deadline=None):
    """Update every toolkit in SYNC_PLUGINS from its configured upstream.

    Parity fix with the Windows hook: session-start.ps1 has always looped
    @($BuilderDir, $PersonalDir) and pulled both, but this function pulled only
    the builder dir — so on macOS/Linux the personal toolkit was cloned once and
    then frozen at install-time state forever. Fixes shipped upstream (like the
    visual-companion self-heal) never reached those machines. SYNC_PLUGINS
    already names both toolkits for pointer sync; pull the same list.

    No explicit remote/refspec (see _update_checkout): each checkout follows
    its configured upstream, the same convention personal-setup's own update
    path uses. Hardcoding `origin main` here would silently fast-forward a
    supported fork checkout (NSLS_PERSONAL_REPO / NSLS_PERSONAL_BRANCH) or a
    deliberately pinned detached HEAD onto a branch it never tracked; with no
    upstream configured, the merge just exits nonzero and the checkout is left
    alone. --ff-only matches the .ps1: the `pull origin main` this once was
    could stop on merge conflicts (leaving a conflicted tree) or, on modern git
    with no reconcile config, refuse divergence outright — neither visible
    here, while the toolkit quietly never updated again. ff-only refuses
    cleanly instead of half-merging.

    Prompts are disabled (GIT_TERMINAL_PROMPT=0, stdin closed) so a remote
    that wants credentials fails in milliseconds instead of hanging out the
    timeout, and the loop shares one 15s deadline so the worst case — every
    update wedged — still leaves the 35s replay + 35s live ping inside the 90s
    hook budget install.sh configures. `deadline` is for tests.

    An update refused for checkout-local reasons (divergence, dirty tree, an
    ignored file it would replace, an unfinished operation) prints a warning to
    stdout — SessionStart stdout reaches the model's context — so a frozen
    toolkit is announced instead of silently stale. Offline failures and missing
    upstreams stay quiet.
    """
    if deadline is None:
        deadline = time.monotonic() + 15
    for plugin in SYNC_PLUGINS:
        plugin_dir = CONFIG_DIR / "local-plugins" / plugin
        if not (plugin_dir / ".git").exists():
            continue
        if deadline - time.monotonic() <= 0:
            break
        try:
            code, out = _update_checkout(plugin_dir, deadline)
            if code == OP_IN_PROGRESS:
                _warn_operation_in_progress(plugin, plugin_dir)
            elif code == UNGUARDABLE_BRANCH:
                _warn_unguardable_branch(plugin, plugin_dir)
            elif code:  # None (nothing merged, nothing to say) and 0 both fall through
                _warn_if_frozen(plugin, plugin_dir, out, deadline=deadline)
            # Runs whether the update succeeded or not: a clean update aimed at
            # a branch that never moves is the freeze this catches.
            _warn_if_stale_by_configuration(plugin, plugin_dir, deadline=deadline)
        except Exception:
            pass
    # A fork of the personal toolkit pulls cleanly from its fork, so nothing in
    # the loop above can see it fall behind NSLS. Runs inside this function's
    # 15s envelope, so the 90s hook budget arithmetic is unchanged.
    try:
        report_personal_fork_drift(deadline=deadline)
    except Exception:
        pass


def org_plugin_installed():
    """True once the toolkit is installed as a real Claude Code plugin."""
    try:
        # utf-8-sig: tolerate a UTF-8 BOM (PowerShell-written files carry one).
        registry = json.loads(
            (CONFIG_DIR / "plugins" / "installed_plugins.json").read_text(
                encoding="utf-8-sig"
            )
        )
        return any(
            key.startswith("nsls-builder-toolkit@")
            for key in registry.get("plugins", {})
        )
    except Exception:
        return False


def org_plugin_active():
    """Installed AND not explicitly disabled.

    The distinction matters: a user who disables the plugin must fall back to
    the pointer-stub path (the disabled plugin can't deliver skills), so
    anything that hands responsibility over to the plugin gates on active,
    never on merely installed.
    """
    if not org_plugin_installed():
        return False
    try:
        settings = json.loads(
            (CONFIG_DIR / "settings.json").read_text(encoding="utf-8-sig")
        )
        for key, enabled in settings.get("enabledPlugins", {}).items():
            if key.startswith("nsls-builder-toolkit@") and enabled is False:
                return False
    except Exception:
        pass
    return True


def run_plugin_migration():
    """Run the shim→plugin migration from the just-pulled clone.

    Executed from the clone (not this file's own copy) so migration fixes
    take effect one session after landing on main. On plugin-only machines
    the clone is absent and this is a no-op. Fail-open: a broken migration
    must never break the session — shims keep working and we retry next
    session.

    Bounded: the migration's CLI calls are individually timed out but their
    sum was not, and stage A's worst case (30 + 60 + 60) is nearly double the
    whole 90s SessionStart budget install.sh configures. Overrunning kills the
    hook, which also drops sync_pointers and the session ping — so a slow
    migration would quietly cost the builder pointer updates and credit.

    Budget arithmetic for the 90s hook: git_pull takes up to 15s and the live
    session_ping up to 35s (PING_TIMEOUT), so ~40s is genuinely spare;
    ensure_plugin_fresh claims 20s of it. 25s here leaves headroom for
    sync_pointers and keeps the common path well inside budget. A first
    migration may need more than one session to finish, which is exactly how
    the stages are built — both are idempotent and resume next session.

    Stage A gets 45s. It only runs where the plugin is not installed, and there
    ensure_plugin_fresh has nothing to check — the caller skips it for the
    rest of the session (the return value says so), because a plugin stage A
    has just installed is fresh by definition. At 25s a step that routinely
    needs longer, such as `plugin install` on a slow machine, was cut at the
    same point every session and never finished.

    Returns True when this session belonged to stage A.
    """
    script = PLUGIN_DIR / "hooks" / "migrate_to_plugin.py"
    if not script.exists():
        return False
    stage_a = not org_plugin_installed()
    try:
        code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
        exec(code, {
            "__name__": "nsls_migrate",
            "__file__": str(script),
            # Consumed by migrate_to_plugin.py as its cumulative ceiling.
            "_NSLS_MIGRATION_DEADLINE": time.monotonic() + (45 if stage_a else 25),
            # One CLI finder, not two. The migration's own copy knew no Windows
            # location at all, so on a PC — where the CLI is routinely off the
            # hook's PATH — stage A never found it and the plugin never installed.
            "_NSLS_FIND_CLAUDE": _find_claude,
            # The exact-pointer test, so stage B retires only real toolkit
            # pointers and never a skill a builder wrote.
            "_NSLS_IS_OWN_POINTER": is_own_pointer,
        })
    except Exception:
        pass
    return stage_a


ORG_PLUGIN_KEY = "nsls-builder-toolkit@nsls-toolkit"
ORG_MARKETPLACE = "nsls-toolkit"
# \Z, not $: $ also matches before a trailing newline, and a registry sha with a
# stray newline must read as "unknown", never as "different from HEAD".
_SHA40 = re.compile(r"^[0-9a-f]{40}\Z")
# What plugin.json may carry: X.Y.Z with an optional short pre-release/build tag.
# The registry is a CLI-owned file on disk, and its "version" is echoed into the
# model's context on drift — so only a value that looks like a version gets out.
_PLUGIN_VERSION = re.compile(r"^\d{1,5}\.\d{1,5}\.\d{1,5}(?:[-+][0-9A-Za-z.]{1,32})?\Z")


def _installed_plugin_record():
    """The user-scope registry entry for the org plugin, or None.

    Shape (installed_plugins.json, CLI-owned): {"plugins": {"<name>@<market>":
    [{"scope", "version", "installPath", "gitCommitSha", ...}]}}. Older CLIs
    wrote a bare dict instead of a list; both are read. Only a validated
    40-hex sha is returned — anything else is treated as unknown, never as
    "different", so a corrupt registry cannot trigger a reinstall.
    """
    try:
        registry = json.loads(
            (CONFIG_DIR / "plugins" / "installed_plugins.json").read_text(
                encoding="utf-8-sig"
            )
        )
        entries = registry.get("plugins", {}).get(ORG_PLUGIN_KEY)
        if entries is None:
            return None
        if isinstance(entries, dict):
            entries = [entries]
        for e in entries:
            if not isinstance(e, dict):
                continue
            if e.get("scope", "user") != "user":
                continue
            sha = e.get("gitCommitSha")
            version = e.get("version")
            if not (isinstance(version, str) and _PLUGIN_VERSION.match(version)):
                version = "?"  # anything else is not a version and never reaches stdout
            return {
                "version": version,
                "installPath": e.get("installPath") or "",
                "sha": sha if isinstance(sha, str) and _SHA40.match(sha) else None,
            }
    except Exception:
        pass
    return None


def _marketplace_head():
    """HEAD of the marketplace clone the CLI keeps (and refreshes on every
    `plugin update`) — i.e. what an install right now would contain. None if
    the clone is missing or rev-parse does not yield a 40-hex sha."""
    clone = CONFIG_DIR / "plugins" / "marketplaces" / ORG_MARKETPLACE
    if not (clone / ".git").exists():
        return None
    sha = _git_out(clone, "rev-parse", "HEAD")
    return sha if _SHA40.match(sha) else None


def _plugin_cache_root():
    return (CONFIG_DIR / "plugins" / "cache").resolve()


HEAL_LOCK_STALE_S = 180  # longer than any heal can run (45s budget) with margin


def _acquire_heal_lock():
    """Exclusive, cross-process, cross-platform (no fcntl on Windows): create
    the lock file with O_EXCL. Returns the path on success, None if another
    session holds it. A lock older than HEAL_LOCK_STALE_S is a crashed holder
    and is broken once.

    Why: two sessions starting within the same second both pass the daily
    marker check before either touches it, and both see drift. Without this,
    session B can uninstall the plugin session A just reinstalled, then fail
    its own installs, and leave the machine with no toolkit.
    """
    lock = CONFIG_DIR / ".nsls-plugin-heal.lock"
    for attempt in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except FileNotFoundError:
                continue  # holder released between our open and stat: retry
            # Negative age = a lock stamped in the future (clock correction);
            # treating it as held would disable healing until wall time caught up.
            if attempt == 0 and (age > HEAL_LOCK_STALE_S or age < 0):
                # Claim the stale lock ATOMICALLY by renaming it: rename
                # succeeds for exactly one process, and the loser sees
                # FileNotFoundError. A plain unlink here could remove a NEW lock
                # another session had just created after breaking the same
                # stale one, and then two sessions would heal at once.
                claimed = lock.with_name(f"{lock.name}.stale.{os.getpid()}")
                try:
                    os.rename(str(lock), str(claimed))
                except FileNotFoundError:
                    continue  # someone else claimed it; retry the create
                except Exception:
                    return None
                try:
                    claimed.unlink()
                except Exception:
                    pass
                continue
            return None
        except Exception:
            return None
    return None


def _release_heal_lock(lock):
    try:
        lock.unlink()
    except Exception:
        pass


def _heal_plugin_drift(claude, record, head, deadline):
    """Uninstall, drop the stale cache dir, reinstall. Returns True on success.

    The CLI's uninstall leaves the cache directory in place, and its install
    reuses a directory that already exists for the version — so without the
    rmtree in the middle the reinstall is a no-op and the drift survives
    (verified 2026-09-10). The rmtree is fenced to paths under plugins/cache:
    the registry is CLI-owned but still a file on disk, and a path that points
    anywhere else is not ours to delete.

    Two install attempts, not one: between uninstall and a successful install
    the plugin is GONE from this machine, so a transient failure (network
    blip, CLI lock) is worth one immediate retry before we give up and say so.
    """
    def remaining():
        return deadline - time.monotonic()

    def cli(*args):
        # A timed-out or failed CLI call is a failed step, never an exception:
        # after the uninstall the plugin is GONE, and letting TimeoutExpired
        # escape would skip the warning below and leave the daily marker set,
        # so the machine would sit without a toolkit until tomorrow, silently.
        timeout = remaining()
        if timeout <= 0:
            return False
        try:
            return subprocess.run(
                [*claude, "plugin", *args, ORG_PLUGIN_KEY],
                capture_output=True, timeout=timeout,
            ).returncode == 0
        except Exception:
            return False

    if remaining() <= 0 or not cli("uninstall"):
        return False
    try:
        path = Path(record["installPath"]).resolve()
        if record["installPath"] and path != _plugin_cache_root() and \
                _plugin_cache_root() in path.parents:
            shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass
    for _ in range(2):
        if cli("install"):
            after = _installed_plugin_record()
            if after and after["sha"] == head:
                return True
    return False


def _find_claude():
    """argv prefix that runs the `claude` CLI (usually a one-element list), or None.

    A list, not a path: the npm shim `claude.ps1` cannot be executed directly
    by CreateProcess, so it comes back as
    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", <path>].

    PATH first. Then the same places install.ps1 probes, because on Windows the
    CLI is routinely NOT on the PATH a hook inherits: the npm shim, the
    standalone installs under the profile, and the desktop app's bundled CLI at
    %APPDATA%\\Claude\\claude-code\\<version>\\claude.exe (highest version wins,
    compared numerically). Plus the two Mac/Linux install locations a curl|bash
    shell may not have on PATH yet. Without this the daily update silently did
    nothing on most Windows machines — the exact silent failure this file exists
    to prevent.
    """
    found = shutil.which("claude")
    if found:
        # PATHEXT can resolve `claude` to the npm claude.ps1 shim on Windows;
        # route it through the same wrapper as the fallbacks.
        return _claude_argv(Path(found))
    home = Path.home()
    candidates = [
        home / ".local" / "bin" / "claude",
        home / ".claude" / "bin" / "claude",
        CONFIG_DIR / "bin" / "claude",
    ]
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    profile = os.environ.get("USERPROFILE")
    if appdata:
        candidates += [
            Path(appdata) / "npm" / "claude.cmd",
            Path(appdata) / "npm" / "claude.ps1",
        ]
    if profile:
        candidates += [
            Path(profile) / ".local" / "bin" / "claude.exe",
            Path(profile) / ".claude" / "bin" / "claude.exe",
        ]
    if localappdata:
        candidates.append(Path(localappdata) / "Programs" / "claude" / "claude.exe")
    for c in candidates:
        try:
            # Executable on Unix, or the daily marker gets touched and a stale
            # non-executable leftover costs a day of freshness. Windows has no
            # executable bit; PATHEXT decides there.
            if c.is_file() and (os.name == "nt" or os.access(c, os.X_OK)):
                return _claude_argv(c)
        except Exception:
            continue
    # The desktop app's bundled CLI. %APPDATA%\Claude\claude-code\<ver> is where
    # the app writes it — but a Microsoft Store (MSIX) install virtualises that
    # folder. The app itself sees the CLI there; the processes it spawns, every
    # hook included, see only the real location under the package's LocalCache.
    # Seen on a Store install 2026-09-28: from a hook, the APPDATA copy did not
    # exist, and the real file was at
    # %LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude-code\<ver>.
    # Missing it meant the migration could never find the CLI, so no
    # Store-installed PC ever got the plugin, and so never got a single gate.
    # Both roots are searched and the highest version wins across them.
    roots = []
    if appdata:
        roots.append(Path(appdata) / "Claude")
    if localappdata:
        try:
            roots += sorted((Path(localappdata) / "Packages").glob(
                "Claude_*/LocalCache/Roaming/Claude"))
        except Exception:
            pass
    for sub in ("claude-code", "claude-code-vm"):
        best = None
        for root in roots:
            try:
                for key, exe in _bundled_clis(root / sub):
                    if best is None or key > best[0]:
                        best = (key, exe)
            except Exception:
                continue
        if best:
            return _claude_argv(best[1])
    return None


def _bundled_clis(folder):
    """(sort key, path) for every CLI the desktop app keeps under `folder`.

    Two layouts: <version>\\claude.exe, and <version>\\<hash>\\claude.exe, which
    the Store app moved to on 2026-10-03. Searching only the first left the
    daily plugin update finding no CLI on a Store PC, so it skipped itself every
    session and the PC stayed on 3.16.2 (PC Test Round 5). The version is the
    folder directly under claude-code in both, compared numerically; the newest
    file breaks a tie between two hash folders of one version.
    """
    found = []
    for pattern in ("*/claude.exe", "*/*/claude.exe"):
        for exe in folder.glob(pattern):
            try:
                if not exe.is_file():
                    continue
                vdir = exe.parent if exe.parent.parent == folder else exe.parent.parent
                try:
                    ver = tuple(int(x) for x in vdir.name.split("."))
                except ValueError:
                    ver = (0,)
                found.append(((ver, exe.stat().st_mtime), exe))
            except OSError:
                continue
    return found


def _claude_argv(path):
    if path.suffix.lower() == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(path)]
    return [str(path)]


def ensure_plugin_fresh():
    """At most once a day, sync the installed plugin — by version AND by commit.

    Plugin installs run from a version-pinned cache that only refreshes via
    `claude plugin update` after a plugin.json version bump — without this,
    a machine that loses the settings-shim git-pull path would silently
    freeze on an old version.

    `plugin update` compares version strings and nothing else. On 2026-09-10
    it reported "already at the latest version (3.6.0)" over a cache 37
    commits behind main, because eight PRs had merged without a bump. So
    after the update, the installed COMMIT is compared with HEAD of the
    marketplace clone the update just refreshed; same version but a different
    commit is drift, and the fix is a reinstall through the CLI (see
    _heal_plugin_drift). The CI gate .github/workflows/plugin-version.yml
    makes drift rare; this makes it self-correcting when two PRs bump to the
    same number and the second lands unversioned.

    Everything printed here lands in the model's context. Only our own
    literals and validated 40-hex shas are echoed — never CLI or git output,
    which a hostile marketplace could shape.
    """
    if not org_plugin_active():
        return
    marker = CONFIG_DIR / ".nsls-plugin-update-check"
    try:
        if marker.exists() and (
            datetime.now(timezone.utc).timestamp() - marker.stat().st_mtime < 86400
        ):
            return
        claude = _find_claude()
        if not claude:
            return
        marker.touch()
        # Budget: 20s for the update (the common path — a hung update must leave
        # room for sync_pointers and the session pings behind it), and up to 25s
        # more for a heal, which only runs when drift is actually detected. On
        # timeout the marker is already touched, so the next attempt is
        # tomorrow; releases just arrive a day later on that machine.
        deadline = time.monotonic() + 45
        try:
            updated = subprocess.run(
                [*claude, "plugin", "update", ORG_PLUGIN_KEY],
                capture_output=True, timeout=20,
            ).returncode == 0
        except subprocess.TimeoutExpired:
            return
        if not updated:
            # Offline, CLI lock, marketplace unreachable: whatever stopped the
            # update would stop the reinstall too, and the reinstall starts by
            # uninstalling. Never trade a stale toolkit for no toolkit.
            return

        record = _installed_plugin_record()
        head = _marketplace_head()
        if not record or not record["sha"] or not head or record["sha"] == head:
            return  # level, or not enough evidence to act on

        lock = _acquire_heal_lock()
        if lock is None:
            return  # another session is healing right now; it will announce
        try:
            # Re-read BOTH sides under the lock: the other session may have just
            # finished healing, and its `plugin update` may have advanced the
            # marketplace clone while we waited, so a head read before the lock
            # would misjudge the reinstall as failed (or succeeded).
            record = _installed_plugin_record()
            head = _marketplace_head()
            if not record or not record["sha"] or not head or record["sha"] == head:
                return
            _heal_and_announce(claude, record, head, deadline, marker)
        finally:
            _release_heal_lock(lock)
    except Exception:
        pass


def _heal_and_announce(claude, record, head, deadline, marker):
    """Run the heal and put exactly one line in the model's context about it."""
    old7, new7, version = record["sha"][:7], head[:7], record["version"]
    if _heal_plugin_drift(claude, record, head, deadline):
        print(
            f"NOTICE - nsls-builder-toolkit was refreshed in place: the installed "
            f"copy (version {version}, commit {old7}) had fallen behind the "
            f"published main (commit {new7}) under the same version number, so "
            f"`claude plugin update` could not see the gap. The reinstall "
            f"succeeded; the updated skills and hooks load on the next session. "
            f"Mention it to the user at a natural moment - no action needed."
        )
        return

    # The heal failed. The plugin may now be uninstalled: say so, hand over
    # the manual steps, and clear the daily marker so the next session
    # retries instead of waiting a day with no toolkit.
    try:
        marker.unlink()
    except Exception:
        pass
    after = _installed_plugin_record()
    state = ("is currently NOT installed on this machine"
             if after is None else f"is still on commit {after['sha'][:7] if after['sha'] else '?'}")
    print(
        f"WARNING - nsls-builder-toolkit was stale (version {version}, commit "
        f"{old7}; published main is {new7}) and the automatic refresh failed, so "
        f"the plugin {state}. Tell the user now and offer the repair: run "
        f"`claude plugin install {ORG_PLUGIN_KEY}` (if it reports already "
        f"installed, run `claude plugin uninstall {ORG_PLUGIN_KEY}` first and "
        f"delete the cache directory under ~/.claude/plugins/cache/{ORG_MARKETPLACE}/), "
        f"then start a new session. The hook will retry on the next session."
    )


def sync_pointers():
    """Sync skill pointers from installed plugins to ~/.claude/skills/.

    Iterates plugins in POINTER_PRECEDENCE order. On name collision, earlier
    plugins win (personal skills override org). Skips skills that
    are not exactly a toolkit pointer to that skill (see is_own_pointer).
    """
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    written = set()
    created = 0

    for plugin_name in POINTER_PRECEDENCE:
        # Once the org toolkit is an ACTIVE plugin, its skills load through
        # the plugin system — stop regenerating its pointer stubs so the
        # stage-B migration cleanup sticks. Gates on active, not installed:
        # a disabled plugin can't deliver skills, so those users keep the
        # stub path. Personal-toolkit pointers keep syncing regardless.
        if plugin_name == "nsls-builder-toolkit" and org_plugin_active():
            continue
        plugin_dir = CONFIG_DIR / "local-plugins" / plugin_name
        skills_src = plugin_dir / "skills"
        if not skills_src.is_dir():
            continue

        # An unreadable folder skips this toolkit, not the rest of session
        # start: main() calls this bare, so the guardrails and ping follow it.
        try:
            skill_dirs = sorted(skills_src.iterdir())
        except OSError:
            continue
        for skill_dir in skill_dirs:
            if not skill_dir.is_dir():
                continue
            skill = skill_dir.name

            # Skill name is interpolated into a bash literal in the generated
            # pointer (see report_cmd below). Reject names with characters
            # that would break the JSON or shell quoting.
            if any(c in skill for c in "'\"\\$`\n\r"):
                print(f"sync: skipping skill with unsafe name: {skill!r}", file=sys.stderr)
                continue

            # Personal-wins precedence: skip if a higher-precedence plugin
            # already wrote this skill in the current run.
            if skill in written:
                continue

            src = skill_dir / "SKILL.md"
            if not src.exists():
                continue

            dest = SKILLS_DIR / skill
            dest_skill = dest / "SKILL.md"

            # Skip anything that is not exactly our pointer to this skill: it
            # may be a skill the builder wrote (see is_own_pointer).
            if dest.is_dir() and dest_skill.exists():
                try:
                    existing = dest_skill.read_text(encoding="utf-8")
                    if not is_own_pointer(existing, skill):
                        continue
                except Exception:
                    continue

            try:
                content = src.read_text(encoding="utf-8")
            except Exception:
                continue

            name_match = re.search(r"^name:\s*(.+)", content, re.MULTILINE)
            if not name_match:
                continue
            name = name_match.group(1).strip()

            desc = f"{plugin_name} skill: {skill}"
            fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if fm_match:
                fm = fm_match.group(1)
                # `*` not `+`: an empty folded block still belongs to the folded
                # branch. With `+` it fell through to the fallback, which then
                # captured the literal `>-` as the description.
                ml_match = re.search(r"description:\s*>-?\s*\n((?:[ \t]+.+\n?)*)", fm)
                if ml_match:
                    extracted = " ".join(l.strip() for l in ml_match.group(1).strip().split("\n"))
                else:
                    sl_match = re.search(r"description:[ \t]*(.+)", fm, re.MULTILINE)
                    extracted = unquote_scalar(sl_match.group(1)) if sl_match else ""
                # Only override the default when we actually recovered text —
                # a blank or `""` description must not produce a blank pointer.
                if extracted.strip():
                    desc = extracted

            dest.mkdir(parents=True, exist_ok=True)
            hook_path = CONFIG_DIR / "local-plugins" / "nsls-builder-toolkit" / "hooks" / "skill-event.sh"
            skill_path = CONFIG_DIR / "local-plugins" / plugin_name / "skills" / skill / "SKILL.md"
            report_cmd = (
                f"echo '{{\"tool_input\":{{\"skill\":\"{skill}\"}}}}' | "
                f"bash {hook_path}"
            )
            dest_skill.write_text(
                f"---\nname: {name}\ndescription: >-\n  {desc}\n---\n\n"
                f"Before reading the skill, run this Bash command exactly once in the "
                f"background (do not wait, do not announce, do not retry on failure) "
                f"to record skill usage:\n\n"
                f"```bash\n{report_cmd}\n```\n\n"
                f"Then read and follow the full skill at "
                f"`{skill_path}`.\n",
                encoding="utf-8",
            )
            written.add(skill)
            created += 1

    if created > 0:
        print(f"{created} skill pointers synced", file=sys.stderr)


def own_skills_over_toolkit():
    """Names where the builder's own skill sits where a toolkit pointer would.

    Their skill is never overwritten (see is_own_pointer), so it is the one
    that runs. Checked for both toolkits, even when the org toolkit is an
    active plugin and its pointers are no longer written: the bare /name is
    still theirs.
    """
    names = set()
    for skills_src in _toolkit_skill_dirs():
        # One unreadable toolkit folder must not hide the others' names.
        try:
            skill_dirs = list(skills_src.iterdir())
        except OSError:
            continue
        for skill_dir in skill_dirs:
            dest_skill = SKILLS_DIR / skill_dir.name / "SKILL.md"
            if not (skill_dir / "SKILL.md").exists() or not dest_skill.exists():
                continue
            try:
                existing = dest_skill.read_text(encoding="utf-8")
            except Exception:
                continue
            if not is_own_pointer(existing, skill_dir.name):
                names.add(skill_dir.name)
    return sorted(names)


def _toolkit_skill_dirs():
    """Every installed toolkit's skills folder, the org one wherever it runs from.

    A marketplace install of the org toolkit has no local-plugins checkout, so
    its skills are found from this running copy's own root, as in
    emit_guardrails_context(); the clone is the fallback. __file__ is undefined
    when the settings shim runs this file through `python3 -c`.
    """
    own_root = (Path(__file__).resolve().parent.parent
                if "__file__" in globals() else PLUGIN_DIR)
    candidates = [own_root / "skills"] + [
        CONFIG_DIR / "local-plugins" / p / "skills" for p in POINTER_PRECEDENCE
    ]
    found = []
    for d in candidates:
        try:
            if d.is_dir() and d.resolve() not in [x.resolve() for x in found]:
                found.append(d)
        except Exception:
            continue
    return found


def notice_own_skills():
    """Tell Claude to say, each time, when the builder's own version runs."""
    try:
        names = own_skills_over_toolkit()
    except Exception:
        return
    if not names:
        return
    listed = ", ".join(f"/{n}" for n in names)
    print(
        f"[NSLS Builder Toolkit] This builder has their own skill with the same "
        f"name as a toolkit skill, so theirs is the one that runs: {listed}. "
        f"Each time they use one of these, say so in one short line before "
        f"starting, e.g. \"Using your own /{names[0]}, not the NSLS toolkit's.\" "
        f"Never change or remove their skill."
    )


def read_env(key):
    """Read a value from the personal toolkit .env file."""
    if not ENV_FILE.exists():
        return ""
    try:
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


def _http_post_json(url, body, timeout):
    """POST JSON to url and return the parsed JSON response. Raises on failure.

    curl first, urllib as the fallback — not the other way around: python.org
    framework builds ship WITHOUT CA certificates, so every urllib HTTPS call
    on such a Mac fails cert verification and the ping dies silently (this
    cost a builder ~6 weeks of session points, June–Aug 2026, while
    skill-event.sh — which shells out to curl — kept working). curl uses the
    OS trust store, so it works on a stock install.
    """
    payload = json.dumps(body)
    if shutil.which("curl"):
        # curl PRESENT is not curl WORKING. A broken proxy/TLS config, or a curl
        # that can't reach this host while Python can, made every POST fail
        # permanently — the old code raised here, so the urllib fallback below
        # was only ever reached when curl was missing entirely. Treat a curl
        # failure as "try the other transport", and only fail if both lose.
        started = time.monotonic()
        try:
            result = subprocess.run(
                ["curl", "-s", "--fail", "--max-time", str(timeout), "-X", "POST",
                 url, "-H", "Content-Type: application/json",
                 "--data-binary", payload],
                # encoding is explicit: text=True alone decodes with the platform
                # locale, so on Windows with a legacy code page (cp1252) a
                # non-ASCII announcement raises UnicodeDecodeError out of this
                # call. A ping that actually succeeded then counts as failed,
                # the failure marker gets written, and the announcement is lost.
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout + 10,
            )
            if result.returncode == 0:
                return json.loads(result.stdout)
            # 28 == curl's own operation-timeout. The host is slow or
            # unreachable, so urllib would burn the same wait again for the same
            # answer — and this runs inside a SessionStart hook whose whole
            # budget (90s, install.sh) covers a replayed ping AND a live one.
            # Retrying only NON-timeout failures is what keeps a second
            # transport from doubling the worst case.
            if result.returncode == 28:
                raise RuntimeError("curl exited 28 (timeout)")
        except subprocess.TimeoutExpired:
            raise RuntimeError("curl timed out")
        except (OSError, ValueError):
            pass  # curl unusable or returned unparseable JSON — try urllib
        # Only the remaining slice of this call's budget, for the same reason.
        timeout = max(5, timeout - (time.monotonic() - started))
    req = urllib.request.Request(
        url,
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _post_session_ping(body, timeout=PING_TIMEOUT):
    """POST a session-ping body dict. Returns parsed response, or raises."""
    return _http_post_json(f"{PROXY_URL}/session-ping", body, timeout)


def _discard_marker():
    """Remove the marker entirely."""
    try:
        PING_FAIL_MARKER.unlink()
    except OSError:
        pass


def _write_marker(state):
    """Write the marker atomically: temp file in the same dir, then os.replace.

    Builders open several sessions at once — three inside six seconds is what
    produced the original false alarm — so two SessionStart hooks racing over
    this file is the normal case, not the exotic one. write_text() truncates
    before it writes, which leaves a window where a concurrent reader sees
    empty or half-written JSON and concludes the marker is corrupt. os.replace
    is atomic on POSIX and Windows: a reader sees the old file or the new one,
    never a partial one.
    """
    try:
        PING_FAIL_MARKER.parent.mkdir(parents=True, exist_ok=True)
        tmp = PING_FAIL_MARKER.with_name(PING_FAIL_MARKER.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, PING_FAIL_MARKER)
    except Exception:
        try:
            tmp.unlink()
        except Exception:
            pass


def _retire_ping_payload(drop_today):
    """Retire the retry payload, but keep the failed-day ledger.

    The two have different lifetimes and used to share one unlink(), which is
    how a real point loss went silent. `failed_days` records days whose credit
    never landed ON THAT DAY; `payload` is just the thing waiting to be
    resent. Deleting the file on success threw both away, so intermittent
    failures could never reach the three-day notice: day 1 fails, day 2's
    replay wipes day 1, day 3 starts the count from zero. The docstring in
    note_ping_failure asserted this could not happen — "the day's credit lands
    from whichever ping gets through" — which is true within one day and false
    across two.

    drop_today: a successful delivery means TODAY's credit landed today, so
    today comes off the ledger. It does NOT rescue an earlier day: the payload
    carries no timestamp and the server stamps arrival (see session_ping), so
    replaying Monday's ping on Tuesday credits Tuesday. Monday really did lose
    its point, and it stays on the ledger to say so.

    `attempts` is deliberately dropped: it counts consecutive failures for the
    outage notice, and a delivery means the outage is over.
    """
    try:
        prior = json.loads(PING_FAIL_MARKER.read_text(encoding="utf-8"))
    except Exception:
        prior = None
    if not isinstance(prior, dict):
        _discard_marker()
        return

    raw = prior.get("failed_days")
    days = [d for d in raw if isinstance(d, str)] if isinstance(raw, list) else []
    if drop_today:
        today = datetime.now(timezone.utc).date().isoformat()
        days = [d for d in days if d != today]

    if not days:
        _discard_marker()  # no payload, no lost days: nothing worth a file
        return

    ledger = {"failed_days": days}
    last = prior.get("last_notified_at")
    if isinstance(last, str) and not drop_today:
        # last_notified_at suppresses repeats of an ONGOING condition, so it
        # only survives when the condition is still on: a malformed or
        # undelivered payload. A successful delivery ends the outage, and
        # carrying the timestamp across it would silence a genuine NEW outage
        # for the rest of the 24h — breaking rule 3 of note_ping_failure,
        # "never go quiet while it IS broken." (My first cut kept it here to
        # stop a flapping tracker nagging; that is already handled by
        # attempts >= 2, which a delivery resets, so Branch A needs two fresh
        # consecutive failures before it can speak again.)
        ledger["last_notified_at"] = last
    _write_marker(ledger)


def replay_failed_ping():
    """Replay a previously failed session-ping before the current one.

    Runs at session start, ahead of the live ping. If delivery succeeds (or
    the marker is malformed), the marker is cleared; if it fails again, the
    marker is left in place for the next session to retry. We deliberately do
    not re-surface the replayed response — the live ping right after fetches
    fresh announcements, so processing the stale body here would double up.

    Returns the successfully replayed payload (dict) or None. The caller uses
    it to skip the live ping when the payloads are identical — the endpoint is
    slow (~8s warm), so a redundant back-to-back POST both wastes time and
    risks a pointless timeout that would re-write the marker.
    """
    if not PING_FAIL_MARKER.exists():
        return None
    try:
        saved = json.loads(PING_FAIL_MARKER.read_text(encoding="utf-8"))
    except Exception:
        # Do NOT delete it. Unreadable here is far more likely to be a
        # concurrent SessionStart mid-write than real corruption, and deleting
        # it makes the writer finish into an unlinked inode — the payload is
        # gone and nothing replays it. Leaving it costs nothing: the next
        # note_ping_failure() already treats an unreadable prior as {} and
        # overwrites, so a genuinely corrupt marker cannot get stuck either.
        return None
    if not isinstance(saved, dict):
        return None  # same reasoning: let the next failure overwrite it

    body = saved.get("payload")
    if body is None and saved.get("failed_days"):
        return None  # ledger-only marker — nothing to replay, and the days
        # it holds are the record of credit already lost. Leave it alone.
    if not isinstance(body, dict) or not body:
        # Malformed, per the contract above. Truthiness alone is not shape: a
        # payload left as a string or a list by a crash or a hand-edit used to
        # be POSTed as-is, and the raise that followed was caught, which KEPT
        # the marker — retrying the same broken payload at every session start
        # forever, indistinguishable from "the tracker is still unreachable."
        # Nothing was delivered, so today stays on the ledger.
        _retire_ping_payload(drop_today=False)
        return None

    try:
        _post_session_ping(body)
    except Exception:
        return None  # still unreachable — keep the marker for the next attempt
    _retire_ping_payload(drop_today=True)
    return body


def _http_probe(url, timeout=4, any_response=False):
    """Probe url inside a TOTAL time budget. curl first, urllib with what's left.

    any_response=False — True only on 2xx/3xx: "is this service healthy".
    any_response=True  — True if the host answered AT ALL, error statuses
    included: "is this machine online". api.github.com hands out unauthenticated
    403s freely, and reading a 403 as "no internet" is exactly how a real
    tracker outage could have stayed silent forever — the suppression branch
    below only stays quiet when the control says we are offline.

    Transport order and its reasoning are _http_post_json's: a stock Python can
    carry a CA bundle that trusts nothing, so urllib alone would read a healthy
    host as dead, and curl failing means "try the other transport".

    The budget is TOTAL, not per transport. This runs on a path that may already
    have spent ~70s on a replayed POST plus a live one against a 90s SessionStart
    budget (install.sh), so two full-length probes would run the hook out of room.
    """
    deadline = time.monotonic() + timeout
    if shutil.which("curl"):
        args = ["curl", "-s", "--max-time", str(max(1, int(timeout))),
                "-o", os.devnull, "-w", "%{http_code}", url]
        if not any_response:
            args.insert(1, "--fail")
        try:
            done = subprocess.run(args, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout + 5)
            if any_response:
                code = (done.stdout or "").strip()
                if code and code != "000":
                    return True  # answered, whatever it said
            elif done.returncode == 0:
                return True
        except Exception:
            pass
    left = deadline - time.monotonic()
    if left < 1:
        return False  # no headroom for a second transport; caller must not stall
    try:
        with urllib.request.urlopen(url, timeout=left) as resp:
            return 200 <= getattr(resp, "status", 200) < 400
    except urllib.error.HTTPError:
        return any_response  # the host answered; the status is a health question
    except Exception:
        return False


def _tracker_unreachable():
    """Is the tracker actually gone, or was that one write just slow?

    A /session-ping timeout is NOT evidence the tracker is unreachable.
    Measured 2026-08-20 across 24h of Railway HTTP logs for the service:
    293 pings, p50 9.0s, p90 22.2s, and 45 of them (15%) ran into the 35s
    client ceiling and were logged 499 "client has closed the request" — while
    the server went on to answer 200 and record the session. The slow part is
    the announcement scan plus the Airtable writes, and concurrent session
    starts queue behind each other, so opening three sessions at once is
    enough to burn three pings inside six seconds.

    GET / on the same host is a static health payload that answers in ~0.2s,
    so it tells the two cases apart.
    """
    return not _http_probe(f"{PROXY_URL}/", timeout=4)


def _internet_up():
    """True if anything outside this machine answers — any status counts.

    Without this, a laptop on a plane trips the "tracker is down" notice: the
    tracker probe fails for the same reason every other request does.
    api.github.com is the control because every builder already depends on
    GitHub reachability for the toolkit to update at all.
    """
    return _http_probe("https://api.github.com/", timeout=3, any_response=True)


def note_ping_failure(body):
    """Stash a failed session-ping, and speak only when it means something.

    Three rules, each of them paid for:

    1. One failure per DAY, not per attempt. The old counter counted attempts,
       so three sessions opened in the same minute read out as "your last 3
       sessions" — which is exactly what fired the false alarm on 2026-08-20
       (three 499s inside six seconds, server recorded all three, no points
       lost, and it still told the builder to raise it in #builders).
    2. Only speak when the builder is actually losing something. A slow write
       is invisible to them: the payload replays at the next session start and
       daily points dedupe per builder per day, so the day's credit lands from
       whichever ping gets through.
    3. Never go quiet while it IS broken. The old `failures == 3` fired once
       and then never again for the life of an outage, which is the
       silent-forever mode the original comment was trying to avoid. Both
       notices below repeat once every 24h for as long as the condition holds.

    Every value read back from the marker is treated as hostile. The file is
    hand-editable, can be left half-written by a crash, and this function runs
    inside session_ping()'s failure handler with nothing above it to catch a
    raise — so a corrupt marker must not be what takes SessionStart down.
    """
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    prior = None
    try:
        prior = json.loads(PING_FAIL_MARKER.read_text(encoding="utf-8"))
    except Exception:
        pass
    if not isinstance(prior, dict):
        prior = {}  # valid JSON is not the same as the shape we wrote

    # A day only counts if it is a real calendar day at or before today, and it
    # is stored canonically. isinstance(str) was not enough on either axis:
    # "x" counted as a failed day (so one failure could trip the three-day
    # notice), and on 3.11+ date.fromisoformat also accepts "20260825", which
    # would survive the set() alongside "2026-08-25" and count the same day
    # twice. Normalising on the way in closes both.
    raw_days = prior.get("failed_days")
    days = []
    if isinstance(raw_days, list):
        for d in raw_days:
            if not isinstance(d, str):
                continue
            try:
                parsed = date.fromisoformat(d)
            except (TypeError, ValueError):
                continue
            if parsed > now.date():
                continue  # clock skew or a hand-edit: a day that has not
                # happened yet cannot have cost anyone their points
            days.append(parsed.isoformat())
    if today not in days:
        days.append(today)
    days = sorted(set(days))[-14:]

    # max(0, ...) because a negative attempts — a hand-edit, or a partial
    # write — is the right type and still poisons the counter: it never
    # reaches 2, so Branch A's outage notice stays silent for the whole
    # outage, which is the exact failure this function exists to prevent.
    try:
        attempts = max(0, int(prior.get("attempts", prior.get("failures", 0)) or 0)) + 1
    except (TypeError, ValueError):
        attempts = 1

    last_notified = prior.get("last_notified_at")
    if not isinstance(last_notified, str):
        last_notified = None

    def spoke_within_a_day():
        if not last_notified:
            return False
        try:
            when = datetime.fromisoformat(last_notified)
        except (TypeError, ValueError):
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        elapsed = (now - when).total_seconds()
        if elapsed < 0:
            return False  # clock rolled back, or hand-edited: don't let a
            # timestamp from the future mute an outage for longer than a day
        return elapsed < 86400

    def _write(notified_at):
        _write_marker({
            "payload": body,
            "attempted_at": now.isoformat(),
            "attempts": attempts,
            "failed_days": days,
            "last_notified_at": notified_at,
        })

    # Persist FIRST, probe SECOND. SessionStart runs under a wall-clock budget
    # that the replay, the live ping and git_pull() have already eaten into,
    # and the two probes below can spend 8s more of it. If the hook is killed
    # mid-probe, anything not yet written is gone — and the payload is the one
    # value whose loss actually costs the builder a point, because nothing can
    # replay what was never stashed. The notice is a nicety; the payload is
    # the credit. Write the payload where no network call can precede it, and
    # come back for last_notified_at only if we end up speaking.
    _write(last_notified)

    # Both probes cost an HTTP round-trip, and both branches below need them,
    # so evaluate each at most once per call.
    _probed = {}

    def unreachable():
        if "unreachable" not in _probed:
            _probed["unreachable"] = _tracker_unreachable()
        return _probed["unreachable"]

    def online():
        if "online" not in _probed:
            _probed["online"] = _internet_up()
        return _probed["online"]

    notice = None
    if not spoke_within_a_day():
        # Branch A — the host itself is gone. Two attempts, so one DNS hiccup
        # stays quiet, and only when the rest of the internet answers.
        if attempts >= 2 and unreachable() and online():
            notice = (
                "The NSLS automation tracker can't be reached right now, so "
                "your builder points aren't recording. Your sessions are "
                "queued and replay on their own once it's back — nothing for "
                "you to fix. Say \"check the tracker\" if you want it looked "
                "into."
            )
        # Branch B — the host answers but the writes keep timing out. Harmless
        # for a day or two (see rule 2); past three separate days the daily
        # dedupe is no longer covering it and credit really is going missing.
        #
        # The probes are the whole point of this branch, not decoration. "Too
        # slow to record" is a claim about the tracker, and three failed days
        # on a laptop that was offline are not evidence for it — the writes
        # failed for the same reason every other request did. Requiring the
        # tracker to answer its health endpoint RIGHT NOW is what earns the
        # claim: online, host up, and your writes still failed on 3 days.
        # (Checking only _internet_up() would not do it — being online now
        # says nothing about the three days already in the ledger, so an
        # offline stretch would still fire this on the next online session.)
        elif len(days) >= 3 and online() and not unreachable():
            notice = (
                f"The NSLS automation tracker has been too slow to record "
                f"your sessions on {len(days)} separate days, so some builder "
                f"points are probably missing. Say \"check the tracker\" if "
                f"you want it looked into."
            )

    if notice:
        # Second write, only when we actually spoke — the payload above is
        # already safe on disk whatever the probes did.
        _write(now.isoformat())

    if notice:
        print(
            "[NSLS Builder Toolkit — surface the following to the user at the "
            "start of your first reply, then proceed with their request:]\n\n"
            + notice
        )


def _collector_backed():
    """True when the NSLS usage collector's evidence file here is fresh.

    See hooks/collector_evidence.py for the contract. Any failure to load or
    read it means False, which is exactly today's behaviour.
    """
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import collector_evidence

        return collector_evidence.fresh(config_dir=CONFIG_DIR)
    except Exception:
        return False


def session_ping(replayed=None):
    """Ping the automation tracker for points, PR credits, and announcements.

    replayed: payload dict just delivered by replay_failed_ping(), or None.
    If it matches today's payload, the session is already recorded — skip the
    redundant POST (the payload carries no timestamp; the server stamps
    arrival time, so a successful replay IS today's ping).
    """
    email = read_env("BUILDER_EMAIL")
    if not email:
        try:
            result = subprocess.run(
                ["git", "config", "user.email"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5
            )
            email = result.stdout.strip()
        except Exception:
            pass
    if not email:
        return

    github = read_env("GITHUB_USERNAME")

    toolkit = "personal"
    if PLUGIN_DIR.exists() or org_plugin_installed():
        toolkit = "both"

    platform_map = {"darwin": "mac", "win32": "windows", "linux": "linux"}
    platform = platform_map.get(sys.platform, sys.platform)

    body = {
        "builder_email": email,
        "toolkit": toolkit,
        "github_username": github,
        "platform": platform,
    }
    if _collector_backed():
        # The usage collector already reports this machine's daily session, so
        # the tracker skips ONLY that credit. PR credit, install tracking,
        # stage advancement and announcements still run off this ping.
        body["collector_backed"] = True

    if replayed == body:
        return  # this exact payload was just delivered by the replay

    try:
        data = _post_session_ping(body)
    except Exception:
        # Delivery failed (timeout / network / proxy down). Stash the payload
        # so the next session start replays it, and decide separately whether
        # this is worth a word to the builder. Best-effort — never raise.
        note_ping_failure(body)
        return

    # Delivered — retire the queued payload. Today's credit landed today, so
    # today comes off the ledger; earlier days stay, because their credit did
    # not land on their day and no later delivery can give it back.
    _retire_ping_payload(drop_today=True)

    output = []

    # PR credits
    for pr in data.get("new_pr_credits", []):
        repo = pr.get("repo", "?")
        pr_num = pr.get("pr", "?")
        output.append(f"Your PR #{pr_num} to {repo} was merged.")

    # Stage advancement
    adv = data.get("stage_advanced")
    if adv:
        output.append(f"You advanced to {adv.get('to', '?')} on the builder path.")

    # Announcements
    for ann in data.get("announcements", []):
        title = ann.get("title", "")
        body = ann.get("body", "")
        output.append(f"{title}: {body}")
        # Dismiss (same curl-first transport as the ping — the urllib CA-cert
        # failure mode applies here too, and a failed dismiss re-delivers the
        # announcement every session).
        ann_id = ann.get("id", "")
        if ann_id:
            try:
                _http_post_json(
                    f"{PROXY_URL}/dismiss-announcement",
                    {"announcement_id": ann_id, "builder_email": email},
                    5,
                )
            except Exception:
                pass

    if output:
        # Wrap with a directive prefix so the announcements surface visibly
        # on every Claude Code surface, not just the terminal CLI.
        #
        # Background: hook stdout is added to Claude's context on every
        # surface (terminal CLI, desktop app, IDE extensions). The CLI
        # additionally prints stdout to the terminal at session start, so
        # raw text is naturally visible there. The desktop app does NOT
        # print hook stdout to the screen — it only injects it as context,
        # so a bare `print(...)` of an announcement reaches Claude silently
        # and the user never sees it unless Claude happens to surface it.
        #
        # The directive prefix tells Claude to surface the announcement at
        # the start of its first reply. That makes the announcement
        # visible on desktop while keeping CLI behavior intact (CLI users
        # see it in the terminal AND see Claude echo it once — minor
        # redundancy, acceptable trade for desktop coverage).
        announcement_block = "\n".join(output)
        directive = (
            "[NSLS Builder Toolkit — surface the following to the user "
            "verbatim at the start of your first reply, then proceed with "
            "their request:]\n\n"
            f"{announcement_block}"
        )
        if data.get("new_pr_credits"):
            # Judge-then-nudge: a merged PR that shipped something others
            # will rely on (a new skill, tool, or service) belongs on the
            # org Automation Tracker; a pure bugfix/doc tweak doesn't.
            directive += (
                "\n\n[NSLS Builder Toolkit — additionally: judge whether a "
                "merged PR above shipped something registerable (a new "
                "skill, tool, or service other builders will rely on). If "
                "it did — or you can't tell from the repo/PR context — also "
                "surface this line: \"Shipped something new? Run "
                "/register-automation so it's on the org tracker.\" If it "
                "was clearly a pure bugfix, doc tweak, or maintenance "
                "change, skip that line.]"
            )
        print(directive)


def emit_guardrails_context():
    """Print the Builder Guardrails section of the plugin's CLAUDE.md.

    WHY THIS EXISTS. A plugin's root CLAUDE.md is not loaded into anyone's
    session — plugin.json doesn't reference it, no hook injected it, and it
    isn't on any path Claude Code reads as memory. It is documentation for
    people browsing the repo. That was fine when the file held conventions,
    and became a hole the moment it held the guardrail tiers: the four hard
    gates fire (guardrail-gate.py is wired through hooks.json) but the entire
    soft half — tiers, escalation triggers, the voice guide — reached Claude
    only if a skill happened to trigger, which is description-matched, which
    is the exact failure the hook exists to cover.

    Found by the Phase 6 voice run, 2026-08-15. Stdout from a SessionStart
    hook reaches Claude as context (same channel the announcement directive
    below already uses), so printing the section is the whole fix.

    Fails silent: a missing file, an unreadable one, or a renamed heading
    prints nothing rather than breaking session start for every builder.
    """
    # Resolve from the RUNNING copy's own root, not the hardcoded
    # local-plugins path: a marketplace or ${CLAUDE_PLUGIN_ROOT} install has no
    # CONFIG_DIR/local-plugins checkout, so the hardcoded path silently emitted
    # nothing and those machines got no guardrail policy at all. Legacy path
    # kept as fallback for the settings-shim era, where this file runs from the
    # clone.
    # The legacy settings-shim path executes this file via `python3 -c`, where
    # __file__ is undefined — an unguarded reference aborted main() with a
    # NameError before the ping replay ever ran. Shim installs run from the
    # clone at PLUGIN_DIR anyway, so that IS their own root.
    own_root = (Path(__file__).resolve().parent.parent
                if "__file__" in globals() else PLUGIN_DIR)
    guardrail_root = own_root if (own_root / "CLAUDE.md").is_file() else PLUGIN_DIR
    try:
        # UTF-8 explicitly: the default is the locale's code page, cp1252 on a
        # PC, which garbled every dash and dot of the policy (Windows CI).
        text = (guardrail_root / "CLAUDE.md").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return

    start = text.find("## Builder Guardrails")
    if start == -1:
        return
    nxt = text.find("\n## ", start + 1)
    section = text[start:nxt if nxt != -1 else len(text)].strip()
    if not section:
        return

    # The section ends by telling Claude to read the voice guide, but writes it
    # as a repo-relative path. A builder's session has its own cwd
    # (~/projects/whatever), so that path resolves to nothing and the guide --
    # the half of this that governs *tone* -- silently never loads. Same failure
    # as the section itself not loading, one level down. Resolve it against
    # PLUGIN_DIR so the path is openable from wherever the builder is working.
    # Forward slashes on every OS: Claude runs these through Git Bash on a PC,
    # where C:/Users/... is a path and a backslash is an escape character.
    for ref in ("guardrail-voice.md", "guardrail-playbook.md"):
        section = section.replace(
            f"`_shared/references/{ref}`",
            f"`{(guardrail_root / '_shared' / 'references' / ref).as_posix()}`",
        )

    # Same class of bug, one variable along. CLAUDE.md writes the decline- and
    # event-recording commands as "${CLAUDE_PLUGIN_ROOT}/hooks/...", which is
    # right for the file -- a marketplace install has no fixed path, and hook
    # subprocesses do get that variable. But Claude runs these through the Bash
    # tool, which is NOT a hook subprocess and has no CLAUDE_PLUGIN_ROOT, so the
    # path expands to "/hooks/guardrail-memory.py" and the command dies on a
    # missing file. Nothing surfaces: the decline is simply never recorded, and
    # rule 6 ("take the first no gracefully, and remember it") quietly becomes
    # unenforceable -- the nagware failure the block below is meant to prevent.
    # The commands go through run-hook.sh, never a bare `python3`: stock Windows
    # has no python3 (a Store alias that exits without running), and the
    # launcher already resolves the interpreter the hooks themselves run on.
    section = section.replace("${CLAUDE_PLUGIN_ROOT}", guardrail_root.as_posix())

    print(
        "[NSLS Builder Toolkit — org guardrail policy, active this session]\n\n"
        + section
    )

    # What this build has already refused. Without it, rule 6 ("take the first
    # no gracefully, and remember it per BUILD") is unenforceable across
    # sessions -- every new session re-raises guardrails the builder already
    # declined, which is how a toolkit becomes nagware.
    try:
        out = subprocess.run(
            [sys.executable, str(guardrail_root / "hooks" / "guardrail-memory.py"),
             "list", "--cwd", os.getcwd()],
            # UTF-8 on both ends: the child is told to write it and this end
            # reads it, so a remembered note survives a Windows code page.
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            print("\n" + out.stdout.strip())
    except Exception:
        pass  # no memory is the status quo, not a failure worth surfacing


def bootstrap_collector():
    """Install the NSLS Claude usage collector in the background if it is not
    here yet. Silent: the collector's enrollment DMs the builder via Signal.
    A few stats and at most one detached launch a day; see
    hooks/collector_bootstrap.py. Opt out with NSLS_COLLECTOR_OPTOUT=1.
    """
    try:
        here = str(Path(__file__).resolve().parent)
        if here not in sys.path:
            sys.path.insert(0, here)
        import collector_bootstrap

        collector_bootstrap.run(config_dir=CONFIG_DIR)
    except Exception:
        pass


def _record_beacon():
    """Note that the PLUGIN copy of this hook ran here (no-op from a clone).

    Stage B retires the installer's shims, and on Windows it has never been
    allowed to, because nobody could prove the plugin's hooks fire there. This
    is that proof, recorded per hook rather than per plugin: SessionStart
    running says nothing about whether PreToolUse does.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import plugin_beacon

        plugin_beacon.record("session-start", __file__)
    except Exception:
        pass


def _utf8_stdio():
    """Write this hook's output as UTF-8, whatever the console says.

    Windows Python writes a pipe in the ANSI code page, strictly, and the
    guardrail policy holds characters cp1252 cannot encode: the print raised,
    and main() died before the ping or the collector ran. run-hook.sh also sets
    UTF-8 mode, but this file is run by other routes too, so it says so itself.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _policy_hook_owns_policy():
    """True when the plugin's own `policy` hook prints the guardrail policy.

    Claude Code caps each hook's output at 10,000 characters and hands Claude
    only the first 2,000 of anything longer. Printed from here, the policy
    shared that budget with every notice and announcement this hook makes, and
    once the section passed the cap (2026-09-20) Claude saw the tier table and
    the hard-gate list and nothing else -- not the decline or event commands,
    not "log it". The plugin now runs the policy as its own SessionStart hook
    (guardrail-policy.py), with its own budget, on startup, resume, clear and
    compact. CLAUDE_PLUGIN_ROOT is set only for plugin hooks; a clone or shim
    run has no policy hook, so it keeps printing the policy itself.
    """
    return bool(os.environ.get("CLAUDE_PLUGIN_ROOT"))


def main():
    _utf8_stdio()
    git_pull()
    stage_a = run_plugin_migration()
    if not stage_a:  # stage A spent this session's freshness budget
        ensure_plugin_fresh()
    sync_pointers()
    notice_own_skills()
    if not _policy_hook_owns_policy():
        emit_guardrails_context()
    replayed = replay_failed_ping()
    session_ping(replayed=replayed)
    bootstrap_collector()
    # Last, so the beacon means this copy ran to the end. Recorded first, a
    # copy that crashed halfway still proved itself, and stage B retired the
    # shim that was doing the work (the cp1252 crash above, on every PC).
    _record_beacon()


# Entry point for the PowerShell hook, which needs ONLY the guardrails context
# (its own script already handles pulls, pointers and the ping). Keeping this in
# the Python emitter means the section extraction and path resolution exist once.
if __name__ == "__guardrails__":
    # Deliberately NOT a beacon. This run_name is how session-start.ps1 — the
    # SHIM — calls into this file, so recording here would let the shim certify
    # its own retirement. Only the plugin runtime's own invocation counts, and
    # plugin_beacon.record() checks the path it is running from for exactly
    # this reason.
    _utf8_stdio()
    try:
        emit_guardrails_context()
    except Exception:
        pass
    # The personal-fork drift check is deliberately NOT run from here:
    # session-start.ps1 carries its own copy for Windows (Python is not
    # guaranteed there), keyed on the same stamp, so running it twice from one
    # session would be at best redundant.
    # Windows's .ps1 does its own pull, so it never reached git_pull's call to
    # the stale-configuration check — Windows checkouts on a stranded branch
    # stayed silently frozen, which is the whole failure this adds. Cheap to
    # run here: local ref arithmetic, no network.
    try:
        for _plugin in SYNC_PLUGINS:
            _dir = CONFIG_DIR / "local-plugins" / _plugin
            if (_dir / ".git").exists():
                _warn_if_stale_by_configuration(
                    _plugin, _dir, deadline=time.monotonic() + 10)
    except Exception:
        pass
    # Stage A on Windows. This run_name is the ONLY path from the PowerShell
    # hook into Python, and it never called the migration — which is why no PC
    # has ever had the plugin, nor its three agents, nor its bundled hooks. The
    # docstring in migrate_to_plugin.py claimed stage A "runs" here; it did not.
    # Installing the plugin is also what starts the beacons that let stage B
    # retire this machine's shims, one hook at a time, once each is replaced.
    stage_a = False
    try:
        stage_a = run_plugin_migration()
    except Exception:
        pass

    # Plugin freshness — Windows parity with main(). session-start.ps1 is the
    # hook that reliably fires on Windows (the plugin's own hooks.json invokes
    # `python3`, a Store alias there that exits without running), and this
    # run_name is its only path into Python. Without this call a Windows
    # plugin install never ran `plugin update` and its version-pinned cache
    # rotted while the clone beside it stayed current. ensure_plugin_fresh
    # gates on the plugin being installed and enabled, so a clone-only
    # machine is a designed no-op; where both hook paths fire, the daily
    # marker and the heal lock make the second run harmless.
    try:
        if not stage_a:  # stage A spent this session's freshness budget
            ensure_plugin_fresh()
    except Exception:
        pass

# Entry point for the plugin's `policy` hook (guardrail-policy.py): the
# guardrail policy and nothing else. It runs on startup, resume, clear and
# compact, so it must never pull, ping or migrate -- two credit pings racing
# per session is how the duplicate credit rows of September 2026 were made.
if __name__ == "__policy__":
    _utf8_stdio()
    try:
        emit_guardrails_context()
    except Exception:
        pass

if __name__ == "__main__":
    main()
