---
name: sysmap
description: Maintain a human-facing living system map for the current project — a docs/system-map.yaml source (component tree with implementation status, plain-language glossary of agent-coined terms, "since you last read" changelog) rendered deterministically to HTML and published as an Artifact, plus one hand-authored free-format deep-dive page per top-level component, each its own Artifact linked from the map. Use when the user says /sysmap (init, update, render), asks to create or refresh the system map or glossary, says a feature landed and the map should be updated, or asks "what changed since I last looked". Also owns the hands-off two-agent review protocol (findings ledger between an implementer and a reviewer agent) — use when the user asks to run or set up such a review cycle.
---

# System Map

The audience is the human owner, who designs the system but does not read the
code. Agents author most changes and most vocabulary; this map is how the
owner stays in touch. Everything written into it must be plain language in
terms the owner has seen — jargon only with a glossary entry behind it.

The map comes in two layers, and they are edited differently:

- **The high-level map** — `docs/system-map.yaml`, rendered to
  `docs/system-map.html` by `scripts/render_sysmap.py`. Overview, component
  tree with a status and one summary per node, changelog, glossary. It has to
  stay readable in two minutes.
- **Deep-dive pages** — one hand-authored HTML page per top-level component
  in `docs/sysmap/`, published as its own Artifact and linked from that
  component's card. Free-format HTML, as visually rich as the subject needs,
  no length limit; this is where all detail lives. The map's fixed structure
  applies to the top-level page only.
  See [references/deep-dive-pages.md](references/deep-dive-pages.md).

Three invariants:

1. **The map's YAML is the only thing edited for the map.** Never hand-write
   or patch `system-map.html`; it is generated.
2. **Detail never goes back into the YAML.** There is no `details:` key — the
   render fails on one. Detail goes on the deep-dive page.
3. **Updates are derived from evidence, not session memory.** `meta.commit`
   is a watermark; an update reconciles `git log/diff <commit>..HEAD` against
   the map, so nothing is missed even when other sessions or other agents
   did the implementing, or cycles were skipped.

Schema, status meanings, and size caps: [references/schema.md](references/schema.md).
Read it before writing any YAML.

## /sysmap init  (bootstrap an existing project)

1. Read the schema reference. Explore the repo (docs first, then code) enough
   to draft `overview` (L0) and the top-level component tree with `summary`
   and `status` (L1). Respect the size caps.
2. **Show the L0/L1 draft to the user in the conversation and get their
   corrections before going deeper** — the map must match the owner's mental
   model, and ratifying names is the point. Ask which vocabulary is theirs
   vs. agent-coined.
3. Then seed the glossary with every term of art the map uses — especially
   agent-coined ones. Set `meta.commit` to current HEAD, `meta.updated` to
   today.
4. Write `docs/system-map.yaml`, render and publish (see below), then store
   the artifact URL in `meta.artifact_url`.
5. Write one deep-dive page per top-level component and link it, following
   [references/deep-dive-pages.md](references/deep-dive-pages.md). Publish
   each, paste its URL into that component's `deep_dive.url`, then re-render
   and republish the map.
6. Offer to append to the project's agent instructions file (CLAUDE.md or
   AGENTS.md — respect symlinks):

   ```markdown
   ## System map
   - `docs/system-map.yaml` is the owner's system map (see the sysmap skill).
     After a feature lands, run /sysmap update.
   - When messaging the user, define any term not in the map's glossary on
     first use; never invent user-facing vocabulary silently.
   ```

## /sysmap update  (after a feature lands)

1. Read `docs/system-map.yaml`. Diff the watermark:
   `git log --oneline <meta.commit>..HEAD` and `git diff --stat` (drill into
   files as needed). If the watermark commit is gone (rebase), fall back to
   date-based log since `meta.updated` and say so.
2. Reconcile components: status changes (implemented work → `implemented`;
   measured/validated work → `validated`; dropped work → `abandoned` with the
   reason), new components, edits to summaries. Never grow the overview or a
   summary to fit new detail.
3. Reconcile the deep-dive pages the diff touched: edit the affected sections
   of `docs/sysmap/<slug>.html` directly — new measurements, dropped
   approaches with the reason, changed contracts. A new top-level component
   needs a new page. Republish each page you changed (pass its `url`).
   **Touch nothing the diff does not reach**: do not rewrite, restyle, or
   "improve" unaffected summaries, glossary entries, or deep-dive pages —
   an update must leave everything else byte-identical, and an untouched
   page is not republished at all.
4. Reconcile the glossary: scan the diff for new user-facing concepts — new
   config knobs, new CLI flags, new doc headings, new recurring identifiers —
   and add plain-language entries for any not yet present. Delete entries for
   concepts that no longer exist — the glossary describes the current system,
   not its history (note the deletion in the changelog entry instead).
5. Prepend one changelog entry (date, headline, notes) written for someone
   who was away: what is now true that wasn't, and which statuses moved.
6. Set `meta.commit` to HEAD and `meta.updated` to today. Render and publish.
7. In the conversation, give the owner the changelog entry, the list of new
   glossary terms, and which deep-dive pages changed — that is their read for
   this cycle.

## Render and publish

```bash
python3 <skill-dir>/scripts/render_sysmap.py docs/system-map.yaml
```

The output (`docs/system-map.html`) is artifact-ready. Publish with the
Artifact tool: pass `url: meta.artifact_url` when set (same page, stable
bookmark), `favicon: meta.favicon` (keep it stable). A render failure about
an unknown status, or about a leftover `details:` key, means the YAML is
wrong — fix the YAML, never the script. `/sysmap render` = just this step,
after manual YAML edits.

Deep-dive pages are published individually, each with its own URL recorded in
the YAML under that component's `deep_dive.url`. They are not rendered from
the YAML and the render script never touches them.

## Review protocol

For running a hands-off review cycle between an implementer agent and a
reviewer agent with a findings ledger the owner can adjudicate: read
[references/review-protocol.md](references/review-protocol.md) and follow it.
When acting as the reviewer, tag findings and write the For-the-owner section
exactly as specified there; plain language rules apply doubly.
