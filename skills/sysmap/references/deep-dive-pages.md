# Deep-dive pages

The map is an index; the deep-dive pages are the documents behind it. One page
per top-level component, hand-authored HTML, published as its own Artifact
with its own stable URL, reached from the `deep_dive` link on that component's
card.

Why the split: the map has to stay readable in two minutes, and detail has no
natural size limit. Every time detail grew inside the map it pushed the
altitude wrong. Now the map carries status and one summary per node, and
everything that wants a table, a diagram, a measurement history or a worked
example goes on the page.

## Division of labour

| | High-level map | Deep-dive page |
| --- | --- | --- |
| Source | `docs/system-map.yaml`, rendered | hand-authored HTML in `docs/sysmap/` |
| Content | overview, tree, status, one summary per node, changelog, glossary | everything else |
| Format | fixed by `render_sysmap.py` — structure applies to this page only | free-format HTML, as visually rich as the subject needs |
| Edited by | the YAML only, never the HTML | the HTML directly |

Rules that keep the two honest:

- **Names, statuses and vocabulary are the map's.** A page may not rename a
  component or invent a term of art the glossary does not carry. If a page
  needs a new term, add it to the glossary in the same cycle.
- **No duplication of prose.** The page starts where the summary stops; do
  not restate the summary as the page's opening paragraph.
- **Say what is unresolved.** The owner reads these pages to make decisions —
  an open question, a measurement that has not been run, or a result that
  lost, is more valuable than another paragraph on what works.
- **Evidence, not memory.** Numbers on a page come from the repo's own docs,
  run logs or code, and carry their date when they are measurements.

## Authoring one

There is no template. Each page is free-format HTML, designed for its
subject: a pipeline earns a diagram, a calibration story earns charts and a
measurement table, a contract earns annotated examples. Be as visually rich
as the subject deserves — and no richer. The fixed structure of the system
map applies to the top-level page only; it stops at the deep-dive link.

1. Before writing, load the `artifact-design` skill (and
   `artifact-diagramming` when the page wants diagrams) — these pages are
   Artifacts and follow the artifact rules: self-contained, theme-aware,
   responsive.
2. Create `docs/sysmap/<slug>.html`, where `<slug>` matches the component
   name. Set `<title>` to the component's name (that is the artifact's name —
   keep it stable across republishes), and put a link back to the map
   (`meta.artifact_url`) near the top.
3. Write the page. Structure, length and visual language are free.
4. Add the link in the YAML:

   ```yaml
     - name: Graph pipeline
       deep_dive:
         file: docs/sysmap/graph-pipeline.html
   ```

5. Publish the page with the Artifact tool (favicon: keep one emoji per page,
   stable across republishes), paste the returned URL into `deep_dive.url`,
   then re-render and republish the map so the link goes live.

Republishing an existing page: pass its `url` so it updates in place. The map
and every deep-dive page are separate artifacts with separate URLs; only the
map's URL lives in `meta.artifact_url`.
