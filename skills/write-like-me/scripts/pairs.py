#!/usr/bin/env python3
"""Pair Claude's drafts with the versions you actually sent (write-like-me).

Reads Claude Code's own transcript files on this computer, read-only. Nothing is
sent anywhere. Standard library only; works on Mac, Windows and Linux.

    python3 pairs.py drafts --start 2026-10-01 --end 2026-10-12
    python3 pairs.py match
    python3 pairs.py show
    python3 pairs.py label --facts 3:0,6 5:2
    python3 pairs.py score
    python3 pairs.py clean

drafts  finds every prose draft Claude wrote in your sessions (fenced or quoted
        blocks, and text Claude put into a message, email or document tool),
        groups the versions of one piece together, and keeps the messages you
        typed in reply to the drafts Claude had just shown, until it revised
        them: your corrections.
        It also reads 7 days before --start, because a message sent early in
        the window may have been drafted before it.
finals  is a JSON list of {"id", "source", "time", "text"} that Claude saves in
        the work folder from the destination: Slack messages you sent, emails
        you sent, Google Docs. "time" is ISO 8601 with an offset.
match   pairs each final with the draft it came from: the piece whose versions
        overlap it most. On a close call it takes the draft worded most like the
        final, then the latest, and lists the others so you can correct it. Then
        it uses that piece's latest version before sending. It measures how much
        of the first draft changed by the time it was sent.
show    prints each item with its corrections and numbered changes.
label   records which changes are facts (item number : change ids).
score   reports the medians, counting fact changes as unchanged (an estimate).
clean   deletes the work folder's files.

Files live in a "write-like-me" folder in the system temp directory unless
--out / --drafts / --finals / --pairs say otherwise.
"""
import argparse
import difflib
import glob
import json
import os
import re
import statistics
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SAME_PIECE = 0.5      # word overlap between two versions of one piece
GROUP_HOURS = 120     # a later version must follow the previous one within this
MATCH = 0.45          # word overlap between a draft and a final
MARGIN = 0.05         # pieces this close to the best one are a close call
MAX_DAYS = 14         # a draft older than this before sending is not its source
POSTED = 0.02         # Claude sent text this close to the final itself
REWRITE = 0.6         # changed this much after Claude's last version: a sample
LEAD_DAYS = 7
MIN_WORDS = 12
WORK = Path(os.environ.get("WRITE_LIKE_ME_WORK") or Path(tempfile.gettempdir()) / "write-like-me")
WORK_FILES = ("drafts.json", "finals.json", "pairs.json")
PROSE_FENCES = {"", "text", "markdown", "md", "txt", "plain", "plaintext"}
TEXT_FIELDS = {"text", "message", "body", "content", "markdown"}
SEND_TOOLS = {"slack_send_message", "slack_schedule_message", "send_message",
              "send_email", "reply", "post_message"}
NOISE = ("<command-", "<local-command", "<system-reminder", "<task-notification",
         "Caveat:", "Base directory for this skill", "[Request interrupted",
         "This session is being continued", "<user-prompt-submit-hook",
         "Another Claude session sent a message", "<cross-session-message")
FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)
EMOJI = re.compile(r":[a-z][a-z0-9_+\-]*:")
MARKUP = re.compile(r"[*_~`]")                     # Slack and Markdown emphasis marks
BULLET = re.compile(r"^\s*[-*•◦]\s+")              # -, * and • bullets render the same
LINK = re.compile(r"<https?://[^>|]+(?:\|[^>]*)?>|\[[^\]]*\]\(https?://[^)]+\)|https?://\S+")
CODE = re.compile(r"^\s*(\$ |def |import |from \S+ import |class |function |const |#!)", re.M)


# ---------- text helpers ----------

def clean(text):
    """Remove what differs only in how a destination renders text: quote styles,
    links, emoji codes, emphasis marks and bullet glyphs."""
    text = (text.replace("’", "'").replace("‘", "'")
                .replace("“", '"').replace("”", '"'))
    text = EMOJI.sub("", LINK.sub(" link ", text))
    return "\n".join(MARKUP.sub("", BULLET.sub("• ", line)) for line in text.split("\n"))


def tokens(text):
    """What a reader sees change: words as written (case kept), every punctuation
    mark, and one token per line, so tone and layout edits all count."""
    out = []
    for line in clean(text).split("\n"):
        out += re.findall(r"[\w']+|[^\w\s]", line)
        out.append("¶")
    return out


def wordset(text):
    """Lower-case words only. Used to find which draft a final came from, never
    to measure change."""
    return set(re.findall(r"[\w']+", clean(text).lower()))


def overlap(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def is_prose(text):
    words = text.split()
    if len(words) < MIN_WORDS or CODE.search(text):
        return False
    plain = sum(1 for w in words if re.fullmatch(r"[^\W\d_][\w'’.,;:!?()\-]*", w))
    return plain / len(words) >= 0.6


def ratio(a, b):
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


def changed(before, after):
    """Share of tokens that changed between two texts."""
    return round(1 - ratio(tokens(before), tokens(after)), 3)


def hunks(before, after):
    a, b = tokens(before), tokens(after)
    out = []
    for n, (op, i1, i2, j1, j2) in enumerate(
            difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()):
        if op != "equal":
            out.append({"id": n, "before": " ".join(a[i1:i2]), "after": " ".join(b[j1:j2]),
                        "span": [i1, i2, j1, j2], "kind": ""})
    return out


def style_changed(row):
    """Change from first draft to sent, with fact hunks put back as drafted."""
    a, b = tokens(row["first_draft"]), tokens(row["final"])
    rebuilt, pos = [], 0
    facts = sorted((h["span"] for h in row.get("hunks", []) if h.get("kind") == "fact"),
                   key=lambda s: s[2])
    for i1, i2, j1, j2 in facts:
        rebuilt += b[pos:j1] + a[i1:i2]
        pos = j2
    rebuilt += b[pos:]
    return round(1 - ratio(a, rebuilt), 3)


def parse_time(value):
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.astimezone()


# ---------- reading sessions ----------

def config_dirs():
    found = []
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    for d in ([Path(env).expanduser()] if env else []) + [Path.home() / ".claude"]:
        if (d / "projects").is_dir() and d not in found:
            found.append(d)
    return found


def short(name):
    return name.rsplit("__", 1)[-1]


def user_text(content):
    if isinstance(content, str):
        parts = [content]
    elif isinstance(content, list):
        parts = [p.get("text", "") for p in content
                 if isinstance(p, dict) and p.get("type") == "text"]
    else:
        return ""
    text = "\n".join(parts).strip()
    if not text or text.startswith(NOISE):
        return ""
    return re.sub(r"</?pasted_content[^>]*>", "", text).strip()


def drafts_in(part):
    """(via, text) prose drafts inside one assistant content part."""
    out = []
    if part.get("type") == "text":
        body = part.get("text", "")
        for lang, block in FENCE.findall(body):
            if lang.strip().lower() in PROSE_FENCES:
                out.append(("fenced", block.rstrip("\n")))
        quoted = []
        for line in FENCE.sub("", body).split("\n") + [""]:
            if line.startswith(">"):
                quoted.append(re.sub(r"^>\s?", "", line))
            elif quoted:
                out.append(("quoted", "\n".join(quoted)))
                quoted = []
    elif part.get("type") == "tool_use":
        name, inp = part.get("name", ""), part.get("input") or {}
        if not isinstance(inp, dict):
            return out
        if name == "Write":
            path = str(inp.get("file_path", ""))
            if path.lower().endswith((".md", ".txt")) and isinstance(inp.get("content"), str):
                out.append(("file " + Path(path).name, inp["content"]))
        elif name.startswith("mcp__"):
            for key, value in inp.items():
                if key in TEXT_FIELDS and isinstance(value, str):
                    out.append(("tool " + short(name), value))
    return [(via, text) for via, text in out if is_prose(text)]


def sends_in(part):
    if part.get("type") != "tool_use" or short(part.get("name", "")) not in SEND_TOOLS:
        return []
    inp = part.get("input") or {}
    return [v for k, v in inp.items() if k in TEXT_FIELDS and isinstance(v, str)]


def read_session(path, start, end):
    events = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("isSidechain") or row.get("type") not in ("user", "assistant"):
                continue
            ts = parse_time(row.get("timestamp"))
            if not ts or not (start <= ts.astimezone().date() <= end):
                continue
            content = (row.get("message") or {}).get("content")
            if row["type"] == "user":
                text = user_text(content)
                if text:
                    events.append(("user", ts, text))
            elif isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    for via, text in drafts_in(part):
                        events.append(("draft", ts, (via, text)))
                    for text in sends_in(part):
                        events.append(("sent", ts, text))
    return events


def pieces_in(events, label):
    """Group one session's drafts into pieces; attach corrections and sends."""
    pieces, batch, batch_open = [], [], False
    pending = {}   # piece id -> user messages typed since its latest version was shown
    for kind, ts, payload in events:
        if kind == "draft":
            via, text = payload
            words = wordset(text)
            best, best_score = None, 0.0
            for p in pieces:
                prev = p["versions"][-1]
                if ts - max(shown_times(prev)) > timedelta(hours=GROUP_HOURS):
                    continue
                score = overlap(words, wordset(prev["text"]))
                if score > best_score:
                    best, best_score = p, score
            version = {"time": ts.isoformat(), "shown": [ts.isoformat()], "via": via, "text": text}
            if best is not None and best_score >= SAME_PIECE:
                last = best["versions"][-1]
                if text.strip() != last["text"].strip():
                    version["corrections_before"] = pending.pop(best["id"], [])
                    best["versions"].append(version)
                else:   # shown again unchanged: the same version, seen more recently
                    last.setdefault("shown", [last["time"]]).append(ts.isoformat())
            else:
                version["corrections_before"] = []
                best = {"id": f"{label}#{len(pieces) + 1}", "versions": [version], "sends": []}
                pieces.append(best)
            if not batch_open:        # the first draft after a user message
                batch, batch_open = [], True
            if best not in batch:
                batch.append(best)
        elif kind == "user":
            # A message answers the drafts Claude showed since the person last
            # typed (one reply often carries several), never older ones.
            for p in batch:
                pending.setdefault(p["id"], []).append({"time": ts.isoformat(), "text": payload[:2000]})
            batch_open = False
        elif kind == "sent":
            words = wordset(payload)
            for p in pieces:
                if overlap(words, wordset(p["versions"][-1]["text"])) >= SAME_PIECE:
                    p["sends"].append({"time": ts.isoformat(), "text": payload})
    return pieces


def add_piece(pieces, seen, piece):
    """Add a piece, merging the copy a resumed session makes of an earlier one:
    keep the copy with more versions, and never lose the other copy's sends."""
    key = (piece["versions"][0]["time"], piece["versions"][0]["text"].strip())
    twin = seen.get(key)
    if twin is None:
        seen[key] = piece
        pieces.append(piece)
        return
    keep, drop = (piece, twin) if len(piece["versions"]) > len(twin["versions"]) else (twin, piece)
    keep["sends"] += [s for s in drop["sends"] if s not in keep["sends"]]
    for kv, dv in zip(keep["versions"], drop["versions"]):   # and every showing
        if kv["text"].strip() == dv["text"].strip():
            kv["shown"] = sorted(set(kv.get("shown", [kv["time"]]) + dv.get("shown", [dv["time"]])),
                                 key=parse_time)
    if keep is piece:
        pieces[pieces.index(twin)] = seen[key] = piece


def cmd_drafts(args):
    start = date.fromisoformat(args.start) - timedelta(days=args.lead_days)
    end = date.fromisoformat(args.end)
    pieces, sessions, seen = [], 0, {}
    for base in config_dirs():
        for path in glob.glob(str(base / "projects" / "*" / "*.jsonl")):
            if datetime.fromtimestamp(os.path.getmtime(path)).date() < start:
                continue
            events = read_session(path, start, end)
            if not events:
                continue
            sessions += 1
            for piece in pieces_in(events, Path(path).stem[:8]):
                add_piece(pieces, seen, piece)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(pieces, fh, ensure_ascii=False, indent=1)
    revised = sum(1 for p in pieces if len(p["versions"]) > 1)
    print(f"{sessions} sessions read ({start} to {end}), {len(pieces)} drafts found, "
          f"{revised} revised at least once.")
    print(f"Work folder: {Path(args.out).parent}  (save the sent items there as finals.json)")


# ---------- matching finals ----------

def shown_times(version):
    """Every time Claude showed this version (or sent it), oldest first."""
    return [parse_time(t) for t in version.get("shown") or [version["time"]]]


def times_in_window(version, sent_at):
    """The showings that could have been the source of something sent at sent_at."""
    times = shown_times(version)
    if not sent_at:
        return times
    return [t for t in times if sent_at - timedelta(days=MAX_DAYS) <= t <= sent_at + timedelta(minutes=2)]


def in_window(version, sent_at):
    return bool(times_in_window(version, sent_at))


def best_version(piece, words, sent_at):
    """Highest overlap among this piece's versions written before sending."""
    return max((overlap(words, wordset(v["text"])) for v in piece["versions"]
                if in_window(v, sent_at)), default=0.0)


def closeness(piece, text, words, sent_at):
    """How alike in wording (then how recent) this piece's best version is."""
    final = tokens(text)
    return max(((ratio(tokens(v["text"]), final), max(times_in_window(v, sent_at))) for v in piece["versions"]
                if in_window(v, sent_at) and overlap(words, wordset(v["text"])) >= MATCH),
               default=(0.0, datetime.min.replace(tzinfo=timezone.utc)))


def cmd_match(args):
    pieces = json.load(open(args.drafts, encoding="utf-8"))
    finals = json.load(open(args.finals, encoding="utf-8"))
    rows = []
    for f in finals:
        text, sent_at = f.get("text", ""), parse_time(f.get("time"))
        words = wordset(text)
        row = {"final_id": f.get("id"), "source": f.get("source"), "time": f.get("time"), "final": text}
        scored = sorted(((best_version(p, words, sent_at), p) for p in pieces),
                        key=lambda x: x[0], reverse=True)
        scored = [(s, p) for s, p in scored if s >= MATCH]
        if not scored:
            row["status"] = "no draft found"
            rows.append(row)
            continue
        close = [(s, p) for s, p in scored if s >= scored[0][0] - MARGIN]
        close.sort(key=lambda c: closeness(c[1], text, words, sent_at), reverse=True)
        score, p = close[0]
        if len(close) > 1:
            row["alternates"] = [{"draft_id": q["id"],
                                  "start": " ".join(q["versions"][-1]["text"].split())[:120]}
                                 for _, q in close[1:4]]
        # The piece was chosen by overlap. Its last version is simply the latest one
        # Claude offered before sending, even if that one drifted from the final.
        k = max(i for i, v in enumerate(p["versions"]) if in_window(v, sent_at))
        first, last = p["versions"][0], p["versions"][k]
        # Claude's own send counts only if the final is that text, near enough
        # word for word, so an edited repost is never mistaken for it.
        posted = any(in_window(s, sent_at) and changed(s["text"], text) < POSTED for s in p["sends"])
        after = changed(last["text"], text)
        row.update({
            "status": "posted by Claude" if posted else "rewritten" if after >= REWRITE else "matched",
            "overlap": round(score, 2), "draft_id": p["id"], "versions": k + 1,
            "first_draft": first["text"], "last_draft": last["text"],
            "corrections": [c for v in p["versions"][1:k + 1] for c in v.get("corrections_before", [])],
            "changed_from_first": changed(first["text"], text),
            "changed_after_claude": after,
            "hunks": hunks(first["text"], text),
        })
        rows.append(row)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    for n, r in enumerate(rows, 1):
        when = f"{r['source']} {str(r['time'])[:16]}"
        if r["status"] == "no draft found":
            print(f"#{n} {when}  {r['status']}")
        else:
            close = f", close call with {len(r['alternates'])} other draft(s)" if r.get("alternates") else ""
            print(f"#{n} {when}  {r['status']}, {r['versions']} version(s), "
                  f"{len(r['corrections'])} correction(s), {r['changed_from_first']:.0%} changed from first draft, "
                  f"{r['changed_after_claude']:.0%} after Claude's last version{close}")
    print(f"Saved to {args.out}")


def cmd_show(args):
    rows = json.load(open(args.pairs, encoding="utf-8"))
    for n, r in enumerate(rows, 1):
        final = " ".join(r.get("final", "").split())
        head = f"#{n} {r['status'].upper()} ({r.get('source')}, {str(r.get('time'))[:10]})"
        if r["status"] == "no draft found":
            print(f"\n{head}, a sample of your own writing: {final[:300]}")
            continue
        print(f"\n{head}, {r['changed_from_first']:.0%} changed from first draft")
        print(f"   draft used: {' '.join(r['last_draft'].split())[:120]}")
        for a in r.get("alternates", []):
            print(f"   close call, also similar: {a['start']}")
        for c in r.get("corrections", []):
            print(f"   correction: {' '.join(c['text'].split())[:1500]}")
        if r["status"] == "rewritten":
            print(f"   sent version (a sample of your own writing): {final[:400]}")
            continue
        for h in r.get("hunks", []):
            before = h["before"].replace("¶", "/")[:160]
            after = h["after"].replace("¶", "/")[:160]
            print(f"   [{h['id']}] {h.get('kind') or '?':5} - {before}  + {after}")
    print("\n'/' is a line break. Label facts with: pairs.py label --facts N:ID,ID N:ID ...")


def cmd_label(args):
    rows = json.load(open(args.pairs, encoding="utf-8"))
    facts = {}
    for spec in args.facts:
        item, _, ids = spec.partition(":")
        facts[int(item)] = {int(i) for i in ids.split(",") if i.strip()}
    for n, r in enumerate(rows, 1):
        for h in r.get("hunks", []):
            h["kind"] = "fact" if h["id"] in facts.get(n, set()) else "style"
    with open(args.pairs, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=1)
    print(f"Labelled {sum(len(v) for v in facts.values())} changes as fact; "
          "every other change counts as style.")


def cmd_score(args):
    rows = json.load(open(args.pairs, encoding="utf-8"))
    used = [r for r in rows if r.get("status") == "matched"]
    count = lambda s: sum(1 for r in rows if r.get("status") == s)
    print(f"{len(used)} pairs scored (not scored: {count('posted by Claude')} posted by Claude, "
          f"{count('rewritten')} rewritten, "
          f"{count('no draft found')} with no draft)")
    if not used:
        return
    style = [style_changed(r) for r in used]
    after = [r["changed_after_claude"] for r in used]
    corr = [len(r.get("corrections", [])) for r in used]
    print(f"Style change from first draft to sent (estimate): median {statistics.median(style):.0%}, "
          f"mean {statistics.mean(style):.0%}")
    print(f"Sent with no style change: {sum(1 for s in style if s < 0.02)} of {len(style)}")
    print(f"All changes after Claude's last version: median {statistics.median(after):.0%}")
    print(f"Corrections typed per piece: median {statistics.median(corr):g}")


def cmd_clean(args):
    removed = 0
    for name in WORK_FILES:
        f = WORK / name
        if f.is_file():
            f.unlink()
            removed += 1
    try:
        WORK.rmdir()
    except OSError:
        pass
    print(f"Removed {removed} working file(s) from {WORK}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("drafts")
    d.add_argument("--start", required=True)
    d.add_argument("--end", required=True)
    d.add_argument("--lead-days", type=int, default=LEAD_DAYS)
    d.add_argument("--out", default=str(WORK / "drafts.json"))
    m = sub.add_parser("match")
    m.add_argument("--drafts", default=str(WORK / "drafts.json"))
    m.add_argument("--finals", default=str(WORK / "finals.json"))
    m.add_argument("--out", default=str(WORK / "pairs.json"))
    for name in ("show", "score"):
        sub.add_parser(name).add_argument("--pairs", default=str(WORK / "pairs.json"))
    lab = sub.add_parser("label")
    lab.add_argument("--pairs", default=str(WORK / "pairs.json"))
    lab.add_argument("--facts", nargs="*", default=[], metavar="N:ID,ID")
    sub.add_parser("clean")
    args = ap.parse_args()
    {"drafts": cmd_drafts, "match": cmd_match, "show": cmd_show, "label": cmd_label,
     "score": cmd_score, "clean": cmd_clean}[args.cmd](args)


if __name__ == "__main__":
    main()
