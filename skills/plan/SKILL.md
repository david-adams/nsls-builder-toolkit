---
name: kw:plan
description: "Research what NSLS already knows, then structure a plan grounded in LOPs and past learning. Use whenever someone asks to plan, write a plan, or draft a brief or strategy for NSLS work: load this skill first, before starting any document or artifact, because it pulls in what NSLS already knows, and the finished plan can become a doc afterwards. Also use after /kw:brainstorm when ready to commit to a direction, or for a campaign plan, research synthesis or operational playbook. Trigger phrases: plan, write a plan, help me plan, let's plan, plan the X, structure this, I need a brief for, draft a brief, make a strategy for, kw:plan, /kw:plan. This is for strategic/knowledge work — NOT for new automations (use /interrogate) and NOT for software features (use /ce-plan)."
argument-hint: "[what to plan]"
---

<plan_request> #$ARGUMENTS </plan_request>

# /kw:plan — Lead With the Answer

Research what NSLS already knows, then structure a plan grounded in LOPs and data. Lead with the recommendation, not the analysis.

## When to Use

- After `/kw:brainstorm`, when you've committed to a direction
- Starting a new strategy doc, campaign plan, research synthesis, brief, or playbook
- "Plan the Q3 campaign", "I need a brief for X", "let's structure this"

## When NOT to Use

- Building a new automation → `/interrogate`
- Software feature work → `/ce-plan`
- One-off how-to question → just answer

## Process

### Step 1: Classify the work type (auto-detect, don't ask)

| Type | Signals |
|------|---------|
| **Strategy** | Roadmap, architecture, long-term, layers, phases |
| **Campaign** | Launch, promotion, timeline, channels, audience |
| **Brief** | Directive for someone else, scope, deliverables |
| **Research** | Investigation, competitive analysis, synthesis |
| **Operations** | Playbook, runbook, SOP, recurring process |

Pick the best fit. Default to Strategy if unclear. Do not ask the user to classify.

Also pick a **detail tier** from scope signals:

| Tier | Signals | Output |
|------|---------|--------|
| **Quick** | "should we?", "gut check", single question, <30 min scope | Title, recommendation, 2-3 bullets, one success metric |
| **Standard** | Default — most plans | Full template for the work type |
| **Deep** | "restructure", "strategy", "multi-quarter", "fundamental" | Full template + research appendix, risk matrix, phased timeline |

Default to Standard if unclear.

### Step 2: Research (parallel)

Launch the `knowledge-researcher` agent — always. This single call covers all three tiers.

```
Task(
  subagent_type="nsls-builder-toolkit:knowledge-researcher",
  prompt="Planning context for <work type>: <plan request>
  Keywords: <5-7 extracted keywords>"
)
```

**Also check (inline, not parallel agents):**

1. **Origin brainstorm** — glob `plans/brainstorm-*.md` matching the topic. If found, reference it throughout as `(see origin: plans/brainstorm-{name}.md)` and cross-check that the plan addresses tensions and load-bearing questions from the brainstorm.

2. **Live data from connected systems** — if the plan involves current metrics (enrollment numbers, campaign performance, user behavior), offer to call `/data-intel` or a domain skill (`/posthog`, `/hubspot`, `/customerio`, `/airtable`). Don't run by default — ask:
   > "This plan would benefit from current numbers. Want me to pull live data from [likely system] via `/data-intel`?"

3. **External/industry research** (only if outward-facing) — call `/web-research` for frameworks, competitive examples, best practices. Skip for internal operations.

Wait for the knowledge-researcher result before proceeding.

### Step 3: Surface the context brief

Before writing anything, present what you found:

```
## What I Found

**LOPs this plan is grounded in:**
- [L1/L2 name, owner, health, deadline] — [how the plan connects]

**Past learnings:**
- [`_shared/learnings/` entry — name + type (correction / playbook / insight / pattern)] — [quoted `## Implication`]
- [tier 2 topic file or tier 3 project] — [the insight]

**Current data:** (if data was pulled)
- [metric]: [value] ([source, date])

**External research:** (if run)
- [finding] — [source]

**Origin document:** (if found)
- `plans/brainstorm-{name}.md` — [summary of tensions it raised]

**Not yet synthesized:** [topics where tier 2 is present-but-unpopulated — gaps worth flagging]
```

Wait for the user to react. They may refine direction, add context, or say "looks good, go."

### Step 4: Structure the plan

Use the template matching the work type. Every plan ends with the same three common sections:

```markdown
## Success Metrics

| Metric | Current Baseline | Target | Source |
|--------|-----------------|--------|--------|
| [Primary metric] | [value] | [goal] | [where to measure] |

## Open Questions

- [What we don't know yet]
- [Decisions that need to be made]

## LOP Alignment

- **Primary LOP:** [L1 or L2 name] — [how this plan advances it]
- **Secondary LOPs touched:** [list]
- **No LOP alignment:** [be explicit if this plan doesn't ladder to a current LOP — that's a signal]

## References

- [Knowledge graph entries, origin brainstorm, data sources used]
```

---

**Strategy** — Pyramid Principle. Lead with the recommendation.

```markdown
# [Plan Title]

**Type:** Strategy
**Status:** Draft
**Created:** [today's date]

---

## Recommendation

[One paragraph: what we should do and why. Lead with the answer.]

## Current State

[What's true right now. Data with source and date for every number.]

## Proposed Approach

[How to get from current state to the desired outcome. Layers or phases.]
```

---

**Campaign** — Timeline-first.

```markdown
# [Plan Title]

**Type:** Campaign
**Status:** Draft
**Created:** [today's date]

---

## Timeline

| Date/Week | Action | Channel | Owner |
|-----------|--------|---------|-------|
| [date] | [what launches] | [where] | [who] |

## Goal

[One paragraph: what this campaign achieves and how we'll know it worked.]

## Audience

[Who this targets. Segment, persona, or behavioral description.]

## Assets Needed

- [Copy, creative, landing pages, emails]

## Current State

[Relevant baselines before we start.]
```

---

**Brief** — Directive-first.

```markdown
# [Plan Title]

**Type:** Brief
**Status:** Draft
**Created:** [today's date]

---

## Recommendation

[One paragraph: what we should do and why.]

## Scope

[What's in and what's out. Explicit boundaries.]

## Deliverables

- [Concrete output]

## Constraints

[Timeline, budget, dependencies, blockers.]

## Context

[Background the reader needs.]
```

---

**Research** — Findings-first.

```markdown
# [Plan Title]

**Type:** Research
**Status:** Draft
**Created:** [today's date]

---

## Key Findings

1. **[Finding]** — [One sentence with data. Source and date.]
2. **[Finding]** — [One sentence with data. Source and date.]
3. **[Finding]** — [One sentence with data. Source and date.]

## Implications

[What these findings mean for NSLS. What should change.]

## Methodology

[How you gathered this. Sources, timeframes, filters, caveats.]

## Raw Data

[Tables, charts, or links to dashboards.]
```

---

**Operations** — Trigger-first.

```markdown
# [Plan Title]

**Type:** Operations
**Status:** Draft
**Created:** [today's date]

---

## Trigger

[When does this run? Schedule, event, or request?]

## Steps

1. [Step] — [details, tools, commands]

## Edge Cases

| Situation | What to do |
|-----------|-----------|
| [When X happens] | [Do Y] |

## Owner

[Who runs this. Who to escalate to.]

## Dependencies

[What this process needs — access, tools, data sources.]
```

### Step 5: Write to plans/

- Filename: `plans/{type}-{descriptive-name}.md` in the current working directory
- If `plans/` doesn't exist, create it
- If filename already exists, append date: `plans/{type}-{name}-{YYYY-MM-DD}.md`
- For personal strategic work (not tied to a repo), offer to write to `$OBSIDIAN_VAULT_PATH/20-projects/{name}/{name}.md` instead

Always write the file BEFORE presenting next-step options.

### Step 6: Offer next steps

Use AskUserQuestion:

**Question:** "Plan written to `plans/{filename}`. What next?"

**Options:**
1. **Run `/kw:review`** — Check strategic alignment and data accuracy before sharing
2. **Share with stakeholders** — Draft a Slack message or email summarizing the plan
3. **Register as automation** — If this is an operations playbook, run `/register-automation`
4. **Refine** — Adjust specific sections
5. **Done** — Close the loop

## Writing Style

Match Kevin's voice (from `~/.claude/skills/kevin-voice/` if present):
- Plain language, not corporate-speak
- Numbers > adjectives
- Declarative headlines ("We missed revenue by $5M", not "Revenue Review")
- Short sentences
- "We" not "the organization"
- No: leverage, synergies, robust, learnings, ecosystem, holistic

## Important Rules

- **Lead with what the reader needs first.** Strategy/Brief: the recommendation. Campaign: the timeline. Research: the findings. Operations: the trigger and steps. If someone only reads the first section, they should get the most important thing.
- **Cite everything.** Every data point needs a source and date. "+32% WoW (PostHog, 2026-04-15)" not "+32%".
- **Surface past work.** The knowledge-researcher findings MUST appear in the context brief. If it came back empty, say so — that's useful information, not a gap to paper over.
- **Ground in LOPs.** Every plan has an `## LOP Alignment` section. If the plan doesn't ladder to a current LOP, say so explicitly — that's a signal worth surfacing, not hiding.
- **Don't over-template.** Skip sections that don't apply. A campaign plan doesn't need an "Architecture" section.
- **Degrade gracefully.** If a data source fails or a tier is unavailable, proceed with what you have. Note what's missing.
