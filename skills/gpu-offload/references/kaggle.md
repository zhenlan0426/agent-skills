# Kaggle GPU batch jobs

The helper is `~/agent-skills/skills/gpu-offload/scripts/kgpu` (called `kgpu`
below). When to use Kaggle, and the fact that it needs no permission, are
covered in [../SKILL.md](../SKILL.md). Kaggle runs a submitted script in a fresh environment; there is no persistent
shell, incremental file push, or Colab-style `up`/`down` in this workflow.

## Before submitting

- Check `kaggle --version` and `kaggle kernels list --mine --page-size 1`.
  The helper targets CLI **2.2.2**; recheck CLI help/source after upgrades.
  If absent, install `kaggle==2.2.2` in an isolated environment using `uv tool
  install`. For missing authentication, have the user run `kaggle auth login`
  locally; existing API-token or legacy `kaggle.json` configuration also works.
  Never print credentials or copy local auth files into a kernel.
- Establish the project, command, data sources, intended outputs, and maximum
  run time. Kernels are private, so uploading the project needs no separate
  permission; the manifest review below still applies.
- Use `NvidiaTeslaT4` (T4 ×2) unless the user or a competition calls for
  another shape. It is not a combined 32 GB GPU: code must explicitly use both
  devices, and data parallelism alone doesn't add model capacity. Some
  competitions allow larger accelerators, so check the competition's code
  requirements when working on one. Other accelerator names and quotas depend
  on account/competition eligibility. Don't promise an A100/H100 or hardcode
  weekly allowances. Check the account UI for remaining quota (`kaggle quota`
  crashes in CLI 2.2.2). If allocation fails, report it before switching
  hardware or resubmitting.
- Kernel code and outputs are private by default. Review the local file manifest
  before upload. Secrets belong in Kaggle Secrets, not source, argv, or datasets.
  Read [kaggle-data-and-runtime.md](kaggle-data-and-runtime.md) when
  handling large local inputs, dependencies, secrets, or checkpoint recovery.

## Workflow

```bash
# All options precede --. The command is an argv, not an implicit shell string.
kgpu prepare ./kaggle-job --project ./myproj --owner KAGGLE_USERNAME \
  --accelerator NvidiaTeslaT4 --run-timeout 3600 \
  --dataset owner/dataset-slug --internet -- python -u train.py --epochs 3

# Local only so far: inspect manifest.json and kernel-metadata.json in kaggle-job.
kgpu submit ./kaggle-job
kgpu status ./kaggle-job
kgpu logs ./kaggle-job -n 80
kgpu wait ./kaggle-job --timeout 45
kgpu pull ./kaggle-job ./results
```

`prepare` embeds a small project archive into a Python kernel. It respects Git
ignore rules in Git repositories, excludes common caches/credentials, and skips
symlinks. The manifest is the authoritative upload list: exclusions are not a
secret scanner. Repeat `--exclude 'pattern'` for project-specific omissions.
Prepare outside the project directory. The helper caps the bundle at **512 KiB
compressed**, **20 MiB unpacked**, and the final UTF-8 kernel script at **704 KiB**
(base64, wrapper, and command included). Preparation reports archive/script sizes
and records them in `job.json`; submission rechecks the actual script, including
older prepared jobs. These are local guardrails backed by the
[live validation record](kaggle-cli-notes.md), not Kaggle's published limits:
a near-2 MiB bundle in a 2,800,000-byte script was rejected by the API.
Use a private dataset and small bootstrap for larger projects, as described in
[data and runtime](kaggle-data-and-runtime.md). A project may contain notebooks, but
the command must be an executable batch entrypoint; a notebook is not executed
just because it is bundled.

`--setup 'ARGS'` runs `envsetup install ARGS` in the kernel before the command
(fingerprint saved as `out/env.json`, install report as `out/envsetup.json`); a
failed install fails the job without running the command. It needs `--internet`
or `--wheels` pointing at an attached dataset. See
[environments.md](environments.md).

Inside the job, the project is `/kaggle/working/project` and is the working
directory. Attached inputs are under read-only `/kaggle/input`; inspect the
actual mount paths. Write results under `/kaggle/working`, such as
`/kaggle/working/out` or `./out`. The helper sets `KGPU_RUN_ID` and
`KGPU_OUTPUT_DIR=/kaggle/working/out`, prints GPU diagnostics, and writes
`/kaggle/working/kgpu-result.json` with the command's exit code. A failed GPU
probe fails a GPU job instead of silently continuing on CPU.

- Each prepared job has a unique private kernel slug. Do not push new versions
  to it: CLI 2.2.2 parses version suffixes but ignores them for status, logs,
  and output requests. Unique kernels and run markers avoid stale result mixups.
- `submit` records the attempt before contacting Kaggle and refuses a second
  attempt from that directory. A CLI timeout or ambiguous response may still
  mean the job started. Check its saved URL/status; don't blindly retry.
  `submission.log` preserves CLI errors and partial output from local timeouts.
  Submission success only means accepted, not that the job succeeded.
- `wait` returns 0 for COMPLETE, 1 for failure/cancellation, 124 when the local
  wait expires, and 2 for an API/parse problem. Local timeouts leave remote work
  running. Poll in short calls, keeping the user updated. Logs may be delayed;
  lack of log text is not evidence the job stopped.
- `pull` requires a new/empty local output directory, downloads all available
  output pages, and verifies the run marker and exit code. A missing marker or
  nonzero exit is not success; inspect the logs and any partial results. Inspect
  the requested artifacts too before reporting the task complete.
- Kaggle owns the batch lifecycle. CLI 2.2.2 has no supported stop command;
  cancel a running version through the notebook UI if needed. `kernels delete`
  deletes the notebook and is not a substitute for cancellation. Retain private
  kernels/datasets until the user requests cleanup.

## Failure and recovery

Kaggle quota is free and resets weekly, so fixing a failure and resubmitting
needs no permission. Don't resubmit unchanged after a failure you haven't
diagnosed, and don't launch sweeps or extra runs the task didn't call for. For 401/403,
check auth, ownership, data access, and competition-rule acceptance. For GPU
eligibility, quota, or verification errors, report the actual server message.
Do not work around account limits.

Successful saved-version outputs can be fetched after execution ends. Files
only on a running VM are not durable checkpoints: timeout, cancellation, or
infrastructure failure may prevent them being saved. For valuable long jobs,
arrange authorized external checkpoint uploads or divide work into completed
versions that consume prior outputs. Do not claim live file downloads or
checkpoint recovery have been verified when only final output retrieval was.

See [kaggle-cli-notes.md](kaggle-cli-notes.md) for upstream sources,
version-specific caveats, and the validation record.
