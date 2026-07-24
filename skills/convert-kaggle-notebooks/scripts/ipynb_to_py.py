#!/usr/bin/env python3
"""Convert a Jupyter notebook to a plain .py file in jupytext "percent" format.

Keeps only markdown and code cells. Code cells are emitted verbatim under a
`# %%` marker; markdown cells become commented blocks under `# %% [markdown]`.
Outputs, execution counts, and all other cell metadata are dropped.

Usage:
    ipynb_to_py.py INPUT.ipynb OUTPUT.py
"""
import json
import sys


def cell_source(cell):
    src = cell.get("source", "")
    if isinstance(src, list):
        src = "".join(src)
    return src.rstrip("\n")


def sanitize_code_source(src):
    lines = src.split("\n")
    first_code = next((line.lstrip() for line in lines if line.strip()), "")
    comment_entire_cell = first_code.startswith("%%")
    sanitized = []
    for line in lines:
        stripped = line.lstrip()
        if comment_entire_cell or stripped.startswith("%") or stripped.startswith("!"):
            sanitized.append(("# " + line).rstrip())
        else:
            sanitized.append(line)
    return "\n".join(sanitized).rstrip("\n")


def convert(nb):
    blocks = []
    for cell in nb.get("cells", []):
        kind = cell.get("cell_type")
        src = cell_source(cell)
        if kind == "code":
            src = sanitize_code_source(src)
            blocks.append("# %%\n" + src)
        elif kind == "markdown":
            commented = "\n".join(
                ("# " + line).rstrip() for line in src.split("\n")
            )
            blocks.append("# %% [markdown]\n" + commented)
        # Raw cells and anything else are ignored.
    return "\n\n".join(blocks) + "\n"


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    with open(sys.argv[1], encoding="utf-8") as f:
        nb = json.load(f)
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        f.write(convert(nb))


if __name__ == "__main__":
    main()
