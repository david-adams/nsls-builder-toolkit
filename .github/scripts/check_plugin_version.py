#!/usr/bin/env python3
"""Refuse a PR to main that does not bump `.claude-plugin/plugin.json`.

Usage: python3 .github/scripts/check_plugin_version.py [--changed-files PATH]
                                                      <base-ref> [<head-ref>]

With one positional argument the PR head is the working tree. With two, the
head manifest is read from <head-ref> via `git show` — the shape the workflow
uses under `pull_request_target`, where this script and the workflow come
from the BASE branch and nothing from the PR is ever executed, only read.

`--changed-files PATH` names a file holding the PR's changed paths, one per
line, as returned by the GitHub API (see CONTEXT_ONLY_PREFIX below).

Why this gate exists: builders run the toolkit from a VERSION-PINNED plugin
cache. `claude plugin update` compares version strings and nothing else, so a
merge that leaves the version alone is a merge no machine ever receives — the
CLI keeps saying "already at the latest version" over a stale cache. Eight PRs
landed that way between 2026-09-05 and 2026-09-09 (37 commits, the whole
guardrails series) and nobody got them.

Rule: the version on the PR head must be strictly greater than the version on
the base branch, compared numerically as X.Y.Z. Patch for fixes, minor for new
skills or behavior, major for breaking changes to how builders install or use
the toolkit. A base with no manifest at all (the first version ever) passes.

Tests: test/test_check_plugin_version.py
"""

import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path

MANIFEST = ".claude-plugin/plugin.json"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# Data the toolkit READS, written by the weekly Sync Org Context job from
# Airtable — org-chart.json, the LOPs, the strategy docs. None of it is code,
# none of it changes plugin behavior, and it has no version to bump.
#
# Requiring a bump here is what broke the sync: on 2026-08-31 branch protection
# started demanding a PR plus these two checks, the bot's direct push to main
# began failing with GH006, and org-chart.json froze at the 2026-08-24 copy for
# four weeks while every dashboard read healthy. The sync now opens a PR
# (.github/workflows/sync-org-context.yml) and this exemption lets that PR pass.
#
# Distribution still works: ensure_plugin_fresh() in hooks/session-start.py
# compares the installed COMMIT against the marketplace HEAD, not just the
# version string, and reinstalls on drift — so an unbumped context merge still
# reaches builders at their next session start. That commit check is the backstop this
# exemption leans on; if it is ever removed, this exemption has to go with it.
CONTEXT_ONLY_PREFIX = "_shared/context/"


def fail(msg):
    print(f"::error file={MANIFEST}::{msg}")
    print(f"\nFAIL: {msg}")
    sys.exit(1)


def read_changed_paths(path):
    """The PR's changed paths, or None if they could not be established.

    None means "unknown" and must route to the version check — never to the
    exemption. Every failure here (missing file, unreadable, empty list) is
    indistinguishable from a PR that touches nothing, and treating that as
    "context only" would wave through a PR that edits the plugin.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        print(f"note: could not read --changed-files {path!r} ({e}); "
              f"falling back to the version check.")
        return None
    paths = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not paths:
        print("note: --changed-files was empty; falling back to the version check.")
        return None
    return paths


def is_context_only(paths):
    """True when every changed path is inside CONTEXT_ONLY_PREFIX.

    Paths are normalized first: `_shared/context/../../.github/workflows/x.yml`
    starts with the prefix as a string but is not inside the directory, and a
    plain `startswith` would exempt a PR that rewrites CI.
    """
    for p in paths:
        normalized = posixpath.normpath(p.replace("\\", "/")).lstrip("/")
        if normalized.startswith("../") or normalized == "..":
            return False
        if not normalized.startswith(CONTEXT_ONLY_PREFIX):
            return False
    return True


def parse_version(raw, where):
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        fail(f"{where} {MANIFEST} is not valid JSON ({e.msg} at line {e.lineno}).")
    version = data.get("version") if isinstance(data, dict) else None
    if not isinstance(version, str):
        fail(f"{where} {MANIFEST} has no string \"version\" field.")
    m = SEMVER.match(version.strip())
    if not m:
        fail(f"{where} {MANIFEST} version {version!r} is not of the form X.Y.Z "
             f"(three integers, e.g. 3.7.0).")
    return tuple(int(g) for g in m.groups()), version.strip()


def main():
    argv = sys.argv[1:]
    changed_files = None
    if argv and argv[0] == "--changed-files":
        if len(argv) < 2:
            print(__doc__)
            sys.exit(2)
        changed_files = argv[1]
        argv = argv[2:]

    if len(argv) not in (1, 2):
        print(__doc__)
        sys.exit(2)
    base_ref = argv[0]
    head_ref = argv[1] if len(argv) == 2 else None

    # Checked before the manifest is read: a context-only PR has no manifest
    # change to inspect, and on the sync branch there is no bump to find.
    if changed_files is not None:
        paths = read_changed_paths(changed_files)
        if paths is not None and is_context_only(paths):
            shown = ", ".join(paths[:5]) + (f" (+{len(paths) - 5} more)"
                                            if len(paths) > 5 else "")
            print(f"ok: PR touches only {CONTEXT_ONLY_PREFIX} ({shown}); "
                  f"no version bump required for synced context data.")
            return

    if head_ref is None:
        head_path = Path(MANIFEST)
        if not head_path.exists():
            fail(f"{MANIFEST} is missing from the PR head.")
        head_raw = head_path.read_text(encoding="utf-8-sig")
    else:
        resolve_head = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{head_ref}^{{commit}}"],
            capture_output=True, text=True,
        )
        if resolve_head.returncode != 0:
            fail(f"head ref {head_ref!r} does not resolve to a commit — was it fetched?")
        show_head = subprocess.run(
            ["git", "show", f"{head_ref}:{MANIFEST}"], capture_output=True, text=True,
        )
        if show_head.returncode != 0:
            fail(f"{MANIFEST} is missing from the PR head ({head_ref}).")
        head_raw = show_head.stdout
    head_tuple, head_str = parse_version(head_raw, "PR head")

    # Does the base ref resolve at all? A typo'd or unfetched ref must not
    # read as "no manifest on base" and wave the PR through.
    resolve = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"],
        capture_output=True, text=True,
    )
    if resolve.returncode != 0:
        fail(f"base ref {base_ref!r} does not resolve to a commit — was it fetched?")

    show = subprocess.run(
        ["git", "show", f"{base_ref}:{MANIFEST}"],
        capture_output=True, text=True,
    )
    if show.returncode != 0:
        print(f"ok: {base_ref} has no {MANIFEST}; {head_str} is the first version.")
        return
    base_tuple, base_str = parse_version(show.stdout, f"base ({base_ref})")

    if head_tuple > base_tuple:
        print(f"ok: {MANIFEST} version {base_str} ({base_ref}) -> {head_str} (PR head)")
        return

    major, minor, patch = base_tuple
    fail(
        f"{MANIFEST} version must be bumped above {base_str} ({base_ref}); "
        f"the PR head has {head_str}. Builders install from a version-pinned "
        f"cache, so an unbumped merge never reaches them. Set \"version\" to "
        f"{major}.{minor}.{patch + 1} for a fix or {major}.{minor + 1}.0 for a "
        f"new skill or behavior, then push."
    )


if __name__ == "__main__":
    main()
