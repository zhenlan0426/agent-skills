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
| image | local Ubuntu | Colab-managed G4 image | `gcr.io/kaggle-images/python@sha256:37c64f7dd9c54116ecd1bcc88817c5469b88387388fade02bfa8bf3fc647d461` |
| Python | 3.12.3, system, externally managed | 3.13.15, `/usr`, root | 3.12.13, `/usr`, root |
| torch | 2.11.0 (PyPI, CUDA 13.0) in `~/.local` | 2.11.0+cu128 | 2.10.0+cu128 |
| driver CUDA | 13.0 | 13.0 | 13.0 (driver 580.159.04) |
| installer | uv 0.12.7, pip 25.2 | uv 0.12.9 (`UV_SYSTEM_PYTHON=true`), pip 24.1.2 | uv 0.11.13 (`UV_SYSTEM_PYTHON=true`), pip 24.1.2 |
| preinstalled | 574 dists, incl. transformers, peft, trl, vllm, flash-attn | 705 dists, incl. transformers 5.16, peft, accelerate, jax, tensorflow; no trl, bitsandbytes | 933 dists, incl. transformers 5.0, peft, accelerate, datasets, jax, tensorflow; no trl, bitsandbytes, vllm |
| internet | yes | yes | only with `--internet` |
| how to run | `envsetup install --venv .venv -r req.txt` | `cgpu setup S -- -r /content/p/req.txt` | `kgpu prepare ... --setup '-r req.txt'` |

Measured 2026-09-27; images change. The Kaggle column is from a private
`--setup` job on the pinned image digest shown in the table. It is the latest
Kaggle Python default observed when this snapshot was pinned. `probe` prints
current values, and `cgpu setup` / `kgpu --setup` run it on every use (Kaggle
saves it as `out/env.json` and stamps `kaggle_docker_image`).

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

The image matches Colab's layout: system Python with uv and PyTorch's cu128
torch, both one release behind Colab. Measured on a T4 ×2 kernel with
`--internet`:

- The LoRA set installed in 1 s with uv. Only bitsandbytes 0.50.2 and trl
  1.14.0 were missing, and a second run was a no-op. A CUDA matmul importing
  all of them passed afterwards.
- An unpinned `pip install --dry-run vllm` resolved vllm 0.30 with **torch
  2.13, torchvision 0.28, torchaudio 2.11 and triton 3.7.1**, replacing the
  image's stack. With `install`'s pins it chose vllm 0.19.1 on the existing
  torch 2.10, a release older than Colab got (0.26). It still upgraded
  transformers 5.0 → 5.17, tokenizers and safetensors, and downgraded
  setuptools 81 → 80.10.
- `torch==2.4.0` and `flash-attn` were refused before anything ran (sm75).

Without `--internet` nothing can be downloaded. Build the wheel set on this
machine from the target's fingerprint, upload it as a private dataset, and
install from the mount. The `env.json` must come from the same Docker image
digest that the job will use: Python, glibc, and torch/CUDA versions affect the
resolution. Keep the digest in `kaggle_docker_image` matched to the explicit
`--docker-image` or the helper's pinned T4 default. Do not reuse an old
fingerprint after changing images.

```bash
envsetup wheels --env kaggle-env.json -o ./wheels -r requirements.txt
kaggle datasets create -p ./wheels ...          # private; see kaggle-data-and-runtime.md
kgpu prepare ./job ... --dataset USER/wheels \
  --setup '-r requirements.txt --wheels /kaggle/input'   # searched recursively
```

Measured 2026-09-27 on T4 ×2 with internet off, using the pinned image digest
listed above: `wheels` fetched only
bitsandbytes 0.50.2 and trl 1.14.0 (44 MB). The private dataset mounted at
`/kaggle/input/datasets/<owner>/<slug>/` as regular files, not symlinks, and
their sizes and sha256s matched the local wheels. `install` ran uv with
`--no-index` and took 2 s. torch stayed at 2.10.0+cu128, and a CUDA matmul
importing trl and bitsandbytes passed. The whole kernel took about 2 minutes
from start to finish.

`kaggle-env.json` is a Kaggle `out/env.json` from an earlier `--setup` run on
the same pinned image digest.
`wheels` resolves with uv for the target's Python and glibc, prefers the
versions the target already has, and downloads only what differs. It
resolves torch against PyTorch's own index (`--torch-backend cu128`) because
PyPI's torch of the same version is a different build with different
`nvidia-*` dependencies. Packages with native code and no wheel for the target
are reported, not built.

## flash-attn

PyPI ships flash-attn only as an sdist, so installing it by name compiles CUDA
code on the target. Measured on Colab G4 (48 vCPU, torch 2.11+cu128, Python
3.13): under uv's build isolation it fails in a second (flash-attn doesn't
declare torch as a build dependency); with `pip install --no-build-isolation`
and `MAX_JOBS=40` it was still compiling at 25 minutes when the VM was lost
(cause unconfirmed; RAM exhaustion from 40 nvcc jobs is plausible). So
`install` refuses a by-name flash-attn that isn't already installed. Use
`--skip flash-attn` with `attn_implementation="sdpa"`, or point the
requirement at a prebuilt wheel for that exact torch/CUDA/Python
(`flash-attn @ https://.../flash_attn-...whl`). The local 4090 already has
2.8.3.post1. Below sm80 (T4) it is refused regardless.

## Changing envsetup

`python3 -m pytest ~/agent-skills/skills/gpu-offload/tests/test_envsetup.py -q`
covers requirement parsing, version checks, the refusals above, and layered-venv
detection offline. Installs themselves can only be checked on the targets.
