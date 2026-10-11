"""Tests for skill/scripts/digest.py on synthetic data. Run from skills/ai-level-up/: python3 -m unittest discover tests"""
import datetime as dt
import io
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import digest  # noqa: E402

NOW = dt.datetime.now(dt.timezone.utc)


def iso(days_ago=0, minutes=0):
    return (NOW - dt.timedelta(days=days_ago) + dt.timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def user(text, **kw):
    o = {"type": "user", "timestamp": kw.pop("ts", iso()), "message": {"role": "user", "content": text},
         "origin": kw.pop("origin", {"kind": "human"}), "cwd": "/work/proj-a"}
    o.update(kw)
    return o


def tool_use(name, inp=None, ts=None):
    return {"type": "assistant", "timestamp": ts or iso(), "message": {"role": "assistant", "content": [
        {"type": "tool_use", "name": name, "input": inp or {}}]}}


def quiet(fn, *a):
    with redirect_stdout(io.StringIO()) as out:
        fn(*a)
    return out.getvalue()


class Home(unittest.TestCase):
    """Each test gets an empty fake home directory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        keys = ("HOME", "USERPROFILE", "AI_LEVEL_UP_DIR", "AI_LEVEL_UP_REPORTS", "APPDATA", "CLAUDE_CONFIG_DIR")
        self.env = {k: os.environ.get(k) for k in keys}
        os.environ["HOME"] = os.environ["USERPROFILE"] = self.home
        os.environ["AI_LEVEL_UP_DIR"] = os.path.join(self.home, "state")
        for k in ("AI_LEVEL_UP_REPORTS", "APPDATA", "CLAUDE_CONFIG_DIR"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    def code_session(self, name, records):
        d = os.path.join(self.home, ".claude", "projects", "-work-proj")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, name + ".jsonl"), "w") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def cowork(self, sid, meta, turns, account="acct"):
        base = os.path.join(self.home, "Library", "Application Support", "Claude", "local-agent-mode-sessions", account, "org")
        os.makedirs(os.path.join(base, sid), exist_ok=True)
        meta = dict({"sessionId": sid, "lastActivityAt": int(NOW.timestamp() * 1000)}, **meta)
        with open(os.path.join(base, sid + ".json"), "w") as fh:
            json.dump(meta, fh)
        with open(os.path.join(base, sid, "audit.jsonl"), "w") as fh:
            for t in turns:
                fh.write(json.dumps({"type": "user", "_audit_timestamp": iso(), "message": {"role": "user", "content": t}}) + "\n")

    def scan(self, days=7):
        quiet(digest.main, ["scan", "--days", str(days), "--no-tracker"])
        with open(digest.digest_path()) as fh:
            return json.load(fh)


class TurnFilters(Home):
    def test_keeps_typed_turns_and_drops_machine_turns(self):
        self.code_session("s1", [
            user("Please draft the board update from these notes"),
            user([{"type": "tool_result", "content": "ok"}]),
            user("Base directory for this skill: /x\n# Skill body"),
            user("<task-notification>done</task-notification>", origin={"kind": "task-notification"}),
            user("hello from another session", origin={"kind": "peer", "from": "local_1"}),
            user("subagent prompt", isSidechain=True),
            user("meta", isMeta=True),
            user("[Request interrupted by user]"),
            user("The app was quit while you were working. Please continue from where you left off."),
            user("<system-reminder>noise</system-reminder>Check the totals against the sheet"),
            user("<command-name>/ce-plan</command-name><command-args>inbox view</command-args>"),
        ])
        s = self.scan()["sessions"][0]
        self.assertEqual([t["text"] for t in s["turns"]],
                         ["Please draft the board update from these notes", "Check the totals against the sheet", "/ce-plan inbox view"])
        self.assertEqual(s["interruptions"], 1)
        self.assertEqual(s["signals"]["peer_messages_in"], 1)

    def test_marker_mid_sentence_does_not_drop_the_turn(self):
        self.code_session("s1", [user("Why did the <task-notification> fire twice?"), user("b"), user("c")])
        self.assertEqual(self.scan()["sessions"][0]["turns"][0]["text"], "Why did the <task-notification> fire twice?")

    def test_pasted_bodies_are_never_kept(self):
        self.code_session("s1", [user('Tighten this <pasted_content id="1">Dear Sam, member 4471234 failed</pasted_content id="1">'),
                                 user('<pasted_content id="2">unterminated paste with secrets'), user("c")])
        s = self.scan()["sessions"][0]
        self.assertEqual(s["turns"][0]["text"], "Tighten this")
        self.assertNotIn("Dear Sam", json.dumps(s))
        self.assertNotIn("unterminated", json.dumps(s))
        self.assertEqual(s["pasted_blocks"], 2)

    def test_long_turns_are_clipped(self):
        self.code_session("s1", [user("x " * 2000), user("b"), user("c")])
        self.assertLessEqual(len(self.scan()["sessions"][0]["turns"][0]["text"]), digest.TURN_CHARS)

    def test_unclosed_tags_parse_in_linear_time(self):
        evil = "<pasted_content " * 4000 + "<system-reminder>" * 4000
        start = time.time()
        digest.classify_turn(evil)
        digest.strip_blocks(evil, "<pasted_content", "</pasted_content")
        digest.mask(evil)
        self.assertLess(time.time() - start, 1.0)


class Masking(unittest.TestCase):
    def check(self, raw, must_not_contain, label=None):
        flags = set()
        out = digest.mask(raw, flags)
        self.assertNotIn(must_not_contain, out)
        if label:
            self.assertIn(label, out + " ".join(sorted(flags)))
        return out

    def test_secrets(self):
        self.check("key sk-ant-abcdefghijklmnopqrstuvwxyz123", "abcdefghijk", "anthropic_key")
        self.check('{"client_secret": "s3cr3tvalue99"}', "s3cr3tvalue99", "credential")
        self.check("password=hunter2hunter2", "hunter2", "credential")
        self.check("Authorization: Bearer abcdef0123456789abcdef", "abcdef0123456789", "bearer_token")
        self.check("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nABCDEF\n-----END RSA PRIVATE KEY-----", "MIIEow", "private_key")
        self.check("-----BEGIN PRIVATE KEY-----\nMIIEow but never closed", "MIIEow", "private_key")

    def test_personal_details_and_ids(self):
        self.check("mail jo@nsls.org", "jo@nsls.org", "[email]")
        self.check("ssn 123-45-6789", "123-45-6789", "[ssn]")
        self.check("call 415-555-0134", "555-0134", "[phone]")
        self.check("member 44712345", "44712345", "[number]")
        self.check("client e755a44e-7bf4-490d-b088-87ac7d5b93fe", "e755a44e", "[id]")
        self.check("doc 1pEbOLh-zumHPltK-1gcbZcNA8KfnSE4r4ps0WD6fXw4", "1pEbOLh", "[id]")

    def test_clip_stops_on_a_word(self):
        out = digest.clip("the stronger fix is a hook in your settings file", 30)
        self.assertTrue(out.endswith("…"))
        self.assertIn(out[:-1].split(" ")[-1], "the stronger fix is a hook in your settings file".split(" "))

    def test_dates_and_words_survive(self):
        self.assertEqual(digest.mask("on 2026-09-19 at 9:30am, nsls-personal-toolkit-cowork"),
                         "on 2026-09-19 at 9:30am, nsls-personal-toolkit-cowork")


class Windows(Home):
    def test_window_excludes_old_and_untimed_records(self):
        self.code_session("s1", [user("old one", ts=iso(days_ago=20)), dict(user("no time"), timestamp=None),
                                 user("new one"), user("b"), user("c")])
        d = self.scan(days=7)
        self.assertEqual([t["text"] for t in d["sessions"][0]["turns"]], ["new one", "b", "c"])
        self.assertEqual(d["surfaces"]["code"]["aggregate"]["untimed_records_skipped"], 1)

    def test_timestamp_forms(self):
        secs = int(NOW.timestamp())
        self.assertEqual(digest.parse_ts(secs).year, NOW.year)
        self.assertEqual(digest.parse_ts(secs * 1000).year, NOW.year)
        self.assertEqual(digest.parse_ts(str(secs * 1000)).year, NOW.year)
        self.assertIsNotNone(digest.parse_ts("2026-09-24T14:09:28").tzinfo)  # naive -> UTC, no crash
        self.assertIsNotNone(digest.parse_ts("2026-09-24T14:09:28.0291Z"))   # 4-digit fraction
        self.assertIsNone(digest.parse_ts("yesterday"))
        self.assertIsNone(digest.parse_ts(True))


class Signals(Home):
    def test_tool_signals(self):
        self.code_session("s1", [
            user("a"), user("b"), user("c"),
            tool_use("Agent", {"prompt": "x"}),
            tool_use("Skill", {"skill": "codex-review"}),
            tool_use("Bash", {"command": "git -C /r push origin feat"}),
            tool_use("Bash", {"command": "gh pr create --title t"}),
            tool_use("mcp__scheduled-tasks__create_scheduled_task", {}),
            tool_use("mcp__1a2b3c4d-1111-2222-3333-444455556666__search_files", {}),
            tool_use("mcp__ccd_session_mgmt__send_message"),
            tool_use("mcp__claude-in-chrome__navigate"),
        ])
        sig = self.scan()["sessions"][0]["signals"]
        self.assertEqual(sig["subagents"], 1)
        self.assertEqual(sig["skills"], {"codex-review": 1})
        self.assertEqual(sig["ship"], {"git_push": 1, "pr_create": 1})
        self.assertEqual(sig["schedules_created"], 1)
        self.assertEqual(sig["connectors"], {"Google Drive": 1, "browser control": 1})
        self.assertEqual(sig["messages_to_sessions"], 1)

    def test_self_runs_are_matched_exactly(self):
        self.code_session("self-cmd", [user("<command-name>/ai-level-up</command-name>"), user("b"), user("c")])
        self.code_session("self-tool", [user("how am I doing with AI?"), tool_use("Skill", {"skill": "ai-level-up"}), user("b"), user("c")])
        self.code_session("talks-about-it", [user("let's improve the ai-level-up skill"), user("b"), user("c")])
        d = self.scan()
        self.assertEqual([s["title"] for s in d["sessions"]], ["let's improve the ai-level-up skill"])
        self.assertTrue(any("own runs" in n for n in d["notes"]))

    def test_desktop_scheduled_runs_are_automated_and_check_ins_are_self(self):
        wrap = '<scheduled-task name="%s" file="/x/SKILL.md"> This is an automated run of a scheduled task. %s</scheduled-task>'
        self.code_session("health", [user(wrap % ("git-health-check", "Scan every repo.")), user("b"), user("c")])
        self.code_session("checkin", [user(wrap % ("weekly", digest.SCHEDULED_PREFIX + " on everything new.")), user("b"), user("c")])
        d = self.scan()
        self.assertEqual(d["sessions"], [])
        self.assertEqual(d["surfaces"]["code"]["aggregate"]["automated_sessions"], 1)
        self.assertTrue(any("own runs" in n for n in d["notes"]))

    def test_a_moved_session_counts_once(self):
        turns = [dict(user("turn %d" % i), uuid="u%d" % i) for i in range(4)]
        self.code_session("original", turns[:3])
        d = os.path.join(self.home, ".claude", "projects", "-work-moved")
        os.makedirs(d)
        with open(os.path.join(d, "moved.jsonl"), "w") as fh:  # the copy holds the history plus one new turn
            for r in turns:
                fh.write(json.dumps(r) + "\n")
        dg = self.scan()
        self.assertEqual([s["human_turns"] for s in dg["sessions"]], [4])
        self.assertTrue(any("counted once" in n for n in dg["notes"]))

    def test_headless_runs_are_automated_and_not_sampled(self):
        self.code_session("cron", [dict(user("nightly job"), entrypoint="sdk-cli"), user("b"), user("c")])
        d = self.scan()
        self.assertEqual(d["sessions"], [])
        self.assertEqual(d["surfaces"]["code"]["aggregate"]["automated_sessions"], 1)

    def test_concurrency_counts_sessions_driven_in_the_same_half_hour(self):
        slot = (int(NOW.timestamp()) // 1800 - 48) * 1800  # a half-hour boundary a day ago
        at = lambda m: dt.datetime.fromtimestamp(slot + m * 60, tz=dt.timezone.utc).isoformat()
        self.code_session("a", [user("a1", ts=at(1)), user("a2", ts=at(5))])
        self.code_session("b", [user("b1", ts=at(10)), user("b2", ts=at(12))])
        self.code_session("c", [user("c1", ts=at(-600))])
        self.assertEqual(self.scan()["surfaces"]["code"]["aggregate"]["max_concurrent_sessions"], 2)

    def test_titles_and_projects_are_masked(self):
        self.code_session("s1", [{"type": "summary", "summary": "Fix for jo@nsls.org"},
                                 user("a", cwd="/x/client-e755a44e-7bf4-490d-b088-87ac7d5b93fe"), user("b"), user("c")])
        s = self.scan()["sessions"][0]
        self.assertNotIn("jo@nsls.org", s["title"])
        self.assertNotIn("e755a44e", s["project"])


class Cowork(Home):
    def test_cowork_meta_and_audit(self):
        self.cowork("local_1", {"title": "Tidy the Q3 folder", "userSelectedFolders": ["/Users/x/Q3"]},
                    ["sort these files", "rename by date", "now summarise"])
        self.cowork("local_2", {"title": "Monday digest", "scheduledTaskId": "t1"}, ["a", "b", "c"])
        d = self.scan()
        cw = [s for s in d["sessions"] if s["surface"] == "cowork"]
        self.assertEqual([(s["title"], s["project"], s["human_turns"]) for s in cw], [("Tidy the Q3 folder", "Q3", 3)])
        self.assertEqual(d["surfaces"]["cowork"]["aggregate"]["automated_sessions"], 1)
        self.assertFalse(d["surfaces"]["code"]["found"])

    def test_several_accounts_fail_closed_until_one_is_confirmed(self):
        self.cowork("local_1", {"title": "Work task", "emailAddress": "me@nsls.org"}, ["a", "b", "c"], account="a1")
        self.cowork("local_2", {"title": "Personal task", "emailAddress": "me@home.com"}, ["a", "b", "c"], account="a2")
        d = self.scan()
        self.assertEqual(d["surfaces"]["cowork"]["aggregate"]["sessions"], 0)
        self.assertTrue(any("several Claude accounts" in n for n in d["notes"]))
        quiet(digest.main, ["config", "email=me@nsls.org"])
        self.assertEqual([s["title"] for s in self.scan()["sessions"]], ["Work task"])

    def test_non_coder_week(self):
        for i, t in enumerate(["Summarise the benefits PDF", "Draft chapter email", "Clean up the events sheet", "Plan Monday"]):
            self.cowork("local_%d" % i, {"title": t}, ["do it", "shorter please", "thanks"])
        d = self.scan()
        self.assertEqual(d["surfaces"]["cowork"]["aggregate"]["sessions"], 4)
        self.assertEqual(len(d["sessions"]), 4)


class SettingsAndHistory(Home):
    def test_config_allowlist(self):
        quiet(digest.main, ["config", "frequency=weekly", "tracker_lookup=false"])
        self.assertEqual(digest.load_config(), {"frequency": "weekly", "tracker_lookup": False})
        for bad in ("stage_on_record=Steward", "frequency=hourly", "email=not-an-email", "anything=1"):
            with self.assertRaises(SystemExit):
                quiet(digest.main, ["config", bad])

    def test_email_never_reaches_the_digest(self):
        quiet(digest.main, ["config", "email=me@nsls.org"])
        self.code_session("s1", [user("a"), user("b"), user("c")])
        self.assertNotIn("me@nsls.org", json.dumps(self.scan()))

    def test_history_is_an_allowlist(self):
        run = digest.clean_run({"date": "2026-10-03", "stage": "Maker", "window_days": 7,
                                "seen_counts": {"code": 3, "chat": "x"},
                                "behaviours": {"1": "3 of 8", "2": "evil text"},
                                "experiments": [{"title": "Ask jo@nsls.org", "direction": "sideways", "quote": "secret words"}],
                                "strengths": [{"quote": "secret words"}], "nested": {"quotes": ["secret words"]}})
        blob = json.dumps(run)
        self.assertNotIn("secret words", blob)
        self.assertNotIn("jo@nsls.org", blob)
        self.assertEqual(run["behaviours"], {"1": "3/8"})
        self.assertEqual(run["seen"], {"code": 3})
        self.assertEqual(run["experiments"][0]["direction"], "same+check")

    def test_private_permissions(self):
        quiet(digest.main, ["config", "frequency=weekly"])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(digest.config_path()).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(digest.state_dir()).st_mode), 0o700)


class Render(Home):
    def report(self, **kw):
        r = {"date": "2026-10-03", "window_days": 7, "seen": "Claude Code (3 sessions)", "stage": "Maker",
             "unlock": "Take a prototype to production.", "toward_unlock": "Your sheet clean-up could count.",
             "surfaces": [{"name": "Claude Code", "summary": "<script>alert(1)</script>",
                           "points": [{"label": "Right level:", "text": "ok",
                                       "quote": "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen",
                                       "ref": "3 Oct"}]}],
             "experiments": [{"title": "Try <b>this</b>", "why": "w", "prompt": "Use key sk-ant-abcdefghijklmnopqrstuvwxyz", "check": "c",
                              "from": "3 Oct · sheet clean-up"}],
             "behaviours": {"1": "3 of 3"}, "seen_counts": {"code": 3}, "frequency": "weekly"}
        r.update(kw)
        path = os.path.join(self.home, "r.json")
        with open(path, "w") as fh:
            json.dump(r, fh)
        quiet(digest.main, ["render", path] + (["--no-record"] if os.path.exists(digest.history_path()) else []))
        with open(os.path.join(digest.reports_dir(), "2026-10-03.html")) as fh:
            return fh.read()

    def test_render_escapes_masks_and_clips(self):
        out = self.report()
        self.assertNotIn("<script>alert", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertNotIn("<b>this</b>", out)
        self.assertNotIn("abcdefghijklmnop", out)
        self.assertNotIn("sixteen", out)  # quotes stop at 15 words
        self.assertIn("Content-Security-Policy", out)
        self.assertNotIn("{{BODY}}", out)
        main = out.split("<main", 1)[1]
        self.assertIn("&lt;script&gt;", main)  # the body landed inside the page, not a comment
        self.assertIn("Builder stage on record", main)
        with open(digest.history_path()) as fh:
            self.assertEqual(json.loads(fh.readline())["stage"], "Maker")

    def test_charts_ladder_bars_and_tiles(self):
        out = self.report(tasks=[{"session": "a", "task": "t1", "level": "Agent", "fit": "right", "checked": True, "why": "fit"},
                                 {"session": "b", "task": "t2", "level": "Agent", "fit": "higher", "better_level": "Workflows",
                                  "checked": False, "why": "could hand over more"},
                                 {"session": "c", "task": "t3", "level": "Autopilot", "verdict": "Too trusting: no review"},
                                 {"session": "d", "task": "t4", "level": "Nonsense"}],
                          behaviours={"1": "7 of 8", "2": "1 of 8", "3": "9 of 8"},
                          previous={"behaviours": {"1": "5/8"}, "stage": "Explorer", "scores": {"right_level": "0/3"}})
        main = out.split("<main", 1)[1]
        self.assertIn('<span class="lv-count">2 sessions</span>', main)  # the count opens the sessions behind the bar
        self.assertEqual(main.count('<details class="lv-row">'), 2)        # one per level with sessions; empty levels don't open
        self.assertNotIn("sessions behind each bar", main)
        self.assertIn('class="seg higher"', main)                 # bar coloured by fit
        self.assertIn("(better: Workflows)", main)
        self.assertIn('class="fit lower"', main)                  # old 'too trusting' verdicts still map
        self.assertNotIn("#s1", main)                             # sessions are never numbered
        self.assertIn("<strong>Range: level 3 (Agent) to level 4 (Autopilot)</strong>", main)
        self.assertIn("<li>Mostly level 3 (Agent).</li>", main)   # a missed hand-over is not "beginnings"
        self.assertNotIn("Your range", main)
        self.assertIn("<strong>Higher isn't better.</strong>", main)
        self.assertIn("Checked the work: no", main)

        self.assertIn("1<small> of 3</small>", main)              # right level: 1 of the 3 judged
        self.assertIn("1<small> of 2</small>", main)              # checks: 1 of the 2 with a check recorded
        self.assertIn("▲ from 0 of 3 last time", main)
        self.assertIn("width:87.5%", main)                        # 7 of 8
        self.assertIn("typical 41%", main)                        # typical sits under your bar, not beside it
        self.assertIn("You: 7 of 8 sessions (last time 5 of 8)", main)  # the count shows on hover
        self.assertNotIn("▼ −", main)                             # no plus/minus column
        self.assertNotIn("9 of 8", main)                          # impossible ratios are dropped
        self.assertIn("Up a stage since last time", main)
        self.assertNotIn("sessions looked at", main)              # the tiles are scores only
        with open(digest.history_path()) as fh:
            self.assertEqual(json.loads(fh.readline())["scores"], {"right_level": "1/3", "checks": "1/2", "habits_ahead": "0/2"})

    def test_scope_card_says_what_was_covered_and_how_to_cover_more(self):
        tasks = [{"level": "Agent", "fit": "right"}] * 4
        local = self.report(mode="local", seen_counts={"code": 66}, tasks=tasks).split("<main", 1)[1]
        self.assertIn("What this check-in covered", local)
        self.assertIn("every project on this computer", local)
        self.assertIn("4 sessions read closely", local)
        self.assertNotIn("found", local)                       # totals are noise; only the sample matters
        self.assertNotIn("scope-hint", local)                  # Claude Code needs no caveat
        chat = self.report(mode="chat", tasks=tasks).split("<main", 1)[1]
        self.assertIn("4 chats read closely", chat)
        self.assertIn("doesn&#x27;t look inside your projects", chat)
        proj = self.report(mode="project", tasks=tasks).split("<main", 1)[1]
        self.assertIn("This project&#x27;s chats only.", proj)
        self.assertIn("run this check-in outside any project", proj)

    def test_missing_score_gets_a_quiet_tile(self):
        main = self.report(behaviours={}, tasks=[{"level": "Agent", "fit": "right", "checked": True}]).split("<main", 1)[1]
        self.assertIn("needs 8 or more sessions read closely", main)

    def test_verdict_names_ties_and_next_level_work_done_well(self):
        tasks = [{"level": "Agent", "fit": "right"}] * 3 + [{"level": "Workflows", "fit": "right"}]
        self.assertIn("Mostly level 3 (Agent), with the beginnings of level 5 (Workflows).", self.report(tasks=tasks))
        tied = [{"level": "Agent", "fit": "right"}] * 3 + [{"level": "Workflows", "fit": "right"}] + [{"level": "Multi-agent", "fit": "right"}] * 3
        out = self.report(tasks=tied)
        self.assertIn("Mostly levels 3 (Agent) and 7 (Multi-agent).", out)  # a tie names both
        self.assertNotIn("beginnings", out)
        spread = [{"level": n, "fit": "right"} for n in ("Agent", "Autopilot", "Workflows")]
        self.assertIn("Spread evenly across 3 levels.", self.report(tasks=spread))
        self.assertIn("All at level 3 (Agent)", self.report(tasks=[{"level": "Agent", "fit": "right"}] * 2))

    def test_habits_are_grouped_and_explained(self):
        beh = {"1": "9 of 9", "2": "1 of 9", "3": "2 of 9", "5": "0 of 9", "9": "6 of 9"}
        main = self.report(behaviours=beh, habit_notes={"2": "Paste last month's agenda as the model."}).split("<main", 1)[1]
        for word in ("Description", "Delegation", "Discernment"):
            self.assertNotIn(word, main)
        ahead, grow, par = (main.index(h) for h in ("Ahead of typical", "Habits to improve", "About typical"))
        self.assertLess(ahead, grow)
        self.assertLess(grow, par)
        self.assertLess(main.index("Shows an example of good"), main.index("Gives tone and style"))  # biggest gap first
        self.assertEqual(main.count("Big gap</span>"), 2)          # 30 and 23 points below typical; 8 isn't big
        self.assertIn('popovertarget="habit-2"', main)               # click a habit for what it means
        self.assertIn('id="habit-2" popover', main)
        self.assertIn("match this tone and length", main)
        self.assertIn("Paste last month&#x27;s agenda as the model.", main)
        self.assertIn('id="habits"', main)
        self.assertIn('See the 3 you could improve', main)           # 2 ahead of 5, so 3 to work on

    def test_template_prompts_need_an_example_from_a_real_session(self):
        exp = {"title": "Open each session with a job card", "direction": "habit", "why": "w", "check": "c",
               "prompt": "Job: <one outcome>. Done when: <check>."}
        with self.assertRaises(SystemExit) as stop:
            self.report(experiments=[exp])
        self.assertIn("add \"from\"", str(stop.exception))
        self.assertIn("is a template", str(stop.exception))
        with self.assertRaises(SystemExit) as stop:
            self.report(experiments=[dict(exp, **{"from": "7 Oct · agenda", "example": "Job: <still blank>."})])
        self.assertIn("still has blanks", str(stop.exception))
        out = self.report(experiments=[dict(exp, **{"from": "7 Oct · agenda", "example": "Job: Trina's agenda. Done when: she approves it."})])
        self.assertIn("Template: fill in the blanks", out)
        self.assertIn("Filled in for 7 Oct · agenda", out)
        self.assertIn("From your week: <strong>7 Oct · agenda</strong>", out)

    def test_about_says_where_to_run_and_footer_says_who_to_ask(self):
        out = self.report(paradox="Mixed. Checking held up.", method="68 counted; 9 judged.").split("<main", 1)[1]
        about = out[out.index("About this check-in"):out.index("<footer")]
        self.assertIn("Where to run it.", about)
        self.assertIn("Fresh or since last time.", about)
        self.assertNotIn("Checking held up", out)                  # old free-text notes are gone
        self.assertNotIn("9 judged", out)
        self.assertIn("Send them to Davo Wood", out[out.index("<footer"):])

    def test_since_last_scope_line(self):
        out = self.report(basis="since-last", since_date="2026-10-09", tasks=[{"level": "Agent", "fit": "right"}] * 2)
        self.assertIn("everything new since your last check-in on 9 Oct", out)
        self.assertIn("since 9 Oct", out)

    def test_experiments_offer_a_walk_through(self):
        out = self.report()
        self.assertIn("walk me through 1", out)
        self.assertNotIn("Step up", out)

    def test_unknown_stage_is_never_shown(self):
        out = self.report(stage="Wizard")
        self.assertNotIn("Wizard", out)
        self.assertIn("couldn", out)


class ScanModes(Home):
    def run_scan(self, *extra):
        quiet(digest.main, ["scan", "--no-tracker"] + list(extra))
        with open(digest.digest_path()) as fh:
            return json.load(fh)

    def test_fresh_scan_widens_until_it_has_enough_sessions(self):
        self.code_session("recent", [user("a", ts=iso(2)), user("b", ts=iso(2)), user("c", ts=iso(2))])
        self.code_session("older", [user("d", ts=iso(10)), user("e", ts=iso(10)), user("f", ts=iso(10))])
        d = self.run_scan("--max-per-surface", "2")
        self.assertEqual(d["window"]["days"], 14)
        self.assertEqual(d["window"]["basis"], "fresh")
        self.assertEqual(len(d["sessions"]), 2)
        self.assertEqual(self.run_scan("--max-per-surface", "2", "--days", "7")["window"]["days"], 7)  # a fixed window stays fixed

    def test_since_last_reads_new_sessions_first_then_new_turns_in_old_ones(self):
        os.makedirs(digest.state_dir(), exist_ok=True)
        digest.write_private(digest.history_path(), json.dumps({"date": "2026-10-09", "covered_to": iso(3)}) + "\n")
        self.code_session("old", [user("before %d" % i, ts=iso(5)) for i in range(6)] + [user("after", ts=iso(1))])
        self.code_session("new", [user("fresh %d" % i, ts=iso(1)) for i in range(3)])
        d = self.run_scan("--since-last", "--max-per-surface", "1")
        self.assertEqual(d["window"]["basis"], "since-last")
        self.assertEqual([s["title"] for s in d["sessions"]], ["fresh 0"])  # new session wins the only place
        d = self.run_scan("--since-last")
        old = [s for s in d["sessions"] if s["continued"]][0]
        self.assertEqual([t["text"] for t in old["turns"]], ["after"])    # only the new part of an old session
        self.assertEqual(d["surfaces"]["code"]["carried_over"], 1)

    def test_since_last_without_a_record_is_a_fresh_check_in(self):
        self.code_session("s", [user("a"), user("b"), user("c")])
        d = self.run_scan("--since-last")
        self.assertEqual(d["window"]["basis"], "fresh")
        self.assertTrue(any("fresh check-in" in n for n in d["notes"]))

    def test_render_records_where_the_reading_stopped(self):
        self.code_session("s", [user("a"), user("b"), user("c")])
        d = self.run_scan()
        path = os.path.join(self.home, "r.json")
        with open(path, "w") as fh:
            json.dump({"date": "2026-10-11", "tasks": []}, fh)
        quiet(digest.main, ["render", path])
        with open(digest.history_path()) as fh:
            self.assertEqual(digest.parse_ts(json.loads(fh.readline())["covered_to"]), digest.parse_ts(d["window"]["to"]))


if __name__ == "__main__":
    unittest.main()
