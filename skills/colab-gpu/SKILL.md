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
  consent). Don't try to fix auth any other way. `cgpu` was verified against
  colab-cli 0.7.4 and depends on its exact messages. If `colab` isn't installed,
  run `uv tool install google-colab-cli==0.7.4`. If `colab version` reports a
  different version, tell the user: the workarounds below may no longer hold.
- **Pick the GPU with the user** unless they named one or delegated the choice
  (for example "whatever fits under N units/hour"). The local 4090 has 24GB:
  T4 (16GB) and L4 (24GB) are slower than the 4090 and only make sense for
  parallel work. For more VRAM or speed, choose A100, H100, or G4. `cgpu up`
  prints the real GPU and memory, and the hourly rate from `colab usage`. Tell
  the user the rate.
- If the requested GPU won't allocate (A100/H100 are often unavailable), **ask
  before switching to another type**, unless the user already said what to fall
  back to. `cgpu up` already refuses unknown GPU names, because the raw CLI
  silently turns them into an A100.
- `cgpu up` refuses a session name that is already live: the raw CLI would
  allocate a second VM under it and orphan the first, still billed. To replace
  a session, `cgpu down` it first.

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
cgpu down job1                                 # always, once results are safe (below)
```

- **Commands are argv, not shell strings.** Arguments after `--` reach the VM
  exactly as quoted locally. For pipes, globs, `&&`, or `cd`, invoke a shell:
  `cgpu sh job1 -- bash -c 'nvidia-smi | head -5'`.

- **Long jobs:** `start` returns once the job is running. Don't block a turn on
  `wait` for longer than your tool timeout. `cgpu wait --timeout SECS` gives up
  after SECS with exit 124 and leaves the job running. In Claude Code, run
  `cgpu wait` with `run_in_background` so you get a notification. Otherwise
  poll with `cgpu logs` every few minutes. `logs` only ever reads a 256KB tail
  the VM keeps up to date (about every 2s), so polling stays cheap for verbose
  jobs; it says so when the tail holds fewer lines than asked for. `--full`
  fetches the whole log. `cgpu kill` stops the job but keeps the VM.
- **One job at a time per session.** `start` refuses while the session's
  previous job is still running. The job occupies the kernel, so `sh`,
  `secrets`, `push`, and directory `pull` refuse until it ends (a queued cell
  would still run after its client gave up). `logs`, `kill`, and single-file
  `pull` work mid-job. Use a second session for parallel work.
- If `start` can't confirm the job left the kernel queue within 3 minutes, it
  cancels it, so it can't run later unnoticed, and says so. Retrying is then
  safe. An interrupted `start` is settled the same way by the next one.
- **Data:** local files go through `push`, which chunks uploads (about 7MB/s
  when tested; a few GB is fine, much more is slow). For Kaggle and Hugging
  Face data, download on the VM: `cgpu secrets` installs
  `~/.kaggle/kaggle.json` and the HF token there, then use the `kaggle` CLI or
  `huggingface_hub` inside the job.
  `colab drivemount` needs a human at the keyboard, so don't use it.
- **Outputs and checkpoints.** Write outputs under `/content/...`. Everything
  there is lost with the VM, whether through `down` or a backend reclaim.
  Periodic saves on the VM protect against the job crashing but not against
  losing the VM. For a job the user can't afford to lose, get checkpoints off
  the VM while it runs:
  - Have the job upload each checkpoint itself, for example with
    `huggingface_hub.upload_file` to a private repo (after `cgpu secrets job1
    hf`). This works even when no one is watching.
  - Or, while polling, copy the newest checkpoint home with a single-file
    `cgpu pull job1 /content/myproj/ckpt/last.pt ./ckpt/` (a directory or a
    trailing `/` puts the file inside it). This works mid-job. `pull` writes
    beside the destination and renames, so an interrupted pull keeps the last
    good copy. Have the job likewise write each checkpoint under a temporary
    name and rename it into place, so a pull never catches half a file.
  Decide which of these to use with the user before starting a multi-hour job.
- The image tested (September 2026) had Python 3.13 and a recent CUDA PyTorch.
  Images change, so check `cgpu sh job1 -- pip list` before installing heavy
  packages.

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
  attached (a ~90-second test held). For multi-hour jobs, keep checkpoints off
  the VM (see above) and check `cgpu logs`. `cgpu` reports a lost session
  explicitly ("session '…' is gone"). If that happens, tell the user what was
  lost and don't quietly re-run.

## If something breaks

- `cgpu logs` shows the job's own output. The detached client's output is in
  `~/.cache/cgpu/<session>-<job>.client.log`.
- If the kernel is wedged, run `colab --auth=oauth2 restart-kernel -s <name>`
  (keeps the VM). A job cut off that way never writes an exit status: after
  about 3 minutes `logs` reports it LOST and `wait` exits 3. Its process may
  still be running, so `start` refuses until `cgpu kill` has been sent.
- If the session is gone, recreate it with `cgpu up` and resume from the last
  off-VM checkpoint, but **only with the user's authorization**, which they may
  have given up front (for example "retry once if the VM dies"). A rerun spends
  compute units again.
- If `pull` fails, don't `down` yet: that would destroy the only copy. Check
  that the session is still alive (`cgpu ls`), then retry. Pull a directory
  that is too large for one download in smaller pieces, as single files, or
  after packing it with `cgpu sh job1 -- tar czf /content/out.tgz -C
  /content/myproj out`. The VM costs money meanwhile. If the results still
  can't be retrieved, tell the user and let them choose between more attempts
  and `down`.
- Orphaned VMs cost money. `cgpu ls` lists every server-side session, and
  `colab --auth=oauth2 stop -s <name>` stops one.

## Changing `cgpu`

`python3 -m pytest ~/agent-skills/skills/colab-gpu/tests -q` runs offline
against a fake `colab` (`tests/fake_colab.py`) that reproduces the failure paths
above: a busy kernel, a reused name, directory and interrupted pulls, oversized
logs, a kernel restart. It can't prove the real service still behaves that way,
so also check the happy path on a real T4.
