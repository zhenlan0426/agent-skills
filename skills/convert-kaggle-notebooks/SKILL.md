---
name: convert-kaggle-notebooks
description: Convert Kaggle or Jupyter `.ipynb` notebooks into readable, code-reviewable Python scripts without executing notebook code. Use when Codex needs to inspect, diff, archive, or edit a notebook as text; remove notebook outputs and metadata; or make a Kaggle notebook easier for an agent to work with.
---

# Convert Kaggle Notebooks

Run the bundled converter; it parses notebook JSON only and never runs notebook cells.

```bash
python /home/zhenlan/.codex/skills/convert-kaggle-notebooks/scripts/ipynb_to_py.py \
  path/to/notebook.ipynb path/to/notebook.py
```

Place the output beside the source notebook unless the task specifies a staging directory. Keep the
original `.ipynb` when provenance or a later interactive run matters.

The output uses `# %%` cell markers: code cells remain code, while markdown becomes commented text.
Line magics (`%...`) and shell escapes (`!...`) are commented individually. If a cell begins with a
cell magic (`%%...`), comment the entire cell because its contents are not ordinary Python. Ignore raw
cells, outputs, execution counts, and all other metadata.

Treat the result as an inspection and editing representation, not an executable equivalent of the
notebook. In particular, recover `%%writefile` payloads, widget state, shell commands, and cell-magic
semantics from the original notebook when they matter. Review changed or magic-containing cells after
conversion before relying on the script.
