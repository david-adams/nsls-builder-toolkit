---
name: kw:review
description: "Review an NSLS plan, brief, or strategy doc for strategic alignment and data accuracy before sharing with stakeholders. Runs two reviewers in parallel — one checks LOP grounding, goals, hypothesis, metrics, scope; the other audits every number for source, date, and freshness. Returns merged P1/P2/P3 findings. Trigger phrases: review this plan, review for alignment, check data accuracy, kw:review, /kw:review, is this ready to share, review before leadership. NOT for code review (use ce-code-review)."
argument-hint: "[path to plan, or leave empty to review the most recent plans/*.md]"
---

<review_target> #$ARGUMENTS </review_target>

# /kw:review — Gate Before Sharing

Run two reviewers in parallel. Surface what would embarrass you in front of a skeptical executive — before they read it.

## When to Use

- After `/kw:plan`, before sharing the plan with SLT / board / stakeholders
- On any existing plan, brief, or strategy doc that's about to go to leadership
- When a plan has been sitting in draft for a while and you want a sanity check

## When NOT to Use

- Code review → `/ce-code-review` or `/code-review`
- Security audit → `/security-review`
- Plan still in "ideas" stage → run `/kw:plan` first, then review the structured output

## Process

### Step 1: Identify the target

If an argument was passed, use it as the file path.

Otherwise:
- Glob `plans/*.md` in the current working directory
- If multiple files exist, take the most recently modified
- If nothing exists in `plans/`, check `$OBSIDIAN_VAULT_PATH/20-projects/` for recently touched project notes with a "Recommendation" or "Key Findings" section
- If nothing matches, ask the user for a path

Confirm the target with the user before proceeding:

> "Reviewing `plans/strategy-q3-enrollment.md` (modified 2 hours ago). Go?"

### Step 2: Run both reviewers in parallel

<parallel_tasks>

**2a. Strategic alignment** — Launch Task agent:

```
Task(
  subagent_type="nsls-builder-toolkit:strategic-alignment-reviewer",
  prompt="Review <path> for strategic alignment. Check LOP grounding, goal clarity, hypothesis falsifiability, success metrics, scope, operating-memo fit."
)
```

**2b. Data accuracy** — Launch Task agent:

```
Task(
  subagent_type="nsls-builder-toolkit:data-accuracy-reviewer",
  prompt="Review <path> for data accuracy. Audit every number for source, date, freshness, NSLS system-of-record match, and consistency."
)
```

</parallel_tasks>

Both agents return structured findings. Wait for both before proceeding. Neither writes files.

### Step 3: Merge findings

Combine the two reviewers' outputs into one report. Deduplicate — if both flag the same issue (e.g., a metric that's both unsourced AND contradicts an LOP), merge into a single finding that credits both reviewers.

Group by severity across both:

```
# Review: [artifact name]

**Reviewed:** [today's date]
**Reviewers:** strategic-alignment, data-accuracy
**Artifact path:** [path]

## P1 — Blocks shipping ([count])

### [Issue title]
- **Lens:** Strategic / Data / Both
- **Where:** [section name]
- **Finding:** [specific issue]
- **Fix:** [suggestion]

## P2 — Should fix ([count])

### [Issue title]
- **Lens:** [strategic | data | both]
- **Where:** [section name]
- **Finding:** [specific issue]
- **Fix:** [suggestion]

## P3 — Nice to have ([count])

- [condensed list]

## Clean

- [What passed — credit by lens. Example: "LOP Alignment section correctly cites L2 'Grow total enrollment' (verified against lops.md, synced 2026-04-13)."]

## LOP Verification

[From strategic-alignment-reviewer's LOP Verification block]

## Data Cross-Check

[From data-accuracy-reviewer's LOP Cross-Check block]

## Coverage

- Tiers cross-referenced: [1, 2, 3]
- Context files read: [N]
- Gaps: [any tier or data source unavailable]
```

### Step 4: Offer next steps

Use AskUserQuestion:

**Question:** "Review complete. [N] P1 / [N] P2 / [N] P3 findings. What next?"

**Options:**
1. **Apply fixes** — Walk through P1s one at a time and edit the artifact
2. **Save review** — Write the merged report to `plans/review-{artifact-name}-{YYYY-MM-DD}.md` (next to the original)
3. **Share anyway** — If findings are minor, proceed to drafting a share message
4. **Refine the plan and re-review** — Run `/kw:plan` again with the findings as input
5. **Done** — Close the loop

<critical_requirement>
If the user chooses "Apply fixes," do NOT batch-edit. Walk through each P1 individually, read the relevant section of the artifact, propose the fix, and wait for confirmation before writing. Data accuracy fixes especially — don't invent numbers, ask where the right number lives.
</critical_requirement>

### Step 5: Save if asked

Only if "Save review" was selected, write to `plans/review-{artifact-slug}-{YYYY-MM-DD}.md`. The filename must match the reviewed artifact, prefixed with `review-`. This makes it greppable later ("show me all reviews of the Q3 enrollment plan").

## Important Rules

- **Two reviewers always.** Don't skip one because "this plan is mostly about strategy" — a mostly-strategy plan still cites numbers.
- **Parallel, not sequential.** The reviewers don't need each other's output. Launch both and wait.
- **Merge, don't concatenate.** If both reviewers flag the same clause, surface it once with both lenses credited.
- **Credit what's clean.** The "Clean" section matters — it tells the author which sections to stop second-guessing.
- **Don't rewrite the artifact for the author.** Findings point to problems; fixes are suggestions. The author decides.
- **Surface LOP drift.** If the plan claims alignment to an LOP that doesn't exist, or cites data that contradicts the LOP's latest update, that's a P1 regardless of how well the rest of the plan reads.
- **Degrade gracefully.** If a tier is unavailable or a source can't be verified, note it in Coverage. Don't fail the review.

## NSLS Style

The review report follows Kevin's voice:
- Plain language, not corporate-speak
- Numbers > adjectives
- Short findings, specific suggestions
- No words like: leverage, synergies, robust, learnings, ecosystem, holistic
