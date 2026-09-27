---
name: gpu-offload
description: >-
  Decide where a GPU job runs and offload it off this machine: the local RTX
  4090 (24GB), private Kaggle kernels (T4 ×2, free weekly quota, `kgpu`
  helper), or a Google Colab Pro VM (G4 RTX PRO 6000 96GB, paid compute units,
  `cgpu` helper). Use when the user asks to run something on Kaggle, Colab, or
  a remote/cloud GPU; when a model or training run clearly won't fit in 24GB
  VRAM even with standard memory techniques; or when the local GPU is occupied
  and a job should run elsewhere. Not for ordinary GPU work that fits locally,
  or for Kaggle notebook discovery, conversion, or version history (other
  skills cover those).
---

# GPU offload: local 4090, Kaggle, or Colab

This skill does two things: choosing the target, and then running the job
there. For the second part, read the target's reference file:
[references/kaggle.md](references/kaggle.md) (`kgpu`, batch submission) or
[references/colab.md](references/colab.md) (`cgpu`, an interactive VM).

## Targets

| Target | GPU memory | Cost | Speed vs 4090 |
|---|---|---|---|
| **Local 4090** | 24GB on one card; bf16, FlashAttention 2 | free | 1× |
| **Kaggle T4 ×2** | 2 × 16GB separate cards; fp16 only (no bf16), no FlashAttention 2 | free; weekly quota that resets and expires unused; queue waits; runs capped at about 12h | several times slower |
| **Colab G4** | 96GB on one card (RTX PRO 6000 Blackwell; bf16/fp8) | 8.90 CU/hr from a balance that expires at month end; observed charges suggest a ~15 min minimum per `up` (inferred) | faster |

Colab's A100 high-mem (6.77 CU/hr, VRAM unverified, hung once in testing), L4,
and T4 are worse value than G4, the 4090, and Kaggle respectively. Use them
only as a fallback.

**Kaggle's 32GB is not one pool.** Data-parallel training (DDP) puts a full
model copy on each 16GB card, so its limit is lower than local. Extra capacity
comes only from splitting the model across the two cards:
`device_map="auto"` for inference (easy), or FSDP/ZeRO-3 for training (fiddly,
and fp16 can overflow on models built for bf16).

## Choosing

1. **The user named a target:** use it, with no justification needed.
2. **Fits locally and the 4090 is free:** run locally. For this routing
   decision, "fits locally" means it runs on one 24GB card in bf16 with
   FlashAttention 2. Include standard techniques that do not change the
   computation: smaller micro-batches with gradient accumulation, gradient
   checkpointing, 8-bit optimizer states, or CPU offload into 94GB of RAM.
3. **Fits locally but the 4090 is occupied**, or the user wants parallel work:
   use **Kaggle only if** the job can be split across two separate 16GB T4s,
   runs correctly in fp16 without FlashAttention 2, and is expected to finish
   within about 12h at T4 speed. T4s are several times slower than the 4090 and
   jobs can queue, so waiting for the 4090 often finishes sooner. If any Kaggle
   check fails, wait for the 4090 or ask the user. The free weekly quota resets,
   so unused hours are lost.
4. **Doesn't fit in 24GB:** use **Kaggle** only when it passes those same three
   checks; otherwise use **Colab G4**.

Kaggle's unmodified default kernel image mirrors the scoring environment for
code competitions. The T4 helper pins a Kaggle image snapshot so its packages
stay reproducible; that snapshot can stop matching scoring after Kaggle updates
its default. Use `kgpu prepare --use-kaggle-default-image` when current scoring
parity matters, and check the competition's code requirements before choosing
a shape.

When the only way to fit locally changes the method or results (QLoRA instead
of bf16 LoRA, 4-bit instead of bf16 weights, a shorter context), that
trade-off belongs to the user. Present both options with their costs, for
example "QLoRA locally, or bf16 LoRA on G4 for about 3h ≈ 27 CU".

## Colab needs a written justification, not permission

Before `cgpu up`, when the user didn't ask for Colab, tell the user in a few
lines, then proceed without waiting:

- **Why not local:** the memory estimate or measurement (below) and why no
  step-2 technique closes the gap.
- **Why not Kaggle:** for example, it needs more than 16GB on a single device,
  needs bf16, or would take more than about 12h on T4s.
- **Cost:** the GPU, expected hours × rate = CU, and that figure against the
  balance `cgpu ls` reports.

A job being faster on G4 is not a justification by itself. The exception is
when the month is nearly over and the balance would otherwise expire unused;
say that is the reason. If the expected cost is a large share of the balance,
or you can't bound the runtime, ask instead of proceeding.

## Estimating memory

Measure when you can. Run `accelerate estimate-memory <hf-model-id>`, or run a
single local step at a small batch/sequence length, read
`torch.cuda.max_memory_allocated()`, and extrapolate. If the step OOMs, that
is also evidence. Rough bytes per parameter:

- **Inference:** about 2 (bf16), about 1 (int8), or about 0.55 (4-bit), plus
  the KV cache (2 × layers × kv_heads × head_dim × seq_len × batch × 2 bytes),
  plus 10–20% overhead.
- **LoRA:** frozen weights at the inference rate, plus adapters, plus
  activations. Activations dominate at long sequence length; gradient
  checkpointing cuts them.
- **Full fine-tune, mixed-precision Adam:** about 16–18, plus activations. A
  7B model needs about 120GB, which exceeds even G4 unless optimizer states
  are 8-bit or offloaded.

## Dependencies differ per target

Each target already has a CUDA build of torch (and more), and a plain
`pip install -r requirements.txt` can silently replace it. Install project
dependencies with `scripts/envsetup`, which pins that stack and reports what
changed: `kgpu prepare --setup '-r requirements.txt'`, `cgpu setup SESSION --
-r /content/proj/requirements.txt`, or locally `envsetup install --venv .venv
-r requirements.txt`. [references/environments.md](references/environments.md)
has what each target ships, the traps measured on each, flash-attn, and
offline wheels for Kaggle.

## Rules for every target

- Don't put secrets in source, argv, logs, or outputs. Each reference file says
  how to provide them on its platform.
- Files on a remote VM are not durable. For long, valuable runs, get
  checkpoints off the VM while the job runs.
- Don't resubmit unchanged after a failure you haven't diagnosed. On Colab,
  every re-allocation costs compute units again.
- Report where the job ran and, for Colab, the CU it actually used.
