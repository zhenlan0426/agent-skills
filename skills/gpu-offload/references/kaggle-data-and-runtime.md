# Data, dependencies, and runtime

## Inputs larger than the embedded code bundle

The embedded path allows 512 KiB compressed and a 704 KiB final kernel script.
Base64 expands the archive by roughly one third; do not bypass the source check
or raise the limits to fit a project. Use the dataset path below when either
limit is exceeded. This keeps the submitted script small regardless of input size.

Prefer existing Kaggle datasets/competition mounts. Repeat `--dataset owner/slug`,
`--competition slug`, or `--kernel-source owner/kernel-slug` during preparation.
Accept competition rules through the website if required; the CLI cannot accept
them for the user. Inspect mount paths at runtime rather than guessing from titles.
Observed 2026-09-27: an attached dataset mounted at
`/kaggle/input/datasets/<owner>/<slug>/`, not `/kaggle/input/<slug>/`. Its files
were regular files, byte-identical to the upload. Search under `/kaggle/input`
instead of hardcoding either layout.

For local data or a large source tree, stage only intended files in a separate
directory, then create a private dataset. This is an upload, not a local packaging
step; ensure the user's offload request covers these files. A minimal
`dataset-metadata.json` alongside the staged files is:

```json
{
  "title": "My private job inputs",
  "id": "USERNAME/my-private-job-inputs",
  "licenses": [{"name": "copyright-authors"}]
}
```

Preserve the files' existing license; use the example only when appropriate.
Do not assign CC0 to private project code merely because the CLI scaffold does.

```bash
kaggle datasets create -p ./staged-inputs --dir-mode zip --keep-tabular
kaggle datasets status USERNAME/my-private-job-inputs
# For an existing dataset the user intends to update:
kaggle datasets version -p ./staged-inputs --dir-mode zip --keep-tabular \
  -m 'Inputs for this run'
```

Create is private unless `--public` is passed. Never add that flag by default.
Wait until processing completes before submitting the kernel. Review the response,
not just the exit code. Do not delete old dataset versions to save space without
an explicit cleanup request. Record the dataset version used; avoid updating it
while a dependent kernel is queued. For strict reproducibility, use an immutable
input snapshot and log input hashes in the job.

Use a small bootstrap project with `kgpu prepare` when the main source is in a
dataset. Copy source that needs writes from `/kaggle/input` to
`/kaggle/working/project`, then invoke its entrypoint. Do not assume an uploaded
archive will appear in precisely the same layout: inspect its mounted files.

## Dependencies and secrets

The Kaggle image includes common ML packages. Install only what is missing,
without replacing its torch stack, using `kgpu prepare --setup '-r
requirements.txt'` ([environments.md](environments.md)). Use `--internet` when
the job needs downloads; network access may be restricted by competition rules.
Without it, attach wheels (`envsetup wheels`), data, and model weights as
inputs.

Use Kaggle's notebook editor **Add-ons → Secrets** to define and attach secrets
to the relevant notebook. The installed CLI cannot manage these bindings.
Inside Kaggle:

```python
from kaggle_secrets import UserSecretsClient
token = UserSecretsClient().get_secret("HF_TOKEN")
```

Never put token values in a prepared kernel, bundled credential file, CLI argument,
log, or output artifact. Fresh helper kernels do not inherit another notebook's
secret bindings. If secrets must be attached before execution, the one-shot helper
submit is insufficient: arrange a draft through the UI, configure its secrets,
then start it. CLI 2.2.2 does not have `push --no-run`; don't invent that flag from
newer documentation. A public model or pre-attached dataset avoids needing a
token during the GPU run.

## Outputs and checkpoints

Write desired results under `/kaggle/working`. `/tmp`, the home directory, and
input mounts are not substitutes for the saved output directory. The helper
keeps project source there too, so download results to a dedicated local folder.
Do not copy secrets or huge package caches into working output.

For resumable stages, complete a kernel that writes a checkpoint and attach its
outputs to the next stage with `--kernel-source`. Unique slugs preserve the
identity of the completed stage. This does not protect against an interrupted
stage failing to save output. For long, valuable runs, have the training process
upload checkpoints to an authorized private destination, with its secret binding
configured before execution. Agree on that destination if none was specified.

Source: [Kaggle dataset metadata](https://github.com/Kaggle/kaggle-cli/blob/main/docs/datasets_metadata.md)
and [kernel commands and Secrets](https://github.com/Kaggle/kaggle-cli/blob/main/docs/kernels.md).
