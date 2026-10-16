# Measured Kaggle platform behavior

Facts measured in live probes (first on Name That LLM, 2026-10). Re-verify any that a decision depends on, and add new
measurements with their date and competition.

| Fact | Value |
| --- | --- |
| Error outcomes | `missing` and `format` (and probably `exception`) report status **`COMPLETE`** with an `errorDescription` and no score, not `ERROR`. The watcher classifies both. |
| Timing residuals | 5 CPU digits finished within [−12, +52] s of target (30 s polling). Residuals did not grow from 0.5 h to 6 h, so the noise is additive, not proportional. No public statement from Kaggle about deliberate timing noise was found. Size buckets with the noise budget anyway (workflow.md, "Timing noise budget"). |
| Duration field | None in the per-submission API response, so timing comes from polling (use `--poll-seconds 15`). |
| Concurrent submissions | 4 digits submitted within 8 s all landed on the calibrated grid, so no serial queueing was observed. |
| Batch sessions | At most 5 CPU kernels running at once. A push refused at that limit left the kernel slug permanently broken (`FileNotFoundError` on the competition input), so push again under a new slug. |
| `kaggle competitions submit -k` | Prints nothing on success. Confirm with `kaggle competitions submissions <slug>`. |
| `docker_image` in kernel-metadata | Honoured on push (check `ApiGetKernelRequest.dockerImage`). |
| Dataset pinning | `dataset_sources` accepts `owner/slug/<version>`. Notebook outputs (`kernel_sources`) cannot be pinned. |
| Notebook scores | Not in the public API. `scripts/fetch_listing.sh` reads them anonymously (see known-scores.md). The listing `pageSize` maxes at 100. |
| Others' kernel versions | Only the current version is pullable; `kernels pull owner/slug/<v>` returns 403. |
| Jupyter kernels | `nbconvert --execute` starts the kernel in its own session, so killing the process group leaves it running. Kill the process tree (`notebook_runner.kill_tree`). |
