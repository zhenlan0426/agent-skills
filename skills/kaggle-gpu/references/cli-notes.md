# CLI notes and validation

Checked 2026-09-27 against locally installed Kaggle CLI 2.2.2.

## Local implementation findings

The installed `kaggle/api/kaggle_api_extended.py` was inspected:

- `kernels_push` reads only the named code file plus metadata. Other files next
  to `kernel-metadata.json` are not uploaded as a project directory. `kgpu`
  embeds the project in the code file to transport small multi-file projects.
- `kernels_push_cli` can print `Kernel push error: ...` and return exit zero.
  The helper requires the numbered successful-push response too, and flags
  rejected data sources even when a run was accepted.
- `kernels_status`, `kernels_logs`, and `kernels_output` parse a version suffix
  but do not send it in the API request. Use a unique kernel per job; never
  rely on an old version suffix to select output in this client release.
- Status text includes enum names such as `KernelWorkerStatus.COMPLETE`.
  Queued, running, new-script, and cancel-requested are nonterminal; complete,
  error, and cancel-acknowledged are terminal.
- Output retrieval walks all pages by default. The helper validates its run
  marker afterwards, and refuses to merge a download with existing local files.
- `push --timeout` limits remote runtime. A local helper wait/download timeout
  does not cancel the kernel. CLI 2.2.2 has no `kernels stop` or `push --no-run`.
- `--accelerator` passes a server machine-shape name. Available names are not
  proof of this account's eligibility. The runtime prints `nvidia-smi` output;
  verify the device actually allocated before claiming a hardware-specific test.
- `kaggle quota` exists but crashes in 2.2.2 (`not enough values to unpack`):
  the SDK's duration parser expects fractional seconds and the server returns
  whole ones. Read remaining GPU quota from the account UI instead.

Recheck these details after a CLI upgrade; upstream main may describe unreleased
or newer features. These workarounds are version-specific, not platform promises.

## Validation

Run the offline helper tests with:

```bash
python3 scripts/test_kgpu.py
```

They exercise packaging and actual generated-runner execution in temporary
directories, plus simulated CLI responses for failure/timeout/result handling.
They use no GPU quota and create no remote resources. Live authentication and a
read-only existing-kernel status query were also checked.

### Live GPU smoke test — 2026-09-27

With user authorization, submitted a private kernel using `NvidiaTeslaT4`, with
internet disabled and a 300-second runtime cap. The helper packaged a generated
507-byte Python script, submitted version 1, observed RUNNING then COMPLETE,
retrieved logs, downloaded outputs, and verified the run marker and exit code.

- Kernel: `zhenlanwang/kgpu-20260927-133455-41c1b73f2c` (private, retained).
- Observed two Tesla T4 GPUs, each reporting 15360 MiB through `nvidia-smi`.
- PyTorch `2.10.0+cu128`; matrix multiplication ran on GPU 0, synchronized,
  and produced the expected value `14.0`. Multi-GPU computation was not tested.
- Command exit code 0; wrapper elapsed time about 10.9 seconds. This is not a
  measure of total billed/quota time, which also includes platform overhead.
- Retrieved `out/smoke.json`, `kgpu-result.json`, source, and the execution log.
- Local artifacts: `~/.cache/kgpu/smoke-20260927-093455/results/`.
- Logs arrived as a JSON event array. `kgpu logs` now joins event `data` fields
  into readable text before selecting the requested tail, with plain-text fallback.

This verifies the small embedded-source path through GPU execution and final
download. Large dataset uploads, secret binding, long runs, cancellation, and
checkpoint recovery were not exercised by this test.

## Upstream references

- [Kernel CLI commands](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md)
- [Kernel metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md)
- [Official CLI source](https://github.com/Kaggle/kaggle-cli/blob/main/src/kaggle/api/kaggle_api_extended.py)
- [Dataset metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets_metadata.md)

The helper's 2 MiB compressed / 20 MiB unpacked source limits are conservative
local packaging limits. They are not measured Kaggle upload limits.
