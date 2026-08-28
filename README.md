# agent-skills

One set of skills, shared by Claude Code and Codex, under git.

```
~/agent-skills/
  skills/<name>/SKILL.md     the only copy of anything
  commands/<name>.md         Claude Code slash commands (see below)
  bin/sync.sh                converge tool dirs onto the repo
  bin/skill                  new | rm | ls | sync | save | doctor
  bin/install.sh             bootstrap / re-attach on a new machine

~/.claude/skills   ->  ~/agent-skills/skills        (whole dir, "mounted")
~/.codex/skills/<name> -> ~/agent-skills/skills/<name>   (per skill, "projected")
~/.claude/commands/<name>.md -> ~/agent-skills/commands/<name>.md   (by hand)
```

## Slash commands

`~/.claude/commands/` cannot be mounted the way `~/.claude/skills` is: Claude
Code also fills it with symlinks to skills' own `SKILL.md` files, so the
directory has to stay real. Hand-written slash commands therefore live in
`commands/` here and are symlinked in one file at a time:

```sh
mv ~/.claude/commands/<name>.md ~/agent-skills/commands/<name>.md
ln -sfn ~/agent-skills/commands/<name>.md ~/.claude/commands/<name>.md
```

`sync.sh` does not manage these yet — a new machine needs the `ln` re-run by
hand after `install.sh`.

## Why the two sides are attached differently

`~/.claude/skills` holds nothing but your skills — Claude Code keeps plugin
skills in `~/.claude/plugins`. So the whole directory can be replaced by one
symlink at the repo. Anything an agent creates there is *already* in the repo;
no sync step exists to forget.

`~/.codex/skills` also contains `.system/` (imagegen, review-agent,
skill-installer, openai-docs) with a `.codex-system-skills.marker`, which Codex
manages. That directory has to stay a real directory, so skills are projected
into it one symlink at a time and Codex-authored skills need adopting.

Both tools were verified to follow symlinked skill dirs.

## The model

The repo is authoritative for which skills **exist**. Each tool dir is a
projection of it. The single exception: a real (non-symlink) directory
containing a `SKILL.md` inside a tool dir means "a skill was just authored
here", and gets adopted into the repo.

Because every projection is a symlink, **there is exactly one copy of a skill on
disk**. Editing a skill from either tool edits the same bytes, so edits need no
syncing whatsoever, and two copies can never diverge. Only create / delete /
rename touch the namespace, and those are what `sync.sh` converges.

| event | what happens |
|---|---|
| edit a skill in either tool | nothing to do — same file |
| create in Claude | already in the repo; next sync projects it to Codex |
| create in Codex | next sync adopts it into the repo; Claude has it via the mount |
| delete from the repo | next sync prunes the dangling Codex link |
| rename in the repo | old link pruned, new one made (fix `name:` yourself) |
| same name authored in both | refused, nothing modified, reported |

Deleting must happen in the repo (`skill rm`). Deleting a *projection* is not a
delete — sync will restore it, because the repo says the skill still exists.

## When it runs

Pass 2 is a full reconciliation, not event detection: it walks the repo and
creates whatever link is missing. Nothing has to *notice* that a folder was
created, so missed events, crashes and manual `mv`s all self-heal.

Hook ordering was measured against both CLIs, and they differ:

- **Codex** runs `SessionStart` hooks *before* enumerating skills. A link created
  by the hook is visible in that same session.
- **Claude Code** enumerates *before* its `SessionStart` hook finishes. A link
  the hook creates is only visible next session.

The asymmetry is harmless because it lands the right way round:

- **Claude -> Codex** needs a link, and Codex's early hook supplies it in time
  (belt and braces: Claude's `Stop` hook usually made it already).
- **Codex -> Claude** needs no link at all — `~/.claude/skills` *is* the repo, so
  a skill is visible the moment it exists there. What this direction depends on
  is adoption, which is why Codex also runs the hook on `PostToolUse`: a session
  killed before `SessionEnd` would otherwise strand a new skill in
  `~/.codex/skills`, unadopted and invisible to Claude.

Adopting a skill while an agent is still authoring it is transparent — the
directory is replaced by a symlink to its new home, so writes to the original
path keep landing in the right place (verified).

`sync.sh` is idempotent, single-flighted with `flock`, and always exits 0, so it
can never block a session from starting.

Caveat: a session already running won't see a skill created elsewhere until it
restarts. Nothing can fix that — discovery happens at startup.

## New machine

```sh
git clone <remote> ~/agent-skills && ~/agent-skills/bin/install.sh
```

## Commits

Never automatic — that would bury real changes in per-session noise. `sync.sh`
reports the dirty count at session start; `skill save "msg"` commits.
