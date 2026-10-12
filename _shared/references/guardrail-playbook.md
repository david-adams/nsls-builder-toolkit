# Guardrail playbook

The session-start policy (CLAUDE.md § Builder Guardrails) carries the rules.
This file carries the reasons and the longer procedures, so the policy itself
stays small enough for Claude to actually read: Claude Code hands Claude only
the first 2,000 characters of any hook output over 10,000, and the policy grew
past that in September 2026, which hid everything after the hard-gate list.

Read the section you need when a guardrail moment calls for more than the
policy says. Commands below write `<toolkit>` for the toolkit folder that the
session-start policy's own commands name.

## Why the two enforcement mechanisms must never be confused

The four hard gates are deterministic: `hooks/guardrail-gate.py` runs before
every Bash/Write/Edit call and matches specific command shapes. Everything else
is advice Claude raises conversationally, and the builder may decline any of it.

Never tell a builder that policy blocked something the hook did not deny, or
that it "would be" blocked. One false claim teaches builders the whole
vocabulary is noise, and then the real blocks get routed around too.

A hard block that offers only the compliance route is "not a flat no" in name
only: it is a no with homework. The authorization route is the second way out,
which is why Kevin must be named in every block message.

## Why declines are recorded

Rule 6 of the voice guide is "take the first no gracefully, and remember it per
build". Without a record, every new session re-raises what the builder already
refused, and being asked a third time about the same script is how the toolkit
becomes nagware. `memory record` keeps the no on this machine (keyed by the git
remote, so it follows the build across checkouts) and reports one
`guardrail_proceeded` row so the guardrail page can show that a nudge was made
and declined.

## Why the conversational moments are reported

Three of the four tiles on Signal's guardrail page are fed only by these
events. A session where the guardrails worked perfectly and nothing was reported
reads, to Kevin and Jenna, exactly like a session where they did nothing.

`disputed` is the load-bearing one: it is the only route by which a gate that
fires wrongly ever becomes visible. Record it before replying, and never argue
first. Record what happened, never an intention: a reviewer who was suggested is
`flagged`, not `mentor`. The command exits 0 whatever happens and collapses
repeats within a day, so it can never cost the builder anything.

## Test bases and the bulk-write gate

Airtable sandboxes and real bases share a hostname and differ only by base ID,
so the gate cannot tell them apart. Rehearsing a backfill against a copy used to
get blocked for being careful. `memory trust-base` declares one base as a
sandbox; after that the gate is silent for it. Only use base IDs that appear in
the blocked command; never guess one.

## Repo transfers

Give these steps only after the builder says yes; they do not belong in the
block itself. Repo **Settings → Danger Zone → Transfer ownership → `thensls`**.
History and their ownership both survive. Two things to do in the same sitting:

- **Scan the git history for credentials first.** The org is on GitHub Team,
  which has no secret scanning for private repos, so nothing catches a key
  already sitting in an old commit. If history is dirty, squash to a fresh
  initial commit and push that instead of transferring.
- **Add reviewers explicitly afterwards.** The org's default repository
  permission is none, so teammates get a 404 until they are added.

## Reviewers

Kevin covers platform, architecture and anything member-facing, has final say,
and usually turns reviews round inside a day. Davo covers Tier 2/3 design,
skills and agentic flows. Jenna covers adoption, UX and HR-ops surfaces. Add a
domain reviewer when a build crosses into their flow.

## The exploration log ("log it") — full steps

The tracker used to hear about builds only when they shipped: of 97 rows, just 8
ever entered at an early stage. That is how Royce's knowledge base and Dejeahn's
BI tool got built in parallel. The exploration log moves the first record to the
moment a build becomes real, pitched as credit, never as paperwork.

**If they say yes** (about 15 seconds of their time):

1. **Check for neighbours, in both places.** The registry knows what has been
   registered; the machine knows what has been built. Most skills never get a
   registry row, so for anything skill-shaped the machine check is the one that
   finds the neighbour:

   ```
   bash "<toolkit>/hooks/run-hook.sh" similar "<name>" "<their one sentence>"
   ```

   Then the registry: `POST https://web-production-6281e.up.railway.app/similar`
   with JSON `{"name": "<name>", "description": "<their one sentence>"}`.
   - A match with `from_idea_backlog: true`: someone already parked this idea.
     Offer to claim that row (their builder link, Stage `Exploring`, their
     description) instead of creating a twin. Frame it as good news.
   - Another live match: mention it as an offer, never a wall ("Royce logged
     something adjacent in June; want me to draft him a quick we-might-overlap
     note?"). Their call entirely. Never send anything automatically.
   - Nothing: say nothing about the check. A clean result covers registered
     automations and skills installed on this machine only, so never tell a
     builder their ground is clear.
2. **Log it:** `POST https://web-production-6281e.up.railway.app/register-automation-with-builder`
   with JSON `{"automation": {"name": "<name>", "description": "<their one
   sentence>", "stage": "Exploring", "scope": "Personal", "github_repo_url":
   "<repo URL, if one exists>"}, "builder_email": "<their NSLS email>"}`. The
   email is `git config user.email` when it is an @nsls.org address. Ask none of
   the checklist, reviewer or design-doc questions: those belong to Tier 2/3
   shipping. Then report it:
   `bash "<toolkit>/hooks/run-hook.sh" event registered "<name> logged as an exploration"`.
3. **Close warm, in one line:** logged, credit recorded, and they own the row.

**Graduation, not re-registration.** When the build later escalates (someone
else uses it, it ships, it turns member-facing) the normal tier conversation
happens, but it updates their existing row. Nobody who logged an exploration
fills in a registration form; their exploration grows up.

**What this never becomes:** a second nag channel. One offer per build, the
decline sticks across sessions, and a declined log is not grounds to re-raise
registration unless the scope genuinely escalates into Tier 2/3 territory.

## Data rule

Raw school lists (for example NCO lead data) must run through the Bedrock PII
gate with zero data retention. NSLS's own member data (PostHog, HubSpot, Hex)
stays on Claude under a do-not-train policy. The gate itself is not built yet;
this is written policy, not yet enforced.
