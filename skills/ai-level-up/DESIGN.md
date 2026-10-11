# AI check-in (`/ai-level-up`)

Tier 3 (company-wide), Standard depth. Builder: Davo Wood. Reviewer: Kevin.

## What this is (for the user)

Say "how am I doing with AI?" and Claude reads your recent sessions on your own computer and gives you a one-page private report:
- your Builder stage and its next unlock;
- whether each session used the right level of AI for what was at stake;
- how your habits compare with typical;
- up to three things to try next week, each tied to one of your real tasks.

Nobody else sees it.

## Customers

### Builder Toolkit staff (about 64 people on the tracker)
- **Who:** mostly non-technical. On 3 Oct the tracker had 15 Explorers, 32 Makers, 9 Builders and 7 Stewards. Many are on Windows.
- **Context:** a quiet moment, or a Friday check-in that runs by itself. Curious, and a little wary of being judged.
- **Flows:** the first run (a fresh check-in on up to 30 sessions); the weekly or monthly check-in they opted into (reads only what's new); "walk me through 1" to practise a suggestion; "stop my AI check-ins".

### claude.ai users
- **Who:** anyone with the skill uploaded to claude.ai.
- **Context:** they want the same check-in on their chats.
- **Flows:** chats outside projects, or one project's chats when they ask inside that project.

## UX principles

1. **Private by default.** Nothing leaves the person's computer except a read of their own Builder stage. The skill never shares the report or offers to.
2. **A coach, not a grader.** No ranking. It never assigns a stage. Higher levels are not better.
3. **Plain words.** No plumbing or framework jargon, so someone who has never heard of the levels can read every line.
4. **Every suggestion is tied to one of their real sessions.** A template always comes with a filled-in example from that session.
5. **Scope comes first.** The report and the reply both open by saying what was and wasn't looked at.
6. **Decisions are respected.** A risk the person knowingly accepted is never raised again.
7. **Visual and short.** Three scores at the top; the details fold away and open on click.

## What this should NOT become

- A manager dashboard, or any report that leaves the person's machine.
- A score used in performance reviews, or a ranking of people.
- A push to use more AI, or higher levels, for their own sake.
- A list of changes since last time. Every check-in is a full report.

## Interaction surface

- **Channel:** Claude Code (desktop Code tab or terminal), or claude.ai and Cowork through an uploaded zip.
- **Trigger:** the person asks, or a desktop scheduled task they opted into runs weekly or monthly.
- **User actions:** read the report, open a bar's sessions or a habit's explanation, say "walk me through N", change how often it runs.
- **Output:** an HTML report that opens in the browser, plus a five-line reply.

## How it works

### Components
- `SKILL.md`: the agent's instructions and promises.
- `scripts/digest.py` (standard-library Python 3.8+): scan, mask, render and settings.
- `references/`: the eight levels, the Builder stages, the 11 habits and their typical rates.
- `assets/report-template.html`: the page shell. Its CSP blocks scripts and network requests.

### Data flow
1. `scan` reads the person's Claude Code transcripts (`~/.claude/projects`) and local Cowork sessions. It drops pasted text, masks secrets, emails and IDs, and writes a digest to `~/.ai-level-up/`, which only the owner can read.
2. One GET to the tracker proxy (`builder-stats/<email>`) reads the person's own stage. It is cached for when the tracker is offline.
3. The agent judges each session from the digest only, and writes `report-draft.json`.
4. `render` escapes every value into the template and saves the page there too. `history.jsonl` keeps an allowlisted summary with no text from sessions.

### Systems touched
| System | Access |
|---|---|
| The person's own Claude Code and Cowork files | Read |
| Automation Tracker proxy | Read the person's own stage only |
| `~/.ai-level-up/` | Write: owner-only files on Mac; on Windows, the user folder's own permissions |
| Desktop scheduled task | Created only if they choose weekly or monthly |
| Tracker, Slack, email, Drive | Never written |

### Failure modes
| Failure | What the person sees | Handling |
|---|---|---|
| No real Python (for example the Windows Store stub) | "I can't read your sessions here; ask me in claude.ai" | Planned: try the toolkit's `nsls-python` first |
| Tracker unreachable | The last stage it saw, or "I couldn't check your Builder stage" | Never fatal |
| Several Claude accounts, or one that isn't theirs | Cowork skipped, and a one-line ask for their work email | Fails closed |
| Fewer than 3 sessions they drove | "Quiet week, nothing worth a report" | Scheduled runs only |
| A suggestion's template has no example | Nothing; the agent fixes it before the page is drawn | The render refuses |
| Moved sessions or scheduled runs | Nothing | Counted once, or left out |

### Risks
- **Transcripts can hold member data and secrets.**
  - Everything is masked before the agent reads it, and pasted text is dropped.
  - Reports are private files: owner-only on Mac, and protected by the user folder on Windows.
  - Reviewed by Codex (at build and again for Tier 3) and by Macroscope.
- **A judgment can feel unfair.** Stakes are judged by where the output goes next, and "not judged" is used when that's unclear.
- **Windows is untested on a real PC.** Two Windows testers have it now.
- **claude.ai can't see inside projects.** The report says so and says where to run it.

## Rollout

1. **Davo (Mac):** weekly since 3 Oct.
2. **Two Windows testers:** testing toolkit 3.23.0 now.
3. **A non-coder in claude.ai:** still to do.
4. **Every builder:** through the toolkit, with a Skill of the Week post.

## Measuring success

- **Baseline (3 Oct, before the build):** no such skill existed, so 0 runs. Stage mix: Explorer 15 · Maker 32 · Builder 9 · Architect 0 · Steward 7.
- **Adoption:** distinct people who run it, and runs per week. The toolkit logs only the skill's name, never what it found.
- **Proposed goal, to agree at review:** 20 of 64 builders run it at least once within 90 days of release, and 10 opt in to a weekly or monthly check-in.
- **Satisfaction:** tester feedback; suggestions people actually try (the next report credits them); how many stop their check-ins.
- **Red flags:** people stop after one run; complaints that a judgment was unfair; any privacy worry.
- **Window:** 90 days from release (11 Oct 2026). Re-pull the stage mix then.

## Change log

| Date | Change | Reviewed |
|---|---|---|
| 2026-10-11 | Initial design intent, written after 3.23.0 shipped | Davo; review requested from Kevin |
