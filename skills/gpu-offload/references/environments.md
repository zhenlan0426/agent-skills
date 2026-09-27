# Setting up a project's dependencies on each target

The same `requirements.txt` needs different handling on the local 4090, a
Kaggle kernel, and a Colab VM, because each already ships a CUDA build of torch
(and more) that a careless install replaces. The helper for all three is
`~/agent-skills/skills/gpu-offload/scripts/envsetup` (stdlib only, runs on the
target itself):

```bash
envsetup probe [--json env.json]           # what is on this machine
envsetup install -r requirements.txt       # install, keeping the CUDA stack
envsetup wheels --env env.json -o wheels/ -r requirements.txt   # offline Kaggle
```

`install` pins every preinstalled CUDA-bound package (torch, torchvision,
torchaudio, triton, xformers, jax/jaxlib and their CUDA plugins, tensorflow,
all `nvidia-*`) to the version already there, then runs uv (or pip) with those
pins as constraints. Afterwards it reports what changed, what each request
resolved to, any *new* `pip check` complaints, and whether torch still sees
the GPU; `--report FILE` saves that as JSON. It refuses in seconds, before
installing anything, when:

- a requirement contradicts a pin (`torch==2.4.0` against the image's 2.11).
  Loosen it, or pass `--allow torch` to let the resolver replace it; the
  replacement must match the driver's CUDA, which `probe` prints.
- `flash-attn` is requested on a GPU below compute capability 8.0 (Kaggle's
  T4 is 7.5). Pass `--skip flash-attn` and use
  `attn_implementation="sdpa"` in code.
- it would install into the local base interpreter (see below).

## Per target

| | Local 4090 | Colab (G4 measured) | Kaggle T4 ×2 |
|---|---|---|---|
| Python | 3.12.3, system, externally managed | 3.13.15, `/usr`, root | _pending_ |
| torch | 2.11.0 (PyPI, CUDA 13.0) in `~/.local` | 2.11.0+cu128 | _pending_ |
| driver CUDA | 13.0 | 13.0 | _pending_ |
| installer | uv 0.12.7, pip 25.2 | uv 0.12.9 (`UV_SYSTEM_PYTHON=true`), pip 24.1.2 | _pending_ |
| preinstalled | 574 dists, incl. transformers, peft, trl, vllm, flash-attn | 705 dists, incl. transformers 5.16, peft, accelerate, jax, tensorflow; no trl, bitsandbytes | _pending_ |
| internet | yes | yes | only with `--internet` |
| how to run | `envsetup install --venv .venv -r req.txt` | `cgpu setup S -- -r /content/p/req.txt` | `kgpu prepare ... --setup '-r req.txt'` |

Measured 2026-09-27; images change. `probe` prints the current values, and
`cgpu setup` / `kgpu --setup` run it on every use (Kaggle saves it as
`out/env.json`).

### Local

Packages live in the user site (`~/.local/lib/python3.12/site-packages`), which
every project shares; the base interpreter is externally managed. So `install`
refuses to touch it unless given `--system`, and the normal form is
`--venv .venv`: a venv created with `--system-site-packages`, which inherits the
4090's torch stack and holds only the project's extras.

In such a layered venv `install` uses pip even though uv is present. uv only
sees the venv's own packages: measured, `uv pip install 'torch>=2.3' peft` into
a system-site venv planned a fresh torch 2.14 + triton stack, while pip
counted the inherited torch 2.11 as satisfied.

### Colab

The image installs torch from PyTorch's cu128 index. Measured on G4: an
unpinned `pip install vllm` resolved vllm 0.30 **plus torch 2.13, torchvision
0.28 and ~25 CUDA-13 `nvidia-*` wheels**, replacing the image's stack; with
`install`'s pins the resolver chose vllm 0.26, which fits torch 2.11. Pins can
make a package older than its latest release. The `requested -> importable`
line shows what was chosen; if the newer release is needed, that is a
deliberate `--allow torch` decision.

A typical LoRA set (`transformers>=4.55 peft accelerate bitsandbytes datasets
trl`) installed in 4 s with uv: only bitsandbytes and trl were missing.

`cgpu setup` needs an idle kernel: run it before `cgpu start`, after `push`.

### Kaggle

_pending live results_

Without `--internet` nothing can be downloaded. Build the wheel set on this
machine from the target's fingerprint, upload it as a private dataset, and
install from the mount:

```bash
envsetup wheels --env kaggle-env.json -o ./wheels -r requirements.txt
kaggle datasets create -p ./wheels ...          # private; see kaggle-data-and-runtime.md
kgpu prepare ./job ... --dataset USER/wheels \
  --setup '-r requirements.txt --wheels /kaggle/input'   # searched recursively
```

`kaggle-env.json` is any Kaggle `out/env.json` from an earlier `--setup` run.
`wheels` resolves with uv for the target's Python and glibc, prefers the
versions the target already has, and downloads only what differs. It
resolves torch against PyTorch's own index (`--torch-backend cu128`) because
PyPI's torch of the same version is a different build with different
`nvidia-*` dependencies. Packages with native code and no wheel for the target
are reported, not built.

## flash-attn

_pending G4 build measurement_

## Changing envsetup

`python3 -m pytest ~/agent-skills/skills/gpu-offload/tests/test_envsetup.py -q`
covers requirement parsing, version checks, the refusals above, and layered-venv
detection offline. Installs themselves can only be checked on the targets.
