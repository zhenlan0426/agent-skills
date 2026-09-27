# Kaggle CLI caveats

Checked against Kaggle CLI 2.2.2 on 2026-09-27. Recheck after upgrades.

- `kernels push` uploads only the named code file and metadata; `kgpu` embeds
  small projects into its generated Python script.
- Push may print an error and exit zero. `kgpu` requires a success response and
  separately checks for rejected data sources.
- CLI 2.2.2 ignores kernel version suffixes for status, logs, and output. Use a
  unique kernel per job and verify the run marker and exit code after download.
- `push --timeout` is the remote runtime cap. `kgpu` defaults it to one hour;
  raise `--run-timeout` for longer jobs. A local `kgpu wait` timeout does not
  stop remote work.
- This CLI has no supported kernel stop command or `push --no-run`. Cancel
  through Kaggle's UI when required.
- `kaggle quota` crashes in 2.2.2 because its duration parser rejects whole
  seconds; use the quota page in the UI until the CLI is fixed.
- Accelerator names do not prove account eligibility. Confirm the hardware in
  the completed kernel's runtime output.
- T4 jobs pin the Kaggle Python image snapshot
  `gcr.io/kaggle-images/python@sha256:37c64f7dd9c54116ecd1bcc88817c5469b88387388fade02bfa8bf3fc647d461`.
  Use `--use-kaggle-default-image` when current scoring-image parity is needed.

The [investigation log](../docs/kaggle-cli-investigation.md) has detailed
measurements and failure analysis. Upstream: [kernel commands](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md),
[kernel metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels_metadata.md),
[official CLI source](https://github.com/Kaggle/kaggle-cli/blob/main/src/kaggle/api/kaggle_api_extended.py).
