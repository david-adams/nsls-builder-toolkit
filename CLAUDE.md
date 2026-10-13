# NSLS Builder Toolkit — Conventions

## What This Plugin Is

This plugin contains **organization-standard skills** for NSLS builders. These skills are maintained via GitHub PRs — do not edit them locally. Updates flow automatically when `main` is updated.

**Personal productivity skills** (open-day, close-day, close-week, log, familiar, person-intelligence, obsidian-setup) are in a separate plugin: [nsls-personal-toolkit](https://github.com/thensls/nsls-personal-toolkit). Run `/nsls-setmeup` to install them — they're optional and fully yours to edit.

## What Gets Installed

The install script sets up three things:

1. **NSLS Builder Toolkit** (this plugin) — org skills for slides, automations, Google Workspace, focus groups, deployment, web research
2. **Superpowers** (marketplace plugin) — process discipline: planning, debugging, verification, TDD
3. **Compound Engineering** (marketplace plugin) — development pipeline: brainstorm, plan, work, review, git

## Skill Routing by Complexity

### Quick tasks (< 30 min, single-step)
- Use **Superpowers** skills: `systematic-debugging`, `verification-before-completion`, `test-driven-development`
- These are lightweight — find root cause, verify the fix, move on

### Feature work (multi-step, needs a plan)
- Use **Compound Engineering** pipeline: `ce-brainstorm` → `ce-plan` → `ce-work`
- Produces durable requirements docs and implementation plans
- Use `ce-code-review` for code review (parallel reviewer personas, structured findings); `ce-doc-review` for plan/requirements docs

### Data intelligence (any question about users, systems, or operations)
- If tools for a system aren't available, run `/connect` to set up the connection
- Use `/data-intel` for cross-system questions that span multiple services
- Use individual domain skills (`/posthog`, `/slack`, `/customerio`, `/n8n`, `/airtable`, `/braintrust-evals`) for deep single-system work
- `/connect` → provides connections. Domain skills → deep expertise. `/data-intel` → orchestrates across all of them.

### NSLS-specific tasks (always)
- Use **NSLS Builder Toolkit** skills for anything NSLS-domain: slides, automations, Google Workspace, focus groups, deployment, web research, expense receipts (`/receipts` — clears Ramp's missing-receipt queue, dry run by default)
- These override generic skills when the task is NSLS-related

### Git operations
- Use **Compound Engineering**: `ce-commit`, `ce-commit-push-pr` (and `ce-worktree` for isolated branches)
- `/feature-branch-protocol-template` — optional template for builders who want their own work-discipline skill (setup, checkpoints, irreversible-step rules, final handoff). Includes Red's developer version as a seed.

### Process discipline (always active)
- **Superpowers** `using-superpowers` fires every session — it ensures skills get checked before acting
- `verification-before-completion` — always verify before claiming done
- `finishing-a-development-branch` — merge/PR decision guide

## Every PR to main bumps the plugin version

Builders run this toolkit from a **version-pinned plugin cache**. Their daily
`claude plugin update` compares the version string in `.claude-plugin/plugin.json`
and nothing else — a merge that leaves it alone is a merge nobody receives. The
`plugin-version-bump` check (required on `main`) refuses a PR whose version is not
strictly greater than main's. Patch for fixes, minor for a new skill or behavior,
major for breaking changes to how builders install or use the toolkit.

If two open PRs bump to the same number, the second lands unversioned; the
session hook catches that by comparing the installed commit with the marketplace
HEAD and reinstalls in place (`ensure_plugin_fresh` in `hooks/session-start.py`).

**Choose a number above main AND above every open PR's claim, then re-check it
right before you merge.** Main moves while a PR sits, so a version that cleared
the gate on Monday fails on Wednesday, and matching main exactly fails as well —
the check wants strictly greater, never equal. Read the field before choosing:

```bash
for n in $(gh pr list --repo thensls/nsls-builder-toolkit --state open --limit 200 --json number --jq '.[].number'); do
  info=$(gh pr view "$n" --repo thensls/nsls-builder-toolkit --json headRefName,headRefOid --jq '.headRefName + " " + .headRefOid')
  printf '#%s %s ' "$n" "${info%% *}"
  gh api "repos/thensls/nsls-builder-toolkit/contents/.claude-plugin/plugin.json?ref=${info##* }" \
    --jq '.content' | base64 -d | grep '"version"' || echo "(no plugin.json on that head)"
done
```

Read the head by its commit SHA rather than its branch name: a PR opened from a fork has a branch
this repository does not have, so a name-based lookup quietly returns nothing and that PR's claim
never enters the comparison. The explicit limit matters for the same reason — `gh pr list` stops at
30 by default, and a claim it never printed is one you cannot clear.

Sitting above the whole field means nothing landing ahead of you knocks your PR
back to a failing gate. Whoever merges after you bumps again — one of the two
always has to, and it may as well be the PR that is not ready yet.

## Hooks are registered in `hooks/hooks.json`. Nowhere else.

The plugin's own `hooks/hooks.json` is the only place a toolkit hook may be
registered. Not `settings.json`, not from an installer, not "as well as" —
**only** there.

Why this is a rule and not a preference. A hook written into a builder's
`settings.json` can only be changed by that builder re-running the installer,
and they don't: there were no install events on any platform for the three
weeks after the gate shipped. A hook in `hooks/hooks.json` ships with the
version and reaches every machine within a day. Worse, `settings.json` is the
surface migration stage B deletes — so a hook registered only there is a hook
with a delete date. That is not hypothetical: #168 registered the four hard
gates through the installers on 2026-09-05, 13a9630 removed the `hooks.json`
copy the next day, and every Mac that had finished migrating ran no gate at all
until 2026-09-20. Nobody noticed, because "no blocks" and "no gate" produce the
same empty table.

Three things follow, and each has already been got wrong once:

- **One entry per hook, through `hooks/run-hook.sh`.** `hooks.json` has no OS
  conditional. A bare `python3` command errors on stock Windows (a Store alias
  that exits without running) and registering the hook twice, `python3` and
  `py -3`, makes the absent twin print a hook-error notice on every matching
  tool call. The launcher resolves the interpreter and names
  `"shell": "bash"` explicitly.
- **Anchor the matcher, and keep every tool that can run a command in it.**
  `"Bash|PowerShell|Write|Edit"` is a substring match: it admits `BashOutput`
  and `KillBash`, and reaches `MultiEdit` by accident while the gates read
  fields a MultiEdit payload does not carry. Write
  `^(Bash|PowerShell|Write|Edit|MultiEdit|NotebookEdit)$` and normalise the
  payload. PowerShell belongs there: Claude Code enables it automatically on
  Windows and it carries its command in the same field Bash does. Admitting it
  is only half the job — the command gates open with `if tool != "Bash"`, so
  `normalize_call` presents it as Bash or the matcher looks fixed while
  nothing judges the call.
- **Never retire a shim you cannot prove you replaced.** `hooks/plugin_beacon.py`
  records, per hook, that the plugin's copy ran on this machine, and stage B
  retires that hook's `settings.json` entry only then. Per hook, not per plugin
  and never per platform: SessionStart firing says nothing about PreToolUse.

If a change here would leave a hook registered in `settings.json` only, it is
wrong. Add the test to `hooks/tests/test_gate_registration.py`.

## PR Review — Macroscope

Macroscope is a code review tool — it pays off when the PR contains claims about APIs, SDKs, query syntax, data system behavior, or other technical facts it can verify against documentation patterns. It has a per-review cost. Use it where it earns its keep.

**Run Macroscope before merging when the PR contains technical claims.** Examples that qualify:

- API behavior notes (request params, response shapes, flags, auth scopes)
- Query or filter syntax (SQL, HogQL, Airtable formulas, HubSpot associations)
- SDK or library usage patterns
- Data-system specs (table schemas, field types, pipelines)

These live anywhere: `_shared/learnings/` corrections, `agents/` or `skills/` reference material, `docs/` runbooks.

**Skip Macroscope when the content is non-technical.** Process playbooks, coaching patterns, org decisions, communication templates — these need human domain review, not a code reviewer.

**Precedent:** PR #18 merged a learning entry with a wrong Airtable API claim. Macroscope caught it post-merge → follow-up PR #19. Running it pre-merge on that kind of content is the right call; running it on every knowledge PR is not.

> **Editing the section below:** it is printed into every builder's session as
> hook output. Claude Code caps hook output at 10,000 characters and gives
> Claude only the first 2,000 of anything longer, which is how the whole soft
> half of this policy went unread from September 2026.
> `hooks/tests/test_guardrail_policy_budget.py` holds the printed text under
> budget. Put reasons and long procedures in
> `_shared/references/guardrail-playbook.md`, not here.

## Builder Guardrails

People can build anything they like for themselves. The moment other people's
data, workflows, customers or eyes are involved, pay closer attention.

| Tier | Scope | Before building |
|---|---|---|
| **1 · Personal** | Only the builder uses the output. No member data. Nothing others depend on. | Nothing. Build. |
| **2 · Team-useful** | Someone else would use it, or it touches internal NSLS data. | Register it · light 1-pager · mentor check past ~2 days' effort |
| **3 · Company-wide / member-facing** | Members see it, it wears NSLS's name, or it writes to a system of record at scale. | Register before code · full design doc · reviewer + platform check |

**Hard blocks come only from the hook.** `guardrail-gate.py` denies four command
shapes: NSLS work in a personal repo; a Tier 3 ship with no tracker record; a
bulk production write with no reviewer or rollback; a non-Anthropic platform at
Tier 2+ without Kevin's sign-off. If the hook did not deny a call, nothing was
blocked: never say policy blocked something that ran, or that it "would be"
blocked. Everything else is your advice, and the builder may decline it — never
phrase advice as "policy blocks" or "isn't allowed".

**Every hard block has two ways out, and both must appear:** the compliance route
(register it, assign a reviewer, move the repo) and Kevin's authorization. If
Kevin isn't named, the message isn't finished. Offer to draft the note.

**When a builder says no, record it so you never ask again:**

```
bash "${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.sh" memory record <topic> --note "<their reason>"
```

`<topic>` is short and stable: `registration`, `mentor-check`, `design-doc`.
Anything this session lists as already declined is closed unless the scope
genuinely escalated since.

**Report the guardrail moments only you can see** — what happened, in one plain
line the builder would recognise, never an intention:

```
bash "${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.sh" event <label> "<what happened>" [--automation <name>]
```

`flagged` you raised a tier, registration or reviewer question · `mentor` a
reviewer actually got brought in · `migrated` the repo moved to `thensls` or the
platform to Anthropic · `authorized` Kevin's route was taken on a hard block ·
`disputed` the builder says a gate misfired (record it before you reply).
`blocked` and `proceeded` are automatic.

**Escalation.** Some builds stay Tier 1 forever. Speak up when one moves up a
tier (1 → 2 → 3): it starts being useful to someone else, runs past about a
week, reaches into shared systems, or becomes customer-facing. **Ambiguous tier?** Ask the one question
that settles it, and treat the build as the lower tier until it's answered.

**A bulk-write block against a test base?** Take them at their word, once per
base, using only base IDs from the blocked command:
`bash "${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.sh" memory trust-base appXXXXXXXXXXXXXX`

**Reviewers.** Kevin: platform, architecture, anything member-facing; final say.
Davo: Tier 2/3 design, skills, agentic flows. Jenna: adoption, UX, HR-ops. Plus
a domain reviewer when the build crosses into their flow.

**Data rule.** Raw school lists (e.g. NCO lead data) go through the Bedrock PII
gate with zero data retention; NSLS member data (PostHog, HubSpot, Hex) stays on
Claude under do-not-train. Written policy — the gate isn't built yet.

**The exploration log ("log it").** Builder credit for a build while it is
still early, and a check against what others at NSLS are building. Once a
session has done real work, a checkpoint hook asks you, at the builder's next
message, whether it is turning into something worth credit or something a
colleague may also be building. Answer it in that reply, after their request,
with the `logit` command it gives: `offer` prints an offer to show them (and
any neighbouring build), then their yes or no; `not-yet` shows them nothing. If
they ask to log something themselves:
`bash "${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.sh" logit offer --name "<short name>" --sentence "<one sentence>"`.
One offer per build, and a declined log is never grounds to re-raise
registration.

**Read `_shared/references/guardrail-voice.md` before raising any guardrail.**
The why behind each rule, repo transfers and how "log it" works are in
`_shared/references/guardrail-playbook.md`.

## Automation Tracking

**Register Tier 2 and Tier 3 automations** with `/register-automation` — anything
someone else would use, anything touching NSLS data, anything member-facing. This
feeds the org-wide Automation Tracker so leadership has visibility into what's
being built, by whom, and at what stage.

**Tier 1 personal builds need nothing.** Offer registration once, lightly, only if
the builder would want credit or others might want the tool — then drop it. Don't
raise it again. Nagging someone about a script only they will ever run is how
builders learn to work outside the toolkit, which costs far more than the missing
row. See § Builder Guardrails above.

## Claude Usage Collector

Session start (`hooks/collector_bootstrap.py` on a Mac, `.ps1` on Windows)
quietly installs the NSLS Claude usage collector in the background when it's
missing. That's the same installer as the self-serve link, at most once a
day and five times in all. It prints nothing. The builder hears about it
through Signal's enrollment DM. If a builder asks what it is or how to stop
it: it reports session counts and skill names, never prompts, responses or
files. `NSLS_COLLECTOR_OPTOUT=1` stops the automatic install. Details are in
the README under "Claude Usage Reporter".

## Google Workspace
- **Always use `gws` for Google Docs, Sheets, Slides, Drive URLs** — never WebFetch, WebSearch, or Firecrawl for google.com URLs
- Use `google-drive` skill for file upload/download/sharing operations

## Skill Creation (3-phase cascade)
When building a new skill, use three tools in sequence. Each tests a different quality axis:
1. **`/skill-creation`** (this toolkit) — design rubric: purpose, safety, macro/micro, diagnostics, domain-specific gotchas
2. **`superpowers:writing-skills`** — pressure test: pure-trigger description, rationalization table, Red Flags list, TDD-for-documentation discipline
3. **`skill-creator`** (official Anthropic plugin) — spec audit: official frontmatter compliance, Quick Start, line count, writing style

**Optional Phase 4**: For broadly-released skills that need reliable auto-triggering across many sessions, run `skill-creator`'s eval/benchmark mode for quantitative trigger accuracy. Skip for personal / time-boxed / Kevin-only skills.

See `/skill-creation` for the full cascade including a worked example (gary-meeting-prep build, 2026-04-18).

## Presentations
- Always ask "NSLS branded or Society branded?" before creating slides
- **NSLS**: Lexend Deca + Avenir, navy/teal/gold
- **Society**: HW Cigars + Inter, cream/yellow
