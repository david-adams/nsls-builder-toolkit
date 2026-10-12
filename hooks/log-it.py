#!/usr/bin/env python3
"""The exploration log, as one helper Claude runs. Through run-hook.sh:

    bash "<toolkit>/hooks/run-hook.sh" logit offer    --session S --key K --name N --sentence "..."
    bash "<toolkit>/hooks/run-hook.sh" logit accept   --session S --key K --name N --sentence "..." [--claim "<idea row>"]
    bash "<toolkit>/hooks/run-hook.sh" logit decline  --session S --key K --name N [--note "..."]
    bash "<toolkit>/hooks/run-hook.sh" logit not-yet  --session S --key K --reason "..."
    bash "<toolkit>/hooks/run-hook.sh" logit settled  --session S --key K --name N

The checkpoint hook prints these with the session and key filled in.

`offer` runs both neighbour checks first, so the builder hears about a
colleague on the same ground even if they decline to log: the installed-skills
check (what has been BUILT on this machine) and the tracker's /similar (what has
been REGISTERED). A hit is worded as an offer to coordinate, never a wall, and
nothing is ever sent to anyone. No hit means saying nothing about the check:
together they see registered automations and local skills only, so a clean
result is not evidence that nobody else is on it.

`accept` registers the build at Stage Exploring, scope Personal, with the
builder as owner: the credit. It used to be four hand-made calls whose address
and payload the policy never gave. A tracker that does not answer queues the log
and the checkpoint hook retries it later, so a slow day never costs the credit.

Every outcome is recorded locally (the checkpoint reads it) and reported:
guardrail_logit_offered / _accepted / _declined, plus guardrail_registered for
a log, because it is a registration. Prints what Claude needs; exits 0.
"""
import argparse
import json
import re
import uuid
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import logit_core as core  # noqa: E402

try:
    import guardrail_emit
except Exception:  # reporting is never worth failing the answer over
    guardrail_emit = None

# A queued log is retried with backoff until the tracker takes it or refuses
# it outright. Never dropped for age: the build reads as settled while it waits,
# so dropping it would lose the credit AND stop the build being offered again.
RETRY_MAX_HOURS = 24
INFLIGHT_SECONDS = 120
PERMANENT_HTTP = (400, 404, 409, 422)  # the tracker refused this payload; retrying won't help


def tracker_url():
    if guardrail_emit is not None:
        return guardrail_emit.TRACKER_URL
    return "https://web-production-6281e.up.railway.app"


def report(label, description, name):
    if guardrail_emit is None:
        return
    try:
        guardrail_emit.emit_detached(label, description, automation=name, variant=name)
    except Exception:
        pass


def post(path, body, timeout):
    req = urllib.request.Request(f"{tracker_url()}{path}", data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace") or "{}")


# ── neighbours ──────────────────────────────────────────────────────────────

def installed_neighbours(name, sentence):
    try:
        out = subprocess.run([sys.executable, str(HERE / "similar-skills.py"), name, sentence],
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=15)
    except Exception:
        return []
    hits, lines = [], out.stdout.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(\d\.\d\d)\s+(\S+)\s", line)
        if m:
            desc = lines[i + 1].strip() if i + 1 < len(lines) and lines[i + 1].startswith(" ") else ""
            hits.append({"name": m.group(2), "description": desc})
    return hits


def registry_neighbours(name, sentence):
    try:
        res = post("/similar", {"name": name, "description": sentence}, timeout=8)
        return [m for m in res.get("matches", []) if isinstance(m, dict)]
    except Exception:
        return []


def offer_text(name, sentence, skills, registry):
    lines = [f"This is turning into something. Want me to log \"{name}\" as an exploration? You get builder "
             "credit for it now, and the tracker shows others at NSLS that you're on it. "
             f"I'd describe it as: \"{sentence}\""]
    claim = None
    for m in registry:
        if m.get("from_idea_backlog") and not claim:
            claim = m.get("name")
            lines.append(f"Good news: someone already parked this idea in the tracker (\"{claim}\"). "
                         "Logging it would claim that row for you instead of making a twin.")
    live = [m for m in registry if not m.get("from_idea_backlog")]
    for m in live[:2]:
        who = m.get("builder") or "someone"
        if isinstance(who, list):
            who = ", ".join(str(w) for w in who) or "someone"
        lines.append(f"Heads-up: {who} has \"{m.get('name')}\" on the tracker ({m.get('stage') or 'no stage'}), "
                     "which sounds close. Want me to draft them a quick we-might-overlap note? Your call; "
                     "parallel work is often fine.")
    for s in skills[:2]:
        lines.append(f"There's also a skill on this machine covering similar ground: /{s['name']}"
                     + (f" ({s['description'][:110]})" if s.get("description") else "") + ".")
    return lines, claim


# ── answers ─────────────────────────────────────────────────────────────────

def cmd_offer(a):
    with core.locked() as data:
        b = core.build(data, a.key)
        prior = b["names"].get(a.name, {})
        if prior.get("outcome") in core.SETTLED:
            sess = core.session(data, a.session)
            sess["answer"] = "settled"
            print(f"\"{a.name}\" was already {prior['outcome'].replace('-pending', '').replace('settled', 'settled here')} on "
                  f"{prior.get('at', '')[:10]}, so it isn't offered again. Nothing to show the builder.")
            return
    skills = installed_neighbours(a.name, a.sentence)
    registry = registry_neighbours(a.name, a.sentence)
    lines, claim = offer_text(a.name, a.sentence, skills, registry)
    with core.locked() as data:
        sess = core.session(data, a.session)
        b = core.build(data, a.key)
        prior = b["names"].get(a.name, {})
        if prior.get("outcome") in core.SETTLED:
            # Settled while the neighbour checks ran (a decline or a log from
            # another session): never overwrite it with "offered".
            sess["answer"] = "settled"
            print(f"\"{a.name}\" was settled while this ran ({prior['outcome']}), so it isn't offered. "
                  "Nothing to show the builder.")
            return
        sess["answer"] = "offered"
        sess["changes_at_answer"] = sess.get("changes", 0)
        b["names"][a.name] = {"outcome": "offered", "at": core.now_iso(), "sentence": a.sentence,
                              "claim": claim or ""}
    report("guardrail_logit_offered", f"exploration log offered: {a.name}", a.name)
    # Exact values travel encoded, so the answer records the very name and the
    # claim finds its row (a parked idea's name comes from the tracker).
    name_arg = ["--name-b64", core.b64e(a.name)]
    claim_arg = ["--claim-b64", core.b64e(claim)] if claim else []
    print("Offer for the builder (your words are fine; keep it to a few lines, after your answer to them):")
    for line in lines:
        print("  " + line)
    print("\nTheir answer is recorded with:")
    print("  yes: " + core.command("accept", a.session, a.key, *name_arg,
                                   "--sentence-b64", core.b64e(a.sentence), *claim_arg))
    print("  no:  " + core.command("decline", a.session, a.key, *name_arg,
                                   "--note", '"<their reason, if they gave one>"'))
    print("Nothing is sent to anyone unless the builder asks for it.")


def repo_url(key):
    if not key.startswith("git:") or key.startswith("git:/"):
        return ""
    slug = key[4:]
    return f"https://{slug}" if "/" in slug else ""


def register_payload(name, sentence, key, email):
    auto = {"name": name, "description": sentence, "stage": "Exploring", "scope": "Personal"}
    url = repo_url(key)
    if url:
        auto["github_repo_url"] = url
    return {"automation": auto, "builder_email": email}


def send_registration(payload):
    """True when the tracker recorded it."""
    try:
        res = post("/register-automation-with-builder", payload, timeout=20)
        return bool(res.get("success")), res
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", errors="replace") or "{}")
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        body.setdefault("error", f"HTTP {e.code}")
        body["status"] = e.code
        return False, body
    except Exception as e:
        return False, {"error": type(e).__name__}


def logged(name, res):
    report("guardrail_logit_accepted", f"exploration log accepted: {name}", name)
    report("guardrail_registered", f"{name} logged as an exploration (Tier 1, volunteered via the checkpoint)",
           name)


def cmd_accept(a):
    email = a.email or (guardrail_emit.builder_email() if guardrail_emit is not None else "")
    target = a.claim or a.name
    if not email:
        print("No NSLS email is set for this builder (git config user.email, or BUILDER_EMAIL), so it can't be "
              "logged yet. Ask for their NSLS email and run this again with --email.")
        return
    payload = register_payload(target, a.sentence, a.key, email)
    ok, res = send_registration(payload)
    with core.locked() as data:
        sess = core.session(data, a.session)
        sess["answer"] = "accepted"
        b = core.build(data, a.key)
        entry = {"outcome": "logged" if ok else "logged-pending", "at": core.now_iso(), "sentence": a.sentence}
        if ok:
            entry["record_id"] = (res.get("automation") or {}).get("record_id", "")
        else:
            data["pending"].append({"id": uuid.uuid4().hex, "payload": payload, "key": a.key, "name": a.name,
                                    "created": core.now_iso(), "attempts": 1, "next_try": core.now_iso()})
        b["names"][a.name] = entry
    if ok:
        logged(a.name, res)
        claimed = " (claiming the parked idea row)" if a.claim else ""
        print(f"Logged \"{target}\" at Exploring{claimed}: builder credit recorded, and the row is theirs. "
              "Close in one warm line.")
    else:
        print(f"The tracker didn't take it ({res.get('error', 'no answer')}), so it's queued and goes out "
              "automatically at a later message. Tell the builder it's noted and the credit will land shortly.")


def cmd_decline(a):
    with core.locked() as data:
        sess = core.session(data, a.session)
        sess["answer"] = "declined"
        b = core.build(data, a.key)
        b["names"][a.name] = {"outcome": "declined", "at": core.now_iso(), "note": (a.note or "")[:200]}
    detail = f" — {a.note}" if a.note else ""
    report("guardrail_logit_declined", f"exploration log declined: {a.name}{detail}", a.name)
    print(f"Recorded. \"{a.name}\" won't be offered again. Nothing more to say to the builder about it.")


def cmd_not_yet(a):
    with core.locked() as data:
        sess = core.session(data, a.session)
        sess["answer"] = "not-yet"
        sess["changes_at_answer"] = sess.get("changes", 0)
        b = core.build(data, a.key)
        b["not_yet"] = (b.get("not_yet", []) + [core.now_iso()])[-10:]
        b["last_not_yet_reason"] = (a.reason or "")[:200]
    print("Recorded. Nothing to show the builder.")


def cmd_settled(a):
    """The work is a build already settled here, or an existing tool already on
    the tracker. Recorded against the name, so a repo is never asked again and
    a folder lists it next time."""
    with core.locked() as data:
        sess = core.session(data, a.session)
        sess["answer"] = "settled"
        b = core.build(data, a.key)
        if b["names"].get(a.name, {}).get("outcome") not in core.SETTLED:
            b["names"][a.name] = {"outcome": "settled", "at": core.now_iso()}
    print("Recorded. Nothing to show the builder.")


def _claim_next(data, now, skip):
    """Claim the next item whose turn it is, so a concurrent retry skips it.
    One at a time, right before its send: a lease taken for a whole batch could
    expire before its turn came and let a second retry send it too. The item
    stays in the durable queue until its delivery is acknowledged, so a retry
    killed mid-send loses nothing (a resend is harmless: the tracker finds the
    row by name and credits it once)."""
    for item in data.get("pending", []):
        item.setdefault("id", uuid.uuid4().hex)
        if item["id"] in skip:
            continue
        nxt = core._parse(item.get("next_try", "")) or now
        busy = core._parse(item.get("inflight_until", ""))
        if nxt <= now and not (busy and busy > now):
            item["inflight_until"] = (now + core.timedelta(seconds=INFLIGHT_SECONDS)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return dict(item)
    return None


def cmd_retry_pending(_a):
    seen = set()
    while True:
        now = core.datetime.now(core.timezone.utc)
        with core.locked() as data:
            item = _claim_next(data, now, seen)
        if item is None:
            return
        seen.add(item["id"])
        ok, res = send_registration(item.get("payload", {}))
        status = res.get("status") if isinstance(res, dict) else None
        name = item.get("name", "")
        with core.locked() as data:
            queue = data.get("pending", [])
            current = next((q for q in queue if q.get("id") == item["id"]), None)
            if current is None:
                continue  # another retry already finished it
            b = core.build(data, item.get("key", ""))
            if ok or status in PERMANENT_HTTP:
                data["pending"] = [q for q in queue if q.get("id") != item["id"]]
            if ok:
                entry = b["names"].get(name, {})
                entry.update(outcome="logged", record_id=(res.get("automation") or {}).get("record_id", ""))
                b["names"][name] = entry
            elif status in PERMANENT_HTTP:
                # Refused for good (no such builder, say): reopen the build so it can
                # be offered again, instead of leaving it "settled" with no row.
                b["names"][name] = {"outcome": "offered", "at": core.now_iso(),
                                    "note": f"tracker refused the log: {res.get('error', status)}"}
            else:
                current["attempts"] = current.get("attempts", 1) + 1
                hours = min(2 ** (current["attempts"] - 1), RETRY_MAX_HOURS)
                current["next_try"] = (now + core.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
                current.pop("inflight_until", None)
        if ok:
            logged(name, res)


def open_checkpoint(key, hours=6):
    """The session whose checkpoint is still waiting on this build, if any.

    A builder can ask to log something themselves (the register-automation and
    interrogate skills run `offer` without a session). If the checkpoint asked
    in that session too, the answer belongs to it; otherwise the Stop hook
    would remind Claude about a question it had in effect just answered.
    """
    cutoff = core.datetime.now(core.timezone.utc) - core.timedelta(hours=hours)
    best, best_at = None, None
    for sid, s in load_sessions().items():
        asked = core._parse(s.get("asked_at", ""))
        if s.get("key") == key and asked and asked >= cutoff and not s.get("answer"):
            if best_at is None or asked > best_at:
                best, best_at = sid, asked
    return best


def load_sessions():
    return core.load().get("sessions", {})


def main(argv):
    p = argparse.ArgumentParser(prog="log-it", add_help=True)
    p.add_argument("command", choices=["offer", "accept", "decline", "not-yet", "settled", "retry-pending"])
    p.add_argument("--session", default="manual")
    p.add_argument("--key", default="")
    p.add_argument("--name", default="")
    p.add_argument("--sentence", default="")
    p.add_argument("--claim", default="")
    p.add_argument("--note", default="")
    p.add_argument("--reason", default="")
    p.add_argument("--email", default="")
    for field in ("key", "name", "sentence", "claim"):
        p.add_argument(f"--{field}-b64", default="", dest=f"{field}_b64")
    a = p.parse_args(argv)
    for field in ("key", "name", "sentence", "claim"):
        token = getattr(a, f"{field}_b64")
        if token:
            try:
                setattr(a, field, core.b64d(token))
            except Exception:
                print(f"--{field}-b64 is not a valid encoded value; nothing was recorded.")
                return
    a.key = a.key or core.build_key(None)
    if a.session == "manual":
        a.session = open_checkpoint(a.key) or "manual"
    a.name = " ".join(a.name.split())[:120]
    a.sentence = " ".join(a.sentence.split())[:500]
    if a.command in ("offer", "accept", "decline", "settled") and not a.name:
        print("Give the build a short --name (and for offer/accept, a one-sentence --sentence).")
        return
    if a.command in ("offer", "accept") and not a.sentence:
        print("Add --sentence: what it does, in one plain sentence.")
        return
    {"offer": cmd_offer, "accept": cmd_accept, "decline": cmd_decline, "not-yet": cmd_not_yet,
     "settled": cmd_settled, "retry-pending": cmd_retry_pending}[a.command](a)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        main(sys.argv[1:])
    except SystemExit:
        pass
    except Exception as e:
        print(f"The exploration log helper hit an error ({type(e).__name__}); nothing was recorded.")
    sys.exit(0)
