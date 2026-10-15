#!/usr/bin/env python3
# windows-ci: python | The installed-skills neighbour check ignores one shared common word, and still finds a real neighbour
"""The installed-skills duplicate check.

Run: python3 test/test_similar_skills.py

Exists because the registry check reported "no overlap" for a skill that was
sitting next to a near-identical one on the same machine (2026-09-19), and
nothing in the system could have noticed.
"""
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("ss", ROOT / "hooks" / "similar-skills.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

FAILED = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'ok' if ok else 'FAIL'}] {label}")
    if not ok:
        FAILED.append(f"{label}: got {got!r}, wanted {want!r}")


def sc(qn, qd, cn, cd):
    return round(m.score(m.tokens(qn), m.tokens(qn + " " + qd),
                         m.tokens(cn), m.tokens(cn + " " + cd)), 2)


print("\nthe case this exists for — a shared distinctive name beats a diluted description")
s = sc("macroscope-watch", "Waits for the Macroscope review bot to finish on a PR",
       "macroscope", "Use when the Macroscope review bot has posted review comments on a pull request")
check(f"macroscope pair scores {s} (>= threshold)", s >= m.THRESHOLD, True)

print("\nunrelated work stays quiet")
s = sc("rippling sync", "Hourly sync of employee records from Rippling into Airtable",
       "macroscope", "Use when the Macroscope review bot has posted review comments")
check(f"rippling vs macroscope scores {s} (< threshold)", s < m.THRESHOLD, True)

print("\nstopwords and plural folding match the registry check exactly")
check("'skills'/'tools'/'automation' dropped", m.tokens("skills tools automation"), set())
check("plural folded to singular", m.tokens("dashboards") == m.tokens("dashboard"), True)
check("short tokens dropped", "to" not in m.tokens("to the pr"), True)

print("\noverlap coefficient, not Jaccard — a one-line query is not punished for a long description")
q, long_row = {"alpha", "beta"}, {"alpha", "beta"} | {f"w{i}" for i in range(30)}
check("full query coverage scores 1.0", m.similarity(q, long_row), 1.0)
check("empty set scores 0.0", m.similarity(set(), long_row), 0.0)

print("\nan exact name match is reported, not suppressed")
# An earlier version skipped it as "not its own neighbour", which got the
# purpose backwards: this runs BEFORE a build, so a skill already installed
# under the proposed name is the loudest possible answer, not noise.
s = sc("macroscope", "anything at all", "macroscope", "Use when the review bot has posted comments")
check(f"identical name scores {s} (>= threshold)", s >= m.THRESHOLD, True)

print("\none common word shared by two names is not a twin")
# 72 of 170 installed skills had a one-word name (2026-10-12), so any proposal
# containing "log", "plan" or "review" scored 1.00 against the skill of that
# name. The real case: "Guardrails log-it checkpoint" reported /log as a twin.
DF = {"log": 5, "macroscope": 2, "plan": 20, "review": 25, "guardrail": 1, "checkpoint": 1}


def scdf(qn, qd, cn, cd):
    return round(m.score(m.tokens(qn), m.tokens(qn + " " + qd),
                         m.tokens(cn), m.tokens(cn + " " + cd), DF), 2)


LOG_IT = ("Guardrails log-it checkpoint",
          "Makes Claude reliably offer to log a build as an exploration once it becomes real")
LOG = ("log", "Log session progress to Obsidian project notes")
s = scdf(*LOG_IT, *LOG)
check(f"'log' in both names no longer makes /log a twin ({s})", s < m.THRESHOLD, True)
s = scdf("macroscope-watch", "Waits for the Macroscope review bot to finish on a PR",
         "macroscope", "Use when the Macroscope review bot has posted review comments on a pull request")
check(f"a distinctive shared word still does ({s})", s >= m.THRESHOLD, True)
s = scdf("log", "anything at all", *LOG)
check(f"an identical name still scores, however common its word ({s})", s >= m.THRESHOLD, True)
s = scdf("weekly plan review", "a weekly planning ritual", "plan review", "reviews a plan")
check(f"two shared words still count, common or not ({s})", s >= m.THRESHOLD, True)
check("without word counts the old reading stands", sc(*LOG_IT, *LOG) >= m.THRESHOLD, True)
def one_shared(word, count):
    # Full text shares only `word` out of four on each side (0.25), so the
    # result is decided by whether the one-word name reading is allowed to count.
    return m.score({word, "alpha"}, {word, "alpha", "delta", "omega"},
                   {word}, {word, "beta", "zeta", "theta"}, {word: count} if count else {})
check("the cutoff is inclusive: a word 3 skills use still counts",
      one_shared("gamma", m.DISTINCTIVE_DF) >= m.THRESHOLD, True)
check("and a word 4 skills use does not", one_shared("gamma", m.DISTINCTIVE_DF + 1) < m.THRESHOLD, True)
check("a word no installed skill uses is distinctive", one_shared("gamma", 0) >= m.THRESHOLD, True)
check("word counts span name and description, once per skill",
      m.document_frequency({"log": ("log your log", "/a"), "plan": ("make a log", "/b")}),
      {"log": 2, "plan": 1, "make": 1})

print("\nend to end on this machine: the false twin is gone, the real neighbour stays")
import contextlib, io
def hits(name, desc):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.main(["similar-skills.py", name, desc])
    return [ln.split()[1] for ln in buf.getvalue().splitlines() if ln[:1].isdigit()]
names_here = set(m.installed_skills())
if "log" in names_here:
    check("/log is not offered as a neighbour of the log-it checkpoint", "log" in hits(*LOG_IT), False)
if "macroscope" in names_here:
    check("macroscope is still offered as a neighbour of macroscope-watch",
          "macroscope" in hits("macroscope-watch", "Waits for the Macroscope review bot to finish on a PR"), True)

print("\ncached plugin versions sort newest-first, numerically not lexically")
import pathlib
def newer(a, b):
    ka = m._version_key(pathlib.Path(f"/x/{a}/skills"))
    kb = m._version_key(pathlib.Path(f"/x/{b}/skills"))
    return a if ka > kb else b
check("3.8.10 beats 3.8.9 (lexical would pick 3.8.9)", newer("3.8.9", "3.8.10"), "3.8.10")
check("3.10.0 beats 3.9.1", newer("3.9.1", "3.10.0"), "3.10.0")
check("a non-numeric directory sorts last",
      m._version_key(pathlib.Path("/x/some-branch/skills")) < m._version_key(pathlib.Path("/x/0.0.1/skills")), True)

print("\ndiscovery, against a fixture corpus (a CI runner has no installed skills)")
# Asserting on this machine's own skills made the test pass on a laptop and
# fail on every clean CI runner (Macroscope, PR #257). The fixture mirrors the
# real shape: a personal skill, two cached plugin versions of one skill, and
# enough skills using "log" that the word is common, as it is in practice.
import os
import tempfile
saved_cfg, saved_cwd = os.environ.get("CLAUDE_CONFIG_DIR"), os.getcwd()


def fixture_skill(root, name, desc):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n", encoding="utf-8")


with tempfile.TemporaryDirectory() as fx:
    home = pathlib.Path(fx) / ".claude"
    fixture_skill(home / "skills", "log", "Log session progress to Obsidian project notes")
    fixture_skill(home / "skills", "log-activity", "Log a sales activity against a deal")
    fixture_skill(home / "skills", "audit-trail", "Keeps an audit log of every change")
    fixture_skill(home / "skills", "close-day", "Wrap up the day and log what shipped")
    cache = home / "plugins" / "cache" / "nsls-toolkit" / "nsls-builder-toolkit"
    fixture_skill(cache / "3.8.9" / "skills", "macroscope", "An old description")
    fixture_skill(cache / "3.8.10" / "skills", "macroscope",
                  "Use when the Macroscope review bot has posted review comments on a pull request")
    os.environ["CLAUDE_CONFIG_DIR"] = str(home)
    os.chdir(fx)
    try:
        roots = [str(r) for r in m.skill_roots()]
        check("no duplicate roots", len(roots), len(set(roots)))
        check("found the fixture's roots", len(roots) >= 3, True)
        found = m.installed_skills()
        check("found every fixture skill once",
              sorted(found), ["audit-trail", "close-day", "log", "log-activity", "macroscope"])
        check("the newest cached copy wins", "review bot" in found["macroscope"][0], True)
        check("/log is not offered as a neighbour of the log-it checkpoint", "log" in hits(*LOG_IT), False)
        check("macroscope is still offered as a neighbour of macroscope-watch",
              "macroscope" in hits("macroscope-watch", "Waits for the Macroscope review bot to finish on a PR"), True)
    finally:
        os.chdir(saved_cwd)
        if saved_cfg is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = saved_cfg

print("\nit never raises — a crashing duplicate check must not stop a build")
try:
    m.main(["similar-skills.py"])
    m.main(["similar-skills.py", "", ""])
    check("degenerate input handled", True, True)
except Exception as e:
    check(f"degenerate input raised {e!r}", False, True)

print()
if FAILED:
    print("FAILURES:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("all green — the skills check agrees with the registry check's arithmetic")
