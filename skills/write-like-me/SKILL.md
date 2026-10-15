---
name: write-like-me
description: >-
  Use when someone wants Claude to write more like them, keeps changing Claude's
  drafts the same way, or wants to build or update a personal style guide for
  Slack messages, emails or Google Docs. Triggers: "write like me", "learn my
  style", "learn my writing style", "make me a style guide", "writing level up",
  "I keep editing your drafts", "update my <name> style".
---

# Write like me

People change Claude's drafts the same way again and again. This skill finds those
changes, keeps the ones about style, and turns them into rules the person approves.
The guide it saves loads whenever Claude drafts that kind of writing again.

It uses two sources, both found automatically:
1. **The session:** Claude's first draft, the replies the person typed, and
   Claude's last version.
2. **The destination:** what they actually sent: the Slack message, the sent email,
   the Google Doc.

Read only. Never send, post, edit, label or share anything. Use only the person's
own writing. Every command below uses `python3`. If that isn't found (often the case
on Windows), use `python` for all of them. Run them from this skill's folder.

## Step 1: Which writing, which guide

- **Destination.** Slack, email or Google Docs. If the request doesn't say, ask one
  question: *"Is this for Slack messages, emails or Google Docs?"*
- **Purpose.** If they named one ("student responses", "reports"), use it. Otherwise
  propose one in Step 7 from what was found.
- **Existing guides.** Look for `my-*-style` folders in `~/.claude/skills/`. "Update
  my <name> style" means update mode: go to Step 9.
- **Window.** The last 30 days, ending today. If Step 4 finds fewer than 5 usable
  items, widen to 90 days once.

## Step 2: The session source

    python3 scripts/pairs.py drafts --start YYYY-MM-DD --end YYYY-MM-DD

This reads the Claude Code transcripts on this computer, plus the 7 days before the
window, and saves every prose draft with its versions and every reply the person typed
to it, including replies after the last version (often edits they then made themselves).
It prints the work folder it uses.

## Step 3: The destination source

Fetch the person's own sent items in the window and save them in the work folder as
`finals.json`: a list of `{"id", "source", "time", "text"}`. Use ISO 8601 with an
offset for `time` (`2026-10-11T13:38:09-04:00`). Keep `text` exactly as returned,
including Slack's `<url|label>` links.

If the connector isn't available, say so in one line, for example *"Slack isn't
connected, so say /connect to add it, or paste up to 5 messages you sent"*, and carry
on with what they paste.

- **Slack:** messages *from the person* (`from:<@their user ID>`; the Slack tool's
  description gives the ID) after the window start. Use detailed results and read all
  pages, up to 100 messages. Skip messages under 8 words.
- **Email:** their work Gmail only: `in:sent after:YYYY/MM/DD before:YYYY/MM/DD`,
  where `before` is the day after the window ends. Keep only what they wrote, and cut
  quoted replies ("On … wrote:") and signatures.
- **Google Docs:** Docs they own, modified in the window, that nobody else has edited.
  The current text is the final, and `time` is when it was last modified.

## Step 4: Pair, then report what was found

    python3 scripts/pairs.py match

Before analysing anything, show one line per item:

- `✓ Slack to Jenna, 10 Oct: first draft matched to the sent version · 2 replies from you · 31% changed`
- `✓ … · close call: used the draft worded most like what you sent (another draft was similar)`
- `◐ Slack poll, 7 Oct: Claude posted it itself · your replies to it still count`
- `↺ Email to the board, 3 Oct: you rewrote most of it · used as a sample of your writing`

Then add one count line: *"10 sent messages had no Claude draft, so they're samples of
your own writing."* Ask: *"Anything to remove, any close call to correct, or a draft
and final you'd like to add?"*

Remove any item with member or student personal information, and say how many you
removed. If fewer than 3 matched items are left, say so and offer to widen the window
or take pasted pairs. If there are still too few, build the guide from their own
writing (the samples) and label it "from samples only".

## Step 5: Keep only style

    python3 scripts/pairs.py show

This prints every item with the person's replies and its numbered changes. `/` marks a line
break.

- **Label the facts:** `python3 scripts/pairs.py label --facts 3:0,6 5:2` marks
  changes 0 and 6 of item 3, and change 2 of item 5, as fact. Every other change counts
  as style.
  - **Fact** is information: names, recipients, numbers, dates, links, what happened,
    and what's included or true.
  - **Style** is how it's said: wording, tone, length, order, layout, line breaks,
    lists, punctuation, greetings and sign-offs.
  - **A mixed change counts as fact.** Style is what's left once the facts are out.
- **Read the replies.** Most are next steps, not corrections. Keep only requests about style ("shorter", "bullet these",
  "no exclamation marks"). Ignore requests about facts or content, and anything not
  about this draft.
- **Use samples as support.** Rewritten items and sent items with no Claude draft are
  the person's own writing. They can confirm a pattern (greetings, sign-offs,
  punctuation, length), but they show what the person writes, not what they change.

## Step 6: Find the rules

- **A rule** is something the person asked for in a reply, or something that
  shows up in 3 or more items. Anything less goes on a **Maybe** list, which is not
  applied yet.
- **Write each rule as one line Claude can follow and check:** "One sentence per
  line", "No exclamation marks", "End with the exact thing you want back". Never
  adjectives such as "be warm" or "be concise".
- **Show each rule's evidence:** the items it came from (as "3 of 7 items", never
  names), plus one short before → after.
- **Group the rules.** **Sound** covers words, tone, punctuation and sentence length.
  **Shape** covers order, layout, what goes first and how it ends.
- **Give each rule a scope** when the evidence has one, for example "(to senior
  colleagues)" or "(to students)". Otherwise it applies to everything this guide covers.
- **Keep the guide short.** Use at most 12 rules, most evidence first, and merge
  duplicates. If two rules contradict each other, keep the one with more evidence, or
  ask.
- **Add 2 or 3 examples:** whole before → after pairs that show several rules at once.
  Examples teach layout better than rules do, and rules teach tone better than
  examples do.
- **Try the rules before proposing them.** Rewrite one or two first drafts using only
  the rules, and show each one next to what was actually sent. Drop any rule that
  doesn't move the draft towards what was sent.
- **Two kinds of writing:** if the items are really two kinds (quick DMs and long
  updates, say), say so and offer two guides.

## Step 7: Redact, name it, and get approval

- **Redact everything that will be saved.** In every rule, before → after and example,
  replace people's names, email addresses, phone numbers, dates, amounts, schools,
  student or member details, and anything else that identifies someone with
  placeholders such as `[name]`, `[date]` or `[student]`. Keep only the words that show
  the style. If an example can't be made safe, drop it.
- **Name it** from the purpose and the destination: "Slack asks" becomes the folder
  `my-slack-asks-style`.
- **Show the exact text that will be saved,** in the Step 8 format, and ask: *"Keep,
  change or drop any rule? And is the name right?"* Apply their edits. Nothing is saved
  until they say yes.

## Step 8: Save the guide

Run `python3 scripts/pairs.py score` for the baseline. It's an estimate, because facts
are labelled by judgment. Then write `~/.claude/skills/my-<slug>-style/SKILL.md` (on
Windows, `%USERPROFILE%\.claude\skills\my-<slug>-style\SKILL.md`):

```
---
name: my-<slug>-style
description: <Name>'s style for <purpose> (<destination>). Use whenever drafting <purpose> for <Name>, or when they say "write like me" for <destination>. Built by write-like-me from their own edits.
---

# <Name>'s <purpose> style

Check every <destination> draft against these rules before showing it. Facts come
from the request, never from this guide.

## Sound
- <rule> (<n> of <N> items). <before> → <after>

## Shape
- <rule> (<n> of <N> items). <before> → <after>

## Examples
<2 or 3 redacted before → after pairs>

## Maybe (not applied yet)
- <pattern> (seen in <n> items)

## Measurements
| Date | Items | Style changed (median) | Sent with no style change |
|---|---|---|---|
| YYYY-MM-DD | N | X% | n of N |
```

If a folder with that name exists and isn't this guide, choose another name. If it is
this guide, show the changes before saving. Never touch any other skill.

Then run `python3 scripts/pairs.py clean` to delete the working files. Tell them where
the guide lives, that Claude uses it automatically from their next new session, and
that "update my <name> style" keeps it learning.

## Step 9: Update mode

Read the guide, and set the window to start at its last Measurements date. Run Steps
2 to 5 on the new items only. Then propose new rules, rules to drop (ones the new edits
contradict), and maybes to promote. Redact, show the changes, apply them on a yes, add a
Measurements row, and run `clean`. Say the new median against the first row in one line:
that is the evidence the guide is working. When new evidence contradicts an approved
rule, the newer evidence wins, but only after the person says so.

## Guardrails

- Read only. Never send, post or share.
- Only the person's own writing.
- No names, member or student details, or other identifying information in the
  guide.
- Working files stay in the temp folder until `clean` removes them. Delete nothing else.
