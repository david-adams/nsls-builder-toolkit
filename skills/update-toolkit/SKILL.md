---
name: update-toolkit
description: Update the NSLS builder toolkit to its newest release right now. Use when someone says "update my NSLS toolkit", "update the toolkit", "get the latest toolkit", "get the new skills", or can't find a toolkit skill a colleague told them to try.
---

# Update the NSLS toolkit

The toolkit already checks for a new release every time a session starts. This
is for "I want it now", or for a skill someone was told to try and can't find.

Run this, where `SKILL_DIR` is the directory holding this SKILL.md:

```bash
bash "$SKILL_DIR/../../hooks/run-hook.sh" update
```

It can take up to two minutes. Pass on what it prints in one or two plain
sentences, with no technical detail.

- **Updated:** tell the builder to open a new session to use the update. The
  session they are in keeps the old version.
- **Already up to date:** say so. If they were looking for a particular skill,
  it has probably not been released yet.
- **Did not finish:** suggest trying again in a few minutes.
