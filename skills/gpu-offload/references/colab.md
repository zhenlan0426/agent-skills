# Colab GPU offload

Runs work on a rented Colab VM. Use it only when the user asked for Colab or
you wrote the justification that [../SKILL.md](../SKILL.md) requires. Every
minute a VM is allocated burns compute units, so finish with `cgpu down`.

The helper is `~/agent-skills/skills/gpu-offload/scripts/cgpu` (call it `cgpu`
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
- **GPU:** use the one the user named. Otherwise use G4 (see the routing
  table in [../SKILL.md](../SKILL.md)). Measured on this account (Colab Pro,
  2026-09-27; rates in compute units per hour):

  | `cgpu up` GPU argument | GPU | VRAM | vCPU / RAM | CU/hr |
  |---|---|---|---|---|
  | T4 | Tesla T4 | 16GB | 2 / 13GB (high-mem: 8 / 53GB) | 1.07 (1.27) |
  | L4 | L4 | 24GB | 12 / 56GB | 1.54 |
  | A100 --high-mem | A100 | not probed (hung) | High-RAM | 6.77 |
  | G4 | RTX PRO 6000 Blackwell | 96GB | 48 / 185GB | 8.90 |
  | H100 | — | — | rejected: no entitlement on Pro | — |

  The local 4090 has 24GB. T4 and L4 are slower than the 4090, and Kaggle's
  free T4 ×2 covers the same need. G4 gives more VRAM per CU than A100 and a
  newer architecture. `cgpu up` prints the actual GPU and the current rate.
  Include the rate in your justification.
- Plain `--gpu A100` returned 503 while `A100 --high-mem` allocated. The first
  exec on that A100 then hung without starting. If `up` fails or hangs, run
  `cgpu down`. Switch to another type only if the justification still holds
  for that card (for example, the job fits the A100's VRAM, which
  `nvidia-smi` shows). Say that you switched and why, unless the user already
  said what to fall back to. `cgpu up` already refuses unknown GPU
  names, because the raw CLI silently turns them into an A100.
- Billing looks like it has a minimum per allocation. Four sub-minute probes
  plus one ~7-minute A100 cost 4.87 CU, which matches ~15 minutes billed for
  each. Don't allocate VMs just to test availability, and reuse one session
  instead of repeatedly running `up`/`down`.
- `cgpu up` refuses a session name that is already live: the raw CLI would
  allocate a second VM under it and orphan the first, still billed. To replace
  a session, `cgpu down` it first.

## Workflow

```bash
cgpu up job1 G4                                # recommended; add --high-mem only when needed
cgpu push job1 ./myproj                        # -> /content/myproj (.git, venvs, caches excluded)
cgpu secrets job1 kaggle hf                    # only if the job needs them
cgpu setup job1 -- -r /content/myproj/requirements.txt   # keeps the image's torch; see environments.md
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
  fetches the whole log. `cgpu kill` sends SIGTERM, waits 5 seconds, escalates
  to SIGKILL if needed, and reports the final job status. The VM stays up.
- **One job at a time per session.** `start` refuses while the session's
  previous job is still running. The job occupies the kernel, so `sh`,
  `secrets`, directory pushes, pushes over 40MB, and directory `pull` refuse
  until it ends (a queued cell would still run after its client gave up).
  `logs`, `kill`, small-file pushes, and single-file `pull` work mid-job. Use a
  second session for parallel work. Directory pushes respect Git ignore rules
  and exclude common credentials; use `cgpu secrets` for Kaggle or HF access.
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
- The image tested (September 2026) had Python 3.13 and torch 2.11+cu128.
  Images change: `cgpu setup` prints the current fingerprint, and
  [environments.md](environments.md) covers installing without replacing it.

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
- If the session is gone, you may recreate it with `cgpu up` once and resume
  from the last off-VM checkpoint. First tell the user what was lost and what
  resuming will cost. If there is no off-VM checkpoint, the session is lost a
  second time, or the user said not to retry, stop and ask. A rerun spends
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

`python3 -m pytest ~/agent-skills/skills/gpu-offload/tests/test_cgpu.py -q` runs offline
against a fake `colab` (`tests/fake_colab.py`) that reproduces the failure paths
above: a busy kernel, a reused name, directory and interrupted pulls, oversized
logs, a kernel restart. It can't prove the real service still behaves that way,
so also check the happy path on a real T4.
