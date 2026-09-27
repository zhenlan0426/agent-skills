# CLI notes and validation

Checked 2026-09-27 against locally installed Kaggle CLI 2.2.2.

## Current T4 image decision (2026-09-27)

A private `--setup` kernel completed on
`gcr.io/kaggle-images/python@sha256:37c64f7dd9c54116ecd1bcc88817c5469b88387388fade02bfa8bf3fc647d461`.
It reported Python 3.12.13, torch 2.10.0+cu128, uv 0.11.13, and pip 24.1.2.
This latest-default snapshot is now the `kgpu` T4 pin. It supersedes the
historical recommendation below to use the older `kaggle-private-byod` image;
the earlier startup-delay comparison remains recorded as historical evidence.
Personal Kaggle owners and local project/cache paths have been replaced with
placeholders in this log.

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
python3 -m pytest tests/ -q
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

- Kernel: `<kaggle-user>/kgpu-20260927-133455-41c1b73f2c` (private, retained).
- Observed two Tesla T4 GPUs, each reporting 15360 MiB through `nvidia-smi`.
- PyTorch `2.10.0+cu128`; matrix multiplication ran on GPU 0, synchronized,
  and produced the expected value `14.0`. Multi-GPU computation was not tested.
- Command exit code 0; wrapper elapsed time about 10.9 seconds. This is not a
  measure of total billed/quota time, which also includes platform overhead.
- Retrieved `out/smoke.json`, `kgpu-result.json`, source, and the execution log.
- Local artifacts: `<local-cache>/kgpu/smoke-20260927-093455/results/`.
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

- Rejected ref: `<kaggle-user>/kgpu-20260927-134950-00d9045fa7`.
  A subsequent status query returned HTTP 404. The CLI exposed no detailed
  rejection reason. The original attempted directory was not resubmitted.
- Accepted ref: `<kaggle-user>/kgpu-20260927-135034-27e8be47cf` (private, retained).
  Downloading its saved source with `kaggle kernels pull --metadata` reproduced
  all 720,896 bytes exactly; downloaded metadata confirmed `is_private: true`.
  Version 1 stayed QUEUED for about 1h55m (13:50 to 15:45 UTC) and was
  cancelled from the UI. The server-side metadata matched the smoke test that ran
  (`NvidiaTeslaT4`, same docker image, private, GPU on). The status API gave no
  failure message. A UI "Save Version" of the same source, version 2, started at
  once and completed. The CLI-pushed version 1 never executed. See the
  queue stall note below.
- Local fixtures and measurements:
  `<local-cache>/kgpu/large-upload-20260927/` and its `512k/` subdirectory.
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
- It recurred with nothing ahead of it. `...160002-df55fe833c` (16:00, internet
  on, `--setup`) started within a minute and completed. The next push,
  `...160253-c183007b81` (16:02, internet off, one private dataset), was still
  QUEUED after 15 minutes. Quota over that window grew only by the first run's
  minute, and TPU use stayed 0.
- Of five CLI pushes, two ran and three stalled. Internet on or off, attached
  datasets, runtime cap and source size do not separate the two groups. Every
  stalled push ran at once when re-saved from the UI.
- Cause not established. Candidates: Kaggle's scheduler does not retry
  API-pushed runs once they are queued, or API pushes are scheduled differently
  from UI saves. Neither is proven.
- The kagglesdk `KernelsApiClient` has `cancel_kernel_session`, but it has not
  been exercised. Until it is, cancel stuck runs in the UI.

#### Follow-up investigation — 17:56–18:04 UTC

**Conclusion: no faulty kgpu submission field was established.** The stall did
not reproduce in the controlled pair below. An intermittent failure in Kaggle's
API scheduling path remains a plausible explanation, not a proven root cause.
Do not claim that changing the source format, timeout, pinning, or push spacing
fixes it. No submission behavior was changed on the strength of these results.

Read all of `scripts/kgpu`, the installed CLI 2.2.2 `kernels_push`, the SDK
request schema, serializer, and transport. Captured the CLI's serialized request
offline by replacing `save_kernel` with a recorder, without submitting again.
Also connected Chrome DevTools to the user's signed-in Chrome and captured the
UI's actual CommitAndRun request body while saving the investigation's smoke
kernel as version 2. Browser connection was enabled by the user. Headers and
cookies were not needed; the saved comparison omits source text.

| Item | CLI 2.2.2 / kgpu | Observed UI Save & Run All |
| --- | --- | --- |
| Actual RPC | `https://api.kaggle.com/v1/kernels.KernelsApiService/SaveKernel` | `/api/i/kernels.KernelsService/CommitAndRun` |
| Identity | `slug`, `newTitle`; new kernel | `scriptId`, `newTitle`, `sequence: 3`; existing kernel |
| Source | `text`: Python script; `kernelType: script`, `language: python` | `newText`: one-cell nbformat 4 JSON; `editorType: EDITOR_TYPE_SCRIPT`, `scriptLanguageName: python` |
| Accelerator | `machineShape: NvidiaTeslaT4`, `enableGpu: true`, `enableTpu: false` | `compute.accelerator: NVIDIA_TESLA_T4` |
| Internet | `enableInternet: false` | `compute.internet.isEnabled: false` |
| Runtime cap | `sessionTimeoutSeconds: 300` | absent |
| Execution type | `kernelExecutionType` absent | `versionType: BATCH` |
| Priority / Docker pinning | absent | absent |
| Other UI fields | no corresponding fields | `dataSources: []`, `isLanguageTemplate: false`, `workerPoolName: ""` |

The SDK declares 21 SaveKernel fields; the CLI assigns 19, leaving only
`priority` and `kernel_execution_type` entirely unused. Optional values such
as `id`, `docker_image`, and `docker_image_pinning_type` are assigned None and
omitted by serialization; empty source/tag lists are omitted too. The SDK says
priority is allowed only for certain clients, so it was not changed. Execution
type defaults to UNSPECIFIED; SAVE_AND_RUN_ALL is available. Current upstream
CLI also leaves it unset for normal runs and sets QUICK_SAVE only for `--no-run`.
That is evidence against treating its absence as an obvious helper defect, not
proof that the server handles it correctly in every case.

`--accelerator` and metadata `machine_shape` populate the **same field**, with
the flag taking precedence. Moving it to metadata cannot change this payload.
An offline capture of both paths confirmed identical serialized requests
(`accelerator-equivalence.json`).
The UI still uses the script editor type, despite transporting a notebook
container. The completed `__script__.ipynb` / NbConvert log is not evidence of
a submission mistake. The base64 extraction and subprocess run only after the
worker starts; they cannot themselves execute while the job remains QUEUED.
These observations do not rule out a server scheduling bug specific to script
requests. UI omission of the timeout is a real difference, but the same 300 s
CLI timeout works in both successful controls and previously stalled jobs.

**Spacing correction.** Slug timestamps are preparation times, not necessarily
push times. Local `submission-attempt.json` records show:

| Original job suffix | Attempt UTC | Gap from preceding recorded attempt |
| --- | --- | --- |
| `133455-41c1b73f2c` | 13:36:15.425 | unknown |
| `134950-00d9045fa7` (HTTP 400) | 13:49:56.976 | 13m41.551s |
| `135034-27e8be47cf` | 13:50:35.040 | 38.064s |
| `144446-991d9872ff` | 14:44:46.967 | **54m11.926s** |
| `160002-df55fe833c` | 16:00:02.909 | 75m15.943s, with UI reruns during this gap |
| `160253-c183007b81` | 16:02:53.706 | 2m50.797s |

Thus “every stall followed another push within three minutes” is not supported
by the recorded pushes: the 14:44 case is a counterexample, although an older
queued job still existed. Other unrecorded sessions cannot be inferred from
these timestamps.

**Controlled live pair.** Fresh job directories; same 358-byte ZIP, 2,462-byte
wrapper, private T4, internet off, no datasets, 300 s runtime cap, and
`python -u smoke.py`. Only generated identity and submission timing changed.

| Kernel suffix | Push UTC | Wrapper start UTC | Outcome |
| --- | --- | --- | --- |
| `175654-c3029d243b` | 17:56:54.816 | 17:57:00.682 | COMPLETE, exit 0 |
| `175848-1f990a39dc` | 17:58:54.854 | 17:59:05.868 | COMPLETE, exit 0 |

The first followed a long gap in recorded pushes; the second followed it by
**120.038 seconds**, after the first completed. Approximate push-to-wrapper
latencies were 5.87 and 11.01 s (local versus remote clocks). Both reported
Tesla T4 and CUDA arithmetic result 13.0. Downloaded outputs and run markers
were verified. The UI capture created version 2 of the first smoke kernel;
its wrapper ran 18:01:56.750–18:02:12.715 and also passed. This UI rerun is a
request comparison, not a one-variable causal experiment. Total: two new CLI
pushes and one UI rerun; no cancellations or deletions. The fourth test slot
was not used because there was no failing control to distinguish a field fix.

Version-specific SDK checks, using `version_label="v1"` and `"v2"`, confirmed
all three original stalled v1s are CANCEL_ACKNOWLEDGED with empty failure
messages, and their v2s are COMPLETE. Bare labels `"1"`/`"2"` return 404;
latest-only CLI status would conceal this history. Both new smoke versions
were also confirmed COMPLETE. GPU quota used rose from 435.427097 to
492.307097 seconds (56.88 s); GPU reserved and TPU used/reserved were zero at
the final check. Whole-second duration parsing was patched only in the
inspection process; quota totals should not be inferred from old snapshots.

Artifacts: `<local-cache>/kgpu/queue-investigation-20260927/`, including fresh
`baseline/` and `rapid/` jobs and outputs, `ui-results/`,
`cli-request-summaries.json`, `ui-request-summary.json`,
`historical-status.json`, and `quota-after.json`. `inspect_requests.py` captures
requests offline and performs read-only status/quota queries; it does not push.

**User-approved mitigation:** `kgpu wait --queue-timeout 900` is now the default.
It returns 124 with the kernel URL and UI cancel + Save Version > Save & Run All
instructions when a fresh query still reports QUEUED after the observation
limit. `queued.json` retains the first observation across short wait calls and
is cleared after a different state is observed. The timer starts at observation,
not submission, and is not a continuous server history. `--queue-timeout 0`
disables it; the existing per-call `--timeout` remains independent. The helper
never cancels or resubmits automatically. Offline tests cover persistence,
poll bounds, state transitions, disabling the limit, and recovery diagnostics.

#### Stall reproduced and known-working submitter compared — 18:11 onward

At the user's request, submitted one more fresh copy of the same tiny smoke
project: `<kaggle-user>/kgpu-20260927-181140-915c129481`, pushed at
18:11:46.260 UTC. Same private T4/script/internet-off/no-sources/300 s request;
the wrapper is identical to the successful controls apart from the run ID.
The preceding CLI push was 771.406 s earlier; the UI rerun was about ten minutes
earlier. This new v1 remained QUEUED at 18:26:39 with an empty failure message.
GPU used stayed 492.307097 s and GPU/TPU reserved time stayed zero. Source
retrieved from the server matched the submitted source exactly. Unlike the
completed controls, this queued kernel's metadata omitted `dockerImage`.
That may be a consequence of not starting; it does not establish causality.
At 18:26:54.458 the local helper returned 124 after 900 s observed QUEUED,
printed the URL and workaround, and did not cancel or resubmit the kernel.
Artifacts are in `queue-investigation-20260927/repeat-1811/`, including
`monitor.log`, `comparison.json`, and `final-status.json`.

The user then supplied a better historical control:
`<kaggle-user>/aas-capture-9e47921188` (v1 verified COMPLETE). Its submitter is
`<local-project>/scripts/notebook_true_candidate_capture.py`, which
calls `kaggle_offline_eval.build_kernel_metadata` and `kaggle_push` from the same
scripts directory. This is a Python script with embedded base64 source too.
The actual historical wire request was not available; reconstructing it offline
through the installed CLI 2.2.2 from that code exposes these differences:

| Setting | Working aas-capture path | kgpu repeat |
| --- | --- | --- |
| Docker image | Explicit `gcr.io/kaggle-private-byod/python@sha256:57e612b484cf3df5026ee4dcc3cb176974b22b2bc0937fb1e16132a8be4cb13c` | Omitted |
| Session timeout | Omitted; plain `kaggle kernels push -p DIR` | `sessionTimeoutSeconds: 300` |
| Internet | On | Off |
| Competition | `ai-agent-security-multi-step-tool-attacks` | None |
| Models | Gemma and GPT-OSS GGUF model sources | None |
| Machine shape | Metadata `NvidiaTeslaT4` | Flag `NvidiaTeslaT4`, same serialized field |
| Kernel type | Script | Script |

Neither submitter sets `docker_image_pinning_type`. **Explicit `docker_image`
is a separate field from that pinning policy**, and the working submitter does
set it. Its image digest is also different from the default image used by the
successful kgpu controls. The working driver's `--timeout` option bounds local
polling, not the remote request; it must not be confused with kgpu's
`--run-timeout`. It retries nonzero CLI failures, but an accepted QUEUED response
does not trigger those retries. Internet-on kgpu jobs also stalled earlier.

The working submitter's code calls the same `kaggle kernels push` CLI command;
its retry wrapper only retries nonzero CLI failures and cannot affect a push
that Kaggle accepted. Its command omits `--accelerator`, but setting
`machine_shape` in metadata serializes to the same `machineShape` field.
The historical CLI version used by that run was not recorded. Offline request
reconstruction and test plan: `queue-investigation-20260927/aas-comparison/`.

#### Authorized one-field experiments — starting 18:31 UTC

The user authorized both prepared tests, 15 minutes apart. Before submission,
an offline capture through CLI 2.2.2 verified that the serialized requests
differ from the failed smoke control only in `dockerImage` or
`sessionTimeoutSeconds`, respectively, apart from generated identity. Each
job's `experiment.json` records the effective request without source text.

The explicit-image test `...182614-e2b067ab1e` was pushed at 18:31:53.916 UTC,
retaining the 300 s API timeout. Its wrapper started at 18:31:59.906 and
finished at 18:32:07.521, exit 0. Outputs and run marker verified; CUDA result
13.0 on Tesla T4. Version-specific server metadata confirmed the requested
`57e612...13c` image. Thus this combination works, but one success is not a
demonstration that explicit image selection fixes the intermittent stall.

At the post-run check the prior stalled `...181140-915c129481` was already
CANCEL_ACKNOWLEDGED. The user confirmed cancelling it; the exact time was not
specified. The agent did not cancel it. Its cancellation time matters
for interpretation: it was no longer a simultaneous queued control.

Re-examining `historical-status.json` shows all three original cancelled v1s
also omit `dockerImage`; their completed UI v2s report `37c64f...d461`. This
limits the earlier claim of identical metadata: the current version-specific
records differ in this field, and latest-version comparisons do not establish
the state of the original stalled version. Missing image may still be an
effect of failure to start, rather than its cause.

The no-timeout test was scheduled for 18:46:53.916 UTC. Its harness
`submit_no_timeout.py` retained kgpu's one-attempt guard and response checks,
removing only `--timeout 300` from the emitted CLI command. The source,
internet flag, accelerator, and default image selection remained unchanged.
It saved `effective-command.json`, `monitor.log`, and `monitor-result.json`.

#### Matched image/no-timeout pair — 18:46–19:04 UTC

The default-image no-timeout control,
`...182614-5622f96b15`, was accepted at 18:46:56.393 UTC. It remained QUEUED
for 711.778 seconds before the wrapper started at 18:58:48.171, then completed
with exit 0. `kgpu pull` verified the run marker and `out/smoke.json` (13.0 on
Tesla T4). Version-specific server status was COMPLETE with an empty failure
message; the server resolved the omitted image to
`gcr.io/kaggle-images/python@sha256:37c64f7dd9c54116ecd1bcc88817c5469b88387388fade02bfa8bf3fc647d461`.

The matched treatment, `...185919-0e50e519e0`, was submitted at 19:02:01.729
UTC, more than 15 minutes later. Like the control it omitted the remote API
timeout. Offline capture through CLI 2.2.2 verified that the only request-field
difference was `dockerImage`, set to the image from the working AAS job:
`gcr.io/kaggle-private-byod/python@sha256:57e612b484cf3df5026ee4dcc3cb176974b22b2bc0937fb1e16132a8be4cb13c`.
Its wrapper started after 4.085 seconds and completed with exit 0. The marker
and smoke output were verified (13.0 on Tesla T4); version-specific server
status was COMPLETE with an empty failure message and confirmed the requested
image. Both the request comparison and server records omit source text.

This matched pair strongly supports explicit image selection as a mitigation
for the reproduced long QUEUED delay. It also shows that omitting the API
runtime timeout does not prevent a long queue. Earlier default-image CLI
controls sometimes started promptly, so the image is not proven to explain
every intermittent stall or guarantee that Kaggle will never queue a job.
At the time this entry was written, `kgpu prepare` selected the AAS image by
default; `--docker-image` overrode it and `--use-kaggle-default-image` left it
unset. That historical pin was replaced by the current image recorded above.
The 15-minute `kgpu wait` queue limit remains the recovery path for delays that
still occur.

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
- [Chrome DevTools connection setup](https://github.com/ChromeDevTools/chrome-devtools-mcp/blob/main/docs/advanced-usage.md)
- [Dataset metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets_metadata.md)

The 20 MiB unpacked limit remains a local packaging guardrail. It is separate
from the transport envelope tested above.
