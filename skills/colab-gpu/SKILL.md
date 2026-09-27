---
name: colab-gpu
description: >-
  Offload a GPU job from this machine to Google Colab (the user's Colab Pro account) using the official `colab` CLI and the bundled `cgpu` helper — allocate a T4/L4/G4/A100/H100, push code and data, run a long job detached, poll logs, pull results, stop the VM. Explicit invocation only (/colab-gpu, $colab-gpu, or the user saying to run something on Colab): it spends paid compute units. Do not use it just because a task involves a GPU; the local RTX 4090 is the default.
disable-model-invocation: true
---

# Colab GPU offload

Runs work on a rented Colab VM. **Only when the user asked for Colab.** Every
minute a VM is allocated burns compute units, so finish with `cgpu down`.

The helper is `~/agent-skills/skills/colab-gpu/scripts/cgpu` (call it `cgpu`
below). It wraps `colab --auth=oauth2 ...`. `cgpu --help` lists commands, and
`colab help <cmd>` covers the raw CLI.

## Before starting

- `cgpu ls` shows live sessions and the compute-unit balance. If it errors with
  401/403 or a missing token, ask the user to run
  `! colab --auth=oauth2 sessions` and paste the code (a one-time browser
  consent). Don't try to fix auth any other way. If `colab` isn't installed,
  run `uv tool install google-colab-cli`.
- **Pick the GPU with the user** unless they named one. The local 4090 has 24GB:
  T4 (16GB) and L4 (24GB) are slower than the 4090 and only make sense for
  parallel work. For more VRAM or speed, choose A100, H100, or G4. `cgpu up`
  prints the real GPU and memory, and the hourly rate from `colab usage`. Tell
  the user the rate.
- If the requested GPU won't allocate (A100/H100 are often unavailable), **ask
  before switching to another type**. `cgpu up` already refuses unknown names,
  because the raw CLI silently turns them into an A100.

## Workflow

```bash
cgpu up job1 A100                              # add --high-mem for high-RAM shape
cgpu push job1 ./myproj                        # -> /content/myproj (.git, venvs, caches excluded)
cgpu secrets job1 kaggle hf                    # only if the job needs them
cgpu sh job1 -- pip install -q -r /content/myproj/requirements.txt
cgpu start job1 --cwd /content/myproj -- python train.py --epochs 3
cgpu logs job1 -n 50                           # any time; also says running/FINISHED
cgpu wait job1                                 # blocks; exits with the job's exit code
cgpu pull job1 /content/myproj/out ./out       # file or directory
cgpu down job1                                 # ALWAYS, including after failures
```

- **Long jobs:** `start` returns once the job is running. Don't block a turn on
  `wait` for longer than your tool timeout. In Claude Code, run `cgpu wait` with
  `run_in_background` so you get a notification. Otherwise poll with `cgpu logs`
  every few minutes. `cgpu kill` stops the job but keeps the VM.
- **One job at a time per session.** The job occupies the kernel, so `sh`,
  directory `pull`, and `push` of a directory all wait behind it. `logs`, `kill`,
  and single-file `pull` still work mid-job. Use a second session for parallel
  work.
- **Data:** local files go through `push`, which chunks uploads at about 7MB/s
  (a few GB is fine, much more is slow). For Kaggle and Hugging Face data,
  download on the VM: `cgpu secrets` installs `~/.kaggle/kaggle.json` and the HF
  token there, then use the `kaggle` CLI or `huggingface_hub` inside the job.
  `colab drivemount` needs a human at the keyboard, so don't use it.
- **Write outputs under `/content/...`** and checkpoint periodically. The VM is
  ephemeral, and nothing survives `down` or a backend reclaim. Pull what matters
  before `down`.
- The VM image ships Python 3.13 and a recent CUDA PyTorch. Check `cgpu sh job1
  -- pip list` before installing heavy packages.

## Behavior the helper works around (verified on colab-cli 0.7.4)

- `colab exec` exits 0 even when the code raises, and its `--timeout` (default
  30s) is a wall-clock budget. When it runs out, the local client dies with a
  traceback but the job keeps running. `cgpu` adds its own exit-code markers and
  uses a week-long timeout.
- The VM stays alive while the kernel is busy, and the job survives the local
  client disconnecting. So `start` runs the job as a subprocess inside one
  blocking kernel cell, detaches the local client (which would otherwise burn a
  CPU core polling), and reads progress by downloading `/content/.cgpu/<job>/log`.
- Uploads over ~50–100MB fail at the proxy, so `cgpu` sends 40MB parts.
  Downloads have no such limit.
- `colab exec --env` leaks values into the local argv and history, so `cgpu
  secrets` uploads files instead.
- Unverified: how long a Pro VM stays alive with a busy kernel and no client
  attached (a ~90-second test held). For multi-hour jobs, checkpoint and check
  `cgpu logs`. If the session disappears (`cgpu ls`), tell the user and don't
  quietly re-run.

## If something breaks

- `cgpu logs` shows the job's own output. The detached client's output is in
  `~/.cache/cgpu/<session>-<job>.client.log`.
- If the kernel is wedged, run `colab --auth=oauth2 restart-kernel -s <name>`
  (keeps the VM). If the session is gone, recreate it with `cgpu up`.
- Orphaned VMs cost money. `cgpu ls` lists every server-side session, and
  `colab --auth=oauth2 stop -s <name>` stops one.
