---
description: Retire a landed implementation spec and replace it with a living, agent-facing usage doc built from the code.
argument-hint: <landed-spec-doc> [new-doc-name]
disable-model-invocation: true
---

The implementation spec **`$1`** has landed. Retire it and replace it with a living, agent-facing
usage doc built from the code.

**0. Locate and orient.** Resolve `$1` against this repo's docs — the argument may be a bare name, a
slug, or a path. If it is empty or matches nothing, stop and ask which doc landed; do not guess.
Then learn this repo's conventions before writing anything: where docs live, whether there is a docs
index or table of contents, whether there is an archive/ or superseded/ subdirectory (create one
alongside the docs if not), and which file carries the agent instructions (`AGENTS.md`,
`CLAUDE.md`, `.cursorrules`, or similar — check whether one is a symlink to another, and if so edit
the real file). Match the existing house style for headings, link style, and line width. Name the
new doc `$2` if that argument was given; otherwise name it after the subsystem, not after the phase
— the phase label dies with the spec.

**1. Verify before writing.** Read the spec, then confirm against the repo what actually shipped:
entry points, modules, config keys, tests. Where code and spec disagree, the code wins. If part of
the spec did not land, say so in your report and leave it out of the new doc — do not document
intent as behaviour.

**2. Write the new doc from the code.** Present tense, current behaviour only, no plan / phase /
status / roadmap language. Use the spec only to recover *why* a contract is shaped the way it is.
Target reader: a coding agent that must use and modify this subsystem without reading the source
first. It must contain:
  - A one-paragraph opener: what the subsystem is, which directories/files it spans.
  - A "not here" paragraph linking the docs that own adjacent topics, plus a link back to the
    archived spec for rationale.
  - **Quick start**: copy-pastable commands with real paths, fixtures, or sample inputs that exist
    in the repo.
  - **The public surface**: entry points, value types, config schema with every key, its default,
    and the file that defines it. Enough that an agent can call it correctly without grepping.
  - **A change→file map**: for each thing likely to change, the exact files to edit, in order, and
    the test that must stay green.
  - **Invariants and gates**: what must not break, how it is enforced, and what to do when a change
    legitimately breaks it.
  - Anything a future agent would otherwise only learn by reading the source or by getting it wrong.

**3. Archive the old doc.** `git mv` it into the archive directory. Add a status block near the top
— `Status: **archived <today's date>.**` — stating that it landed, naming the living doc that
replaces it, and warning it is not a description of current behaviour. Do not otherwise rewrite the
body.

**4. Fix every inbound reference.** Grep the whole repo for the old filename and repoint links,
adjusting relative prefixes for the doc's new depth. Update the docs index: replace the old entry
with one for the new doc, described specifically enough to route on. Update the agent-instructions
file if the routing changed.

**5. Verify.** No dangling links; every command, path, config key, and symbol named in the new doc
exists. Then report: what the new doc covers, what you dropped as unlanded or stale, and every file
you touched.

Throughout, follow this repo's own documentation rules if it states any. Otherwise: precise
symbol/file/config names, progressive disclosure (details in the topic doc, not the index), and no
backward-compatibility prose.
