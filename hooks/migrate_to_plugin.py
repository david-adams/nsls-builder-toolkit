#!/usr/bin/env python3
"""
migrate_to_plugin.py — self-migration from the shim install to a real plugin.

Executed by session-start.py on every session start (from the freshly pulled
clone, so fixes to this file take effect one session after they land on main).
Two stages, at most one per session:

Stage A — plugin not installed yet:
    Register this repo as a private marketplace and install the plugin.
    Nothing else changes: the running session keeps using the shims, and the
    plugin loads at the NEXT session start. Anyone with git access to the repo
    can install; the marketplace is not public.

Stage B — plugin installed, shims still present (mac/linux only):
    Remove exactly the shims the installer created, now that the plugin
    provides the same components:
      * pointer stubs in ~/.claude/skills whose SKILL.md points at
        local-plugins/nsls-builder-toolkit/skills/ — personal-toolkit stubs
        and user-authored skills are never touched (their SKILL.md lacks that
        marker; see the /skills/ discriminator below),
      * settings.json hook entries whose command references
        nsls-builder-toolkit/hooks/ (session-start + skill-event),
      * the user-scope `signal` MCP registration if it points into this repo
        (the plugin registers signal at plugin scope).

Safety properties:
  * fail-open — every step is wrapped; any failure leaves the machine on the
    still-working shim path and retries next session
  * idempotent — each stage checks live state, never history
  * serialized — a lockfile keeps the settings-shim copy and the plugin-hook
    copy of session-start.py from migrating concurrently during the one
    overlap session
  * announced — every state change prints a visible line (silent failure is
    the recurring bug in this pipeline)
  * reversible — settings.json is backed up to settings.json.pre-plugin-migration
    before its first edit
  * escape hatch — set NSLS_NO_PLUGIN_MIGRATION=1 to freeze migration
  * Windows — NEITHER stage runs today, and this docstring used to claim
    stage A did. Nothing calls this module on Windows: session-start.ps1 is the
    hook that fires there, and its only entry into Python is
    runpy.run_path(..., run_name="__guardrails__"), which executes that block
    alone and never main(). That, not a shortage of installs, is why no PC has
    ever had the plugin — so no PC has the three agents, and stage B has never
    had anything to be deferred from. Wiring stage A into the Windows path is
    the Windows-parity change; stage B stays gated on per-hook beacon evidence
    that the plugin's own hooks actually fire on that machine.
"""

import json
import re
import os
import shutil
import subprocess
import sys
import time
import tempfile
from pathlib import Path

def _config_dir() -> Path:
    """The Claude config directory, resolved without ever raising.

    `Path.home()` raises when neither HOME nor a password-database entry
    resolves, and at module scope that stops session start dead.
    """
    raw = os.environ.get("CLAUDE_CONFIG_DIR")
    if raw:
        return Path(raw)
    try:
        return Path.home() / ".claude"
    except Exception:
        return Path(tempfile.gettempdir()) / "nsls-claude-config"


_CONFIG_DIR = _config_dir()
_PLUGIN_DIR = _CONFIG_DIR / "local-plugins" / "nsls-builder-toolkit"
_SKILLS_DIR = _CONFIG_DIR / "skills"
_SETTINGS = _CONFIG_DIR / "settings.json"

_MARKETPLACE = "nsls-toolkit"
_PLUGIN_ID = f"nsls-builder-toolkit@{_MARKETPLACE}"
_REPO_URL = os.environ.get(
    "NSLS_TOOLKIT_REPO", "https://github.com/thensls/nsls-builder-toolkit.git"
)
# Matches the shim hook commands install.sh/.ps1 wrote into settings.json.
_HOOK_MARKER = "nsls-builder-toolkit/hooks/"


def _norm(text) -> str:
    """Compare paths the way BOTH installers write them.

    install.sh writes POSIX separators. install.ps1 builds every path with
    Join-Path, so a Windows shim command reads
    `...\\nsls-builder-toolkit\\hooks\\session-start.ps1`. A marker spelled with
    forward slashes matched none of it: _shims_present() answered False,
    _remove_settings_hooks() removed nothing, and stage B therefore concluded
    the machine was clean and wrote the PERMANENT done marker — leaving every
    PC running the shim AND the plugin copy of every hook, for good, behind a
    marker saying the migration had finished.
    """
    return str(text).replace("\\", "/")


def _is_shim_command(command) -> bool:
    return _HOOK_MARKER in _norm(command)


def _iter_hook_commands(settings):
    """Every hook entry's command, and nothing else.

    _shims_present() used to substring-search the whole settings.json as raw
    text, so any mention of this repo anywhere in the file counted — a
    leftover `permissions.allow` rule most of all. After an otherwise clean
    stage B that left the machine reading as dirty permanently: `clean` never
    became True, the done marker was never written, and stage B re-ran every
    session for the life of the install.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            entries = group.get("hooks", []) if isinstance(group, dict) else []
            if not isinstance(entries, list):
                continue
            for h in entries:
                if isinstance(h, dict):
                    yield str(h.get("command", ""))


# ...and which shim belongs to which hook. Stage B used to strip every entry
# carrying the marker in one go, which is only safe if the plugin has replaced
# ALL of them. It has not, per hook and per machine: on Windows nothing had
# replaced any of them, and "SessionStart fired" is not evidence that PreToolUse
# does. Retiring a shim is now decided one hook at a time, against that hook's
# own beacon. The cost of getting this wrong is not abstract — skill-event is
# the credit logger, and losing it silently costs builders the record of their
# work, which is the thing NSLS measures them by.
_HOOK_SCRIPTS = {
    "session-start": ("session-start.py", "session-start.ps1"),
    "skill-event": ("skill-event.sh", "skill-event.ps1"),
    "guardrail-gate": ("guardrail-gate.py",),
}


def _beacon_fired(hook: str) -> bool:
    """Has the PLUGIN copy of this hook been seen running on this machine?"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import plugin_beacon

        return plugin_beacon.fired(hook)
    except Exception:
        return False  # no evidence is not evidence of parity


def _proven_hooks():
    return {h for h in _HOOK_SCRIPTS if _beacon_fired(h)}


def _hook_for_command(command: str):
    normalised = _norm(command)
    for hook, scripts in _HOOK_SCRIPTS.items():
        if any(s in normalised for s in scripts):
            return hook
    return None
# What counts as an org stub is decided by session-start.py's is_own_pointer,
# handed over when it runs this file: the folder's SKILL.md must BE a pointer
# the toolkit writes for that skill, not merely mention a toolkit path. The old
# test here was a substring match, and stage B deleted every folder that
# matched, so a builder's own skill that credited or linked a toolkit skill was
# deleted outright with no copy (PC test, 2026-10-03; on Macs since 12 Aug).
# Run any other way, nothing is treated as a stub: deleting less is the safe
# direction, and detection then reports "not clean" so stage B simply retries.
_IS_OWN_POINTER = globals().get("_NSLS_IS_OWN_POINTER")
_ORG_PLUGIN = "nsls-builder-toolkit"
# Files an OS drops into any folder it has shown. They don't make a pointer
# folder the builder's.
_OS_LITTER = {".DS_Store", "Thumbs.db", "desktop.ini"}
# Removed pointers are moved here, never deleted outright.
_REMOVED_DIR = _CONFIG_DIR / ".nsls-removed-skills"
_LOCK = _CONFIG_DIR / ".nsls-plugin-migration.lock"
_LOCK_STALE_SECS = 300
# Written once stage B has VERIFIED the machine is clean (no shim hooks, no
# org stubs, no stale user-scope signal). Stage B re-runs every session until
# then, so a partial cleanup (one stub failing to delete, a timed-out mcp
# remove) retries instead of being orphaned forever.
_DONE = _CONFIG_DIR / ".nsls-plugin-migration-done"
# The done-marker is permanent: once written, stage B never runs again on that
# machine. That was fine while stage B only had to remove what the installer
# wrote before the marker existed — and became a trap the moment a LATER
# installer run added something new. #168 (2026-09-07) registered the guardrail
# gate into settings.json; any machine that re-ran install.sh after that date
# with the marker already written keeps that entry for good, and once the
# plugin registers the gate too, both fire.
#
# Versioning the marker gives stage B exactly one more pass per schema bump.
# Every step it runs is idempotent and announces only when something changed,
# so a machine that is already clean sees nothing at all.
_MIGRATION_SCHEMA = 2


def _done_schema():
    """Schema of this machine's done-marker, or None if it has never run.

    Pre-versioning markers hold the literal text "migrated" and are schema 1.
    """
    try:
        raw = _DONE.read_text(encoding="utf-8-sig").strip()
    except Exception:
        return None
    try:
        return int(json.loads(raw).get("schema", 1))
    except Exception:
        return 1 if raw else None


def _stage_b_reason():
    """Why stage B should run this session: "full", "reappeared", or None.

    The done-marker is permanent, and both installers deliberately re-create any
    missing settings.json shim. So a builder who re-runs the installer after
    migrating gets every shim back, for good, silently duplicating whatever the
    plugin already registers — which is the permanent-overlap failure the marker
    versioning was meant to end, arriving by a different door. Live state
    therefore outranks the marker: if a shim or an org stub is present, stage B
    has work regardless of what the marker says.

    "reappeared" runs the cheap half only. The signal MCP check is a CLI call
    against a 15 s timeout, and it was settled the first time through; paying
    for it every session to re-confirm would be a real cost on every machine.
    """
    schema = _done_schema()
    if schema is None or schema < _MIGRATION_SCHEMA:
        return "full"
    if _shims_present():
        return "reappeared"
    # Without session-start.py's exact-pointer check (an older plugin copy
    # running this newer migration from the clone) no stub can be retired, and
    # _org_stubs_exist() says "not clean" by design. Counting that here sent
    # every already-migrated machine through stage B, CLI call included, every
    # session until its plugin caught up.
    if _IS_OWN_POINTER is not None and _org_stubs_exist():
        return "reappeared"
    return None


def _read_json(path):
    # utf-8-sig: settings.json on machines installed via PowerShell can carry
    # a UTF-8 BOM, which plain utf-8 json.loads rejects — that failure mode
    # would wedge the machine in the shim/plugin overlap state permanently.
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _find_claude():
    """argv prefix that runs the claude CLI, or None (retry next session).

    Prefers the finder session-start.py injects as _NSLS_FIND_CLAUDE, which
    knows every Windows location: the npm shim, the profile installs, and the
    desktop app's bundled CLI including where a Microsoft Store install really
    keeps it. This file's own copy knew none of them, so on a PC stage A found
    nothing and the plugin never installed, silently. The local fallback below
    only serves a direct run of this script.
    """
    injected = globals().get("_NSLS_FIND_CLAUDE")
    if callable(injected):
        try:
            found = injected()
        except Exception:
            found = None
        if found:
            return list(found)
    found = shutil.which("claude")
    if found:
        return _cli_argv(Path(found))
    for candidate in (
        _CONFIG_DIR / "local" / "claude",
        Path("/usr/local/bin/claude"),
        Path("/opt/homebrew/bin/claude"),
    ):
        if candidate.exists():
            return [str(candidate)]
    found = _windows_cli()
    return _cli_argv(found) if found else None


def _cli_argv(path):
    if path.suffix.lower() == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)]
    return [str(path)]


def _windows_cli():
    """The claude CLI in the places a Windows hook can't see on PATH, or None.

    This file's own fallback, for when the running session-start.py hands over
    no finder or one that comes back empty. That is exactly the state of a PC
    stuck on an old plugin copy: its finder predates the Store app's
    <version>\\<hash>\\claude.exe layout, so it finds nothing, and the clone's
    copy of this file is the only new code that machine runs (PC Test Round 5).
    Same places as session-start.py's _find_claude: the npm shim, the profile
    installs, then the desktop app's bundled CLI in %APPDATA% and the Store
    package's LocalCache, both layouts, highest version first.
    """
    env = os.environ
    simple = []
    if env.get("APPDATA"):
        simple += [Path(env["APPDATA"]) / "npm" / "claude.cmd",
                   Path(env["APPDATA"]) / "npm" / "claude.ps1"]
    if env.get("USERPROFILE"):
        simple += [Path(env["USERPROFILE"]) / ".local" / "bin" / "claude.exe",
                   Path(env["USERPROFILE"]) / ".claude" / "bin" / "claude.exe"]
    if env.get("LOCALAPPDATA"):
        simple.append(Path(env["LOCALAPPDATA"]) / "Programs" / "claude" / "claude.exe")
    for c in simple:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    roots = []
    if env.get("APPDATA"):
        roots.append(Path(env["APPDATA"]) / "Claude")
    if env.get("LOCALAPPDATA"):
        try:
            roots += sorted((Path(env["LOCALAPPDATA"]) / "Packages").glob(
                "Claude_*/LocalCache/Roaming/Claude"))
        except OSError:
            pass
    for sub in ("claude-code", "claude-code-vm"):
        best = None
        for root in roots:
            folder = root / sub
            for pattern in ("*/claude.exe", "*/*/claude.exe"):
                try:
                    hits = list(folder.glob(pattern))
                except OSError:
                    continue
                for exe in hits:
                    try:
                        if not exe.is_file():
                            continue
                        vdir = exe.parent if exe.parent.parent == folder else exe.parent.parent
                        key = (_version_tuple(vdir.name), exe.stat().st_mtime)
                    except OSError:
                        continue
                    if best is None or key > best[0]:
                        best = (key, exe)
        if best:
            return best[1]
    return None


def _version_tuple(text):
    """Numeric parts of a version, ignoring any suffix: 3.17.3-beta is (3, 17, 3)."""
    parts = []
    for piece in str(text).strip().split("."):
        m = re.match(r"\d+", piece)
        if not m:
            break
        parts.append(int(m.group()))
    return tuple(parts) or (0,)


# Cumulative wall-clock ceiling for this whole migration run.
#
# The per-call timeouts below bound each CLI call individually but nothing
# bounded their SUM: stage A alone is marketplace list (30) + add (60) +
# install (60) = 150s worst case, inside a SessionStart hook whose entire
# budget is 90s (install.sh). Blowing it doesn't just fail the migration — the
# hook is killed, so sync_pointers and the session ping never run either, and
# the builder silently stops getting pointer updates and credit.
#
# session-start.py injects `_NSLS_MIGRATION_DEADLINE` (a time.monotonic()
# value) into the exec globals with the slice it can spare. The fallback
# applies when the script is run directly.
#
# Running out of budget is not a failure mode here: every stage is idempotent
# and retries next session, so a partial run just makes progress and stops.
_DEADLINE = globals().get("_NSLS_MIGRATION_DEADLINE") or (time.monotonic() + 35)


def _budget_left():
    return _DEADLINE - time.monotonic()


def _claude(args, timeout):
    """Run a claude CLI subcommand. Returns (ok, stdout+stderr).

    The requested timeout is clamped to whatever remains of the run's
    cumulative budget, so no sequence of calls can overrun the hook.
    """
    claude = _find_claude()
    if not claude:
        return False, "claude CLI not found"
    remaining = _budget_left()
    if remaining <= 1:
        # Don't start work we can't finish; next session picks up here.
        return False, _BUDGET_EXHAUSTED
    effective = min(timeout, remaining)
    try:
        result = subprocess.run(
            [*claude, *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=effective,
        )
        return result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired:
        # If the run's budget, not the call's own limit, cut it short, this is
        # the ordinary slow first session: stage A resumes next session. Only a
        # call that outlived its OWN limit is a real hang worth reporting.
        if effective < timeout:
            return False, _BUDGET_EXHAUSTED
        return False, _TIMED_OUT
    except Exception as e:
        return False, str(e)


def _plugin_installed():
    try:
        registry = _read_json(_CONFIG_DIR / "plugins" / "installed_plugins.json")
        return any(
            key.startswith("nsls-builder-toolkit@")
            for key in registry.get("plugins", {})
        )
    except Exception:
        return False


def _plugin_disabled_by_user():
    """True if the user explicitly disabled the plugin — respect that."""
    try:
        settings = _read_json(_SETTINGS)
        for key, enabled in settings.get("enabledPlugins", {}).items():
            if key.startswith("nsls-builder-toolkit@") and enabled is False:
                return True
    except Exception:
        pass
    return False


def _shims_present():
    """True if a settings.json HOOK entry still points at this repo.

    Fail-CLOSED when the file cannot be read or parsed, for the same reason
    _org_stubs_exist() does: this gates the permanent done marker, and one
    extra retry next session is far cheaper than a machine wired twice forever.
    """
    try:
        raw = _SETTINGS.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return False  # no settings file at all — nothing to retire
    except Exception:
        return True
    try:
        settings = json.loads(raw)
    except Exception:
        return True
    if not isinstance(settings, dict):
        return True
    return any(_is_shim_command(c) for c in _iter_hook_commands(settings))


def _is_org_stub(entry):
    """True only for a folder that is exactly a toolkit pointer and nothing else.

    Its SKILL.md must be a pointer the toolkit writes for THIS skill
    (is_own_pointer, organisation toolkit only, never the personal one), and
    the folder must hold nothing else: scripts or references beside it mean a
    builder made it. Raises when it cannot read; callers decide what that means.
    """
    if not (entry.is_dir() and not entry.is_symlink()):
        return False
    stub = entry / "SKILL.md"
    if not stub.is_file():
        return False
    if any(p.name not in _OS_LITTER and p.name != "SKILL.md" for p in entry.iterdir()):
        return False
    if _IS_OWN_POINTER is None:
        return False
    try:
        text = stub.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        # The toolkit only writes UTF-8, so this file is a builder's (an ANSI
        # save with an accented letter). Raising here instead made detection
        # report "not clean" every session, so stage B never finished.
        return False
    return bool(_IS_OWN_POINTER(text, entry.name, plugins=[_ORG_PLUGIN]))


def _org_stubs_exist():
    """True if any org stub remains — or if we could not prove otherwise.

    Fail-CLOSED on purpose. This is the gate on writing the done-marker, and
    the marker is permanent: once written, _stage_b never runs again. An
    unreadable SKILL.md used to be swallowed by `continue` here AND by the same
    guard in _remove_org_stubs, so a stub that couldn't be deleted also
    couldn't be detected — the run looked clean, the marker was written, and
    that machine kept both the legacy shim and the plugin copy of every skill
    active forever, with no retry.

    Treating "I couldn't tell" as "something's still there" costs one extra
    retry next session; the alternative costs a permanently double-wired
    install that nothing will ever notice. Without the exact-pointer test (this
    file run on its own) it cannot tell at all, so it says not clean.
    """
    if not _SKILLS_DIR.is_dir():
        return False
    if _IS_OWN_POINTER is None:
        return True
    for entry in _SKILLS_DIR.iterdir():
        try:
            if _is_org_stub(entry):
                return True
        except Exception:
            return True  # undetermined — assume not clean, retry next session
    return False


def _announce(text):
    # Directive prefix so the message is surfaced on every Claude Code surface
    # (desktop injects hook stdout as context without printing it) — same
    # pattern as session-start.py's announcement block.
    print(
        "[NSLS Builder Toolkit — surface the following to the user verbatim "
        "at the start of your first reply, then proceed with their request:]\n\n"
        f"{text}"
    )


_STATUS = _CONFIG_DIR / ".nsls-plugin-migration-status"
_NOTICE_EVERY = 24 * 3600
_BUDGET_EXHAUSTED = "migration budget exhausted; will retry next session"
_TIMED_OUT = "a plugin command timed out"
_NOT_FOUND = "claude CLI not found"
_REASONS = {
    _NOT_FOUND: "the claude command could not be found",
    _TIMED_OUT: "a plugin command timed out",
}
# A budget cut once is a slow session. The same cut session after session is a
# step that can never finish in the time it is given: stuck by another name.
_OUT_OF_TIME = "each attempt runs out of time"
_CUTS_BEFORE_STUCK = 3


def _clean_detail(out):
    """CLI output, made safe to keep on disk: one line, printable, bounded."""
    text = "".join(c if c.isprintable() else " " for c in str(out))
    return " ".join(text.split())[:200]


_UPDATE_STATUS = _CONFIG_DIR / ".nsls-plugin-update-status"
_MESSAGES = {
    "a": ("[NSLS Builder Toolkit] Setup could not finish on this machine: "
          "{reason}, so the toolkit plugin and its guardrails are not "
          "installed here yet. It retries every session. Mention this to "
          "the user once, in one plain sentence, and suggest they tell the "
          "NSLS AI team if it keeps happening."),
    "update": ("[NSLS Builder Toolkit] The toolkit could not update itself on "
               "this machine: {reason}, so it is still running an older "
               "version. It retries every session. Mention this to the user "
               "once, in one plain sentence, and suggest they tell the NSLS AI "
               "team if it keeps happening."),
}


def _report_stuck(out, stage="a"):
    """Say once a day that setup is stuck, and always record why.

    Stage A used to return without a word when its first CLI call failed; both
    callers swallow exceptions; and the Windows hook throws away stderr, so the
    later failures that did print went nowhere either. A PC that could never
    install the plugin therefore looked exactly like one that had: no gates, no
    beacons, and nothing said. The status file is what a test or a health check
    reads; the stdout line is what reaches Claude's context on both platforms.

    Only toolkit-authored words go to stdout. SessionStart output is injected
    into the model's context, so interpolating raw CLI text there would let an
    error message add lines, instructions or private paths of its own. The raw
    detail, cleaned, stays in the local status file.

    Running out of the run's time budget once is not being stuck: that is a
    normal slow first session, and stage A resumes next session. But a machine
    where some step always needs longer than the budget allows would never
    install and never say so, which is the silent failure this exists to end.
    So budget cuts are counted, recorded every time, and said out loud once
    they have happened _CUTS_BEFORE_STUCK sessions in a row.
    """
    status = _UPDATE_STATUS if stage == "update" else _STATUS
    prev = {}
    try:
        prev = json.loads(status.read_text(encoding="utf-8"))
        if not isinstance(prev, dict):
            prev = {}
    except Exception:
        pass
    now = int(time.time())
    try:
        noticed = int(prev.get("noticed", 0))
    except (TypeError, ValueError):
        noticed = 0
    if noticed > now:
        noticed = 0  # a future stamp (clock change) must not silence it for good
    if out == _BUDGET_EXHAUSTED:
        reason = _OUT_OF_TIME
        try:
            cuts = int(prev.get("cuts", 0)) + 1 if prev.get("reason") == _OUT_OF_TIME else 1
        except (TypeError, ValueError):
            cuts = 1
        loud = cuts >= _CUTS_BEFORE_STUCK
    else:
        reason = _REASONS.get(out, "a plugin command failed")
        cuts = 0
        loud = True
    speak = loud and now - noticed >= _NOTICE_EVERY
    record = {"at": now, "stage": stage, "reason": reason, "cuts": cuts,
              "detail": _clean_detail(out), "noticed": now if speak else noticed}
    try:
        tmp = status.with_suffix(".tmp")
        tmp.write_text(json.dumps(record) + "\n", encoding="utf-8")
        os.replace(tmp, status)
    except Exception:
        pass
    if speak:
        print(_MESSAGES.get(stage, _MESSAGES["a"]).format(reason=reason))


def _clear_stuck(stage="a"):
    try:
        (_UPDATE_STATUS if stage == "update" else _STATUS).unlink()
    except OSError:
        pass


def _stage_a():
    """Install the plugin. The running session stays on shims."""
    ok, out = _claude(["plugin", "marketplace", "list"], timeout=30)
    if not ok:
        _report_stuck(out)
        return
    if _MARKETPLACE not in out:
        ok, out = _claude(["plugin", "marketplace", "add", _REPO_URL], timeout=60)
        if not ok:
            _report_stuck(out)
            return
    # 60s, not more: the SessionStart hook budget is 90s total and a killed
    # install is safe — stage A is idempotent and retries next session.
    ok, out = _claude(["plugin", "install", _PLUGIN_ID], timeout=60)
    if not ok:
        _report_stuck(out)
        return
    _clear_stuck()  # unstuck: nothing left to report
    _announce(
        "The NSLS Builder Toolkit installed itself as a Claude Code plugin "
        "(step 1 of 2). Starting next session, the toolkit's agents "
        "(knowledge-researcher and the two reviewers) are finally available. "
        "The old wiring will be cleaned up automatically in a later session — "
        "nothing to do."
    )


_UPDATE_MARKER = _CONFIG_DIR / ".nsls-plugin-update-check"


def _installed_version():
    """The installed plugin's version, from the CLI's registry, or None."""
    try:
        reg = _read_json(_CONFIG_DIR / "plugins" / "installed_plugins.json")
        for key, entries in (reg.get("plugins") or {}).items():
            if not str(key).startswith("nsls-builder-toolkit@"):
                continue
            for entry in ([entries] if isinstance(entries, dict) else entries or []):
                entry = entry or {}
                ver = entry.get("version") or (Path(entry["installPath"]).name
                                               if entry.get("installPath") else None)
                if ver:
                    return str(ver)
    except Exception:
        pass
    return None


def _clone_version():
    """The version of the checkout this file runs from, or None."""
    try:
        here = Path(globals()["__file__"]).resolve().parent.parent
        return str(_read_json(here / ".claude-plugin" / "plugin.json").get("version") or "") or None
    except Exception:
        return None


def _catch_up_plugin():
    """Update a plugin whose own copy can no longer update itself.

    session-start.py's ensure_plugin_fresh runs `plugin update` when main moves,
    but with the CLI finder of whatever plugin copy is installed. When that finder
    goes blind (the Store app moved its CLI into a hash folder on 2026-10-03),
    the copy can never fetch the release that fixes it: the test PC sat on
    3.16.2 for days while its checkout was on 3.16.13 (PC Test Round 5). This
    file is read from the checkout every session, so it is the one place a fix
    can reach such a machine.

    It only acts when the running copy's finder comes back empty, the checkout
    is ahead of the installed plugin, and the shared daily marker is stale; a
    healthy copy keeps doing its own update. The marker is touched once a CLI
    is found, so a hung update is not retried every session, and the copy's own
    check stays quiet for the day.
    """
    injected = globals().get("_NSLS_FIND_CLAUDE")
    if callable(injected):
        try:
            if injected():
                return
        except Exception:
            pass
    installed, clone = _installed_version(), _clone_version()
    if not installed or not clone or _version_tuple(installed) >= _version_tuple(clone):
        return
    try:
        if time.time() - _UPDATE_MARKER.stat().st_mtime < 86400:
            return
    except OSError:
        pass
    if not _find_claude():
        _report_stuck(_NOT_FOUND, stage="update")
        return
    try:
        _UPDATE_MARKER.touch()
    except OSError:
        pass
    ok, out = _claude(["plugin", "update", _PLUGIN_ID], timeout=20)
    if ok:
        _clear_stuck(stage="update")
    else:
        _report_stuck(out, stage="update")


def _remove_settings_hooks(only=None):
    """Drop shim hook entries for the named hooks. Returns count.

    `only` is the set of hooks whose plugin copy has been PROVEN to run here.
    An entry for a hook outside that set is left alone, however old it is.
    """
    if only is not None and not only:
        return 0
    settings = _read_json(_SETTINGS)
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return 0

    backup = _SETTINGS.with_name("settings.json.pre-plugin-migration")
    if not backup.exists():
        shutil.copy2(_SETTINGS, backup)

    removed = 0
    for event in list(hooks.keys()):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        for group in groups:
            entries = group.get("hooks", []) if isinstance(group, dict) else []
            kept = []
            for h in entries:
                command = str(h.get("command", ""))
                if not _is_shim_command(command):
                    kept.append(h)
                    continue
                hook = _hook_for_command(command)
                if only is not None and hook not in only:
                    kept.append(h)  # nothing has replaced this one yet
            
            removed += len(entries) - len(kept)
            if kept:
                group["hooks"] = kept
                kept_groups.append(group)
            elif not entries:
                kept_groups.append(group)
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]

    if removed:
        tmp = _SETTINGS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, _SETTINGS)
    return removed


def _remove_org_stubs(allowed=True):
    """Retire org-toolkit pointer stubs. Returns count.

    Only folders that are exactly a toolkit pointer (_is_org_stub) are touched,
    so a builder's own skill, a personal-toolkit pointer, or a pointer the
    builder extended is left alone. Each one is moved under
    ~/.claude/.nsls-removed-skills/<time>/ rather than deleted, so even a wrong
    call can be undone by moving it back.

    `allowed` is False until the plugin has been seen running here: the stubs
    are how skills reach a machine that has no plugin, and on Windows that has
    been every machine.
    """
    removed = 0
    if not allowed or _IS_OWN_POINTER is None:
        return 0
    if not _SKILLS_DIR.is_dir():
        return 0
    batch = _REMOVED_DIR / time.strftime("%Y%m%d-%H%M%S")
    for entry in sorted(_SKILLS_DIR.iterdir()):
        try:
            if not _is_org_stub(entry):
                continue
            batch.mkdir(parents=True, exist_ok=True)
            shutil.move(str(entry), str(batch / entry.name))
            removed += 1
        except Exception:
            continue  # never fall back to deleting; next session retries
    return removed


def _remove_user_scope_signal():
    """Remove the user-scope signal MCP registration if it points into this
    repo — the plugin now registers signal at plugin scope.

    Returns (changed, clean): clean means there is verifiably nothing left to
    do (absent, not ours, or removal confirmed). A failed removal returns
    clean=False so the done-marker is withheld and the next session retries.
    """
    ok, out = _claude(["mcp", "get", "signal"], timeout=15)
    if not ok and out in (_BUDGET_EXHAUSTED, _TIMED_OUT, _NOT_FOUND):
        # We never got an answer: the run's time ran out, the CLI hung, or
        # there is no CLI. That is not "absent", and reading it as absent let
        # the permanent done marker go down with the user-scope entry still
        # registered. Not clean; the next session asks again.
        return False, False
    if not ok or "nsls-builder-toolkit" not in out:
        return False, True  # absent or not ours — nothing to migrate
    removed, _ = _claude(["mcp", "remove", "signal", "-s", "user"], timeout=15)
    return (True, True) if removed else (False, False)


def _stage_b():
    """Retire the shims now that the plugin is live.

    Re-runs every session until the machine VERIFIES clean, then writes the
    done-marker. This is what makes partial failures (one stub that wouldn't
    delete, a timed-out mcp remove) retry instead of being orphaned once the
    settings hooks are gone.
    """
    # Windows is no longer excluded wholesale. It was, because the plugin's
    # hooks.json named `python3` — a Store alias there — and nobody could prove
    # any of it ran. Both halves of that have changed: hooks.json now goes
    # through run-hook.sh, which resolves the interpreter, and the beacons below
    # replace the guess with per-hook evidence from this machine. A hook whose
    # plugin copy has not been observed running keeps its shim, on every
    # platform, for as long as that stays true.
    #
    # Until all three are proven, a machine runs both copies of the proven ones.
    # That overlap is bounded and deliberate: the tracker dedupes session points
    # per builder per day, the gate single-flights on tool_use_id, and a second
    # git pull loses a race on index.lock and gives up. It ends on its own, one
    # hook at a time, as each beacon lands.
    proven = _proven_hooks()

    # CLI calls first: the claude CLI may normalize/rewrite settings.json as a
    # side effect (observed live: it rewrote a model alias during `mcp get`),
    # so our own settings edit must come after every CLI invocation.
    # Both passes, not just the first. A "reappeared" pass exists precisely
    # because the installer ran again, and the installer re-registers the
    # user-scope signal server alongside the shims it put back. Skipping the
    # removal there leaves signal registered at user AND plugin scope while
    # this run goes on to write the done marker, so nothing ever comes back
    # for it.
    signal_moved, signal_clean = _remove_user_scope_signal()
    hooks_removed = _remove_settings_hooks(only=proven)
    stubs_removed = _remove_org_stubs(allowed="session-start" in proven)

    local_key_removed = _remove_inert_local_enablement()

    # The done-marker is permanent, so it is withheld until every shim is gone.
    # An unproven hook therefore keeps stage B coming back each session, which
    # is exactly the retry this needs: the beacon may be one Skill call away.
    clean = signal_clean and not _shims_present() and not _org_stubs_exist()
    if clean:
        _DONE.write_text(
            json.dumps({"schema": _MIGRATION_SCHEMA, "at": int(time.time())}) + "\n",
            encoding="utf-8",
        )

    if hooks_removed or stubs_removed or signal_moved or local_key_removed:
        _announce(
            "NSLS Builder Toolkit plugin migration"
            + (" complete (step 2 of 2)" if clean else " progressed") + ": "
            f"removed {stubs_removed} legacy skill pointers"
            + (" (copies kept in ~/.claude/.nsls-removed-skills)" if stubs_removed else "")
            + f", {hooks_removed} legacy hook entries"
            + (", and moved the signal MCP server to plugin scope"
               if signal_moved else "")
            + ". Org skills now load as nsls-builder-toolkit:<name> — if a "
            "bare skill name fails this session, use the prefixed form. "
            "Rollback, if ever needed: restore "
            "~/.claude/settings.json.pre-plugin-migration and run "
            "`claude plugin uninstall nsls-builder-toolkit@nsls-toolkit`."
        )


def _remove_inert_local_enablement():
    """Drop `nsls-builder-toolkit@local` from enabledPlugins. Returns bool.

    Both installers write this key. It names a marketplace called `local` that
    does not exist, so Claude Code can never resolve it and it has no effect —
    but it LOOKS like a second live installation, and twice now that appearance
    has produced the wrong conclusion: an 2026-08-23 test against this key
    "proved" that bundled plugin hooks do not load, which is the belief that
    removed the guardrail gate from hooks.json on 2026-09-06 and left every
    migrated Mac unguarded for two weeks.

    Removed only when the absence of a `local` marketplace can be positively
    confirmed. If that file cannot be read, the key stays: a misleading no-op
    is cheaper than disabling a plugin someone actually installed.
    """
    try:
        known = _read_json(_CONFIG_DIR / "plugins" / "known_marketplaces.json")
    except Exception:
        return False
    names = known.get("marketplaces", known) if isinstance(known, dict) else {}
    if not isinstance(names, dict) or "local" in names:
        return False
    try:
        settings = _read_json(_SETTINGS)
    except Exception:
        return False
    plugins = settings.get("enabledPlugins")
    if not isinstance(plugins, dict) or "nsls-builder-toolkit@local" not in plugins:
        return False
    backup = _SETTINGS.with_name("settings.json.pre-plugin-migration")
    if not backup.exists():
        try:
            shutil.copy2(_SETTINGS, backup)
        except Exception:
            return False
    del plugins["nsls-builder-toolkit@local"]
    try:
        tmp = _SETTINGS.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, _SETTINGS)
        return True
    except Exception:
        return False


def _acquire_lock():
    """O_EXCL-first: never unlink before an exclusive create has failed.

    A pre-emptive stale unlink lets two contenders each remove the other's
    fresh lock (both validated against the SAME stale stat) and both proceed.
    Here a contender only reclaims after O_EXCL fails AND a fresh stat still
    shows the lock stale — and then must still win a second O_EXCL.
    """
    for _ in range(2):
        try:
            fd = os.open(str(_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - _LOCK.stat().st_mtime <= _LOCK_STALE_SECS:
                    return False  # live contender holds it
                _LOCK.unlink()
            except OSError:
                return False
        except Exception:
            return False
    return False


def run_migration():
    if os.environ.get("NSLS_NO_PLUGIN_MIGRATION"):
        return
    if not _acquire_lock():
        return
    try:
        if not _plugin_installed():
            _stage_a()  # stage B never follows in the same session
        else:
            # However it got here — an earlier session, or by hand — an
            # installed plugin means nothing is stuck any more.
            _clear_stuck()
            if not _plugin_disabled_by_user():
                # First: a newer plugin brings every other fix with it.
                _catch_up_plugin()
                if _stage_b_reason():
                    _stage_b()
    except Exception:
        pass  # fail-open: shims still work; retry next session
    finally:
        try:
            _LOCK.unlink()
        except OSError:
            pass


run_migration()
