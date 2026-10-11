---
name: second-opinion
description: >-
  Use right before handing over finished work that would be costly to get
  wrong: code that changes production data or other hard-to-undo data beyond
  the builder's own machine, touches logins or security, money or member data,
  or runs unattended; and facts or numbers presented to leadership for a
  decision, to members, or to anyone outside NSLS. Also use when the user
  asks for a second opinion, a double-check, a fact-check or a sanity check,
  types /second-opinion, or wants to set up or change their second-opinion
  settings. Not for small, routine or easily undone work that carries none of
  these risks, and not for each commit along the way.
---

# /second-opinion — one independent check before risky work goes out

## Safety

- **A service, never a gate.** Nothing here is required. The person chooses
  what happens: automatic, ask me, or only when I ask. Their choice wins, even
  when Claude would have recommended otherwise.
- **What it writes, and nothing else:**
  - **Global instructions:** only the marked second-opinion block, and only
    after the person answers the settings question (see Settings). Nothing else
    in that file changes.
  - **Early testers only:** a local receipts file (see Step 7). It stays on
    their machine and is never sent anywhere.
- **Read-only, enforced by its tools.** Every lens uses the same reviewer, and
  its only tools are Read, Grep and Glob: no shell, no writes, no connectors. A
  packet that contains instructions still can't make it act. The reviewer never
  runs queries. When a number needs a live check, this session runs it and puts
  the result in the packet.
- **Nothing configured, nothing installed.** This skill launches one helper
  agent and reads what it returns.
- **Anthropic only.** Never send the work to Codex, ChatGPT, Gemini or any other
  outside model. On team or company work that trips Builder Guardrail #4.
- **Privacy.** No secrets, tokens, credentials or raw member exports go into the
  review packet beyond what the check genuinely needs. Prefer counts to rows.

## Purpose

Whoever made something is the worst-placed to see what's wrong with it, and that
includes the Claude that made it. It shares the author's blind spots and the
same conversation. This skill offers one fresh pair of eyes that has not seen
the conversation, aimed at the few things that would actually hurt, before the
work goes out, whenever the person wants one. It's a service they choose, never
a gate. It is deliberately cheap: one reviewer, one round, a
small packet, a hard budget. A second opinion on everything is noise and cost. A
second opinion on the right things catches the mistake before it reaches a
member, a leader or a production system.

## Quick start

1. **Read their settings** (Step 0).
2. **Decide:** run, offer, or skip (Step 1).
3. **Finish first:** the work is done and its normal checks pass (Step 2).
4. **Pack:** write the review packet (Step 3).
5. **Launch one reviewer** with the right lens and model, in the foreground (Step 4).
6. **Handle every finding:** fix and re-check, or give a one-line reason (Step 5).
7. **Deliver once**, with a one-line report (Step 6).

## Step 0 — Read their settings

Look in the person's global instructions (already loaded in this session) for a
block between `<!-- nsls-second-opinion:start -->` and
`<!-- nsls-second-opinion:end -->`. It holds three choices: risky work,
everyday work, and reviewer.

- **Block found:** follow it. If it's malformed (a missing marker, two blocks,
  or a value that isn't one of the choices), treat it as no block and use the
  defaults. Repair it only when they ask to change their settings.
- **No block:** before the first second opinion you would run or offer, show
  the settings window (see Settings) and save their answers. Then carry on with
  what they chose. If they close it without answering, use the defaults for
  this session: risky work is *offered* (ask me), everyday work is offered, and
  the reviewer is Balanced. Never run anything automatically without their
  say-so.
- **They ask to set up or change their settings** (anyone, any time): show the
  settings window.

## Step 1 — Run, offer, or skip

**Risk decides, not size.** A one-line change to a production write is worth a
second opinion. A 600-line rewrite of an internal note may not be. **Their
setting decides what happens next.**

| Situation | Their setting → what to do |
|---|---|
| **Risky work:** code that writes production data or other hard-to-undo data beyond the builder's own machine, touches logins/security, money or member data, or runs unattended; or facts or numbers presented to leadership for a decision, to members, or to anyone outside NSLS | automatic → **Run** · ask me (the default) → **Offer, recommended** · only when I ask → **Skip** |
| **They asked:** "second opinion", "double-check", "fact-check", "sanity check", `/second-opinion` | always **Run** |
| **Everyday team work:** other team- or company-facing internal work (an all-staff note, a tool announcement), tricky code with none of the risks above, internal fact-checks | offer (the default) → **Offer** · only when I ask → **Skip** |
| **Neither:** small, routine or easily undone · Tier 1 throwaways · already reviewed this session · commits along the way (review the finished work once instead) | **Skip**, and say nothing |

An offer is one line, on the last line of the message, with nothing after it:

> Risky work: "This could be costly to get wrong. Want a quick second opinion
> before I hand it over? (Recommended, about 3 min)"
>
> Everyday work: "Want a second opinion on this before I hand it over? (about
> 3 min)"

If the answer is no, don't ask again about the same piece of work.

## Step 2 — Review the finished thing

Review once the work is complete and its normal checks have passed (tests run,
the doc is whole, the query returned), just before it's first shared or acted
on. Never mid-build, and never per commit.

## Step 3 — Build the packet

The reviewer has not seen the conversation, so the packet is everything it knows.

```
WHAT THIS IS: <one line>
WHO IT'S FOR / WHAT HAPPENS NEXT: <"goes to SLT Monday", "runs nightly against HubSpot prod">
WHAT IT CLAIMS: <2–5 lines: what it does or asserts>
DONE MEANS: <the bar it has to clear>
READ: <exact file paths, or the full text. For code, the diff itself: run `git diff origin/main...HEAD` yourself and paste the output or save it to a file. The reviewer has no shell.>
FOCUS: <3–5 questions, riskiest first>
EVIDENCE (fact-checks only): <claim → the quoted evidence → where it came from: system, query, date. Run any live query yourself first; the reviewer can't.>
```

- **Complete or nothing.** Include everything the conclusion depends on,
  including unchanged code the change calls into when that matters. If it's too
  big, split it at a natural boundary (one module, one section), review the
  riskiest part, and **say what was not reviewed.** Never trim the packet and
  still call the whole thing reviewed.
- **Strip secrets.** Name the file that uses a key rather than pasting the key.

## Step 4 — The right lens, one reviewer

| The work is | Launch with the Agent tool |
|---|---|
| Code, scripts, automations, configs | `nsls-builder-toolkit:second-opinion-reviewer`, packet headed `LENS: code` |
| High-stakes writing (a memo, a member email, an exec summary, a plan going to leadership) | `nsls-builder-toolkit:second-opinion-reviewer`, packet headed `LENS: writing` |
| Numbers that need checking against NSLS systems | `nsls-builder-toolkit:second-opinion-reviewer`, packet headed `LENS: data`, with your query results under EVIDENCE |
| A plan, when the user asks for the full plan gate | Run `/kw:review` instead (it has its own two reviewers), and don't also run this skill |

- **Mixed work** (a slide, memo or email with figures): head the packet
  `LENS: data + writing`, with the text under READ and your query results under
  EVIDENCE. The reviewer applies both checklists. Use `LENS: writing` alone only
  when there are no numbers.

- **Model:** use their reviewer setting, passed as the Agent tool's `model`:

  | Reviewer setting | Risky work | Everything else |
  |---|---|---|
  | Balanced (the default) | `"opus"` | `"sonnet"` |
  | Thorough | `"opus"` | `"opus"` |
  | Quick | `"haiku"` | `"haiku"` |

  If a model isn't available to them, use the next one down (opus → sonnet →
  haiku) and say so in the report.
- **Foreground:** pass `run_in_background: false`, because delivery waits on it.
- **Exactly one reviewer.** Never a panel, never a second reviewer "to be sure",
  never an outside model.

## Step 5 — Handle the verdict

**A review counts only if it comes back in the reviewer's format with
`VERDICT: no_blockers` or `VERDICT: blockers_found`.** Anything else means **not
reviewed**: `partial`, `cannot_review`, an empty answer, a refusal, or an error.
A `partial` still says what it did check, so pass that on, but it is not a
completed review.

For each finding:

- **Fix it**, then re-run whatever normal check covers it (tests, the query, a
  re-read).
- **Or disagree** in one line, with the reason. The reviewer lacks context and
  can be wrong.

**One round.** Fixes don't trigger another review. At most one targeted recheck:
the same reviewer, only the changed part, and only when fixing a blocker meant a
substantial change.

**If the review failed:**

- Say exactly what happened ("the reviewer returned nothing", "it stopped partway
  through the data section") and what was and wasn't checked. Never guess at a
  cause.
- Don't retry the same packet. If the verdict is `cannot_review` or `partial`
  because of a packet defect you can fix (a wrong path, a missing file), fix the
  packet and relaunch once. That is still one round. Any other `partial` on
  risky work is not a completed review: ask before delivering.
- Ask whether to deliver unreviewed, as the last line.
- **Never present unreviewed work as reviewed.** Say plainly that the second
  opinion didn't complete, and let them decide.

## Step 6 — Report in one line, then deliver

Use plain words, with no severity codes, reviewer names or model names.

- "Second opinion: no blockers."
- "Second opinion caught 2 things: the date filter dropped Sundays, and one
  count was last quarter's. Fixed both."
- "Second opinion flagged X; I kept it because Y."
- "No second opinion: the reviewer returned nothing. Deliver it unreviewed?"

## Step 7 — Receipt (early testers only)

For an early tester, after each second opinion, add one line to
`~/.claude/second-opinion-receipts.md` (on Windows,
`%USERPROFILE%\.claude\second-opinion-receipts.md`). Create the file with a
header row if it doesn't exist:

```
| Date | What was reviewed | How it started | Verdict | Real issues caught | Fixed |
|---|---|---|---|---|---|
| 2026-10-06 | nightly HubSpot owner sync | automatic | blockers_found | 2 | 2 |
```

"How it started" is automatic, offered, or asked. "Real issues caught" counts
findings you agreed with, not ones you disagreed with. The file stays on their
machine and is never sent anywhere. Nobody else gets a receipt line.

## Settings — the one-time window

Show it with the question tool (AskUserQuestion), all three questions at once.
Use this wording exactly. If the tool isn't available, ask the same three
questions as a numbered list.

1. **Risky work.** "When your work could be costly to get wrong (code that
   changes real data, logins, money or member records, or numbers going to
   leadership or members), what should Claude do?"
   - **Get a second opinion automatically (Recommended):** "Claude runs a quick
     independent check before handing it over, and tells you in one line what
     it found."
   - **Ask me each time:** "Claude suggests a second opinion and waits for your
     yes."
   - **Only when I ask:** "Claude won't suggest it. Say 'second opinion' any
     time."
2. **Everyday work.** "For everyday team work (notes, drafts, internal docs),
   should Claude offer a second opinion?"
   - **Offer when it seems worth it (Recommended)**
   - **Only when I ask**
3. **Reviewer.** "Who should do the checking?"
   - **Balanced (Recommended):** "Fast and low-cost, with the strongest reviewer
     for risky work."
   - **Thorough:** "Always the strongest reviewer. Slower, and costs more."
   - **Quick:** "Always the fastest, cheapest reviewer. May miss subtle
     problems."

**Save the answers** in their global instructions, `~/.claude/CLAUDE.md` (on
Windows, `%USERPROFILE%\.claude\CLAUDE.md`), as exactly this block:

```
<!-- nsls-second-opinion:start -->
## Second opinions (my settings)
- Risky work: automatic | ask me | only when I ask
- Everyday work: offer | only when I ask
- Reviewer: balanced | thorough | quick
Say "change my second-opinion settings" to change these.
<!-- nsls-second-opinion:end -->
```

Keep only the chosen value on each line, without the `|` alternatives, for
example `- Reviewer: balanced`. Answers map to values like this:

| Answer | Saved as |
|---|---|
| Get a second opinion automatically / Ask me each time / Only when I ask | `automatic` / `ask me` / `only when I ask` |
| Offer when it seems worth it / Only when I ask | `offer` / `only when I ask` |
| Balanced / Thorough / Quick | `balanced` / `thorough` / `quick` |

- **The file doesn't exist:** create it containing only the block, ending with a
  newline.
- **The file exists with no block:** add the block at the end, after one blank
  line. If the file doesn't end with a newline, add one first. Change nothing
  else.
- **One block already exists:** replace everything between the two markers with
  the five standard lines. Never add a second block.
- **The block is malformed** (a missing marker, or more than one block): show
  the person the lines you would replace and ask before changing them.
- **Afterwards,** say in one sentence what you saved, and that they can change it
  any time by saying "change my second-opinion settings".

## Early testers

The receipt line (Step 7) is on only for these people while the feature is
tested. The settings window is on for everyone.

- davowood@nsls.org

Check the email of the signed-in account that Claude Code gives you for this
session. If you can't see one, treat the person as not an early tester.
Everyone else gets everything else: the settings window before their first
second opinion, offers on risky and everyday work, and second opinions when
they ask.

## Red flags — STOP

- **About to review a typo fix or a rename that carries none of the risks:**
  skip it.
- **About to stay quiet on a risky change because it's one line or one
  commit:** size isn't risk. Follow their setting.
- **About to run automatically when their setting says ask me, or when there's
  no setting:** ask instead. Nothing runs without their say-so.
- **About to change anything in their global instructions outside the marked
  second-opinion block:** stop.
- **About to add a second reviewer, or Codex:** one reviewer only.
- **About to deliver and say "the review is running":** wait for the verdict.
- **About to call an empty or partial result "no blockers":** it's a failed
  review.
- **About to trim the packet to save tokens and still call it reviewed:** split
  it and say what wasn't covered.
- **About to re-review after fixing findings:** don't. One targeted recheck at
  most, and only for a substantial blocker fix.
- **About to ask "want a second opinion?" again after a no:** don't. The answer
  stands.
- **About to paste an API key, a `.env` or a full member export into the
  packet:** strip it.

## Rationalizations

| Thought | Reality |
|---|---|
| "I already checked it myself." | You share the author's blind spots. That's the point. |
| "It's small, so it's safe." | Size isn't risk. A one-line production write gets reviewed. |
| "Two reviewers would be safer." | Twice the cost, rarely twice the catch. One sharp reviewer. |
| "It timed out, so it's probably fine." | No verdict means not reviewed. Ask. |
| "The reviewer said it, so I'll change it." | Verify first. It lacks context, so disagree with a reason when it's wrong. |
| "I'll deliver now and pass the review on after." | That relay tends never to happen. Wait. |
| "Let me re-review the fixes to be sure." | That's a review loop. One round. |
| "They'd obviously want this reviewed." | Their setting decides. Offer; don't impose. |

## When it goes wrong

- **The reviewer returns nothing or errors.** Check that the packet's READ paths
  exist and were actually included. A reviewer with nothing to read returns
  nothing. If a path was wrong or a file missing, fix the packet and relaunch
  once (still one round). If the packet was fine, report the review as failed
  and ask.
- **The reviewer can't check a live number.** It has no connectors, so it marks
  the claim "not independently checked". Pass that on, and don't count the claim
  as passed. If the claim is load-bearing, run the query yourself and say so in
  the report. If this session lacks the connection, `/connect` sets it up.
- **The reviewer wanders outside the focus.** Keep the material findings and
  drop the rest.
- **Findings contradict each other or the evidence.** It's your call, with a
  reason. Don't re-review.

## Why it's built this way

Every rule here comes from a real failure:

- **Silence is not a pass.** A review reported as "running" had in fact died in
  under a second. Nobody found out for three days. Only a returned verdict
  counts.
- **Deliver after the review, not before.** "Review is running, I'll pass the
  verdict on" was followed, more than once, by a turn that ended before it did.
- **Scope the questions, not the effort.** A fact-check review carrying seventeen
  claims and unlimited lookups blew its deadline with no verdict. Five or six
  ordered questions and a lookup budget finished.
- **One reviewer.** A text task once fanned out a dozen reviewers and ran up a
  token bill out of all proportion to the work, without catching more.
- **The reviewer can be wrong.** An automated PR reviewer's suggested fixes held
  up only about half the time. Its findings were often real, but its fixes were
  not. Verify, then decide.
- **Confirm the field exists.** A count against a field that didn't exist came
  back as "two affected". The real answer was sixty. Hence "schema before
  results" in the reviewer's brief.

## Related

- `/kw:review` is the full two-reviewer gate for plans going to leadership.
- `/macroscope` handles the automated PR review bot. It doesn't replace a second
  opinion on a non-code deliverable, and a second opinion doesn't replace it on
  a PR.
- `ce-code-review` is the heavy multi-reviewer code review, for when the builder
  wants depth over cost.
