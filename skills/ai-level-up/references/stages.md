# NSLS Builder stages (Kevin's rubric)

Source: Kevin's memo "NSLS Builder Adoption — Memo for HR + AI Consultants" (May 2026). The Automation Tracker records each person's stage.

**The stage comes from the tracker. This skill never assigns one.** Stages are earned by shipped evidence: an install, a production automation, a merged PR, something others rely on. Chats can't prove any of that, so never say someone "works like" a higher stage. Show the stage on record, the next unlock in Kevin's words, and which of this week's work could become that evidence.

| Stage | What it looks like | Evidence | What unlocks the next stage |
|---|---|---|---|
| **Explorer** | Has Claude. Uses it occasionally to draft, summarise or ask questions. Hasn't installed the toolkit. | Claude account exists. No toolkit install. No registered automation. | Install the Builder Toolkit. Use one org skill to do real work. |
| **Maker** | Uses Claude regularly for cross-system research, prototypes, scripts. Likely on Cowork connected to a few tools. | Toolkit installed. Has tried /connect, /data-intel, or a domain skill. Has prototyped at least one workflow. | Take a prototype to production on shared infra. Push code to GitHub. Register the automation. |
| **Builder** | Takes ideas to production. Code on GitHub, deployed on shared infra, registered in the tracker. | At least one automation in Production with a GitHub URL. Authored or merged at least one PR to a thensls repo. | Ship something a teammate uses. Move an automation to Org-Owned. Author a reusable skill. |
| **Architect** | Designs interconnected systems. Helps other builders. Sees the whole and where it's broken. | Multiple automations in Production. At least one Org-Owned. Authored skills others use. Reviews PRs. | Steward a system someone else built. Mentor a Builder up the curve. |
| **Steward** | Owns and maintains a production automation that serves others and has real ROI. Passes the won-the-lottery test (it would still run if they vanished tomorrow). | Org-Owned automation with runbook, on shared infra, with a collaborator on the repo. ROI documented in the tracker. | Terminal stage: keep stewarding, take on more. |

Kevin's framing: everyone moves up at least one stage a quarter, "we celebrate movement, not pick winners". Keep that tone.

## "This week's work that could count"

Point at one real task from the evidence that is a step toward the unlock. Name it plainly, plus the one action that would turn it into evidence. Examples:

- **Explorer:** "You summarised the chapter survey by hand three times this week. The toolkit has a skill for that; installing it and using it once is your unlock."
- **Maker:** "The events-sheet clean-up you prototyped in Cowork could be registered and moved to shared infra. That's the Builder step."
- **Builder:** "The report script is in production and only you use it. Getting one teammate onto it is the next unlock."
- **Architect / Steward:** "Your weekly digest is relied on by others, but nobody else can run it. A runbook and a collaborator would pass the won-the-lottery test."

If nothing this week points toward the unlock, say so in one line and don't invent something.

## When the stage can't be checked

The tracker may be unreachable from claude.ai, or the person isn't registered. In that case:

- Write "I couldn't check your Builder stage this time" and carry on with the rest of the report.
- Never ask a non-coder to name their stage.
- Never guess one.
