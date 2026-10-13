---
name: register-automation
description: >-
  Register, update, or check an NSLS automation in the Automation Tracker.
  Use when the user says "register automation", "register this", "track this
  automation", "update automation tracker", "check automation status", "list
  automations", or is working in an NSLS automation repo and wants to record it.
---

# Register Automation

Register or update NSLS automations in the Automation Tracker and automatically
link the builder as owner/maintainer. The proxy handles points, stage
advancement, and assignment creation.

**Proxy URL**: `https://web-production-6281e.up.railway.app`

## How It Works

This skill is a thin layer over a smart proxy. The proxy handles:
- Finding/creating the automation record
- Linking the builder as Owner/Maintainer
- Calculating points for stage changes and checklist completions
- Checking and applying builder stage advancement (Explorer → Maker → Builder → Architect → Steward)
- Logging all events for audit

The skill's job: detect context, gather data, call the proxy, present results.

## Light path: log an exploration

For a build that is real but early — no ship, no team, just someone making
something — use the exploration log instead of full registration. The offer,
the once-per-build rule, and the decline handling live in
`${CLAUDE_PLUGIN_ROOT}/_shared/references/guardrail-playbook.md` § The
exploration log. Mechanics: `POST /similar` first (offer to claim an
idea-backlog row or an intro on a live match), then
`POST /register-automation-with-builder` with `stage: "Exploring"`,
`scope: "Personal"`, and their one-sentence description. Ask none of the
checklist/reviewer/design-doc questions. Later escalation updates the same row
via the full flow below.

## Step 1: Detect the Builder

Auto-detect who's registering. Try these in order:
1. `git config user.email` — most reliable
2. Read email from `~/.claude/local-plugins/nsls-personal-toolkit/.env` or `~/.claude/local-plugins/nsls-builder-toolkit/.env`
3. Ask: "What's your NSLS email?"

Verify the builder exists: `POST /find-builder` with `{"email": "<detected>"}`.
If not found, offer to register them: `POST /builder` with their details.

## Step 2: Detect the Automation

### In a git repo (auto-detect mode)

Scan the repo:
- `git remote -v` → GitHub URL, repo name
- **`code_on_github` means the NSLS org owns the code — not that git is in use.** Set
  `code_on_github: true` only when the `origin` remote is `github.com/thensls/<repo>`
  (SSH or HTTPS). A personal-account remote is `false`. Getting this wrong is how a
  Production automation ends up registered as org-owned while its code sits in someone's
  personal account — which is the exact governance gap the checklist exists to surface.

  When the remote is personal, say so plainly and hand over the fix:
  > Your remote is `<owner>/<repo>`, not in the `thensls` org, so this registers as *not*
  > org-owned. To transfer: repo **Settings → Danger Zone → Transfer ownership → `thensls`**.
  > Org members have repo-create rights, so you don't need an admin. Two things to do in
  > the same sitting:
  > - **Scan the git *history* for credentials before you transfer.** The org is on GitHub
  >   Team, which has no secret scanning for private repos — nothing will catch a key
  >   that's already sitting in an old commit. If history is dirty, squash to a fresh
  >   initial commit and push that instead of transferring.
  > - **Add your reviewers explicitly.** The org's default repository permission is `none`,
  >   so transferring alone gives teammates zero access — they'll get a 404 until you add
  >   them as collaborators.
- `README.md` exists? → `readme: true`
- `CLAUDE.md` exists (root or `.claude/`)? → `claude_md: true`
- `docs/runbook.md` or `runbook.md`? → `runbook: true`
- `docs/architecture.md` or `architecture.md`? → `architecture_doc: true`
- `DESIGN.md`? → `design_intent: true`
- `railway.toml` or `Procfile`? → `deployed_shared_infra: true`
- `gh api repos/{owner}/{repo}/collaborators --jq length` > 1? → `collaborators_on_repo: true`

Check if it's already tracked: `POST /find` with `{"name": "<repo name>"}`.

If found: note the current `stage` as `previous_stage`.
If not found: this is a new registration.

Ask the user only for fields you can't detect:
- **Required if new**: description
- **Recommended**: department, scope, type, stage
- **Optional**: everything else

### Guardrail fields (set these alongside scope)

`scope` doubles as the guardrail tier — `Personal` = Tier 1, `Department` =
Tier 2, `Company-wide` = Tier 3, and **both `Customer Facing` scopes
(`Advisors`, `Members`) = Tier 3** — they exist in the tracker and are the most
consequential builds there are; leaving them unmapped let member-facing work
skip every Tier 3 requirement. Four fields hang off the tier:

| Field | When to set |
|---|---|
| `Platform Used` | Always. `Anthropic` (default) / `OpenAI` / `Other`. |
| `Design Doc URL` | Tier 2+. Link the doc `/product-design` produces. |
| `Reviewer` | Tier 3 always; Tier 2 when effort > ~2 days. Links to the Builders table. |
| `Review Status` | Tier 2+. `Not needed` → `Requested` → `In review` → `Go` / `Go with notes` / `Pull in another reviewer` / `Slow down`. |

**Reviewer pool:** Kevin (platform, architecture, member-facing — final say) ·
Davo (Tier 2/3 design, skills, agentic flows) · Jenna (adoption, UX, HR-ops
surfaces) · a domain reviewer when the build crosses into their flow.

**Platform check.** If `Platform Used` is not `Anthropic` and scope is
`Department` or `Company-wide`, that's a hard gate — it needs a short written
memo plus Kevin's sign-off. Say so, and offer to draft the memo in the same
breath. Never a flat no.

**Read `${CLAUDE_PLUGIN_ROOT}/_shared/references/guardrail-voice.md` before raising any of this.**

### Not in a repo (manual mode)

Ask for:
- **Required**: name, description
- **Recommended**: department, scope, type, stage
- **Optional**: everything else

## Step 3: Register

Call `POST /register-automation-with-builder`:

```json
{
  "automation": {
    "name": "...",
    "stage": "...",
    "department": "...",
    "...all detected and user-provided fields"
  },
  "builder_email": "jdoe@nsls.org",
  "previous_stage": "Prototype"
}
```

### Record it as a guardrail event

Straight after a successful registration, before presenting results:

```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guardrail-event.py" registered \
  "<automation name> registered at Tier <1|2|3> — <a guardrail raised it | volunteered>" \
  --automation "<repo or name>"
```

Every registration, not only the ones a guardrail prompted. A registered build
is a build somebody owns, which is the whole point; and Signal reads the absence
of this event as "declined and carried on", so a voluntary registration that
goes unreported makes a careful builder look like a careless one. Say in the
description whether a guardrail prompted it — that distinction is worth having,
but it belongs in the sentence, not in whether the row exists.

## Step 4: Present Results

From the response, show:

**Always:**
> Registered **[Name]** at [Stage]. You're listed as owner/maintainer.

**If checklist items were updated:**
> Updated org-owned checklist: README, CLAUDE.md, GitHub repo confirmed.

**If stage_advanced is not null:**
> This moves you from [from] to **[to]** — [encouragement based on stage]:
> - Maker: "You're getting your hands dirty."
> - Builder: "You've shipped something real."
> - Architect: "You're building systems that outlast you."
> - Steward: "You own it. Others depend on it."

**Always — close with what's still open.** This is the part that makes the checklist
work. Registration currently ends on a completed note, so builders walk away thinking
they're done and the remaining items go untouched for weeks. Leave the loop open.

Call `GET /builder-stats/{builder_email}`, find the automation you just registered, and
read `checklist_complete`, `checklist_total`, and `checklist_remaining`. Then show:

> Checklist: **7 of 12.** Still open:
> - **Collaborators on Repo** — teammates can't review until you add them
> - **Service Account Creds** — so it isn't running on your personal credentials
> - **Deployed on Shared Infra** — off your own account, onto shared
> - **🎰 Won the Lottery** — could someone else run this if you disappeared tomorrow?
> - **DESIGN.md** — why this exists, for whoever inherits it

Two rules for how you say it:

1. **Frame these as what makes the automation survivable, not as a debt owed.** The
   builder just shipped something; this is the difference between a tool and an asset.
2. **Name which open items their current work already covers.** Most builders are
   mid-migration or mid-deploy, and two or three of the open items are things they're
   doing this week anyway — pointing that out converts a list into momentum. If a stage
   or scope answer implied upcoming work (a cutover, a handoff, a new environment), say
   which boxes it will tick.

**Key → label.** Mirrors the tracker's checkbox fields. Title-case any key not listed
here rather than dropping it — the field set grows.

| Key | Label |
|---|---|
| `code_on_github` | Code on GitHub (in the `thensls` org) |
| `readme` | README |
| `claude_md` | CLAUDE.md |
| `runbook` | Runbook |
| `architecture_doc` | Architecture Doc |
| `service_account_creds` | Service Account Creds |
| `deployed_shared_infra` | Deployed on Shared Infra |
| `env_vars_documented` | Env Vars Documented |
| `collaborators_on_repo` | Collaborators on Repo |
| `handoff_guide` | Handoff Guide |
| `won_the_lottery` | 🎰 Won the Lottery |
| `design_intent` | DESIGN.md |

**Do NOT show points to the builder.** Points are Kevin's internal metric. The checklist
count is different — **do** show that. Completion is the builder's own signal; points
are not.

## Checking Status

If the user asks "what automations do we have" or "show me the tracker":
- `GET /automations` to list all
- Present as a table: name, stage, department, type

If the user asks "what are my automations":
- `GET /builder-stats/{email}` to get their portfolio
- Show automations with checklist progress

## Scope Change Check — the tier gate

Scope changes are how Tier 1 builds become Tier 2 and Tier 3 ones, so this is
where most guardrail conversations actually happen.

When scope rises (Personal → Department, Department → Company-wide, any →
Customer Facing), gate on design-doc depth **before** letting `stage` advance
past `Idea`:

| New scope | Depth needed | Blocking? |
|---|---|---|
| `Department` (Tier 2) | Light — 1-pager, 20–30 min | No. Strong suggestion; take the first no gracefully and log it. |
| `Company-wide` (Tier 3) | Standard or Extensive | **Yes.** Registration must exist before code, and a reviewer must be assigned. |

**Tier 2: offer, then honor the answer.** Suggest the 1-pager once; if they
decline, record it —

```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guardrail-memory.py" record design-doc --note "<their reason>"
```

(ships in part 3 of this set; recording it also emits the `guardrail_proceeded`
event the declined count is built from) — and leave
`Design Doc URL` empty and `Review Status` at `Not needed` — running
`/product-design` anyway is exactly the soft-path violation the tier table
forbids. **Tier 3: not optional.** Run `/product-design` in Generate Mode,
write the URL to `Design Doc URL`, set `Review Status` to `Requested`.

**Tier 3 hard gate.** A Company-wide or member-facing automation may not advance
to a shipping stage without a tracker record and an assigned `Reviewer`. State
the policy, then offer the authorization route — Kevin can authorize an
exception, and you should offer to draft that note immediately. Never a flat no.

**Emit the event either way** (see Guardrail Events below) — a declined
suggestion is as worth recording as an accepted one.

## Guardrail Events

Every guardrail moment gets an `Events` row so it can be reported on. `Event Type`
is free text, so no schema change is needed; Signal reads it via the existing
15-minute Airtable→Supabase sync.

**How to record one.** One command, from anywhere:

```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guardrail-event.py" <label> "<what happened>" [--automation <name>]
```

`<label>` is the bare word — `mentor`, `disputed`, `registered`. It prints one
line saying what it did and always exits 0; a tracker that is down or slow is
never a reason to interrupt what the builder is doing. Two it handles on its own,
so don't send them by hand: `blocked` comes from the gate itself, and
`proceeded` from `guardrail-memory.py record` when a builder declines.

Only the labels below are accepted. The proxy checks the `guardrail_` prefix but
Signal checks the exact string, so an invented label would write a real Airtable
row that the dashboard silently ignores — reporting that looks like it worked.
The command refuses unknown labels instead.

| Event Type | When |
|---|---|
| `guardrail_flagged` | Claude raised a tier flag |
| `guardrail_registered` | Build registered — note in the description if a flag prompted it |
| `guardrail_mentor` | Reviewer or mentor brought in |
| `guardrail_migrated` | Repo moved to the NSLS org, or platform moved to Anthropic |
| `guardrail_proceeded` | Builder declined; build continued (soft path) |
| `guardrail_blocked` | A hard gate stopped the action |
| `guardrail_authorized` | Kevin authorized an exception to a hard gate |
| `guardrail_disputed` | Builder says a block misfired — **log it, don't argue** |

Set `Description` to one plain sentence naming the build and what happened, and
link `Builder` and `Automation` where known. These four counts — acted on,
declined, hard blocked, authorized — are what the guardrail report shows.

**Only record what actually happened.** A repo move is observable. Whether a
reviewer genuinely reviewed, or whether someone really migrated off OpenAI, is
not — those are self-reported. Never emit `guardrail_migrated` or
`guardrail_mentor` on the strength of an intention.

### Disputed blocks — the feedback loop

Some gates will misfire in situations nobody could simulate, and a builder who
hits a wrong block with no way to say so quietly stops trusting the toolkit.

When a builder says a block was wrong, record it immediately, before replying:

```
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/guardrail-event.py" disputed \
  "bulk-write gate fired on a 30-row backfill into her own test base — she was rehearsing before the real run"
```

Put in the description what they were trying to do, which gate fired, and their
reason in their own words. Then help them get where they were going — the
authorization route is still open, and a disputed block is not an argument to
win. **Never push back before logging it.**

These surface in the guardrail report's Needs-attention list, which is the only
channel through which a false positive ever becomes visible. Treat a rising
dispute count as the system working, not failing.

## API Reference

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/similar` | Rank existing registry rows by similarity to a proposed build (name + description). Top 3, score ≥ 0.5; `from_idea_backlog` flags claimable Idea rows. Read-only. |
| POST | `/find` | Search automations by name |
| POST | `/find-builder` | Find builder by email/Slack ID/GitHub |
| POST | `/register-automation-with-builder` | Main smart endpoint |
| GET | `/automations` | List all (optional `?stage=` or `?department=`) |
| GET | `/builder-stats/{email}` | Builder's automation portfolio |
| POST | `/builder` | Register a new builder |
