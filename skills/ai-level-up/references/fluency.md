# How well someone works with AI: Anthropic's 4D framework

Sources: Anthropic's [AI Fluency Index](https://academy.claude.com/tutorials/the-ai-fluency-index), a study of 9,830 claude.ai conversations from 20–26 January 2026, and the 4D AI Fluency Framework by Rick Dakan and Joseph Feller. The detection notes are adapted from GitVelocity's /ai-fluency skill.

**The four Ds.**

| D | Question it asks |
|---|---|
| **Delegation** | What's the goal, and what should AI handle versus you? |
| **Description** | Did you say clearly what you need: context, format, audience, tone? |
| **Discernment** | Is it right? Does the reasoning hold? Does it actually help? |
| **Diligence** | Are you protecting data, owning the result, being open about AI's part? |

Most work is small loops of describing and then judging.

## The 11 observable behaviours

**A behaviour counts as present if the person shows it at least once in a session or chat.** "Typical" is the share of claude.ai conversations in the study that showed it.

| # | Behaviour | D | Typical | What to look for |
|---|---|---|---|---|
| 1 | Iterates and refines | Description | 85.7% | Builds on the output: asks for changes, adjustments, a second pass |
| 2 | Shows an example of what good looks like | Description | 41.1% | Pastes a sample, says "like last month's", gives a reference |
| 3 | Specifies format and structure | Description | 30.0% | Table, bullets, length, a template to follow |
| 4 | Sets how the AI should work | Delegation | 30.0% | "Just analyse, don't change anything yet", "ask before sending", "be brief" |
| 5 | Gives tone and style | Description | 22.7% | Formal or warm, plain English, "sound like me" |
| 6 | Names who it's for | Description | 17.6% | "This is for the board", "for a new staff member" |
| 7 | States the goal before asking | Delegation | 51.1% | Gives the why and the background before the request |
| 8 | Asks for the approach before it acts | Delegation | 10.1% | "How should we tackle this?", "give me options first", asks for a plan |
| 9 | Fills in context the AI couldn't know | Discernment | 20.3% | "You should know that…", corrects a wrong assumption about NSLS |
| 10 | Pushes back on weak reasoning | Discernment | 15.8% | "Why X over Y?", "That doesn't seem right because…" |
| 11 | Checks facts that matter | Discernment | 8.7% | Verifies numbers or claims against the source, tests the result, asks for evidence |

Behaviour 1, iteration, is the gateway: people who iterate show about twice as many of the others. Behaviour 11 is the rarest, and for most people the highest-leverage one to grow.

## Diligence signals (no published baseline)

| Signal | In chat or Cowork | In Claude Code |
|---|---|---|
| Protects sensitive data | Doesn't paste member or student records, passwords or keys; anonymises before pasting | Same, plus no secrets in code; `secret_flags` in a digest is evidence against this |
| Owns the result | Reads and edits the draft before sending it; checks the numbers before forwarding | Reviews the diff, runs tests, doesn't merge unreviewed |
| Checks before it reaches others | "Let me check before this goes out"; asks for a review before sharing | Confirms before pushing, deploying or messaging |
| Open about AI's part | Mentions AI help where it matters | Co-author tags; notes in PRs and docs |
| Considers fairness and who's affected | Asks whether wording works for all members or chapters | Considers accessibility and different user groups |

## The artifact paradox

**When AI produces something polished (a document, a page, code), people get more directive but less critical.** In the study, every Discernment behaviour dropped in conversations that produced an artifact:

- checking facts −3.7 points
- questioning reasoning −3.1 points
- identifying missing context −5.2 points

Check for it: compare the person's Discernment behaviours in sessions that produced something against sessions that didn't. Name one example either way.

## Rules for using the numbers

- **Compare against "typical" only when 8 or more sessions or chats were seen.** Below that, say "too few to compare this week" and describe what you saw.
- **The baselines come from claude.ai chats.** For Claude Code and Cowork, treat the comparison as indicative only, and say so once in the details section.
- **In the main body, use plain sentences**, e.g. "You checked a fact before relying on it in 2 of 6 sessions." Percentages and "typical" figures go in the details section only.
