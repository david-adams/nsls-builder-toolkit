#!/usr/bin/env python3
# windows-ci: python | Stage B retires only exact toolkit pointers and never deletes a builder skill, on a real PC
"""Stage B retires only exact toolkit pointers, and never deletes anything.

Plain stdlib: `python3 hooks/tests/test_stage_b_keeps_builder_skills.py`.

The PC test on 2026-10-03 planted a builder's own skill at
~/.claude/skills/gws/SKILL.md whose body credited the toolkit skill ("Based on
~/.claude/local-plugins/nsls-builder-toolkit/skills/gws/SKILL.md, but always use
my shared drive first."). Stage B treated any SKILL.md that CONTAINED that path
as an org stub and shutil.rmtree'd the whole folder, with no copy. The same code
has run on Macs since 12 Aug. Stage B now asks session-start.py's
is_own_pointer, requires the folder to hold nothing but the pointer, and moves
what it retires under ~/.claude/.nsls-removed-skills instead of deleting it.
"""
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]
failures = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {'' if cond else detail}")
    if not cond:
        failures.append(name)


def load(cfg, inject=True):
    os.environ["CLAUDE_CONFIG_DIR"] = str(cfg)
    os.environ["NSLS_NO_PLUGIN_MIGRATION"] = "1"
    spec = importlib.util.spec_from_file_location(f"mig_{time.time_ns()}", HOOKS / "migrate_to_plugin.py")
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    if inject:
        ss_spec = importlib.util.spec_from_file_location(f"ss_{time.time_ns()}", HOOKS / "session-start.py")
        ss = importlib.util.module_from_spec(ss_spec)
        ss_spec.loader.exec_module(ss)
        mig._IS_OWN_POINTER = ss.is_own_pointer
    return mig


def pointer(skill, plugin="nsls-builder-toolkit", path_prefix="~/.claude/", nl="\n", extra_front=""):
    return nl.join(["---", f"name: {skill}", "description: >-", f"  {skill} skill", *( [extra_front] if extra_front else []),
                    "---", "", f"Read and follow the full skill at `{path_prefix}local-plugins/{plugin}/skills/{skill}/SKILL.md`.", ""])


def plant(skills, name, text, extra_files=()):
    d = skills / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8", newline="")
    for f in extra_files:
        (d / f).write_text("x")
    return d


with tempfile.TemporaryDirectory() as tmp:
    cfg = Path(tmp) / "claude"
    skills = cfg / "skills"
    skills.mkdir(parents=True)

    # Should be retired: exact toolkit pointers, in the shapes the writers use.
    plant(skills, "slack", pointer("slack"))
    plant(skills, "plan", pointer("plan", path_prefix="C:\\Users\\x\\.claude\\").replace("/", "\\")
          .replace("\\SKILL.md`", "\\SKILL.md`"), ())
    plant(skills, "review", "\ufeff" + pointer("review", nl="\r\n"))
    plant(skills, "brainstorm", pointer("brainstorm"), extra_files=(".DS_Store",))

    # Must survive: everything a builder made or changed.
    plant(skills, "gws", "---\nname: gws\ndescription: my own Workspace helper\n---\n\n"
          "Based on ~/.claude/local-plugins/nsls-builder-toolkit/skills/gws/SKILL.md, "
          "but always use my shared drive first.\n")
    plant(skills, "hubspot", pointer("hubspot", extra_front="model: sonnet"))
    plant(skills, "airtable", pointer("airtable"), extra_files=("helper.py",))
    plant(skills, "notes", pointer("notes") + "\nAlso: cc my manager.\n")
    plant(skills, "personal-setup", pointer("personal-setup", plugin="nsls-personal-toolkit"))
    plant(skills, "mixup", pointer("slack"))   # a pointer to ANOTHER skill's path

    mig = load(cfg)
    print("detection agrees with removal")
    check("stubs are detected before stage B", mig._org_stubs_exist())

    n = mig._remove_org_stubs(allowed=True)
    left = sorted(p.name for p in skills.iterdir())
    print("\nwhat stage B retires")
    check("exactly the four exact toolkit pointers are retired", n == 4, f"({n}, left {left})")
    for name in ("slack", "plan", "review", "brainstorm"):
        check(f"retired: {name}", name not in left)

    print("\nwhat stage B must never touch")
    for name, why in (("gws", "a builder skill that credits the toolkit skill (the PC's exact fixture)"),
                      ("hubspot", "a pointer with the builder's own front-matter key"),
                      ("airtable", "a pointer folder holding a file of the builder's"),
                      ("notes", "a pointer the builder extended with notes"),
                      ("personal-setup", "a personal-toolkit pointer"),
                      ("mixup", "a file pointing at a different skill's path")):
        check(f"kept: {name}, {why}", name in left)
    gws = (skills / "gws" / "SKILL.md").read_text()
    check("and the builder's gws file is byte-for-byte untouched", "always use my shared drive first" in gws)

    print("\nnothing is deleted outright")
    kept = list((cfg / ".nsls-removed-skills").glob("*/*"))
    check("each retired pointer is moved to ~/.claude/.nsls-removed-skills", sorted(p.name for p in kept)
          == ["brainstorm", "plan", "review", "slack"], f"({[p.name for p in kept]})")
    check("with its SKILL.md intact", all((p / "SKILL.md").is_file() for p in kept))
    check("detection now reads clean", not mig._org_stubs_exist())

with tempfile.TemporaryDirectory() as tmp:
    cfg = Path(tmp) / "claude"
    plant(cfg / "skills", "slack", pointer("slack"))
    mig = load(cfg, inject=False)
    print("\nrun without session-start.py's check")
    check("nothing is removed", mig._remove_org_stubs(allowed=True) == 0 and (cfg / "skills" / "slack").exists())
    check("and it reports not clean, so the done marker is withheld", mig._org_stubs_exist())

with tempfile.TemporaryDirectory() as tmp:
    cfg = Path(tmp) / "claude"
    d = cfg / "skills" / "cafe"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(b"---\nname: cafe\n---\n\nMy caf\xe9 notes.\n")  # ANSI, not UTF-8
    mig = load(cfg)
    print("\na builder skill saved in ANSI")
    check("is not a stub, and detection reads clean", not mig._org_stubs_exist())
    check("and it is left alone", mig._remove_org_stubs(allowed=True) == 0 and d.is_dir())

with tempfile.TemporaryDirectory() as tmp:
    cfg = Path(tmp) / "claude"
    plant(cfg / "skills", "slack", pointer("slack"))
    mig = load(cfg, inject=False)
    mig._DONE.parent.mkdir(parents=True, exist_ok=True)
    mig._DONE.write_text('{"schema": %d}' % mig._MIGRATION_SCHEMA, encoding="utf-8")
    print("\nan already-migrated machine whose plugin copy predates the check")
    check("stage B is not re-run for stubs it cannot judge", mig._stage_b_reason() is None,
          f"({mig._stage_b_reason()!r})")

print("\nsession start hands the check over")
src = (HOOKS / "session-start.py").read_text(encoding="utf-8")
check("run_plugin_migration injects is_own_pointer", '"_NSLS_IS_OWN_POINTER": is_own_pointer,' in src)
mig_src = (HOOKS / "migrate_to_plugin.py").read_text(encoding="utf-8")
check("no rmtree call is left in the migration", "shutil.rmtree(" not in mig_src)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
