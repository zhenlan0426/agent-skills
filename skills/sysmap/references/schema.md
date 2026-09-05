# system-map.yaml schema

The data file is the single source of truth for the **high-level page**. Edit
it; never hand-edit `system-map.html`. Detail does not live here at all — it
lives in hand-authored deep-dive pages, one per top-level component that
exists in code, linked from the map (see
[deep-dive-pages.md](deep-dive-pages.md)). A `planned` component has no
deep-dive page and no `deep_dive` key. Every field holding prose supports minimal markdown:
paragraphs separated by blank lines, `- ` bullets, `` `code` ``, `**bold**`.

Write all prose for the human owner: plain language, no unexplained jargon.
Any term of art used in `overview`, `summary`, or `changelog`
should have a glossary entry — the renderer auto-links the first occurrence
in each text block to its glossary definition with a hover tooltip.

```yaml
meta:
  project: Cell Tracking          # page title: "<project> — System Map"
  updated: 2026-08-23             # date of last reconcile (set on every update)
  commit: 7e42b0d                 # WATERMARK: last commit reconciled into this map.
                                  # `/sysmap update` diffs commit..HEAD, then advances it.
  artifact_url: https://claude.ai/...   # set after first publish; republish here
  favicon: "🗺️"                   # emoji for the Artifact tab icon (keep stable)

overview: |                       # L0 — must fit on one screen. What the system
  Plain-language description...   # does end to end, in the owner's vocabulary.

components:                       # L1 tree, drill-down via nesting
  - name: Detection
    status: validated             # planned | in-progress | implemented | validated | abandoned
    summary: One or two sentences. This is all the map shows for a node.
    deep_dive:                    # optional; top-level components that exist in
                                  # code. OMIT on a `planned` component.
      file: docs/sysmap/detection.html      # the hand-authored page in this repo
      url: https://claude.ai/code/artifact/...  # its published Artifact (set after first publish)
      label: Deep dive            # optional link text, defaults to "Deep dive"
    children:                     # optional, same shape, arbitrarily deep
      - name: NMS
        status: implemented
        summary: ...

# There is NO `details:` key. A details block in the YAML fails the render on
# purpose: that prose belongs on the component's deep-dive page.

glossary:
  - term: rung                    # matched case-insensitively, word-boundary
    definition: Plain-language meaning, one to three sentences.
    why: What problem forced this concept to exist.        # optional
    where: scripts/p6_rank_candidates.py                   # optional pointer
    # No deprecated state: when a concept dies, DELETE its entry.
    # The glossary describes the current system, not its history.

changelog:                        # NEWEST FIRST. Write each entry for someone
  - date: 2026-08-23              # who was away, in ratified vocabulary.
    title: Short headline         # Top 3 render expanded, the rest collapse.
    notes: |
      - What is now true that wasn't.
      - Status changes (X moved to validated).
```

## Status meanings

| Status | Meaning |
| --- | --- |
| `planned` | Designed or agreed, no code yet — no deep-dive page either |
| `in-progress` | Being implemented right now |
| `implemented` | Code exists and passed review |
| `validated` | Measured / tested end-to-end and shown to work or help |
| `abandoned` | Tried and dropped — keep it, with the reason in the `summary` or on the deep-dive page, so dead ideas aren't re-proposed |

The implemented/validated split is deliberate: "the code exists" and "it was
measured and it helps" must be different colors.

## How the component tree renders

Every node renders as one headline: a numbered path badge (`2.4.1`, from tree
position), the `name`, the status chip, and the `summary` underneath.
Top-level components become cards with a status-coloured left edge; children
are hairline-separated rows inside the card; grandchildren are indented behind
a rule. A card is open on load, deeper nodes start collapsed. An
"Expand all / Collapse all" pair sits above the tree.

A `deep_dive` renders as a pill link under the summary — with a `url` it opens
the published page in a new tab; with only a `file` it renders greyed as
"not published yet", which is the reminder to publish it. A node with no
`deep_dive` renders no pill at all, which is the correct look for a `planned`
component: status, name and summary, nothing to open yet.

So `summary` must read as a standalone one-liner: it is the only prose the map
carries for that node, and anything longer belongs on the deep-dive page.

## Size discipline

- `overview`: one screen, hard cap. If it grows, push detail down.
- Top-level components: aim for 4–9. More means the altitude is wrong.
- `summary`: two sentences max. Everything else goes on the deep-dive page.
- The whole map should stay scannable in a couple of minutes. It is an index
  with status colours, not a document.
- Unknown `status` values, and any `details:` key, fail the render on purpose.
