---
name: kw:brainstorm
description: "Brain dump and compile NSLS knowledge before structuring a plan. Use when starting any non-trivial strategic or knowledge work — after a meeting, when tackling a new problem, when you have scattered inputs that need organizing. Trigger phrases: brainstorm, brain dump, help me think through this, let me brain dump, I need to figure out, kw:brainstorm, /kw:brainstorm. This is for strategy/campaigns/knowledge work — NOT for new automations (use /interrogate) and NOT for software features (use /ce-brainstorm)."
argument-hint: "[topic, brain dump, or meeting notes]"
---

<brain_dump> #$ARGUMENTS </brain_dump>

# /kw:brainstorm — Think Before You Plan

Get everything out of your head and into one place. Pull in what NSLS already knows. Find the shape of the problem before committing to a plan.

## When to Use

- After a meeting where next steps need to be figured out
- Starting a new campaign, strategy, initiative, or investigation
- "I need to think through X", "let me brain dump", "help me figure this out"
- When you have scattered inputs (meeting transcripts, notes, links) that need organizing

## When NOT to Use

- Building a new automation → use `/interrogate` (produces DESIGN.md + project scaffolding)
- Defining a concept without a name → use `/full-shape`
- Software feature work → use `/ce-brainstorm` from Compound Engineering
- Simple how-to questions → just answer directly

## Process

### Step 1: Capture the brain dump

Accept whatever the user gives you. A pasted transcript, bullet points, half-formed thoughts, a link to a document, "here's what I'm thinking about."

**Do not organize yet.** Acknowledge what you received and identify the input type.

If nothing was passed in, prompt:

> "What are you working on? Paste meeting notes, describe the problem, or just start talking. I'll help organize it."

### Step 2: Extract the core elements

From the brain dump, pull out:

- **Key decisions** that need to be made
- **Open questions** that don't have answers yet
- **Constraints** (timeline, budget, dependencies, blockers)
- **Stakeholders** and what they care about
- **Data points** mentioned (numbers, metrics, sources)
- **Ideas and options** that were floated (even half-baked ones)

Present back as a structured summary:

```
## What I Heard

**The problem:** [One sentence — what are we trying to figure out?]

**Decisions to make:**
- [Decision 1]

**Open questions:**
- [Question 1]

**Constraints:**
- [Timeline, budget, dependencies]

**Stakeholders:**
- [Who] — [what they care about]

**Ideas floated:**
- [Idea 1] — [brief note on pros/cons if mentioned]

**Data points mentioned:**
- [Any numbers, metrics, or sources cited]
```

Use the user's language. Don't sanitize their thinking into corporate-speak.

### Step 3: Pull in references (parallel)

Launch the `knowledge-researcher` agent in parallel with any other lookups you need. Pass the topic description plus keywords extracted from Step 2.

```
Task(
  subagent_type="nsls-builder-toolkit:knowledge-researcher",
  prompt="Brainstorm context for: <one-sentence problem from Step 2>
  Keywords: <5-7 keywords from the brain dump>"
)
```

This searches:
- **Tier 1 (always):** LOPs, strategy memo, org chart
- **Tier 2 (SLT):** `60-nsls-knowledge/` topic files + one-hop related topics
- **Tier 3 (Obsidian users):** operating memo, active learning goals, recent project decisions, saved resources

**Also consider:** if the topic is outward-facing (pricing, competitors, industry), offer to call `/data-intel` for live system data (PostHog, HubSpot, Customer.io, Snowflake). Don't run it by default — ask.

**Surface findings:**

```
## What NSLS Already Knows

**LOPs touched by this topic:**
- [L1/L2 name, owner, health, deadline — one-liner]

**Institutional knowledge:**
- [tier 2 topic file] — [key decision or current state]
- [or: "No tier 2 matches"]

**Personal context:**
- [operating memo I Don't / My Traps overlap, if any]
- [active learning goals related]
- [recent project decisions]

**Not yet synthesized:** [topics the knowledge graph flagged as present-but-unpopulated]
```

If the agent returns nothing relevant, say so. Don't fabricate context.

### Step 4: Identify themes, tensions, gaps

Look across the brain dump + references. Call out:

- **Themes** — what keeps coming up? What's the real underlying question?
- **Tensions** — where do ideas conflict? Where are the tradeoffs?
- **Gaps** — what's missing? What hasn't been addressed?

```
## Themes
1. [Theme] — [why it matters]

## Tensions
- [Option A] vs [Option B] — [the tradeoff]

## Gaps
- [What we don't know yet]
- [What needs more research]
```

### Step 5: Resolve load-bearing questions

Take the open questions and tensions and pick out ones where different answers would lead to different plans. Those are load-bearing. Ignore the rest until execution.

Use AskUserQuestion to ask **1–3 load-bearing questions at once** (never more than 3). Frame each with options drawn from what surfaced — not open-ended.

Common load-bearing questions:
- **Scope:** "Quick win or multi-phase initiative?"
- **Audience:** "Who is this for?"
- **Priority:** "You mentioned X and Y — which matters more if we have to choose?"
- **Timeline:** "Days, weeks, or months?"
- **Owner:** "Who's making the final call?"

If all open questions are non-load-bearing, skip this step:

> "The open questions won't change the plan's shape — we can resolve them during execution."

### Step 6: Suggest a direction

Offer a point of view:

> "Based on what I'm seeing, the core question is [X]. The main tension is between [A] and [B]. My suggestion would be to [direction] because [reasoning]. But [caveat]."

This is a suggestion, not a decision. The user decides.

### Step 7: Offer next steps

Use AskUserQuestion:

**Question:** "Brainstorm captured. What next?"

**Options:**
1. **Run `/kw:plan`** — Structure this into an actionable plan
2. **Dig deeper** — Research a specific theme or question further
3. **Save and continue later** — Write to `plans/brainstorm-{descriptive-name}.md`
4. **Keep going** — Add more context or refine the themes

<critical_requirement>
If "Save" or "Run /kw:plan" is selected: ALWAYS write the brainstorm to `plans/brainstorm-{descriptive-name}.md` first. This file becomes the origin document for `/kw:plan` to reference. If `plans/` doesn't exist in the current working directory, create it. Never skip the file write.
</critical_requirement>

## Writing Style

Match Kevin's voice (from `~/.claude/skills/kevin-voice/` if present, otherwise default):
- Plain language, not corporate-speak
- Numbers > adjectives
- Short sentences
- "We" not "the organization"
- No words like: leverage, synergies, robust, learnings, ecosystem, holistic

## Important Rules

- **Don't jump to solutions.** The point of brainstorming is to understand the problem space before committing to a path. Resist the urge to plan.
- **Reflect, don't rewrite.** When summarizing back, use the user's language.
- **Pull, don't push.** Ask where references might live rather than guessing. The user knows their information landscape better than you do.
- **Surface tensions early, resolve the load-bearing ones late.** Don't collapse tensions prematurely.
- **Quantity of input is fine.** A 30-minute meeting transcript is good input. Don't ask people to pre-organize — that defeats the purpose.
