# Progressive Disclosure Checklist for Documentation

Checklist for structuring docs so coding LLMs (and humans) extract maximum signal per token. Audit any doc against these items, fix what fails.

**Core principle:** Most important information first. Scanning the top gives a complete-if-shallow picture. Depth lives further down or in linked files. Every line must earn its token cost.

## How to Apply

1. Pick a doc file
2. Walk each numbered section below — check every box or fix the violation
3. Use the anti-patterns table as a second pass
4. Validate with the testing section at the end

---

## Checklist

### 1. Lead with Purpose [P0]
- [ ] First 1–2 sentences answer: **what** is this and **why** does it matter?
- [ ] No preamble, setup instructions, or boilerplate before the purpose statement
- [ ] A reader (or LLM) that reads only the first paragraph knows whether this file is relevant to their task

### 2. Layered Heading Structure [P0]
- [ ] H1 = project/component name (exactly one per file)
- [ ] H2 = major sections (Quick Start, Usage, Architecture, API, Contributing)
- [ ] H3+ = details within sections
- [ ] Sections ordered by **frequency of need** — most-used first, edge cases last
- [ ] Heading text is descriptive enough to be useful in a table-of-contents scan (no bare "Overview" or "Details")

### 3. Quick Start Near the Top [P0]
- [ ] A "Quick Start" or "Getting Started" section appears within the first 3 sections
- [ ] Contains the **minimal steps** to get running (3–7 steps max)
- [ ] Each step is one command or one action — no compound steps
- [ ] Defers environment setup, troubleshooting, and advanced config to later sections

### 4. Conciseness [P1]
- [ ] No section longer than ~50 lines without subsections or collapsible blocks
- [ ] Bullet points over paragraphs for lists of features, options, or requirements
- [ ] Code examples show the **common case only** — not every parameter or variant
- [ ] No redundant explanations (don't say the same thing in prose and in code comments)
- [ ] No filler phrases ("It should be noted that...", "As mentioned above...", "Basically...")
- [ ] Paragraphs max 3–4 sentences; prefer 1–2

### 5. Detail Deferral [P1]
- [ ] Advanced topics, edge cases, and exhaustive references live in **separate files** or at the bottom
- [ ] Long reference tables or API docs link out rather than inline
- [ ] Use `<details><summary>` tags for content most readers skip
- [ ] When linking out, the link text states **what the reader will find** (not "click here")

### 6. Machine-Parseable Patterns [P0]
- [ ] Code blocks use fenced syntax with **language tags** (` ```bash `, ` ```python `, etc.)
- [ ] Commands that should be run are clearly distinguished from output/examples
- [ ] Key-value information uses **consistent format** (tables, definition lists, or `key: value`) — not buried in prose
- [ ] File paths, function names, and CLI commands are in `inline code`
- [ ] No critical information conveyed only through formatting (bold/italic) that may be lost in plain-text contexts
- [ ] Lists use consistent marker style within a file (all `-` or all `*`, not mixed)

### 7. Token-Efficient Structure [P1]
- [ ] No walls of text restating what the code already communicates
- [ ] Changelogs, contributor lists, and badges are **not** in files an LLM will load for task context
- [ ] Large examples are in separate files and referenced, not inlined
- [ ] If a section exceeds ~30 lines, ask: can an LLM accomplish its task without this section? If yes, defer or link out
- [ ] Avoid deep nesting (H5+) — restructure into separate files instead

### 8. Context-Loading Strategy [P0]
- [ ] Repo has a **doc index** (README or DOCS.md) that maps file → purpose in 1 line each
- [ ] Each doc file's first line or heading makes its **scope** immediately obvious
- [ ] Files are sized so an LLM can load one file for one concern (<300 lines preferred, hard max ~500)
- [ ] Related but distinct topics are in **separate files**, not concatenated into a mega-doc
- [ ] CLAUDE.md (or equivalent) tells the LLM **which files to read for which tasks**
- [ ] Each topic doc's first ~10 lines include a `Files:` line naming the code files it documents — an agent that only needs the file list can stop there
- [ ] Plan/design docs state their implementation status (pending / partial / landed) in the first lines AND in their index row

### 9. CLAUDE.md / LLM-Instruction File Specifics [P1]
- [ ] Commands and workflows appear **before** architectural explanations
- [ ] Most-used commands listed first
- [ ] Conventions section is concise — one-liners per convention, not paragraphs
- [ ] No duplicate information that exists in other project docs — **link instead**
- [ ] Includes a short start-doc map: broad task → one topic doc (use the doc index for specialist routing)
- [ ] Map has 5–8 rows; 10 rows triggers a consolidation review
- [ ] Stays within 500 words and 45 nonblank lines unless required instructions justify the excess
- [ ] Does not repeat topic-doc file inventories, CLI flags, implementation details, or lifecycle history
- [ ] States constraints and anti-patterns the LLM should avoid (not just what to do)
- [ ] Uses imperative mood ("Run X", "Do not Y") — not descriptive mood ("X is typically run")

### 10. Cross-Linking [P1]
- [ ] Related docs are linked, **not duplicated**
- [ ] Links use **relative paths** within the repo
- [ ] Dead links are removed
- [ ] Circular links are avoided — define a clear hierarchy (index → topic → detail)
- [ ] Anchor links to specific sections used when linking to large files

### 11. File Organization [P2]
- [ ] Docs live in a predictable location (`/docs`, `README.md` at root, `CLAUDE.md` at root)
- [ ] File names are lowercase-kebab-case and descriptive (`api-reference.md`, not `docs2.md`)
- [ ] One clear entry point exists (README or index) that links to everything else
- [ ] No orphan docs — every file is reachable from the entry point

---

## Anti-Patterns

| Anti-Pattern | Fix | Priority |
|---|---|---|
| Wall of text before any actionable content | Move context after quick start | P0 |
| Pending plan doc archived/rewritten as if landed | Classify lifecycle from code first; leave pending plans alone | P0 |
| Doc index row with no lifecycle tag on a plan doc | Tag rows: plan / partial / resolved diagnosis | P0 |
| Entire API reference inline in README | Extract to `api.md`, link from README | P1 |
| Same info in README + CLAUDE.md + CONTRIBUTING.md | Single source of truth; link from others | P1 |
| CLAUDE.md mirrors the doc index or lists each subsystem's files | Keep 5–8 broad start-doc rows; route specialist work through the index | P0 |
| All sections at same heading level | Add hierarchy with H2/H3 | P0 |
| Alphabetical ordering of sections | Order by usage frequency | P1 |
| Long changelog inline | Move to `CHANGELOG.md` or rely on git history | P2 |
| Code blocks without language tags | Add ` ```lang ` to every fenced block | P0 |
| Critical info only in comments within code blocks | State it in prose too (or instead) | P1 |
| Mega-doc covering 5+ unrelated topics | Split into focused files, link from index | P1 |
| Vague link text ("see here", "more info") | Link text = what the target contains | P2 |
| Instructions written in passive/descriptive voice | Use imperative: "Run X", "Add Y to Z" | P2 |
| Nested docs deeper than 3 directory levels | Flatten; use naming conventions for grouping | P2 |

---

## Validation

After applying the checklist, verify:

- [ ] **5-second test:** Reading only H1 + H2 headings tells you what this project does and what's in the doc
- [ ] **30-second test:** Reading the first paragraph + Quick Start is enough to start using the project
- [ ] **File-count test:** No single file tries to be the only doc — concerns are separated
- [ ] **LLM-load test:** An LLM given just this file (or just CLAUDE.md) can identify which other files to read for a given task
- [ ] **Staleness test:** No information is duplicated in a way that will cause inconsistency when one copy is updated
- [ ] **Tier-0 budget test:** CLAUDE.md has at most 500 words, 45 nonblank lines, and 8 task-map rows unless a documented required-instruction exception applies

---

## Priority Key

| Tag | Meaning |
|---|---|
| **P0** | Fix immediately — high impact on LLM efficiency and reader comprehension |
| **P1** | Fix soon — causes meaningful token waste or confusion |
| **P2** | Fix eventually — polish and maintainability |
