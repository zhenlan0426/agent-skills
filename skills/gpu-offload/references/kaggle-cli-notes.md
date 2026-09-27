# CLI notes and validation

Checked 2026-09-27 against locally installed Kaggle CLI 2.2.2.

## Local implementation findings

The installed `kaggle/api/kaggle_api_extended.py` was inspected:

- `kernels_push` reads only the named code file plus metadata. Other files next
  to `kernel-metadata.json` are not uploaded as a project directory. `kgpu`
  embeds the project in the code file to transport small multi-file projects.
- `kernels_push_cli` can print `Kernel push error: ...` and return exit zero.
  The helper requires a successful-push response (with or without a version number), and flags
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
  whole ones. To work around it, patch
  `kagglesdk.kaggle_object.TimeDeltaSerializer._from_dict_value` to accept
  `"<n>s"`, then call `KaggleApi().quota_view()`. This reports GPU and TPU used
  and total time, plus the refresh time (verified 2026-09-27).

Recheck these details after a CLI upgrade; upstream main may describe unreleased
or newer features. These workarounds are version-specific, not platform promises.

## Validation

Run the offline helper tests with:

```bash
python3 tests/test_kgpu.py
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

### Large embedded upload test — 2026-09-27

Two private T4 submissions were attempted with CLI 2.2.2, internet disabled,
and a 300-second remote runtime cap. Each fixture contained generated Python,
deterministic incompressible bytes, and an expected SHA-256 digest. A trailing
comment padded the generated source to the stated test size. No real project
files or credentials were uploaded.

| Attempt | ZIP bytes | Base64 bytes | Final source bytes | Submission |
| --- | ---: | ---: | ---: | --- |
| Near old 2 MiB cap | 2,097,151 | 2,796,204 | 2,800,000 | HTTP 400 Bad Request |
| Near new 512 KiB cap | 524,285 | 699,048 | 720,896 (704 KiB) | Version 1 accepted |

- Rejected ref: `zhenlanwang/kgpu-20260927-134950-00d9045fa7`.
  A subsequent status query returned HTTP 404. The CLI exposed no detailed
  rejection reason. The original attempted directory was not resubmitted.
- Accepted ref: `zhenlanwang/kgpu-20260927-135034-27e8be47cf` (private, retained).
  Downloading its saved source with `kaggle kernels pull --metadata` reproduced
  all 720,896 bytes exactly; downloaded metadata confirmed `is_private: true`.
  Version 1 stayed QUEUED for about 1h55m (13:50 to 15:45 UTC) and was
  cancelled from the UI. The server-side metadata matched the smoke test that ran
  (`NvidiaTeslaT4`, same docker image, private, GPU on). The status API gave no
  failure message. A UI "Save Version" of the same source, version 2, started at
  once and completed. The CLI-pushed version 1 never executed. See the
  queue stall note below.
- Local fixtures and measurements:
  `~/.cache/kgpu/large-upload-20260927/` and its `512k/` subdirectory.
  Each contains `prepare_probe.py`, `measurements.json`, `project/`, and `job/`.
  The fixture script prepares locally; it does not submit automatically.
  `512k/live-result.json` and `512k/monitor.log` record subsequent execution
  status. A detached local monitor polls this same job for up to two hours,
  retrieves completed outputs, verifies the helper run marker, compares the
  remote and downloaded payload SHA-256 against the original, and checks the
  CUDA result. It neither submits another job nor cancels the existing one.

### CLI-pushed runs stuck QUEUED — 2026-09-27

After the smoke test ran, two later CLI pushes stayed QUEUED until the user
cancelled them: `...135034-27e8be47cf` (13:50) and `...144446-991d9872ff`
(14:44, internet on, 3600 s cap). Re-running each from the UI as version 2 at
about 15:46 started immediately, and both completed.

- Their server-side metadata matched the smoke kernel that ran. So did
  `machine_shape`, the docker image, and the privacy and GPU flags.
- `get_kernel_session_status` returned QUEUED with an empty `failure_message`.
- Quota was not the cause. GPU use was about 4 minutes of 45 hours. TPU use was
  0, so no hidden accelerator session was running.
- Cause not established. Candidates: the account's GPU batch slot was blocked,
  with the second job queued behind the first; or API-pushed versions are
  scheduled differently from UI saves. Neither is proven.
- The kagglesdk `KernelsApiClient` has `cancel_kernel_session`, but it has not
  been exercised. Until it is, cancel stuck runs in the UI.

These observations do not establish Kaggle's exact script or request limit, or
guarantee future service acceptance. The helper now limits embedded archives to
512 KiB and final UTF-8 source to 704 KiB; the latter includes the base64 payload,
wrapper, and command. This also protects against wrapper growth and long argv.
`prepare` records sizes, and `submit` checks the current source before recording
an attempt or contacting Kaggle, including for jobs prepared by older helpers.
HTTP errors and partial CLI timeout output are retained in `submission.log`;
ambiguous submissions still cannot be blindly retried.

Offline regressions cover an incompressible archive near the new cap, byte-for-byte
extraction, a real oversized archive, UTF-8 command growth, source-size checks at
submit, the exact source boundary, and diagnostics retained after failed requests.
Larger projects must use the private-dataset/bootstrap path in
[data and runtime](kaggle-data-and-runtime.md). Dataset transport was not live-tested here.

## Upstream references

- [Kernel CLI commands](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md)
- [Kernel metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md)
- [Official CLI source](https://github.com/Kaggle/kaggle-cli/blob/main/src/kaggle/api/kaggle_api_extended.py)
- [Dataset metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets_metadata.md)

The 20 MiB unpacked limit remains a local packaging guardrail. It is separate
from the transport envelope tested above.
