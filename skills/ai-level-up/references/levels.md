# The eight ways of working with AI

Adapted in our own words from Every's guide, [The Eight Levels of AI Adoption](https://every.to/guides/the-eight-levels-of-ai-adoption) (Mike Taylor and Laura Entis). Use it as a lens for judging **each task**. It is not a score for a person.

## Rules that come before the table

- **The right level depends on the task.** Two things decide it: how far you trust the AI to do this job unsupervised, and how bad a mistake would be. Higher is not better.
- **Judge stakes by where the output goes next, not by what it's about.**
  - A draft that stays with the person, or that they will review before it goes anywhere, is low or medium stakes, even when the topic is serious.
  - It's high stakes only when it reaches members, colleagues, money, or a system of record without another look.
  - Never call a session "too trusting" on the assumption that its output goes public. If you can't see where it goes next, say "not judged".
- **High stakes means stay lower, or add a check.** Member or student data, money, anything sent out under NSLS's name, anything other people will rely on: supervise more closely, or add a review step before handing more over.
- **Most staff work best somewhere in levels 1–4.** Levels 5–8 mostly need technical help today. Never push someone past 4 because the ladder goes higher.
- **It's not a competition.** Nobody is ranked. You'd never brag about eight interns working overnight whose work nobody checked.
- **New models move the line.** Something that didn't work three months ago may work now, so it's worth re-trying a level someone gave up on.

## The levels

| # | Name | What it is | What it looks like at NSLS | The check you owe | Move up when… |
|---|---|---|---|---|---|
| 1 | **Chatbot** | You ask, it answers. It doesn't touch your files or systems. | claude.ai chat: draft from rough notes, summarise a pasted or uploaded document, answer a question | Facts and tone are right. You'd put your name to it. It told you what was missing. | You keep copying and pasting between Claude and where the work actually lives. |
| 2 | **Copilot** | AI works inside the file you're already in. | Claude in Excel, Claude in Chrome on the page you're reading, Gemini in a Google Doc, Claude editing the one doc you're working on | It still sounds like you. Formulas and numbers are right. | You need information from several places at once, not one file. |
| 3 | **Agent** | You describe a task, and it works step by step across your files and tools, checking in with you for approval as it goes. | claude.ai with Drive, Slack or Gmail connected; Cowork working through a folder; Claude Code asking before each change | You approve the plan. You correct how it read the data. You catch where the sources disagree. | You trust the steps and would trade control for speed, or want a working prototype in one go. |
| 4 | **Autopilot** | You hand over the whole task and review the result at the end. | A working mini-tool or page built as a claude.ai artifact; a Cowork task left to finish; Claude Code with auto-accept on | You say what done looks like before handing over, then test the result yourself. You note what must improve before anyone else relies on it. | Results are uneven, and the work matters enough to need a repeatable process. |
| 5 | **Workflows** | You build a repeatable process around the AI: plan, do, self-review, check confidence. Your standards are written into it. | A saved skill or instruction set with a review step built in; plan → build → review loops; tests; second-opinion reviews | You review the plan before it runs, then judge whether its self-review is honest and add what it missed. | There's a low-stakes area, usually admin, that you'd trust it to handle without checking in first. |
| 6 | **Assistant** | It works in the background on a schedule, without being prompted. | Cowork or desktop scheduled tasks; a weekly brief that arrives on its own | You decide what's urgent, check its summaries catch what matters, and tune its rules from what it gets wrong. Reaching work systems needs technical or IT help. | You want more handled, but one assistant is getting overloaded. |
| 7 | **Multi-agent** | Several long-running agents or sessions, each with its own job, run at once. | Separate sessions for separate jobs running side by side and passing messages to each other | You give each one a clear job and what done looks like, then review each one's output. | You've lost track of which agent is doing what. |
| 8 | **Orchestrator** | A lead agent plans the work, hands pieces to sub-agents, merges the results and escalates only the judgment calls. | One lead session sending work to helper agents (subagents, workflows) | You set the goal, what done looks like, and what must come to you. Then check it escalates the right amount. Still experimental. | Terminal. Most people should not aim here. |

## Stay, or step down, when…

- **The output keeps needing heavy fixes.** Drop one level, or add the check from the level you're on.
- **The task touches member or student data, money or public NSLS content, and nothing checks the result.** That's over-reach: add the check before handing over more.
- **It would be quicker to do it yourself.** That's a fine answer. Say so.
- **A scheduled helper acts on its own.** If someone set one up without technical help and it sends messages, edits records or touches member data, judge it `lower` (handed over too much). Read-only briefs and personal admin are fine.

## What each surface can reach

| Surface | Typical range | Notes |
|---|---|---|
| claude.ai chat | 1–4 | Connectors make it 3. An artifact someone uses makes it 4. |
| Cowork | 3–4, plus 6 | It works on files and apps for you. A scheduled read-only brief makes it 6. |
| Claude Code | 3–8 | Skills, plan-and-review loops, scheduled tasks, parallel sessions and subagents. |

Judge each surface against its own range. A chat user at level 3 is using chat well. Never call a surface's ceiling a personal shortfall.

## Reading level from a session digest (local mode)

These are hints, not rules. Judge from what the person actually asked for.

- **Level 1–2:** few or no tools; the person pastes material in and refines text.
- **Level 3:** connectors or file tools across sources; approvals (`permission_modes.default`); the person steers step by step.
- **Level 4:** auto-accept or bypass modes; long runs with few human turns; `artifacts_published`; `ship.deploy`.
- **Level 5:** review skills (e.g. codex-review, code-review, macroscope); `plan_mode`; `ship.tests`; skills the person wrote; edits to standing instructions.
- **Level 6:** `schedules_created`; automated sessions (scheduled tasks).
- **Level 7:** `max_concurrent_sessions` ≥ 3 *with distinct jobs*; `messages_to_sessions` / `peer_messages_in`.
- **Level 8:** the person asked for one goal to be split across helpers and merged (`subagents`, `workflows`). Helpers Claude started on its own don't count.
