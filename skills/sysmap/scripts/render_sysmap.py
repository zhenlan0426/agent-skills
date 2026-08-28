#!/usr/bin/env python3
"""Render a system-map.yaml into a self-contained, artifact-ready HTML page.

Deterministic: the same YAML always produces the same HTML. Agents edit the
YAML; presentation lives here and only here.

This page is the HIGH-LEVEL layer only: overview, the component tree with one
status and one summary per node, the changelog and the glossary. Detail lives
in hand-authored deep-dive pages (see references/deep-dive-pages.md), reached
through the `deep_dive` link on a component. A `details:` key in the YAML is a
hard error — that text belongs on a deep-dive page.

Usage:
    render_sysmap.py path/to/system-map.yaml [-o output.html]

Output defaults to the input path with a .html extension. The HTML is an
artifact-compatible fragment (starts with <title>, no <!doctype>/<html>/<body>)
that also opens fine directly in a browser.
"""

import argparse
import html
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("render_sysmap.py needs PyYAML (pip install pyyaml)")

STATUSES = ["planned", "in-progress", "implemented", "validated", "abandoned"]

STATUS_LABEL = {
    "planned": "Planned",
    "in-progress": "In progress",
    "implemented": "Implemented",
    "validated": "Validated",
    "abandoned": "Abandoned",
}

RECENT_CHANGELOG = 3  # entries shown expanded; the rest collapse


# ---------------------------------------------------------------- text helpers

def esc(text):
    return html.escape(str(text), quote=True)


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return s or "x"


def render_inline(text):
    """Inline markdown: `code`, **bold**. Input is raw text, output is HTML."""
    out = esc(text)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    return out


def render_block(text):
    """Minimal block markdown: paragraphs, '- ' bullet runs. Returns HTML."""
    if not text:
        return ""
    lines = str(text).splitlines()
    chunks = []  # list of ("p", [lines]) or ("ul", [items])
    for line in lines:
        stripped = line.strip()
        if not stripped:
            chunks.append(None)
        elif stripped.startswith("- "):
            if chunks and chunks[-1] and chunks[-1][0] == "ul":
                chunks[-1][1].append(stripped[2:])
            else:
                chunks.append(("ul", [stripped[2:]]))
        else:
            if chunks and chunks[-1] and chunks[-1][0] == "p":
                chunks[-1][1].append(stripped)
            else:
                chunks.append(("p", [stripped]))
    parts = []
    for chunk in chunks:
        if chunk is None:
            continue
        kind, items = chunk
        if kind == "p":
            parts.append("<p>%s</p>" % render_inline(" ".join(items)))
        else:
            lis = "".join("<li>%s</li>" % render_inline(i) for i in items)
            parts.append("<ul>%s</ul>" % lis)
    return "\n".join(parts)


# ------------------------------------------------------------ glossary linking

def link_terms(html_text, glossary, skip_term=None):
    """Wrap the first occurrence of each glossary term (outside tags and
    <code>) in a link to its glossary entry with a hover definition."""
    if not glossary or not html_text:
        return html_text
    terms = [g for g in glossary if g.get("term") and g.get("term") != skip_term]
    # longest first so "tier 1p cache" wins over "tier"
    terms.sort(key=lambda g: -len(str(g["term"])))
    parts = re.split(r"(<[^>]+>)", html_text)
    in_code, in_anchor = 0, 0
    done = set()
    for i, part in enumerate(parts):
        if part.startswith("<"):
            low = part.lower()
            if low.startswith("<code"):
                in_code += 1
            elif low.startswith("</code"):
                in_code -= 1
            elif low.startswith("<a"):
                in_anchor += 1
            elif low.startswith("</a"):
                in_anchor -= 1
            continue
        if in_code or in_anchor:
            continue
        # Collect spans against the untouched text, then splice once. Rewriting
        # `part` inside the loop would let a later term match inside markup an
        # earlier one just inserted — a link nested in a title= attribute.
        spans = []  # (start, end, glossary entry), non-overlapping
        for g in terms:
            term = str(g["term"])
            if term in done:
                continue
            pat = re.compile(
                r"(?<![\w-])(%s)(?![\w-])" % re.escape(esc(term)), re.IGNORECASE
            )
            for m in pat.finditer(part):
                if any(m.start() < e and s < m.end() for s, e, _ in spans):
                    continue
                spans.append((m.start(), m.end(), g))
                done.add(term)
                break
        if spans:
            spans.sort()
            out, last = [], 0
            for start, end, g in spans:
                definition = esc(g.get("definition", "")).replace("\n", " ").strip()
                out.append(part[last:start])
                out.append(
                    '<a class="term" href="#term-%s" title="%s">%s</a>'
                    % (slug(str(g["term"])), definition, part[start:end])
                )
                last = end
            out.append(part[last:])
            part = "".join(out)
        parts[i] = part
    return "".join(parts)


# ------------------------------------------------------------------- sections

def status_chip(status):
    if status not in STATUSES:
        sys.exit(
            "unknown status %r (expected one of: %s)" % (status, ", ".join(STATUSES))
        )
    return '<span class="chip chip-%s">%s</span>' % (status, STATUS_LABEL[status])


def deep_dive_link(deep, name):
    """The one link out of the high-level page. `deep_dive.url` is the
    published page; with only a `file`, say so rather than linking a path
    the reader cannot open."""
    if not deep:
        return ""
    label = esc(deep.get("label") or "Deep dive")
    url = deep.get("url")
    if url:
        return (
            '<div class="comp-deep"><a class="deep-link" href="%s" '
            'target="_blank" rel="noopener">%s <span class="deep-arrow">'
            "&#8599;</span></a></div>" % (esc(url), label)
        )
    if deep.get("file"):
        return (
            '<div class="comp-deep"><span class="deep-link deep-pending">'
            "%s &middot; not published yet</span></div>" % label
        )
    sys.exit("deep_dive on %r needs a 'url' or a 'file'" % name)


def render_component(comp, glossary, depth, path):
    """One component. Two visual layers: an always-visible headline row
    (path badge, name, status, one-line gist) and a collapsed Detail block."""
    name = comp.get("name")
    if not name:
        sys.exit("component missing 'name': %r" % comp)
    status = comp.get("status", "planned")
    if comp.get("details"):
        sys.exit(
            "component %r still has a 'details:' block. Detail belongs on a "
            "deep-dive page; link it with 'deep_dive:' instead." % name
        )
    summary = link_terms(render_inline(comp.get("summary", "")), glossary)
    deep = comp.get("deep_dive") or {}
    children = comp.get("children") or []
    children_html = "".join(
        render_component(c, glossary, depth + 1, "%s.%d" % (path, i))
        for i, c in enumerate(children, 1)
    )

    lvl = min(depth, 2)
    head = (
        '<div class="comp-head head%d">'
        '<div class="comp-line"><span class="comp-path">%s</span>'
        '<span class="comp-name">%s</span>%s</div>'
        % (lvl, esc(path), esc(name), status_chip(status))
    )
    if summary:
        head += '<div class="comp-gist">%s</div>' % summary
    head += deep_dive_link(deep, name)
    head += "</div>"

    body = ""
    if children_html:
        body += (
            '<div class="comp-children"><div class="parts-label">%d part%s</div>%s</div>'
            % (len(children), "" if len(children) == 1 else "s", children_html)
        )

    cls = "comp lvl%d st-%s" % (lvl, status)
    if body:
        open_attr = " open" if depth == 0 else ""
        return (
            '<details class="%s"%s><summary>%s</summary>'
            '<div class="comp-body">%s</div></details>' % (cls, open_attr, head, body)
        )
    return '<div class="%s comp-leaf">%s</div>' % (cls, head)


def render_changelog(entries, glossary):
    if not entries:
        return "<p class='muted'>No entries yet.</p>"

    def one(e):
        title = link_terms(render_inline(e.get("title", "(untitled)")), glossary)
        notes = link_terms(render_block(e.get("notes", "")), glossary)
        return (
            '<div class="log-entry"><div class="log-head">'
            '<span class="log-date">%s</span> <span class="log-title">%s</span>'
            "</div>%s</div>" % (esc(e.get("date", "")), title, notes)
        )

    recent = "".join(one(e) for e in entries[:RECENT_CHANGELOG])
    older = entries[RECENT_CHANGELOG:]
    if older:
        recent += (
            '<details class="older"><summary>%d older entries</summary>%s</details>'
            % (len(older), "".join(one(e) for e in older))
        )
    return recent


def render_glossary(glossary):
    if not glossary:
        return "<p class='muted'>No terms yet.</p>"
    rows = []
    for g in sorted(glossary, key=lambda g: str(g.get("term", "")).lower()):
        term = g.get("term", "")
        bits = [link_terms(render_block(g.get("definition", "")), glossary, skip_term=term)]
        if g.get("why"):
            bits.append(
                '<p class="gloss-why"><em>Why it exists:</em> %s</p>'
                % link_terms(render_inline(g["why"]), glossary, skip_term=term)
            )
        if g.get("where"):
            bits.append('<p class="gloss-where"><code>%s</code></p>' % esc(g["where"]))
        rows.append(
            '<div class="gloss-entry" id="term-%s"><dt>%s</dt><dd>%s</dd></div>'
            % (slug(term), esc(term), "".join(bits))
        )
    return "<dl class='glossary'>%s</dl>" % "".join(rows)


# ------------------------------------------------------------------------ css

CSS = """
:root {
  --bg: #f7f6f3; --panel: #ffffff; --ink: #1f2933; --muted: #6b7280;
  --line: #e3e0d8; --accent: #305672; --code-bg: #edeae2; --inset: #f2f0ea;
  --planned-fg: #5b6472; --planned-bg: #e7e9ee;
  --progress-fg: #8a5a09; --progress-bg: #f6e8c8;
  --implemented-fg: #1d4f8f; --implemented-bg: #dbe7f6;
  --validated-fg: #1e6b3a; --validated-bg: #d9eddf;
  --abandoned-fg: #8f2f2f; --abandoned-bg: #f3dcdc;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14181d; --panel: #1c2229; --ink: #e5e7eb; --muted: #9aa3af;
    --line: #2c343d; --accent: #7fa8c9; --code-bg: #262e37; --inset: #232a32;
    --planned-fg: #aeb6c2; --planned-bg: #2a313b;
    --progress-fg: #e3b04b; --progress-bg: #3a2f18;
    --implemented-fg: #82aede; --implemented-bg: #1e2c40;
    --validated-fg: #7fc796; --validated-bg: #1c3324;
    --abandoned-fg: #d98c8c; --abandoned-bg: #3a2222;
  }
}
:root[data-theme="dark"] {
  --bg: #14181d; --panel: #1c2229; --ink: #e5e7eb; --muted: #9aa3af;
  --line: #2c343d; --accent: #7fa8c9; --code-bg: #262e37; --inset: #232a32;
  --planned-fg: #aeb6c2; --planned-bg: #2a313b;
  --progress-fg: #e3b04b; --progress-bg: #3a2f18;
  --implemented-fg: #82aede; --implemented-bg: #1e2c40;
  --validated-fg: #7fc796; --validated-bg: #1c3324;
  --abandoned-fg: #d98c8c; --abandoned-bg: #3a2222;
}
body { background: var(--bg); color: var(--ink); margin: 0;
  font: 16px/1.55 "Source Serif 4", Georgia, serif; }
.wrap { max-width: 860px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
h1 { font-size: 1.7rem; margin: 0 0 .2rem; }
h2 { font-size: 1.15rem; margin: 2.2rem 0 .7rem; letter-spacing: .02em;
  text-transform: uppercase; color: var(--accent);
  font-family: "Source Sans 3", system-ui, sans-serif; }
.meta-line { color: var(--muted); font-size: .85rem; margin-bottom: 1.6rem;
  font-family: "Source Sans 3", system-ui, sans-serif; }
code { background: var(--code-bg); padding: .08em .35em; border-radius: 4px;
  font-size: .85em; }
p { margin: .5em 0; }
ul { margin: .4em 0; padding-left: 1.3em; }
.muted { color: var(--muted); }
.panel { background: var(--panel); border: 1px solid var(--line);
  border-radius: 10px; padding: 1rem 1.2rem; }
.chip { display: inline-block; border-radius: 999px; padding: .05em .6em;
  font: 600 .68rem/1.5 "Source Sans 3", system-ui, sans-serif;
  vertical-align: middle; letter-spacing: .02em; white-space: nowrap; }
.chip-planned { color: var(--planned-fg); background: var(--planned-bg); }
.chip-in-progress { color: var(--progress-fg); background: var(--progress-bg); }
.chip-implemented { color: var(--implemented-fg); background: var(--implemented-bg); }
.chip-validated { color: var(--validated-fg); background: var(--validated-bg); }
.chip-abandoned { color: var(--abandoned-fg); background: var(--abandoned-bg); }
.legend { display: flex; gap: .5rem; flex-wrap: wrap; margin: .6rem 0 0; }
/* ---- components: level 0 is a card, deeper levels are rows inside it ---- */
.comps { display: flex; flex-direction: column; gap: .85rem; }
.comp > summary { cursor: pointer; list-style: none; position: relative;
  padding-right: 1.5rem; }
.comp > summary::-webkit-details-marker { display: none; }
.comp > summary::after { content: "▸"; position: absolute; right: .25rem;
  top: .1rem; color: var(--muted); font-size: .8em; }
.comp[open] > summary::after { content: "▾"; }
.comp-line { display: flex; align-items: baseline; flex-wrap: wrap; gap: .4em; }
.comp-path { display: inline-block; border-radius: 5px; padding: .1em .45em;
  font: 700 .67rem/1.4 "Source Sans 3", system-ui, sans-serif;
  letter-spacing: .04em; }

.comp.lvl0 { background: var(--panel); border: 1px solid var(--line);
  border-left: 5px solid var(--line); border-radius: 10px;
  padding: .9rem 1.1rem 1rem; }
.comp.lvl0.st-planned { border-left-color: var(--planned-fg); }
.comp.lvl0.st-in-progress { border-left-color: var(--progress-fg); }
.comp.lvl0.st-implemented { border-left-color: var(--implemented-fg); }
.comp.lvl0.st-validated { border-left-color: var(--validated-fg); }
.comp.lvl0.st-abandoned { border-left-color: var(--abandoned-fg); }
.head0 .comp-path { background: var(--accent); color: var(--panel); }
.head0 .comp-name { font: 700 1.14rem/1.3 "Source Sans 3", system-ui, sans-serif;
  letter-spacing: -.01em; }
.head0 .comp-gist { margin-top: .35rem; font-size: .97rem; }

.comp.lvl1 { border-top: 1px solid var(--line); padding: .6rem 0 .1rem; }
.head1 .comp-path, .head2 .comp-path { background: var(--code-bg);
  color: var(--muted); }
.head1 .comp-name { font: 600 1rem/1.35 "Source Sans 3", system-ui, sans-serif; }
.head1 .comp-gist { margin-top: .2rem; font-size: .9rem; color: var(--muted); }

.comp.lvl2 { border-left: 2px solid var(--line); margin: .3rem 0 .3rem .1rem;
  padding: .3rem 0 .1rem .8rem; }
.head2 .comp-name { font: 600 .92rem/1.35 "Source Sans 3", system-ui, sans-serif; }
.head2 .comp-gist { margin-top: .15rem; font-size: .86rem; color: var(--muted); }

.comp-body { padding-left: .1rem; }
.comp-children { margin-top: .2rem; }
.comp.lvl0 > .comp-body > .comp-children { margin-top: .9rem; }
.parts-label { font: 600 .66rem/1.5 "Source Sans 3", system-ui, sans-serif;
  text-transform: uppercase; letter-spacing: .09em; color: var(--muted);
  margin-bottom: .1rem; }

/* ---- the one way out of this page: a link to a deep-dive page ---- */
.comp-deep { margin: .45rem 0 .1rem; }
.deep-link { display: inline-block; border: 1px solid var(--line);
  border-radius: 999px; padding: .12em .75em; text-decoration: none;
  color: var(--accent); background: var(--inset);
  font: 600 .72rem/1.6 "Source Sans 3", system-ui, sans-serif;
  letter-spacing: .02em; }
.deep-link:hover { border-color: var(--accent); background: var(--panel); }
.deep-link:focus-visible { outline: 2px solid var(--accent);
  outline-offset: 2px; }
.deep-arrow { font-size: .9em; }
.deep-pending { color: var(--muted); border-style: dashed; cursor: default; }
.comp-ctl { display: flex; gap: .5rem; margin: 0 0 .2rem; }
.comp-ctl button { font: 600 .72rem/1.6 "Source Sans 3", system-ui, sans-serif;
  color: var(--muted); background: var(--panel); border: 1px solid var(--line);
  border-radius: 999px; padding: .1em .8em; cursor: pointer; }
.comp-ctl button:hover { color: var(--accent); border-color: var(--accent); }
.log-entry { padding: .55rem 0; border-bottom: 1px solid var(--line); }
.log-entry:last-child { border-bottom: none; }
.log-date { color: var(--muted); font: .78rem/1.5 "Source Sans 3", system-ui,
  sans-serif; margin-right: .5rem; }
.log-title { font-weight: 700; }
.older { margin-top: .6rem; }
.older > summary { cursor: pointer; color: var(--muted); font-size: .85rem; }
.glossary { margin: 0; }
.gloss-entry { padding: .5rem 0; border-bottom: 1px solid var(--line); }
.gloss-entry:last-child { border-bottom: none; }
.gloss-entry dt { font-weight: 700; }
.gloss-entry dd { margin: .15rem 0 0; }
.gloss-why, .gloss-where { font-size: .88em; color: var(--muted); }
a.term { color: inherit; text-decoration: none;
  border-bottom: 1px dotted var(--accent); cursor: help; }
a.term:hover { color: var(--accent); }
:target { scroll-margin-top: 2rem; background: var(--code-bg); }
"""


# ------------------------------------------------------------------------- js

# Buttons are created by the script, so nothing dead is left behind if the
# page is viewed somewhere scripts do not run.
EXPAND_JS = """<script>
(function () {
  var comps = document.querySelector('.comps');
  if (!comps) return;
  var bar = document.createElement('div');
  bar.className = 'comp-ctl';
  [['Expand all', true], ['Collapse all', false]].forEach(function (spec) {
    var b = document.createElement('button');
    b.type = 'button';
    b.textContent = spec[0];
    b.addEventListener('click', function () {
      comps.querySelectorAll('details').forEach(function (d) {
        d.open = spec[1];
      });
    });
    bar.appendChild(b);
  });
  comps.parentNode.insertBefore(bar, comps);
})();
</script>"""


# ----------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("yaml_path", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    args = ap.parse_args()

    data = yaml.safe_load(args.yaml_path.read_text()) or {}
    meta = data.get("meta") or {}
    glossary = data.get("glossary") or []
    project = meta.get("project", args.yaml_path.stem)

    fonts = (
        '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
        "family=Source+Serif+4:opsz,wght@8..60,400;8..60,700&"
        'family=Source+Sans+3:wght@400;600;700&display=swap">'
    )

    meta_bits = []
    if meta.get("updated"):
        meta_bits.append("updated %s" % esc(meta["updated"]))
    if meta.get("commit"):
        meta_bits.append("reconciled to <code>%s</code>" % esc(meta["commit"]))

    legend = '<div class="legend">%s</div>' % "".join(
        status_chip(s) for s in STATUSES
    )

    overview = link_terms(render_block(data.get("overview", "")), glossary)
    components = data.get("components") or []
    comps_html = "".join(
        render_component(c, glossary, 0, str(i))
        for i, c in enumerate(components, 1)
    )

    out = f"""<title>{esc(project)} System Map</title>
{fonts}
<style>{CSS}</style>
<div class="wrap">
<h1>{esc(project)} — System Map</h1>
<div class="meta-line">{" · ".join(meta_bits)}</div>

<h2>Overview</h2>
<div class="panel">{overview or "<p class='muted'>No overview yet.</p>"}{legend}</div>

<h2>Components</h2>
<div class="comps">{comps_html or "<div class='panel'><p class='muted'>No components yet.</p></div>"}</div>
{EXPAND_JS}

<h2>Since you last read</h2>
<div class="panel">{render_changelog(data.get("changelog") or [], glossary)}</div>

<h2>Glossary</h2>
<div class="panel">{render_glossary(glossary)}</div>
</div>
"""
    out_path = args.output or args.yaml_path.with_suffix(".html")
    out_path.write_text(out)
    print("wrote %s (%d bytes)" % (out_path, len(out)))


if __name__ == "__main__":
    main()
