---
name: interrogate
description: >-
  Interrogate a project or automation idea until goals, scope, risks, audience,
  and trade-offs are crystal clear. Produces an Obsidian project file, DESIGN.md,
  and Automation Tracker registration. Use when the user says "interrogate this",
  "new project", "new automation", "I have an idea", "help me scope this",
  "define this project", "what should I build", or is starting something new
  and needs to think it through before building. Also use when a builder wants
  to register an automation they've built but hasn't documented it yet.
  Fires on natural phrasings too — "I want to build something that…",
  "I'm starting a new automation", "I've got an idea for a tool",
  "can you help me build X", "thinking about automating Y" — this skill is
  the front door for new builds and classifies the guardrail tier before
  any code gets written.
---

# Interrogate

Interrogate the user relentlessly about every aspect of their project or automation until you reach a shared understanding. Walk down each branch of the decision tree — goals, scope, audience, stakeholders, dependencies, risks, metrics — resolving them one by one.

If a question can be answered by checking existing project docs or dashboards, have the user point you there and treat those as the source of truth instead.

**This skill does not write code.** It produces three outputs:
1. An **Obsidian project home note** (if the user uses Obsidian)
2. A **DESIGN.md** file (for automations with user-facing interactions)
3. An **Automation Tracker registration** (via the register-automation skill)

---

## Step 1: Establish Context

Ask these first — one at a time, not a wall of questions:

1. **"What are you building?"** — Get a plain-language description. No jargon.
2. **"Who is it for?"** — Specific people or roles, not "the team." How many? What's their tech comfort?
3. **"What problem does it solve?"** — What's painful today? What does the user currently do manually?

If the user already has a repo, scan it:
```bash
# Check for existing docs
ls README.md DESIGN.md CLAUDE.md docs/ 2>/dev/null
git remote -v 2>/dev/null
```

If they have existing docs, read them and skip questions they already answer.

---

## Step 1b: Classify the tier

The three answers above are usually enough to place the build. Do this now, not
at the end — it decides how much of the rest of this skill applies.

**Read `${CLAUDE_PLUGIN_ROOT}/_shared/references/guardrail-voice.md` before saying any of this aloud.**

| Tier | Test | Sets `Scope` |
|---|---|---|
| **1 · Personal** | Only they use the output. No member data. Nothing others depend on. | `Personal` |
| **2 · Team-useful** | Someone else would use it, or it touches internal NSLS data. | `Department` |
| **3 · Company-wide / member-facing** | Members see it, it wears NSLS's name, or it writes to a system of record at scale. | `Company-wide` |

If the first three answers left it ambiguous, ask **only** the question that
resolves it — not all three:

- "If this works, is anyone other than you going to use the output?"
- "Does it touch member data, or anything members will see?"
- "Does it write back to HubSpot, Customer.io, Airtable or Feather?"

**What changes by tier:**

- **Tier 1** — say nothing about registration. Offer it once, lightly, only if
  they'd want credit. Then get on with the interrogation.
- **Tier 2** — register it, light 1-pager. Suggest a mentor if effort looks like
  more than ~2 days. They can decline any of it; take the first no gracefully.
- **Tier 3** — register *before* code. Full design doc via `/product-design`.
  Reviewer assigned. Flag the platform check if it isn't Anthropic.

**Watch for escalation mid-conversation.** Builds routinely start as "just for
me" and turn out to be Tier 2 three questions later. When that happens, say so
plainly and move on — don't make it a moment.

**Also flag now, before any code exists:**
- Working in a repo whose `git remote` isn't an NSLS org → this will hard-block
  later. Far cheaper to move it now. `register-automation` has the transfer steps.
- Raw school lists (e.g. NCO lead data) → Bedrock PII gate, zero data retention.
  NSLS's own member data (PostHog, HubSpot, Hex) is fine on Claude under
  do-not-train.

---

## Step 2: Walk the Decision Tree

Go through each branch. Ask one question, wait for the answer, then follow up or move to the next branch. Don't rush.

### Goals
- What does success look like in 30 days? 90 days?
- What's the single most important outcome?
- How will you know it's working? (Not "metrics" — what would you observe?)

### Scope
- **Stage**: Is this an idea, prototype, production tool, or something already running?
- **Department**: Which NSLS department owns this? (People/HR, Client Services, Marketing, Finance, SLT, Product, Engineering)
- **Reach**: Personal (just you), Team (your department), Department+ (cross-team), Org-wide?
- What's explicitly NOT in scope? (This prevents scope creep later)

### Audience / Customers
For each user segment:
- Who are they? (role, name if known, count)
- When do they encounter this? (trigger, context, mindset)
- What do they actually do? (their actions, not the system's)
- What do they see back? (output, confirmation, result)

### Stakeholders & Collaborators
- Who else cares about this? (manager, sponsor, DRI)
- Who needs to approve before it ships?
- Who will maintain it after you move on?

### Dependencies
- What systems does this connect to? (Airtable, Slack, Google Workspace, Railway, etc.)
- What access do you need that you don't have?
- Is anything blocking you from starting today?

### Risks & Trade-offs
- What could go wrong? (data quality, user confusion, breaking existing workflows)
- What are you choosing NOT to do, and why?
- What's the rollback plan if it doesn't work?

### Metrics
- How will you measure adoption? (concrete: "X of Y people use it weekly")
- How will you measure satisfaction? (concrete: response rate, feedback, opt-out rate)
- What's a red flag that means you should stop or pivot?

---

## Step 2.5: Reuse check — search the open-source building-blocks catalog

Before synthesizing, check whether existing open-source projects could do part of the job — so
the plan is "what to build vs. what to adopt," not "build everything from scratch." This is
especially relevant for Society / Ignite Next (repo `ignite-next`) product work.

**Only run this if the builder maintains the personal OSS catalog** at
`$OBSIDIAN_VAULT_PATH/40-learning/oss-catalog.md` (created by the `/learn` skill). If the file
doesn't exist, skip this step silently — it's a personal-toolkit add-on, not a requirement.

1. From the Step 1–2 answers, derive 2–4 capability queries — the building blocks this project
   would need (e.g. `auth`, `push notifications`, `in-app chat`, `agent framework`, `gamification`,
   `content cms`, `analytics`).
2. For each, run the `/learn` skill's **Search OSS Catalog** mode (`/learn oss <query>`) against
   `oss-catalog.md`. (Operationally: grep/scan that file's `###` project blocks across name,
   what-it-does, relevance, and tags.)
3. Present any strong matches as *options to consider*, not prescriptions:
   ```
   Possible building blocks already in your catalog:
   - <Capability> → <Project> — <one line>. [repo](<url>) (status: <status>)
   Want to lean on any of these, or build it yourself? (This shapes scope + dependencies.)
   ```
4. Fold the builder's choices into the **Dependencies** and **Risks & Trade-offs** branches
   (an adopted dependency is a dependency *and* a risk). If nothing matches, say so briefly and
   move on — don't force a recommendation.

This step reads the catalog only; it never writes to it. New projects are added to the catalog
through `/learn inbox`, not here.

---

## Step 3: Synthesize and Confirm

After the interrogation, present a summary back to the user:

```
Here's what I understand:

**Project**: [name]
**Problem**: [one sentence]
**Solution**: [one sentence]
**Audience**: [who, how many]
**Scope**: [stage, department, reach]
**Success looks like**: [observable outcome]
**Key risk**: [biggest thing that could go wrong]
**Dependencies**: [systems, access, people]

Does this look right? What did I miss?
```

Wait for confirmation. Iterate if needed.

---

## Step 4: Write the Outputs

After the user confirms, produce all three outputs.

### 4A: Obsidian Project Home Note

Resolve the vault cross-platform — prefer `$OBSIDIAN_VAULT_PATH` (the same var
the rest of this skill and the personal toolkit already use), and fall back to
the macOS iCloud path. **Don't skip silently on Windows**: the old macOS-only
`-d` check always failed there, so Obsidian output was dropped with no message.

```bash
VAULT="${OBSIDIAN_VAULT_PATH:-$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/KP}"
if [ -d "$VAULT" ]; then
  echo "obsidian: $VAULT"
else
  echo "no Obsidian vault found — set OBSIDIAN_VAULT_PATH (or create the vault) to get the project note; skipping it for now"
fi
```

If a vault is found, create the project home note there. Use the template from
the `/log` skill:

```
$VAULT/20-projects/[slug]/[slug].md
```

**Frontmatter fields** (map from interrogation answers):

```yaml
---
title: "[Project Name]"
aliases:
  - "[Short Name]"
type: project
collaborators:
  - "[[Collaborator Name]]"
repos:
  - "thensls/[repo-name]"
status: [idea | active | paused | done]
priority: [1-5, where 1 is highest]
energy: [low | medium | high]
domain: [nsls | personal-productivity | slt-ops | people-ops | personal]
department: "[Department Name]"
tags:
  - [relevant-tag]
blocked-by: [dependency or empty]
next-action: "[First concrete step]"
last-touched: [today's date YYYY-MM-DD]
---
```

**Body sections:**
- **What This Is** — from Step 1 answers
- **Current Phase** — from scope/stage
- **Key Decisions** — from trade-offs discussed
- **Open Questions** — anything unresolved from the interrogation

Also create the first session log at `sessions/YYYY-MM-DD.md`.

### 4B: DESIGN.md

If the automation has user-facing interactions (Slack messages, emails, web UI, scheduled prompts), generate a DESIGN.md.

Read the full template at: `~/.claude/local-plugins/nsls-builder-toolkit/skills/product-design/references/design-md-template.md`

Map interrogation answers to template sections:
- **What This Is** ← Step 1 problem/solution
- **Customers** ← Step 2 audience segments
- **UX Principles** ← derive from audience context and risks (4-7 principles)
- **What This Should NOT Become** ← Step 2 scope exclusions and risks
- **Interaction Surface** ← Step 2 audience "what do they do / what do they see"
- **Measuring Success** ← Step 2 metrics

If the automation is purely backend (no user interaction), skip DESIGN.md and note why.

### 4C: Register in Automation Tracker — gated on tier

**Tier 1 builds do not go through full registration.** Routing them here
contradicted the Tier 1 rule ("say nothing about registration") one screen
after stating it. Instead, offer the **exploration log** once — the light,
credit-framed row from `${CLAUDE_PLUGIN_ROOT}/_shared/references/guardrail-playbook.md`
§ The exploration log: one sentence, stage
`Exploring`, no checklist or reviewer questions, decline recorded and never
re-raised. If they logged one earlier in this conversation, this step is
already done — say nothing.

**Tier 2 and Tier 3 continue below.** Use the `register-automation` skill to
register or update the automation in Airtable. Map fields:

| Interrogation answer | Tracker field |
|---------------------|---------------|
| Project name | `name` |
| Plain-language description | `description` |
| Department | `department` |
| Stage (idea/prototype/production) | `stage` |
| Reach (personal/team/dept/org) | `scope` |
| GitHub repo URL | `github_repo_url` |
| Builder name | `builder_name` |
| DESIGN.md exists | `design_intent: true` |

If the user doesn't want to register yet (it's just an idea), skip this step and note it as a next action.

---

## Step 5: Present and Confirm

Show the user what was created:

```
Created:
- Obsidian project: 20-projects/[slug]/[slug].md
- DESIGN.md: [repo path]/DESIGN.md (if applicable)
- Automation Tracker: [registered / skipped — register when ready]

Next action: [first concrete step from the interrogation]
```

---

## Tone

Be thorough but not tedious. If the user gives a long answer that covers multiple branches, acknowledge what's covered and skip those questions. If they say "I don't know yet," that's a valid answer — note it as an open question and move on.

The goal is shared understanding, not a form to fill out.
