---
name: docs-sync
description: >-
  Full documentation audit for this repo, run only on explicit request (/docs-sync in Claude Code, $docs-sync in Codex). Syncs every .md file and CLAUDE.md against code changes since the last Docs-Sync-Checkpoint commit, archives landed design/plan/fix docs, tightens the doc index and CLAUDE.md task map, enforces progressive disclosure, reconciles Claude Code project memory, and commits. Not for ordinary doc edits: do not invoke this when a task merely touches a README, adds a docstring, or asks to "update the docs" for one change — edit the file directly instead.
disable-model-invocation: true
---

# Docs Sync

**Explicit invocation only.** This skill is a heavyweight, repo-wide operation that moves files into `docs/archived/`, rewrites CLAUDE.md, edits Claude Code memory, and commits. It runs only when the user invokes it by name (`/docs-sync` or `$docs-sync`). If you are reading this because it was auto-selected for a task that just mentions docs, stop: do not run the workflow, and handle the doc edit directly.

Audit all markdown files and CLAUDE.md against recent code changes, then update them.

**Guiding principle — the top-level surface is minimal.** A doc describes only what a subsystem *is* and how/when to use it — never its history, rationale, or implementation detail. Code is the single source of truth for *how things work*; a doc is only an entry point into the code. Once a design, plan, or fix has landed and the subsystem is **settled** — the question it answered is closed and you won't reopen it — its doc is archived, not kept-and-polished. Only **living** subsystems (ones you keep running or keep changing) earn a place on the top-level surface.

Five jobs:
1. **Content sync** — ensure living docs reflect features added since the last docs-sync checkpoint
2. **Archival discipline** — archive docs whose work has landed and whose subsystem is settled; strip history/implementation prose from the ones you keep (Phase 2.3, 2.5)
3. **Routing surface** — ensure the doc index, CLAUDE.md task map, and scripts index route a coding agent to the one right doc / script / code-file list, loading right-sized context at each hop
4. **Progressive disclosure** — ensure docs are structured for scanability and layered depth
5. **Memory cleanup** — ensure this project's Claude Code memory notes still match the current codebase (Phase 4.7)

The doc surface is primarily read by coding agents. Optimize for an agent that arrives with a task, not a human reading linearly: it scans an index, opens at most one topic doc, and wants the code file list as early as possible.

The "last docs-sync checkpoint" is the most recent commit whose message contains the trailer `Docs-Sync-Checkpoint: true`. This marker is written by Phase 6 of this skill, so the baseline advances only when a real docs-sync runs — not when some unrelated commit happens to mention "doc".

## Workflow

### Phase 0: Sync with Remote

1. Check if a remote exists (`git remote -v`)
2. If yes, pull latest changes (`git pull --rebase`) to ensure working on up-to-date code
3. If no remote, skip silently

### Phase 1: Gather Context

1. Run `bash <skill_path>/scripts/git_changes_since_last_doc.sh` to get:
   - The last commit with the `Docs-Sync-Checkpoint: true` trailer (the baseline)
   - All commits and file changes since then
   - If the script prints `NO_DOCS_SYNC_CHECKPOINT_FOUND`, this is the first run with the marker — the script falls back to the last 20 commits, and Phase 6 will plant the first checkpoint.

2. Find all doc files: `glob **/*.md` and `glob **/CLAUDE.md`

3. Read changed code files — focus on new/modified modules, public APIs, CLI flags, config

### Phase 2: Content Sync Audit

For each doc file, check coverage of changes from Phase 1:

- **New features/modules** — documented somewhere?
- **Changed APIs/CLI/config** — docs reflect new signatures/options?
- **Removed features** — stale references cleaned up?
- **New dependencies** — setup/install docs updated?

**CLAUDE.md specifically — check for:**
- New build/test/lint commands
- New conventions or patterns from recent commits
- Changed file structure or module organization
- New environment variables or configuration
- Whether it has become a duplicate of the doc index or scripts index (run the CLAUDE bloat gate in Phase 2.7)

Do not pause for confirmation — proceed directly to fixing.

### Phase 2.3: Classify each doc on two axes (code is the referee)

Before consolidating, archiving, or restructuring anything, classify every non-archived doc on **two independent axes**. Classify by checking the **code**, not the doc's self-reported status — grep for the symbols, files, flags, and tests the doc names. A doc that says "implemented" can be stale in either direction. For repos with many plan docs, fan this verification out to a search subagent.

**Axis 1 — lifecycle (has the code landed?):**

- **CURRENT** — describes shipped code as a reference. Sync content normally, then apply Axis 2.
- **PLAN-PENDING** — proposes work whose code has NOT landed. **Leave the body alone.** Do not archive, merge, restructure, or polish its prose — it is a working design document someone intends to execute. Only ensure (a) its first lines and (b) its index row clearly say it is a plan and what is pending.
- **PLAN-PARTIAL** — some stages landed, some pending. Leave pending sections untouched; update only the status markers so an agent knows exactly which sections describe live code vs intent (e.g. "Status: guard implemented; deep fix planned").
- **PLAN-LANDED** — everything it planned is now in code. Eligible for Axis 2 and Phase 2.5.
- **DIAGNOSIS-RESOLVED** — root-cause writeup of a fixed issue. Almost always **settled** (see Axis 2): archive once the fix has landed, unless the mechanism is one you actively re-tune and an agent would reopen the writeup to do so.

When unsure between PENDING and PARTIAL, err toward leaving the doc alone and fixing only the status line.

**Axis 2 — liveness (will a future task open this doc to do work?):** apply to every CURRENT, PLAN-LANDED, or DIAGNOSIS-RESOLVED doc. Ask one question — *will a future task plausibly open this doc to get work done?* This axis is independent of lifecycle: a doc can describe live, correct code and still be settled.

- **LIVING** — fronts a subsystem you keep running or keep changing (the submission flow, the training loop, the sampler). Keep a lean top-level doc; it earns a CLAUDE.md row.
- **SETTLED** — the question the doc answers is closed: a proven parity/equivalence result, a one-time migration, a resolved fix you won't re-tune, an established measurement. Archive it in Phase 2.5, even though the code it describes is live and correct. A settled doc is not *wrong* — it is *answered*, and answered questions do not belong on the always-scanned surface.

A doc can be **mixed** — part living, part settled (e.g. an eval doc whose local-vs-remote parity study is settled but whose submission commands are living). Do not let the settled half hold the living half hostage: split it (Phase 2.5).

### Phase 2.5: Archive by default; keep or recreate by exception

The default fate of a doc whose work has landed is **archive**, not keep-and-polish. History, rationale, "why X over Y", touchpoints, migration steps, round-by-round logs, and diagnosis writeups have no place on the top-level surface — they belong in `docs/archived/` (and in git). Recreate or keep a top-level doc only when the subsystem is **LIVING** (Phase 2.3 Axis 2) and an agent needs an entry point the code alone doesn't provide.

**Decision, per doc classified in Phase 2.3:**

- **SETTLED (any lifecycle)** — `mv` to `docs/archived/`. Do **not** recreate. Drop its index and CLAUDE.md rows. If a living doc still needs the result, add a one-line pointer to the archive, never a re-explaining section.
- **PLAN-LANDED + LIVING** — archive the plan-stage doc(s), then recreate ONE lean doc from code (below). Skip recreation if a CURRENT living doc already covers the how/when-to-use surface — archive + a pointer suffices; don't recreate a doc nobody will route to.
- **CURRENT + LIVING** — keep, but strip it to the living contract (see "What a kept doc contains").
- **Mixed** — split: archive the settled section (as its own file, or folded into an existing archive), keep/promote the living section. Don't let a settled half keep a living half buried.

**Hard guard:** never archive a PLAN-PENDING or PLAN-PARTIAL doc — a pending plan that disappears into `archived/` looks abandoned and its work gets lost. Only landed/settled material is eligible.

**Consolidation trigger** — multiple sprawled docs on one *landed* subsystem, signalled by any of: ≥3 overlapping-scope docs (e.g. `td_training.md`, `td_data_pipeline.md`, `td_grad_unification.md`); "Touchpoints"/"Migration"/"Plan"/"v2"/"followups"/dated-rebuild sections whose code has landed; a "supersedes"/"superseded by" chain; or a single subsystem whose docs exceed ~600 lines across files. Move the whole cluster to `docs/archived/` in one `mv`, then — only if the subsystem is LIVING — recreate ONE lean doc. Do not merge in place; design-stage prose contaminates the result.

**Recreate from code, not from the archived docs.** Open the actual implementation (entry points, argparse blocks, schema constants, top-level classes, tests). Use the archived docs only to recover the one-sentence *motivation* for a non-obvious choice, and only when that motivation is load-bearing for a future caller.

**What a kept or recreated doc contains — and nothing else:**
- **Purpose** (1–2 sentences): what the subsystem *is*.
- **`Files:` line:** the code it fronts.
- **How / when to use:** the commands, flags, and the situations that call for each.
- **Contracts / invariants** a caller must respect (artifact schemas, ordering guarantees, gate conditions).

**What it must NOT contain — these are the archive/strip signals:**
- History: rationale narratives, "why X over Y", explored alternatives, round-by-round or tuning logs, migration/touchpoints sections, diagnosis writeups.
- Implementation walk-throughs that restate what the code already says — point at the file/function and stop. Code is the source of truth for *how it works*.

Aim for one doc per "open when" row, ~100–300 lines, structured runbook → contracts → invariants → tests. **Repoint the index** after any move: update `docs/README.md`, `CLAUDE.md`/`AGENTS.md`, and cross-links; `grep -rn "<archived-stem>" docs/ CLAUDE.md AGENTS.md | grep -v archived/` must return nothing. **Leave archived files alone** — do not edit them; they are historical context for future agents researching rationale.

### Phase 2.7: Routing-Surface Audit (how an agent finds the right file)

The doc surface exists so a coding agent can route: **task → index row → one topic doc → code files**, loading right-sized context at each hop. Run `bash <skill_path>/scripts/doc_surface_audit.sh` first — it mechanically flags `ORPHAN_DOC` (doc missing from the index), `DEAD_LINK` (index row pointing at nothing), and `UNINDEXED_SCRIPT` (script with no row in the scripts index). Fix every finding — or explicitly dismiss it in the summary when intentional (e.g. a link to a deliberately untracked local-only file whose index row says so). Then audit the three layers by hand:

1. **Doc index** (`docs/README.md` or equivalent) — the **complete** router: one row per active doc (living, plans, and any settled doc not yet archived), each carrying what it *owns*, when to *open* it ("open when …"), and a lifecycle tag for anything not CURRENT (e.g. "plan, not yet implemented", "partial: guard shipped, deep fix planned", "resolved diagnosis"). The tag is what stops an agent from implementing against a design that never shipped — or re-proposing one that did. If one task plausibly matches 2+ rows, sharpen the "open when" wording until the rows are disjoint.
2. **CLAUDE.md / AGENTS.md task map** — this is the tier-0 context loaded into **every agent**, not a compressed copy of the README index. Keep project constraints plus only 5–8 high-frequency workflow rows; 10 rows is a hard review limit. Each row is a short task cue and one start link. A linked topic doc owns its `Files:` list, commands, flags, and specialist vocabulary; do not repeat them in CLAUDE.md. For a narrow task that spans multiple specialist docs, point one umbrella row to the complete doc index instead of adding rows for each subsystem. Add a row only when it displaces no more useful route; drop it when the linked work is settled, rare, or already covered by an umbrella route.

   **CLAUDE bloat gate — run before completing every sync:**
   - Run `wc -w -l CLAUDE.md` (or its equivalent) and count task-map rows.
   - Default maximum: 500 words, 45 nonblank lines, and 8 task-map rows. Required legal/security/user instructions may exceed the budget, but record the reason in the summary.
   - Fail the gate if a row contains a code-file inventory, CLI flags, implementation detail, lifecycle history, or a second-level list of docs. Move that material to the linked topic doc or the complete index.
   - Confirm that an agent can choose a broad starting document from CLAUDE.md, then use the index for any specialist decision. If CLAUDE.md alone routes every niche task, it is bloated.
3. **Scripts index** (`docs/scripts.md` or equivalent) — every entry-point script gets one line: what it does + when to run it, grouped by workflow (train / eval / submit / debug …), not alphabetically. A script an agent can't find from an index may as well not exist; it will be reimplemented.

**Right-sizing check** per topic doc: the first ~10 lines should carry the purpose (1–2 sentences), a `Files:` line listing the code it documents, and a status line if it's a plan — enough for an agent to make a load-vs-skip decision without reading further.

### Phase 3: Progressive Disclosure Audit

Read `<skill_path>/references/progressive-disclosure.md` for the full checklist (11 sections + anti-patterns + validation tests). Priority-order the fixes:

**P0 — fix immediately:**
1. **Lead with purpose** — first 1–2 sentences answer what and why
2. **Layered headings** — H1 > H2 > H3, ordered by usage frequency
3. **Quick start near top** — minimal steps within first 3 sections
4. **Machine-parseable patterns** — language-tagged code blocks, consistent formatting, inline code for paths/commands
5. **Context-loading strategy** — files <300 lines, one concern per file, doc index exists

**P1 — fix soon:**
6. **Conciseness** — no section > ~50 lines; bullets over paragraphs; no filler phrases
7. **Detail deferral** — advanced content at bottom or in separate files; use `<details>` tags
8. **Token-efficient structure** — no restating what code communicates; large examples in separate files
9. **CLAUDE.md specifics** — enforce the bloat gate; keep constraints and a short start-doc map, not commands or a files-to-read inventory
10. **Cross-linking** — link not duplicate; relative paths; no dead links

After fixing, run the **validation tests** from the reference (5-second, 30-second, file-count, LLM-load, staleness).

### Phase 4: Apply Updates

1. Edit each file directly — add missing docs, restructure for disclosure, remove stale content
2. Keep edits minimal — do not rewrite correct, well-structured content
3. Do not pause for confirmation — proceed directly

### Phase 4.7: Memory Cleanup (project memory vs. current code)

Claude Code keeps a per-project memory store — free-text notes about the project, its
workflows, and hard-won gotchas — separate from the docs. These notes drift out of sync with
the code just like docs do, but nothing else audits them. Reconcile them here.

**Locate the store — never hardcode the path.** Run
`bash <skill_path>/scripts/locate_project_memory.sh` to derive it from the current repo root at
runtime (Claude Code stores it under `~/.claude/projects/<encoded-repo-root>/memory/`, where the
repo path's `/` become `-`). The script prints:
- `MEMORY_DIR=…` (or `MEMORY_DIR_NOT_FOUND` → **skip this phase silently**; not every project has memory)
- `MEMORY_INDEX=…` pointing at `MEMORY.md` (the one-line-per-note index), or `MEMORY_INDEX_ABSENT`
- one line per note file under `---MEMORY_FILES---`, each prefixed with its age in days

**The gate is future usefulness, not code-verifiability.** The question for every note is *"would a
future session benefit from recalling this?"* — NOT *"can I find it in the code?"* Those come apart, and
conflating them makes the audit too timid (nothing gets deleted) — the common failure mode. "I can't
find it in the code" splits two ways: state that is **confirmed gone** (a positive staleness signal, act
on it) versus knowledge that was **never code-shaped** (verification doesn't apply, keep it). Do not park
the first in the second.

First, tag what *kind* of claim each note (or each paragraph of a mixed note) is:

- **State-tracking** — asserts what the code/artifacts *currently are*: a file path, function/class/flag
  name, checkpoint id, config value, "the default is X", "the sampler points at Y". Code is the referee —
  grep for the symbol/path/value.
- **Transferable lesson or non-code fact** — a gotcha, workflow trap, modeling insight, design rationale,
  environment/hardware fact, user preference, or external pointer. Not verifiable from code and does not
  need to be; it fires in *future* work regardless of whether any specific artifact still exists.

Then classify and act. Prioritize the oldest notes (highest age-in-days) — drift accumulates with time.

- **CURRENT** — state-tracking claim that still matches the code. Leave it.
- **FIX-IN-PLACE** — state-tracking claim that is mostly right but names a renamed/moved symbol, a
  superseded checkpoint, or an outdated value. Correct that specific claim; don't rewrite the whole note.
- **DELETE (confirmed-gone state)** — a note whose value was pointing at code/artifacts that **no longer
  exist**, and it carries no transferable lesson. This is the case the timid version wrongly preserved:
  the subject is gone, so the note can never usefully fire again, and because memory has no "this is
  history" tag a future session will recall it and treat the dead state as current — worse than a stale
  doc. Confirmed-absent *is* positive confirmation; delete the file and its `MEMORY.md` row. There is **no**
  requirement that a newer note "covers the truth" first.
- **DELETE (dead narration)** — a running log of past attempts/rounds/decisions with no forward-looking
  pointer and no reusable lesson. This is history a future session won't re-pull; drop it.
- **KEEP (lesson)** — a transferable lesson or non-code fact, *even when the code that triggered it is
  gone*. This is the genuinely un-verifiable, still-useful knowledge — do not delete it for failing a code
  grep. If it is phrased as current state ("the loop is at round 7…"), reframe the durable lesson and cut
  the perishable state around it.
- **PRUNE (mixed note)** — a note that is part live pointer / lesson and part dead state or narration (long
  running logs are usually this). Cut the dead paragraphs, keep the live ones. Do **not** leave the whole
  note untouched just because one paragraph is still current.

**Guards:**
- Act only on drift you have **positively confirmed** — either the code confirms a value changed, or a
  grep confirms a named symbol/path/checkpoint is **absent**. "I didn't check" is not "confirmed gone";
  when you truly haven't verified, verify or leave it. But do not hide behind uncertainty to avoid every
  deletion — a confirmed-absent subject is a delete, and refusing all deletes is itself a failure.
- The store lives under `~/.claude/`, outside git, so a delete has no version-control undo. That argues
  for **pruning the dead part in place** over nuking a whole mixed note — not for freezing everything.
- Never delete a note solely for being old or for failing a code grep; age and non-code-shape are not
  staleness. The delete triggers are confirmed-gone state and dead narration, nothing else.
- Keep `MEMORY.md` consistent with the files: every remaining note has exactly one index row; deleted
  notes have none. Match the existing row format (`- [Title](file.md) — hook`).
- Preserve each note's frontmatter and `type`; edit only the body that drifted.
- Report every memory change in the Phase 5 summary. Memory edits are **not** committed in Phase 6 —
  the store lives outside the repo (under `~/.claude/`), so it is never staged or pushed.

### Phase 5: Summary

Output:
- Files updated and why
- **Archival decisions (from Phase 2.5)** — each doc archived / recreated / split / kept, labelled living or settled with the one-line reason, plus each CLAUDE.md row dropped. This is the record you review to catch a wrong settled-vs-living call and revert via git.
- Content added/removed
- Structural changes made
- Memory notes fixed / pruned / deleted vs. kept (from Phase 4.7), each with the trigger (confirmed-gone state, dead narration, or verified current), or "no project memory store" if skipped
- Items needing manual attention (screenshots, diagrams, external links)

### Phase 6: Commit

Immediately after edits — no confirmation needed:

1. Stage only the updated doc files (`git add` each changed `.md` file by name, including files moved to `archived/` — do not use `git add .`)
2. Commit with a message prefixed by `docs:`, e.g. `docs: sync documentation with recent changes`
3. Include a brief body listing which files were updated and why
4. **Plant the checkpoint trailer.** The commit message MUST end with a trailer line:

   ```
   Docs-Sync-Checkpoint: true
   ```

   This is what the next run of this skill greps for to find the baseline. Without it, the next sync will not know where to start. Pass the message via heredoc so the trailer lands on its own line, e.g.:

   ```
   git commit -m "$(cat <<'EOF'
   docs: sync documentation with recent changes

   - Updated README.md with new CLI flags
   - Refreshed CLAUDE.md build commands

   Docs-Sync-Checkpoint: true
   EOF
   )"
   ```

   Plant this trailer **even if no doc files changed** — create an empty commit (`git commit --allow-empty`) so the baseline still advances. This prevents the next run from re-scanning the same window of code commits.
5. Check if a remote exists (`git remote -v`). If yes, push (`git push`). If no remote, skip push silently
