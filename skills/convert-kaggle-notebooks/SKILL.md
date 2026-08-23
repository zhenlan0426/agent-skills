---
name: convert-kaggle-notebooks
description: Download or refresh top-rated Kaggle notebooks and convert Kaggle or Jupyter `.ipynb` files into readable, code-reviewable Python scripts without executing notebook code. Use when Codex needs to find public Kaggle notebooks, refresh a local notebook collection, inspect, diff, archive, or edit a notebook as text, remove notebook outputs and metadata, or make a Kaggle notebook easier for an agent to work with.
---

# Download and Convert Kaggle Notebooks

Run the combined helper to convert existing notebooks. It parses notebook JSON only and never runs
notebook cells.

```bash
python /home/zhenlan/.codex/skills/convert-kaggle-notebooks/scripts/download_and_convert.py \
  path/to/notebook.ipynb path/to/notebook-folder/
```

To refresh the highest-ranked notebooks of a competition first, pass `--refresh`. This requires an
authenticated Kaggle CLI. By default, it takes the deduplicated union of the top `--top-n` results for
Kaggle's `hotness`, `voteCount`, and `scoreDescending` rankings — `--top-n` applies independently to
each ranking. The default competition is the AI agent security competition; supply `--competition` for
another one. To replace the default rankings, repeat `--sort-by ORDER`. Refreshing re-pulls the current
selection into per-notebook directories and then converts the downloaded `.ipynb` files.

```bash
python /home/zhenlan/.codex/skills/convert-kaggle-notebooks/scripts/download_and_convert.py \
  --refresh --destination notebooks --top-n 30
```

Keep the original `.ipynb`, Kaggle metadata, and the adjacent generated `.py` together. A refresh
updates selected notebooks in place but does not delete previously downloaded notebooks. Treat the
generated script as an inspection and editing representation, not an executable equivalent.

The output uses `# %%` cell markers: code cells remain code, while markdown becomes commented text.
Line magics (`%...`) and shell escapes (`!...`) are commented individually. If a cell begins with a
cell magic (`%%...`), comment the entire cell because its contents are not ordinary Python. Ignore raw
cells, outputs, execution counts, and all other metadata.

Recover `%%writefile` payloads, widget state, shell commands, and cell-magic semantics from the
original notebook when they matter. Review changed or magic-containing cells after conversion before
relying on the script.
