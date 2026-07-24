---
name: kaggle-notebook-versions
description: Download and inspect exact historical source revisions of a public Kaggle notebook. Use when asked to retrieve one notebook version, archive every version, compare historical Kaggle notebook code, or work around the Kaggle CLI returning only the current notebook output.
---

# Kaggle Notebook Versions

Use the bundled downloader for public Kaggle notebooks. It lists version metadata and downloads source from the historical session associated with each version.

## Run

List available revisions before choosing one:

```bash
python /home/zhenlan/.codex/skills/kaggle-notebook-versions/scripts/download_notebook_versions.py \
  owner/notebook-slug --list
```

Download one revision or all revisions. The notebook URL form also works; a `/versions/N` URL supplies the version unless `--version` is given.

```bash
# One exact source revision
python /home/zhenlan/.codex/skills/kaggle-notebook-versions/scripts/download_notebook_versions.py \
  owner/notebook-slug --version 58 --output /tmp/notebook-history

# Every available revision, plus manifest.json
python /home/zhenlan/.codex/skills/kaggle-notebook-versions/scripts/download_notebook_versions.py \
  https://www.kaggle.com/code/owner/notebook-slug --all --output /tmp/notebook-history
```

Use `--include-output` only when cell outputs are needed, `--overwrite` to replace existing files, and `--kernel-id` when the public metadata lookup cannot resolve a kernel. Kaggle can rate-limit a rapid historical-source archive with transient 404s: in `--all` mode the downloader checkpoints `manifest.json`, waits once for 30 seconds after the first failure, then resumes. Change this with `--bulk-backoff`, or use `--retries` for extra per-source retries. Inspect `manifest.json` after an all-version download; it maps version numbers to Kaggle session IDs, timestamps, SHA-256 digests, and any source-session failures. A listed version whose source remains unavailable after recovery is recorded and skipped so the remaining history is still archived.

## Reliability and scope

- Treat `ListKernelVersions` and `GetKernelSessionSource` as public frontend APIs, not a stable Kaggle API guarantee. Re-check the utility if Kaggle changes them.
- Use the listed `run.id` as `kernelSessionId`; it is not the displayed version number, `version.id`, or a `scriptVersionId` in a URL.
- Do not rely on `kaggle kernels output owner/slug/version` for historical source: in this workflow it can return current content. The CLI's historic pull syntax may also be denied.
- Download only notebooks the caller is authorized to access. The utility does not authenticate, circumvent access controls, or run downloaded notebooks.
