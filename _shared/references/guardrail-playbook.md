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

## The exploration log ("log it")

The tracker used to hear about builds only when they shipped: of 97 rows, just 8
ever entered at an early stage. That is how Royce's knowledge base and Dejeahn's
BI tool got built in parallel. The exploration log moves the first record to the
moment a build becomes real, pitched as credit, never as paperwork. As a
paragraph Claude had to remember, it was offered zero times in the org; it now
runs from a checkpoint hook.

**When it asks.** Once a session has made about six changes (edits, commands,
connector writes) across three builder messages, or at the first message of a
later session on a build that already had one, the hook hands Claude the
question with the builder's next message. The builder does not see it. Volume
only makes the question eligible: Claude decides whether the work is worth
credit, because only Claude has the conversation.

**How Claude answers**, through `bash "<toolkit>/hooks/run-hook.sh" logit ...`
(the hook prints each command with the session and build filled in):

- `offer` runs both neighbour checks: the skills installed on this machine (what
  has been built) and the tracker's similar-builds search (what has been
  registered). It prints the offer for the builder, with any hit worded as an
  offer to coordinate (a parked idea to claim, a colleague to draft a
  we-might-overlap note to, a skill covering the same ground), then the two
  commands for their answer. No hit means saying nothing about the check: the
  two checks see registered automations and local skills only, so never tell a
  builder their ground is clear. Nothing is ever sent to anyone automatically.
- `accept` logs it at Stage `Exploring`, scope Personal, with the builder as
  owner and the credit that comes with registering. If it matched a parked idea,
  it claims that row instead of making a twin. If the tracker does not answer,
  the log is queued and sent at a later message.
- `decline` records the no; that build is never offered again on this machine.
- `not-yet` records Claude's judgment that it isn't worth offering yet. Nothing
  is shown to the builder. It is asked again only after another real stretch of
  work, at most three times a month per build.

An unanswered question gets one reminder at the end of the turn, then is
recorded as ignored. A git repo is one build, so one answer settles it. A plain
folder can hold several, so the question lists what has been settled there and
Claude judges whether today's work is one of them.

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
