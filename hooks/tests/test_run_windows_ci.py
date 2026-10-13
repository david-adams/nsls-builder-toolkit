#!/usr/bin/env python3
# windows-ci: python | The Windows CI runs every marked test in one step, and the workflow lists none by hand
"""The Windows CI finds its tests by their `# windows-ci:` marker.

Plain stdlib: `python3 hooks/tests/test_run_windows_ci.py`.

Pins every run the workflow made when each test was its own step, so a marker
lost in an edit fails here instead of quietly dropping a test from CI. Also
proves a failing test fails the step, through the PowerShell wrapper too when a
PowerShell is installed (always on the Windows runner; on a Mac only if pwsh is
on PATH).
"""
import contextlib
import io
import shutil
import sys
import tempfile
from pathlib import Path

import run_windows_ci as ci

ROOT = Path(__file__).resolve().parents[2]
failures = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {'' if cond else detail}")
    if not cond:
        failures.append(name)


# Every run the workflow made as separate steps (2026-10-13), by file and lane.
PINNED = {
    "python": ["test_skill_event_beacon_windows.py", "test_stage_b_keeps_builder_skills.py",
               "test_gate_powershell_paths.py", "test_guardrail_memory_folders.py", "test_plugin_hooks_utf8.py",
               "test_logit_checkpoint.py", "test_guardrail_emit.py", "test_guardrail_policy_budget.py",
               "test_similar_skills.py"],
    "ps51": ["parse-hooks.ps1", "install-find-cli.ps1", "shim-python-step.ps1", "install-settings-roundtrip.ps1",
             "install-git-bash.ps1", "collector-evidence.ps1", "personal-pull-upgrade.ps1",
             "rerun-after-update.ps1", "fork-ff-smoke.ps1", "pointers-after-plugin.ps1", "daily-pull-smoke.ps1",
             "install-beacon-answer.ps1", "collector-bootstrap.ps1", "installer-native-output.ps1"],
    "ps7": ["parse-hooks.ps1", "lock-smoke.ps1", "installer-native-output.ps1"],
}

print("every test the workflow used to list is marked, in the same lanes")
found = ci.marked()
runs = {(f.name, lane) for f, ls, _ in found for lane in ls}
for lane, names in PINNED.items():
    for name in names:
        check(f"{name} runs under {lane}", (name, lane) in runs)
check("every marker names known lanes and says what it proves",
      all(ls and all(lane in ci.LANES for lane in ls) and title for _, ls, title in found),
      repr([(f.name, ls, t) for f, ls, t in found if not ls or not t or any(x not in ci.LANES for x in ls)]))
check("this test marks itself too", ("test_run_windows_ci.py", "python") in runs)
check("the runner never picks itself up (its docstring's examples are indented)",
      "run_windows_ci.py" not in {f.name for f, _, _ in found})

print("\nthe workflow runs the runner and lists no test by hand")
wf = (ROOT / ".github" / "workflows" / "windows-hooks.yml").read_text(encoding="utf-8")
run_lines = [l.strip() for l in wf.splitlines() if l.strip().startswith("run:")]
check("one run line, and it is the runner", run_lines == ["run: python hooks/tests/run_windows_ci.py"], repr(run_lines))
check("the job keeps its required-check name", "name: hooks parse (PS 5.1 + 7) and the fork-check lock excludes" in wf)


def fake_repo(files):
    tmp = Path(tempfile.mkdtemp())
    for rel, text in files.items():
        (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp / rel).write_text(text, encoding="utf-8")
    return tmp


def quiet(argv, root):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = ci.main(argv, root=root)
    return code, buf.getvalue()


print("\na failing test fails the step, and every test still runs")
repo = fake_repo({
    "hooks/tests/test_a_fails.py": "# windows-ci: python | fails on purpose\nimport sys; sys.exit(3)\n",
    "hooks/tests/test_b_passes.py": "# windows-ci: python | passes\nprint('b ran')\n",
    "hooks/tests/test_c_unmarked.py": "import sys; sys.exit(9)\n",
})
code, out = quiet(["--lane", "python"], repo)
check("the step fails", code == 1, out)
check("the test after the failure still ran", "1 passed, 1 failed" in out, out)
check("an unmarked test is left alone", "unmarked" not in out and "exit 9" not in out, out)
code, out = quiet(["--lane", "python"], fake_repo({"test/test_ok.py": "# windows-ci: python | fine\n"}))
check("all passing passes", code == 0, out)
code, out = quiet(["--list"], fake_repo({"test/test_typo.py": "# windows-ci: ps5 | a typo'd lane\n"}))
check("a misspelled lane fails instead of skipping the test", code == 1 and "unknown lane" in out, out)
for bad in ("# windows-ci: ps-51 | punctuation in the lane", "# windows-ci: python", "# windows_ci: python | x",
            "# Windows-CI python | no colon"):
    code, out = quiet(["--list"], fake_repo({"test/test_bad.py": bad + "\n", "test/test_ok.py": "# windows-ci: python | ok\n"}))
    check(f"a marker that doesn't parse fails the step: {bad!r}", code == 1 and "doesn't parse" in out, out)
code, out = quiet([], fake_repo({"test/notes.py": "nothing marked\n"}))
check("no marked tests at all fails loudly", code == 1 and "no marked tests" in out, out)

print("\nPowerShell scripts run the way a `shell: powershell` / `pwsh` step runs them")
shells = [lane for lane, (exe, _) in ci.SHELLS.items() if shutil.which(exe)]
if not shells:
    print("  (no PowerShell on this machine; the Windows runner always has both)")
for lane in shells:
    repo = fake_repo({
        "hooks/tests/windows/a-error.ps1": f"# windows-ci: {lane} | a non-terminating error\nWrite-Error 'boom'\n'after'\n",
        "hooks/tests/windows/b-exit.ps1": f"# windows-ci: {lane} | a failing exit code\nexit 4\n",
        "hooks/tests/windows/c-zero.ps1": f"# windows-ci: {lane} | exit 0\nexit 0\n",
        "hooks/tests/windows/d-ok.ps1": f"# windows-ci: {lane} | fine\n'ok'\n",
    })
    by_name = {f.name: f for f, _, _ in ci.marked(repo)}
    check(f"{lane}: a Write-Error stops the script and fails it, as $ErrorActionPreference = 'stop' does",
          ci.run_one(by_name["a-error.ps1"], lane, repo) not in (0, None))
    # Under `-command` (GitHub's own wrapper), a script's `exit 4` reaches the caller as 1:
    # still a failure, which is all a step checks.
    check(f"{lane}: a script's failing exit fails the run", ci.run_one(by_name["b-exit.ps1"], lane, repo) not in (0, None))
    check(f"{lane}: exit 0 passes", ci.run_one(by_name["c-zero.ps1"], lane, repo) == 0)
    check(f"{lane}: a clean script passes", ci.run_one(by_name["d-ok.ps1"], lane, repo) == 0)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
