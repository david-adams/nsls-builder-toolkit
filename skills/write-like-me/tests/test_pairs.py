"""Tests for write-like-me's pairs.py. Run: python3 skills/write-like-me/tests/test_pairs.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "scripts" / "pairs.py"
if not SCRIPT.exists():  # flat layout while prototyping
    SCRIPT = HERE / "pairs.py"
sys.path.insert(0, str(SCRIPT.parent))
import pairs  # noqa: E402

DRAFT_1 = ("Hi Sam! I wonder if you would test a skill for me? It drafts your weekly note "
           "from what you did this week. Try it and see how close it gets!")
DRAFT_2 = ("Hi Sam.\nI wonder if you would test a skill for me?\nIt drafts your weekly note "
           "from what you did this week.\nTry it and see how close it gets.")
SENT = ("Hi Sam.\nI wonder if you would test a skill for me?\nIt drafts your weekly note "
        "from what you did this week.\nTry it and tell me how close it gets.")
REWORK = ("Monday budget review covers travel catering venues printing speakers volunteers "
          "awards signage parking badges lanyards photography recording captions")
POSTED = ("Quick poll for the team: how long does your weekly note take you to write? "
          "React with one for under two minutes and two for longer than that.")
OTHER = ("Reminder for the board: the quarterly finance pack goes out on Thursday and "
         "every committee chair needs to confirm their section before noon on Wednesday.")


def row(kind, ts, content, **extra):
    r = {"type": kind, "timestamp": ts, "message": {"role": kind, "content": content}}
    r.update(extra)
    return json.dumps(r)


def text(t):
    return [{"type": "text", "text": t}]


def fenced(t):
    return text("Draft:\n\n```\n" + t + "\n```")


def send(ts, msg, call_id):
    return row("assistant", ts, [{"type": "tool_use", "id": call_id, "name": "mcp__abc__slack_send_message",
                                  "input": {"channel_id": "C1", "message": msg}}])


def result(ts, call_id, error=False):
    return row("user", ts, [{"type": "tool_result", "tool_use_id": call_id, "is_error": error,
                             "content": "The user doesn't want to proceed with this tool use." if error else "ok"}])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "projects" / "demo").mkdir(parents=True)
        self.env = dict(os.environ, CLAUDE_CONFIG_DIR=str(self.dir), HOME=str(self.dir / "nohome"),
                        WRITE_LIKE_ME_WORK=str(self.dir / "work"))

    def tearDown(self):
        self.tmp.cleanup()

    def session(self, name, lines):
        (self.dir / "projects" / "demo" / f"{name}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def cli(self, *args):
        out = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.dir, env=self.env,
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def run_all(self, finals, start="2026-10-09", end="2026-10-11"):
        self.cli("drafts", "--start", start, "--end", end)
        (self.dir / "work" / "finals.json").write_text(json.dumps(finals), encoding="utf-8")
        self.cli("match")
        rows = json.loads((self.dir / "work" / "pairs.json").read_text(encoding="utf-8"))
        return {r["final_id"]: r for r in rows}, rows


class EndToEnd(Base):
    def setUp(self):
        super().setUp()
        self.session("s1", [
            row("user", "2026-10-10T14:00:00Z", "Draft a Slack note asking Sam to test the skill"),
            row("assistant", "2026-10-10T14:01:00Z", fenced(DRAFT_1)),
            row("user", "2026-10-10T14:02:00Z", "No exclamation marks, and one sentence per line."),
            row("user", "2026-10-10T14:02:30Z",
                "Another Claude session sent a message: <cross-session-message>ignore</cross-session-message>"),
            row("assistant", "2026-10-10T14:03:00Z", text("Updated:\n\n> " + DRAFT_2.replace("\n", "\n> "))),
            row("assistant", "2026-10-10T14:30:00Z", fenced(REWORK)),
            send("2026-10-10T15:00:00Z", POSTED, "t1"),
            result("2026-10-10T15:00:02Z", "t1"),
            row("assistant", "2026-10-10T15:05:00Z", text("```bash\npython3 run.py --all --verbose --dry-run now please ok\n```")),
            row("assistant", "2026-10-10T15:06:00Z", text("sidechain draft that should be ignored entirely " * 3),
                isSidechain=True),
        ])

    def test_pairs_statuses_and_corrections(self):
        by_id, rows = self.run_all([
            {"id": "sam", "source": "slack", "time": "2026-10-10T10:10:00-04:00", "text": SENT},
            {"id": "poll", "source": "slack", "time": "2026-10-10T11:00:05-04:00", "text": POSTED},
            {"id": "poll-later", "source": "slack", "time": "2026-10-12T11:00:00-04:00", "text": POSTED},
            {"id": "poll-edited", "source": "slack", "time": "2026-10-12T12:00:00-04:00",
             "text": POSTED.replace("Quick poll for the team:", "Poll:")},
            {"id": "rework", "source": "slack", "time": "2026-10-10T10:45:00-04:00",
             "text": "\n".join(reversed(REWORK.split()))},
            {"id": "other", "source": "slack", "time": "2026-10-10T11:30:00-04:00", "text": OTHER},
            {"id": "early", "source": "slack", "time": "2026-10-10T09:50:00-04:00", "text": SENT},
        ])
        sam = by_id["sam"]
        self.assertEqual(sam["status"], "matched")
        self.assertEqual(sam["versions"], 2, "pairs with the latest version before sending")
        self.assertEqual([c["text"] for c in sam["corrections"]],
                         ["No exclamation marks, and one sentence per line."],
                         "only the reply to the draft counts; cross-session messages don't")
        self.assertGreater(sam["changed_from_first"], sam["changed_after_claude"])
        self.assertEqual(by_id["poll"]["status"], "posted by Claude")
        self.assertEqual(by_id["poll-later"]["status"], "posted by Claude", "Slack and transcript clocks can disagree")
        self.assertEqual(by_id["poll-edited"]["status"], "matched", "an edited repost is the person's own edit")
        self.assertEqual(by_id["rework"]["status"], "rewritten", "a full rewrite is a sample, not edits")
        self.assertEqual(by_id["other"]["status"], "no draft found")
        self.assertEqual(by_id["early"]["status"], "no draft found", "a draft written after sending isn't its source")

        shown = self.cli("show")
        self.assertIn("a sample of your own writing", shown)
        self.assertIn("reply: No exclamation marks", shown)
        n = [i for i, r in enumerate(rows, 1) if r["final_id"] == "sam"][0]
        first_id = sam["hunks"][0]["id"]
        self.cli("label", "--facts", f"{n}:{first_id}")
        labelled = [r for r in json.loads((self.dir / "work" / "pairs.json").read_text(encoding="utf-8"))
                    if r["final_id"] == "sam"][0]
        self.assertEqual([h["kind"] for h in labelled["hunks"]][0], "fact")
        self.assertTrue(all(h["kind"] == "style" for h in labelled["hunks"][1:]))
        scored = self.cli("score")
        self.assertIn("Sent with no style change", scored)
        self.assertIn("Removed 3 working file(s)", self.cli("clean"))
        self.assertFalse((self.dir / "work").exists())

    def test_reads_drafts_written_before_the_window(self):
        self.cli("drafts", "--start", "2026-10-11", "--end", "2026-10-11")
        pieces = json.loads((self.dir / "work" / "drafts.json").read_text(encoding="utf-8"))
        self.assertTrue(any("Sam" in p["versions"][0]["text"] for p in pieces))

    def test_skips_code_and_sidechains(self):
        self.cli("drafts", "--start", "2026-10-09", "--end", "2026-10-11")
        texts = [v["text"] for p in json.loads((self.dir / "work" / "drafts.json").read_text(encoding="utf-8"))
                 for v in p["versions"]]
        self.assertFalse(any("python3 run.py" in t or "sidechain" in t for t in texts))


class Attribution(Base):
    def test_copies_in_resumed_sessions_are_one_draft(self):
        for name in ("a", "b"):
            self.session(name, [row("assistant", "2026-10-10T14:00:00Z", fenced(DRAFT_1))])
        by_id, _ = self.run_all([{"id": "sam", "source": "slack",
                                  "time": "2026-10-10T11:00:00-04:00", "text": SENT}])
        self.assertEqual(by_id["sam"]["status"], "matched")
        self.assertNotIn("alternates", by_id["sam"])

    def test_close_call_takes_the_closer_wording_and_lists_the_other(self):
        near = DRAFT_2.replace("Try it and see", "Try it out and see")
        self.session("a", [row("assistant", "2026-10-10T14:00:00Z", fenced(near))])
        self.session("b", [row("assistant", "2026-10-10T13:00:00Z", fenced(DRAFT_2))])
        by_id, _ = self.run_all([{"id": "sam", "source": "slack",
                                  "time": "2026-10-10T11:00:00-04:00", "text": SENT}])
        self.assertEqual(by_id["sam"]["last_draft"], DRAFT_2, "closer wording wins over a later draft")
        self.assertEqual(len(by_id["sam"]["alternates"]), 1)

    def test_revision_days_later_is_the_same_piece(self):
        self.session("s", [
            row("assistant", "2026-10-07T14:00:00Z", fenced(DRAFT_1)),
            row("user", "2026-10-10T09:00:00Z", "Shorter, one sentence per line."),
            row("assistant", "2026-10-10T09:01:00Z", fenced(DRAFT_2)),
        ])
        by_id, _ = self.run_all([{"id": "sam", "source": "slack",
                                  "time": "2026-10-10T11:00:00-04:00", "text": SENT}], start="2026-10-10")
        self.assertEqual(by_id["sam"]["versions"], 2)
        self.assertEqual(len(by_id["sam"]["corrections"]), 1)

    def test_reply_to_another_draft_is_not_a_correction(self):
        self.session("s", [
            row("assistant", "2026-10-10T14:00:00Z", fenced(DRAFT_1)),
            row("user", "2026-10-10T14:00:30Z", "Now the board reminder please."),
            row("assistant", "2026-10-10T14:01:00Z", fenced(OTHER)),
            row("user", "2026-10-10T14:02:00Z", "Make the board reminder shorter."),
            row("assistant", "2026-10-10T14:03:00Z", fenced(DRAFT_2)),
        ])
        by_id, _ = self.run_all([{"id": "sam", "source": "slack",
                                  "time": "2026-10-10T11:00:00-04:00", "text": SENT}])
        self.assertEqual(by_id["sam"]["versions"], 2)
        self.assertEqual([c["text"] for c in by_id["sam"]["corrections"]], ["Now the board reminder please."],
                         "the reply to the later board draft is not a correction to Sam's note")

    def test_latest_revision_is_the_last_draft_even_if_it_drifted(self):
        v1 = " ".join(f"alpha{i}" for i in range(20))
        v2 = v1 + " " + " ".join(f"beta{i}" for i in range(10))
        final = v1 + " " + " ".join(f"gamma{i}" for i in range(15))
        self.session("s", [
            row("assistant", "2026-10-10T14:00:00Z", fenced(v1)),
            row("user", "2026-10-10T14:01:00Z", "Add the beta items."),
            row("assistant", "2026-10-10T14:02:00Z", fenced(v2)),
        ])
        by_id, _ = self.run_all([{"id": "f", "source": "slack",
                                  "time": "2026-10-10T10:30:00-04:00", "text": final}])
        self.assertEqual(by_id["f"]["versions"], 2)
        self.assertEqual(by_id["f"]["last_draft"], v2)
        self.assertEqual([c["text"] for c in by_id["f"]["corrections"]], ["Add the beta items."])

    def test_send_in_a_resumed_copy_is_kept(self):
        self.session("a", [row("assistant", "2026-10-10T14:00:00Z", fenced(POSTED))])
        self.session("b", [
            row("assistant", "2026-10-10T14:00:00Z", fenced(POSTED)),
            send("2026-10-10T14:05:00Z", POSTED, "t2"),
            result("2026-10-10T14:05:02Z", "t2"),
        ])
        by_id, _ = self.run_all([{"id": "poll", "source": "slack",
                                  "time": "2026-10-10T10:06:00-04:00", "text": POSTED}])
        self.assertEqual(by_id["poll"]["status"], "posted by Claude")

    def test_reshown_draft_counts_from_its_latest_showing(self):
        self.session("s", [
            row("assistant", "2026-09-30T14:00:00Z", fenced(DRAFT_1)),
            row("user", "2026-10-04T13:59:00Z", "Show me that note again."),
            row("assistant", "2026-10-04T14:00:00Z", fenced(DRAFT_1)),
        ])
        by_id, _ = self.run_all([{"id": "sam", "source": "slack", "time": "2026-10-16T10:00:00-04:00",
                                  "text": DRAFT_1}], start="2026-10-01", end="2026-10-16")
        self.assertEqual(by_id["sam"]["status"], "matched", "shown again 12 days before sending")
        self.assertEqual(by_id["sam"]["versions"], 1, "an unchanged re-showing is not a new version")

    def test_grouping_gap_counts_from_the_latest_showing(self):
        self.session("s", [
            row("assistant", "2026-09-25T14:00:00Z", fenced(DRAFT_1)),
            row("assistant", "2026-09-29T14:00:00Z", fenced(DRAFT_1)),
            row("user", "2026-10-03T13:59:00Z", "One sentence per line, no exclamation marks."),
            row("assistant", "2026-10-03T14:00:00Z", fenced(DRAFT_2)),
        ])
        by_id, _ = self.run_all([{"id": "sam", "source": "slack", "time": "2026-10-03T11:00:00-04:00",
                                  "text": SENT}], start="2026-10-01", end="2026-10-03")
        self.assertEqual(by_id["sam"]["versions"], 2, "8 days after the first showing, 4 after the last")

    def test_files_cannot_be_written_outside_the_work_folder(self):
        for args in (["drafts", "--start", "2026-10-09", "--end", "2026-10-11", "--out", "x.json"],
                     ["match", "--out", "x.json"], ["match", "--finals", "x.json"], ["score", "--pairs", "x.json"]):
            out = subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.dir, env=self.env,
                                 capture_output=True, text=True, encoding="utf-8")
            self.assertNotEqual(out.returncode, 0, args)
            self.assertFalse((self.dir / "x.json").exists())

    def test_a_message_after_an_unrelated_answer_is_not_a_correction(self):
        self.session("s", [
            row("assistant", "2026-10-10T14:00:00Z", fenced(DRAFT_1)),
            row("user", "2026-10-10T14:01:00Z", "No exclamation marks."),
            row("assistant", "2026-10-10T14:02:00Z", text("The board meets on Thursday, by the way.")),
            row("user", "2026-10-10T14:03:00Z", "What time on Thursday?"),
            row("assistant", "2026-10-10T14:04:00Z", fenced(DRAFT_2)),
        ])
        by_id, _ = self.run_all([{"id": "sam", "source": "slack",
                                  "time": "2026-10-10T11:00:00-04:00", "text": SENT}])
        self.assertEqual([c["text"] for c in by_id["sam"]["corrections"]], ["No exclamation marks."])

    def test_closing_text_after_a_draft_keeps_the_next_reply(self):
        self.session("s", [
            row("assistant", "2026-10-10T14:00:00Z", fenced(DRAFT_1)),
            row("assistant", "2026-10-10T14:00:05Z", text("Want me to change anything?")),
            row("user", "2026-10-10T14:01:00Z", "One sentence per line."),
            row("assistant", "2026-10-10T14:02:00Z", fenced(DRAFT_2)),
        ])
        by_id, _ = self.run_all([{"id": "sam", "source": "slack",
                                  "time": "2026-10-10T11:00:00-04:00", "text": SENT}])
        self.assertEqual([c["text"] for c in by_id["sam"]["corrections"]], ["One sentence per line."])

    def test_a_denied_send_is_not_a_post(self):
        self.session("s", [
            row("assistant", "2026-10-10T14:00:00Z", fenced(POSTED)),
            send("2026-10-10T14:01:00Z", POSTED, "t9"),
            result("2026-10-10T14:01:05Z", "t9", error=True),
        ])
        by_id, _ = self.run_all([{"id": "poll", "source": "slack",
                                  "time": "2026-10-10T10:20:00-04:00", "text": POSTED}])
        self.assertEqual(by_id["poll"]["status"], "matched", "the person posted it themselves")

    def test_a_reply_after_the_last_draft_counts_if_typed_before_sending(self):
        self.session("s", [
            row("assistant", "2026-10-10T14:00:00Z", fenced(DRAFT_2)),
            row("user", "2026-10-10T14:01:00Z", "Say tell me, not see. I'll fix it myself."),
            row("user", "2026-10-10T15:30:00Z", "Thanks, sent it."),
        ])
        by_id, _ = self.run_all([{"id": "sam", "source": "slack",
                                  "time": "2026-10-10T11:00:00-04:00", "text": SENT}])
        self.assertEqual([c["text"] for c in by_id["sam"]["corrections"]],
                         ["Say tell me, not see. I'll fix it myself."], "typed after sending: not a correction")

    def test_a_message_recorded_twice_counts_once(self):
        self.session("s", [
            row("assistant", "2026-10-10T14:00:00Z", fenced(DRAFT_1)),
            row("user", "2026-10-10T14:01:00Z", "No exclamation marks."),
            row("user", "2026-10-10T14:01:01Z", "No exclamation marks."),
            row("assistant", "2026-10-10T14:02:00Z", fenced(DRAFT_2)),
        ])
        by_id, _ = self.run_all([{"id": "sam", "source": "slack",
                                  "time": "2026-10-10T11:00:00-04:00", "text": SENT}])
        self.assertEqual(len(by_id["sam"]["corrections"]), 1)

    def test_one_reply_to_several_drafts_counts_for_each(self):
        self.session("s", [
            row("assistant", "2026-10-10T14:00:00Z", text("Two drafts:\n\n```\n" + DRAFT_1 + "\n```\n\n```\n" + OTHER + "\n```")),
            row("user", "2026-10-10T14:02:00Z", "Sam: no exclamation marks. Board: fine."),
            row("assistant", "2026-10-10T14:03:00Z", fenced(DRAFT_2)),
        ])
        by_id, _ = self.run_all([{"id": "sam", "source": "slack",
                                  "time": "2026-10-10T11:00:00-04:00", "text": SENT}])
        self.assertEqual(len(by_id["sam"]["corrections"]), 1)


class Measures(unittest.TestCase):
    def test_links_and_emoji_are_ignored(self):
        self.assertEqual(pairs.changed("Please review https://github.com/o/r/pull/1 today :tada:",
                                       "Please review <https://github.com/o/r/pull/1|o/r#1> today"), 0.0)

    def test_fact_changes_count_as_unchanged(self):
        row = {"first_draft": "Hi Trina, the pilot starts Monday. Could you approve it?",
               "final": "Hi Jeff, the pilot starts Monday.\nCould you approve it?"}
        row["hunks"] = pairs.hunks(row["first_draft"], row["final"])
        raw = pairs.style_changed(row)
        for h in row["hunks"]:
            h["kind"] = "fact" if "Trina" in h["before"] else "style"
        self.assertLess(pairs.style_changed(row), raw)
        self.assertGreater(pairs.style_changed(row), 0, "the line break is still style")

    def test_exclamation_marks_count_as_a_change(self):
        self.assertGreater(pairs.changed("Hi Sam! Could you test it?", "Hi Sam. Could you test it?"), 0)

    def test_case_and_punctuation_edits_count(self):
        self.assertGreater(pairs.changed("Hi Jenna. Thanks.", "hi Jenna - thanks."), 0)

    def test_rendering_differences_do_not_count(self):
        self.assertEqual(pairs.changed("**Quick poll:** pick one\n- yes\n- no",
                                       "Quick poll: pick one\n\u2022 yes\n\u2022 no"), 0.0)

    def test_merging_copies_keeps_sends_in_either_order(self):
        def piece(sends):
            return {"versions": [{"time": "2026-10-10T14:00:00+00:00", "text": POSTED}],
                    "sends": [{"time": "2026-10-10T14:05:00+00:00", "text": POSTED}] if sends else []}
        for order in ((False, True), (True, False)):
            pieces, seen = [], {}
            for has_send in order:
                pairs.add_piece(pieces, seen, piece(has_send))
            self.assertEqual(len(pieces), 1)
            self.assertEqual(len(pieces[0]["sends"]), 1, f"order {order}")

    def test_a_fork_keeps_both_revisions_and_a_copy_still_merges(self):
        def chain(*texts):
            return {"versions": [{"time": f"2026-10-01T1{i}:00:00+00:00", "text": x} for i, x in enumerate(texts)],
                    "sends": []}
        pieces, seen = [], {}
        pairs.add_piece(pieces, seen, chain(DRAFT_1, DRAFT_2))
        pairs.add_piece(pieces, seen, chain(DRAFT_1, OTHER))       # forked, revised differently
        self.assertEqual(len(pieces), 2, "neither revision is lost")
        pairs.add_piece(pieces, seen, chain(DRAFT_1, DRAFT_2, SENT))  # a copy that went further
        self.assertEqual(len(pieces), 2)
        self.assertEqual([len(p["versions"]) for p in pieces], [3, 2])

    def test_merging_copies_keeps_replies_after_the_last_version(self):
        def copy(after):
            return {"versions": [{"time": "2026-10-01T14:00:00+00:00", "text": POSTED}], "sends": [],
                    "after_last": after}
        note = {"time": "2026-10-01T14:05:00+00:00", "text": "Shorter."}
        pieces, seen = [], {}
        pairs.add_piece(pieces, seen, copy([]))
        pairs.add_piece(pieces, seen, copy([note]))
        self.assertEqual(pieces[0]["after_last"], [note])

    def test_merging_copies_keeps_every_showing(self):
        a = {"versions": [{"time": "2026-10-01T14:00:00+00:00", "shown": ["2026-10-01T14:00:00+00:00"],
                           "text": POSTED}], "sends": []}
        b = {"versions": [{"time": "2026-10-01T14:00:00+00:00",
                           "shown": ["2026-10-01T14:00:00+00:00", "2026-10-09T14:00:00+00:00"],
                           "text": POSTED}], "sends": []}
        pieces, seen = [], {}
        pairs.add_piece(pieces, seen, a)
        pairs.add_piece(pieces, seen, b)
        self.assertEqual(pieces[0]["versions"][0]["shown"][-1], "2026-10-09T14:00:00+00:00")

    def test_recency_tie_break_compares_real_times(self):
        same = "The plan for the spring term is ready for your review this week, thanks."
        earlier = {"versions": [{"time": "2026-10-10T14:00:00+00:00", "text": same}]}
        later = {"versions": [{"time": "2026-10-10T10:30:00-04:00", "text": same}]}  # 14:30 UTC
        sent = pairs.parse_time("2026-10-10T11:00:00-04:00")
        words = pairs.wordset(same)
        self.assertGreater(pairs.closeness(later, same, words, sent), pairs.closeness(earlier, same, words, sent))

    def test_repeated_words_keep_fact_spans_exact(self):
        row = {"first_draft": "Thanks Ana. Thanks again Ana for the notes.",
               "final": "Thanks Ben. Thanks again Ben for the notes."}
        row["hunks"] = pairs.hunks(row["first_draft"], row["final"])
        for h in row["hunks"]:
            h["kind"] = "fact"
        self.assertEqual(pairs.style_changed(row), 0.0)


if __name__ == "__main__":
    unittest.main()
