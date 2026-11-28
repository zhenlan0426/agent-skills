"""Offline regression tests for cgpu, run against tests/fake_colab.py.

    python3 -m pytest ~/agent-skills/skills/gpu-offload/tests -q

These cover failure paths a real VM rarely shows on demand. The happy path is
still worth checking on a real T4 after changing cgpu.
"""
import json
from concurrent.futures import ThreadPoolExecutor
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
CGPU = HERE.parent / "scripts" / "cgpu"


class CgpuTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fakecolab-test-"))
        self.fake = self.tmp / "fake"
        (self.fake / "vms").mkdir(parents=True)
        bin_ = self.tmp / "bin"
        bin_.mkdir()
        (bin_ / "colab").symlink_to(HERE / "fake_colab.py")
        nvidia = bin_ / "nvidia-smi"
        nvidia.write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *driver_version,compute_cap*) echo 'Tesla T4, 15360, 580.0, 7.5' ;;\n"
            "  *name,memory.total*) echo 'Tesla T4, 15360' ;;\n"
            "  *) echo 'CUDA Version: 13.0' ;;\n"
            "esac\n")
        nvidia.chmod(0o755)
        self.env = {**os.environ, "FAKE_COLAB": str(self.fake),
                    "HOME": str(self.tmp / "home"), "CGPU_START_WAIT": "4",
                    "PATH": f"{bin_}:{os.environ['PATH']}"}
        self.addCleanup(self.reap)

    def reap(self):
        # Workers and jobs run detached; kill anything still using our tree.
        out = subprocess.run(["pgrep", "-f", str(self.tmp)], capture_output=True,
                             text=True).stdout.split()
        for pid in out:
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        subprocess.run(["rm", "-rf", str(self.tmp)])

    def cgpu(self, *args, ok=True, env=None, cwd=None):
        r = subprocess.run([sys.executable, str(CGPU), *args], capture_output=True,
                           text=True, env={**self.env, **(env or {})},
                           cwd=cwd or self.tmp, timeout=120)
        if ok is not None:
            self.assertEqual(r.returncode == 0, ok, r.stdout + r.stderr)
        return r

    def vm(self, session="s"):
        return self.fake / "vms" / json.loads((self.fake / "sessions.json")
                                              .read_text())[session] / "fs"

    def record(self, session="s"):
        return json.loads((self.tmp / "home/.cache/cgpu" / f"{session}.json")
                          .read_text())

    def calls(self):
        return [json.loads(l) for l in (self.fake / "calls.log").read_text()
                .splitlines()]

    def occupy_kernel(self, seconds):
        """Queue a cell that holds the kernel, as another client's work would."""
        cell = self.tmp / "busy.py"
        cell.write_text(f"import time\ntime.sleep({seconds})\n")
        p = subprocess.Popen(["colab", "exec", "-s", "s", "--timeout", "999",
                              "-f", str(cell)], env=self.env,
                             stdout=subprocess.DEVNULL, start_new_session=True)
        self.addCleanup(p.kill)
        time.sleep(1)

    def wait_for(self, cond, secs=15):
        end = time.time() + secs
        while time.time() < end:
            if cond():
                return
            time.sleep(0.2)
        self.fail("condition not met")

    # ------------------------------------------------------------- setup

    def test_setup_probes_then_installs_and_propagates_refusal(self):
        self.cgpu("up", "s", "T4")
        r = self.cgpu("setup", "s")
        self.assertIn("installer: pip", r.stdout)
        self.assertTrue((self.vm() / "content/.cgpu/env.json").exists())
        # The local-base and fake-sm75 guards both refuse before installing.
        r = self.cgpu("setup", "s", "--", "flash-attn", ok=False)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertTrue(any(message in r.stdout for message in (
            "refusing to install into the base interpreter", "needs compute capability >= 8.0")))

    def test_setup_probe_failure_skips_install(self):
        self.cgpu("up", "s", "T4")
        fixture = self.tmp / "failing-envsetup"
        fixture.write_text(
            "import pathlib, sys\n"
            "if sys.argv[1] == 'probe':\n"
            "    print('simulated probe failure')\n"
            "    raise SystemExit(7)\n"
            "if sys.argv[1] == 'install':\n"
            "    pathlib.Path(__file__).with_name('install-ran').write_text('yes')\n")
        r = self.cgpu("setup", "s", "--", "requests", ok=False,
                      env={"FAKE_COLAB_ENVSETUP": str(fixture)})
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)
        self.assertIn("simulated probe failure", r.stdout)
        self.assertFalse((self.vm() / "tmp/install-ran").exists())

    def test_setup_refused_mid_job(self):
        self.cgpu("up", "s", "T4")
        self.cgpu("start", "s", "--", "sleep", "5")
        r = self.cgpu("setup", "s", ok=False)
        self.assertIn("needs an idle kernel", r.stderr)

    # ------------------------------------------------------------- start

    def test_start_timeout_cancels_queued_job_and_allows_retry(self):
        self.cgpu("up", "s", "T4")
        self.occupy_kernel(8)
        marker = self.vm() / "content" / "ran-first"
        r = self.cgpu("start", "s", "--", "touch", str(marker), ok=False)
        self.assertIn("cancelled and will not run later", r.stderr)
        self.assertEqual(self.record()["state"], "cancelled")
        # The retry is allowed and runs once the kernel frees up; the first
        # job reaches the kernel too but aborts instead of running.
        second = self.vm() / "content" / "ran-second"
        self.cgpu("start", "s", "--", "touch", str(second),
                  env={"CGPU_START_WAIT": "30"})
        self.cgpu("wait", "s", "--poll", "1")
        self.assertTrue(second.exists())
        self.assertFalse(marker.exists())

    def test_interrupted_start_is_settled_by_next_start(self):
        self.cgpu("up", "s", "T4")
        self.occupy_kernel(8)
        marker = self.vm() / "content" / "ran-first"
        p = subprocess.Popen([sys.executable, str(CGPU), "start", "s", "--",
                              "touch", str(marker)], env=self.env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait_for(lambda: (self.tmp / "home/.cache/cgpu/s.json").exists())
        p.send_signal(signal.SIGKILL)
        p.wait()
        self.assertEqual(self.record()["state"], "submitting")
        # Next start settles the orphan (cancels it), then runs its own job.
        second = self.vm() / "content" / "ran-second"
        self.cgpu("start", "s", "--", "touch", str(second),
                  env={"CGPU_START_WAIT": "30"})
        self.cgpu("wait", "s", "--poll", "1")
        self.assertTrue(second.exists())
        self.assertFalse(marker.exists())

    def test_start_refuses_while_job_runs(self):
        self.cgpu("up", "s", "T4")
        self.cgpu("start", "s", "--", "sleep", "30")
        r = self.cgpu("start", "s", "--", "true", ok=False)
        self.assertIn("still running", r.stderr)

    def test_kill_reports_finished_job_without_signalling(self):
        self.cgpu("up", "s", "T4")
        self.cgpu("start", "s", "--", "true")
        self.cgpu("wait", "s", "--poll", "1")
        n = len(self.calls())
        r = self.cgpu("kill", "s")
        self.assertIn("already finished", r.stdout)
        self.assertNotIn("console", [c[0] for c in self.calls()[n:]])

    def test_kill_escalates_when_process_ignores_sigterm(self):
        self.cgpu("up", "s", "T4")
        self.cgpu("start", "s", "--", sys.executable, "-c",
                  "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
        r = self.cgpu("kill", "s", env={"CGPU_TERM_GRACE": "0.3", "CGPU_KILL_WAIT": "3"})
        self.assertIn("required SIGKILL", r.stdout)
        self.assertEqual([c[0] for c in self.calls()].count("console"), 2)
        self.assertIn("exit=-9", r.stdout)

    # ---------------------------------------------------------------- up

    def test_up_refuses_existing_name(self):
        self.cgpu("up", "s", "T4")
        self.cgpu("start", "s", "--", "sleep", "30")
        r = self.cgpu("up", "s", "T4", ok=False)
        self.assertIn("already exists", r.stderr)
        allocs = (self.fake / "allocations.log").read_text().splitlines()
        self.assertEqual(len(allocs), 1)
        self.assertEqual(self.record()["cmd"], "sleep 30")  # record kept

    def test_up_reuses_name_after_down(self):
        self.cgpu("up", "s", "T4")
        self.cgpu("down", "s")
        self.cgpu("up", "s", "T4")

    def test_up_retries_capacity_refusals_then_allocates(self):
        r = self.cgpu("up", "s", "G4", "--retry-for", "5s", "--retry-every", "0.2s",
                      env={"FAKE_COLAB_NO_CAPACITY": "2"})
        self.assertEqual(r.stdout.count("has no capacity"), 2)
        allocs = (self.fake / "allocations.log").read_text().splitlines()
        self.assertEqual(len(allocs), 1)
        self.assertTrue(all(c[c.index("--gpu") + 1] == "G4"
                            for c in self.calls() if c[0] == "new"))

    def test_up_gives_up_on_capacity_with_tempfail(self):
        r = self.cgpu("up", "s", "G4", "--retry-for", "0.5s", "--retry-every", "0.2s",
                      ok=False, env={"FAKE_COLAB_NO_CAPACITY": "99"})
        self.assertEqual(r.returncode, 75)
        attempts = [c[0] for c in self.calls()].count("new")
        self.assertGreater(attempts, 1)
        self.assertIn(f"no G4 capacity after {attempts} attempt(s)", r.stderr)
        self.assertFalse((self.fake / "allocations.log").exists())

    def test_up_retries_fingerprint_after_lost_connection(self):
        r = self.cgpu("up", "s", "G4", env={"FAKE_COLAB_EXEC_LOST": "1"})
        self.assertIn("fingerprint check failed (exit 1, attempt 1/3)", r.stdout)
        self.assertEqual([c[0] for c in self.calls()].count("new"), 1)

    def test_up_fails_after_three_lost_fingerprints(self):
        r = self.cgpu("up", "s", "G4", ok=False, env={"FAKE_COLAB_EXEC_LOST": "9"})
        self.assertIn("did not pass the fingerprint check after 3 attempts", r.stderr)

    def test_up_retries_failed_nvidia_query_without_reallocating(self):
        nvidia = self.tmp / "bin/nvidia-smi"
        nvidia.write_text("#!/bin/sh\necho device-details\necho driver-error >&2\nexit 9\n")
        r = self.cgpu("up", "s", "G4", ok=False)
        self.assertIn("nvidia-smi exited 9: driver-error", r.stdout)
        self.assertIn("nvidia-smi stdout: device-details", r.stdout)
        self.assertEqual(r.stdout.count("fingerprint check failed"), 3)
        self.assertEqual([c[0] for c in self.calls()].count("exec"), 3)
        self.assertEqual([c[0] for c in self.calls()].count("new"), 1)
        self.assertEqual(len((self.fake / "allocations.log").read_text().splitlines()), 1)

    def test_up_retries_empty_fingerprint(self):
        (self.tmp / "bin/nvidia-smi").write_text("#!/bin/sh\nprintf '  \\n'\n")
        r = self.cgpu("up", "s", "G4", ok=False)
        self.assertIn("nvidia-smi returned empty output", r.stdout)
        self.assertEqual(r.stdout.count("fingerprint check failed"), 3)
        self.assertEqual([c[0] for c in self.calls()].count("new"), 1)

    def test_up_retries_missing_nvidia_binary(self):
        bin_ = self.tmp / "bin"
        (bin_ / "nvidia-smi").unlink()
        (bin_ / "python3").symlink_to(sys.executable)
        r = self.cgpu("up", "s", "G4", ok=False, env={"PATH": str(bin_)})
        self.assertIn("cannot run nvidia-smi", r.stdout)
        self.assertEqual(r.stdout.count("fingerprint check failed"), 3)
        self.assertEqual([c[0] for c in self.calls()].count("new"), 1)

    def test_up_recovers_fingerprint_on_same_vm(self):
        (self.tmp / "bin/nvidia-smi").write_text(
            "#!/bin/sh\n"
            "counter=\"$FAKE_COLAB/nvidia-attempts\"\n"
            "n=0; [ ! -f \"$counter\" ] || n=$(cat \"$counter\")\n"
            "n=$((n + 1)); echo \"$n\" > \"$counter\"\n"
            "if [ \"$n\" -lt 3 ]; then echo 'driver starting' >&2; exit 9; fi\n"
            "echo 'RTX PRO 6000, 98304'\n")
        r = self.cgpu("up", "s", "G4")
        self.assertEqual(r.stdout.count("fingerprint check failed"), 2)
        self.assertIn("RTX PRO 6000, 98304", r.stdout)
        self.assertEqual((self.fake / "nvidia-attempts").read_text().strip(), "3")
        self.assertEqual([c[0] for c in self.calls()].count("new"), 1)
        self.assertEqual([c[0] for c in self.calls()].count("exec"), 3)

    def test_cpu_allocation_does_not_require_nvidia_smi(self):
        bin_ = self.tmp / "bin"
        (bin_ / "nvidia-smi").unlink()
        (bin_ / "python3").symlink_to(sys.executable)
        r = self.cgpu("up", "s", "cpu", env={"PATH": str(bin_)})
        self.assertIn("CPU allocation (no GPU requested)", r.stdout)
        self.assertEqual([c[0] for c in self.calls()].count("exec"), 1)

    # ---------------------------------------------------------- ownership

    def test_claim_refuses_running_job_without_changing_files(self):
        self.cgpu("up", "s", "cpu")
        self.cgpu("start", "s", "--", "sleep", "30")
        target = self.vm() / "content/job.json"
        target.write_text("original job")
        before = len(self.calls())
        r = self.cgpu("claim", "s", "a" * 32, ok=False)
        self.assertIn("cannot establish ownership", r.stderr)
        self.assertIn("is running", r.stderr)
        self.assertEqual(target.read_text(), "original job")
        self.assertFalse((self.vm() / "content/.cgpu-session-owner").exists())
        self.assertNotIn("upload", [c[0] for c in self.calls()[before:]])
        self.assertNotIn("stop", [c[0] for c in self.calls()[before:]])

    def test_two_callers_cannot_claim_same_idle_vm(self):
        self.cgpu("up", "s", "cpu")
        tokens = ["a" * 32, "b" * 32]
        # Separate homes bypass local flock; the VM-side claim must still arbitrate.
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda item: self.cgpu("claim", "s", item[1], ok=None,
                                                          env={"HOME": str(self.tmp / f"home{item[0]}")}),
                                    enumerate(tokens)))
        self.assertEqual(sorted(r.returncode == 0 for r in results), [False, True])
        winner = tokens[next(i for i, r in enumerate(results) if r.returncode == 0)]
        loser = tokens[1 - tokens.index(winner)]
        owner = self.vm() / "content/.cgpu-session-owner"
        self.assertEqual(owner.read_text().strip(), winner)
        bad = self.cgpu("release", "s", loser, ok=False)
        self.assertIn("token does not match", bad.stdout)
        self.assertEqual(owner.read_text().strip(), winner)
        self.cgpu("release", "s", winner)
        self.assertFalse(owner.exists())
        self.cgpu("claim", "s", loser)
        self.assertEqual(owner.read_text().strip(), loser)

    def test_busy_unknown_kernel_cannot_claim_later(self):
        self.cgpu("up", "s", "cpu")
        self.occupy_kernel(4)  # No local cgpu job record.
        r = self.cgpu("claim", "s", "a" * 32, "--timeout", "1", ok=False)
        self.assertIn("kernel is busy or the idle probe failed", r.stderr)
        time.sleep(4)  # The queued, read-only probe runs after the client gives up.
        self.assertFalse((self.vm() / "content/.cgpu-session-owner").exists())

    def test_claim_queued_after_probe_expires_without_changing_vm(self):
        self.cgpu("up", "s", "cpu")
        with patch.dict(os.environ, self.env, clear=True):
            action = runpy.run_path(str(CGPU))["ownership_action"]
            globals_ = action.__globals__
            original = globals_["exec_py"]
            def occupy_after_probe(session, code, **kwargs):
                rc = original(session, code, **kwargs)
                if "__CGPU_IDLE_" in code:
                    self.occupy_kernel(4)
                return rc
            globals_["exec_py"] = occupy_after_probe
            with self.assertRaises(SystemExit):
                action(SimpleNamespace(session="s", token="a" * 32, timeout=1))
        time.sleep(4)  # Let the abandoned claim cell leave the kernel queue.
        self.assertFalse((self.vm() / "content/.cgpu-session-owner").exists())

    def test_claim_refuses_lost_job_that_may_still_run(self):
        self.cgpu("up", "s", "cpu")
        self.cgpu("start", "s", "--", "sleep", "30")
        job_dir = self.vm() / self.record()["dir"].lstrip("/")
        (job_dir / "beat").write_text(str(time.time() - 1000))
        r = self.cgpu("claim", "s", "a" * 32, ok=False)
        self.assertIn("lost its kernel cell", r.stderr)
        self.assertIn("may still be running", r.stderr)
        self.assertFalse((self.vm() / "content/.cgpu-session-owner").exists())

    def test_exec_watchdog_kills_a_hung_client(self):
        self.cgpu("up", "s", "T4")
        (self.fake / "exec-lost-left").write_text("1")  # `up` already used its own count
        env = {**self.env, "FAKE_COLAB_EXEC_LOST_HANG": "1"}
        with patch.dict(os.environ, env, clear=True):
            exec_py = runpy.run_path(str(CGPU))["exec_py"]
            exec_py.__globals__["EXEC_GRACE"] = 0.5
            start = time.monotonic()
            rc = exec_py("s", "print(1)", timeout="0.5", check=False)
        self.assertEqual(rc, -9)
        self.assertLess(time.monotonic() - start, 30)

    def test_up_without_retry_fails_once(self):
        r = self.cgpu("up", "s", "G4", ok=False, env={"FAKE_COLAB_NO_CAPACITY": "1"})
        self.assertEqual(r.returncode, 75)
        self.assertEqual([c[0] for c in self.calls()].count("new"), 1)

    # -------------------------------------------------------------- pull

    def pull_setup(self):
        self.cgpu("up", "s", "T4")
        ckpt = self.vm() / "content" / "ckpt"
        ckpt.mkdir()
        (ckpt / "last.pt").write_bytes(b"weights" * 100)
        return ckpt

    def test_pull_file_into_new_dir_with_trailing_slash(self):
        self.pull_setup()
        self.cgpu("pull", "s", "/content/ckpt/last.pt", "./ckpt/")
        self.assertEqual((self.tmp / "ckpt" / "last.pt").read_bytes(), b"weights" * 100)

    def test_pull_file_into_existing_dir(self):
        self.pull_setup()
        (self.tmp / "out").mkdir()
        self.cgpu("pull", "s", "/content/ckpt/last.pt", "out")
        self.assertTrue((self.tmp / "out" / "last.pt").exists())

    def test_pull_missing_file_does_not_touch_kernel(self):
        self.pull_setup()
        n = len(self.calls())
        r = self.cgpu("pull", "s", "/content/nope.pt", "x.pt", ok=False)
        self.assertIn("does not exist", r.stderr)
        self.assertNotIn("exec", [c[0] for c in self.calls()[n:]])

    def test_pull_directory(self):
        self.pull_setup()
        self.cgpu("pull", "s", "/content/ckpt", "got")
        self.assertTrue((self.tmp / "got" / "last.pt").exists())

    def test_pull_directory_refused_mid_job(self):
        self.pull_setup()
        self.cgpu("start", "s", "--", "sleep", "30")
        n = len(self.calls())
        r = self.cgpu("pull", "s", "/content/ckpt", "got", ok=False)
        self.assertIn("idle kernel", r.stderr)
        self.assertNotIn("exec", [c[0] for c in self.calls()[n:]])
        # A single file still works mid-job.
        self.cgpu("pull", "s", "/content/ckpt/last.pt", "got/")

    # ---------------------------------------------------------------- push

    def test_push_filters_gitignored_data_and_credentials(self):
        self.cgpu("up", "s", "T4")
        project = self.tmp / "project"
        project.mkdir()
        (project / "safe.py").write_text("print('safe')\n")
        (project / ".gitignore").write_text("large-data.bin\n")
        (project / ".env").write_text("SECRET=value\n")
        (project / "credential.pem").write_text("private key\n")
        (project / "large-data.bin").write_bytes(b"data")
        (project / ".ssh").mkdir()
        (project / ".ssh" / "id_rsa").write_text("private key\n")
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "add", ".gitignore", "safe.py"], check=True)
        self.cgpu("push", "s", str(project), "/content/project")
        remote = self.vm() / "content/project"
        self.assertTrue((remote / "safe.py").exists())
        self.assertTrue((remote / ".gitignore").exists())
        for name in (".env", "credential.pem", "large-data.bin", ".ssh/id_rsa"):
            self.assertFalse((remote / name).exists(), name)
        self.assertFalse(list((self.vm() / "tmp").glob("cgpu-push-*")))

    def test_push_directory_refuses_before_remote_upload_during_job(self):
        self.cgpu("up", "s", "T4")
        project = self.tmp / "project"
        project.mkdir()
        (project / "code.py").write_text("pass\n")
        self.cgpu("start", "s", "--", "sleep", "30")
        n = len(self.calls())
        r = self.cgpu("push", "s", str(project), ok=False)
        self.assertIn("idle kernel", r.stderr)
        self.assertNotIn("upload", [c[0] for c in self.calls()[n:]])
        self.assertFalse(list((self.vm() / "tmp").glob("cgpu-push-*")))

    def test_push_large_file_refuses_before_upload_during_job(self):
        self.cgpu("up", "s", "T4")
        large = self.tmp / "large.bin"
        with large.open("wb") as f:
            f.truncate(40 * 1000 * 1000 + 1)
        self.cgpu("start", "s", "--", "sleep", "30")
        n = len(self.calls())
        r = self.cgpu("push", "s", str(large), ok=False)
        self.assertIn("idle kernel", r.stderr)
        self.assertNotIn("upload", [c[0] for c in self.calls()[n:]])

    def test_fake_path_rewrite_preserves_python_under_root(self):
        with patch.dict(os.environ, {"FAKE_COLAB": str(self.fake)}):
            rewrite = runpy.run_path(str(HERE / "fake_colab.py"))["rewrite"]
        vm = self.fake / "vms" / "vm"
        code = "cmd=['/root/.venv/bin/python', '/content/project/train.py', '/tmp/cgpu-part-x']"
        rewritten = rewrite(vm, code)
        self.assertIn("'/root/.venv/bin/python'", rewritten)
        self.assertIn(str(vm / "fs/content/project/train.py"), rewritten)
        self.assertIn(str(vm / "fs/tmp/cgpu-part-x"), rewritten)

    def test_interrupted_pull_keeps_previous_copy(self):
        self.pull_setup()
        (self.tmp / "ckpt").mkdir()
        (self.tmp / "ckpt" / "last.pt").write_bytes(b"old")
        self.cgpu("pull", "s", "/content/ckpt/last.pt", "ckpt/last.pt", ok=False,
                  env={"FAKE_COLAB_DOWNLOAD_FAIL": "1"})
        self.assertEqual((self.tmp / "ckpt" / "last.pt").read_bytes(), b"old")
        self.assertEqual(sorted(p.name for p in (self.tmp / "ckpt").iterdir()),
                         ["last.pt"])  # no .part left behind

    # -------------------------------------------------------------- logs

    def test_logs_never_fall_back_to_full_log(self):
        self.cgpu("up", "s", "T4")
        self.cgpu("start", "s", "--", sys.executable, "-c",
                  "import sys; sys.stdout.write('x' * 600_000)")
        self.cgpu("wait", "s", "--poll", "1")
        n = len(self.calls())
        r = self.cgpu("logs", "s")
        downloads = [c for c in self.calls()[n:] if c[0] == "download"]
        self.assertFalse(any(c[3].endswith("/log") for c in downloads), downloads)
        self.assertIn("--full", r.stderr)
        r = self.cgpu("logs", "s", "--full")
        self.assertEqual(len(r.stdout.strip()), 600_000)

    # ----------------------------------------------------- kernel restart

    def test_kernel_restart_is_reported_and_unblocks_after_kill(self):
        self.cgpu("up", "s", "T4")
        self.cgpu("start", "s", "--", "sleep", "300")
        subprocess.run(["colab", "restart-kernel", "-s", "s"], env=self.env, check=True)
        jdir = self.vm() / self.record()["dir"].lstrip("/")
        time.sleep(3)  # let a heartbeat write that raced the kill land
        (jdir / "beat").write_text(repr(time.time() - 1000))  # cell is gone
        self.assertFalse((jdir / "status.json").exists())
        r = self.cgpu("logs", "s")
        self.assertIn("LOST", r.stderr)
        r = self.cgpu("wait", "s", ok=False)
        self.assertEqual(r.returncode, 3)
        r = self.cgpu("start", "s", "--", "true", ok=False)
        self.assertIn("cgpu kill", r.stderr)
        pid = int((jdir / "pid").read_text())
        self.cgpu("kill", "s")
        self.wait_for(lambda: subprocess.run(["kill", "-0", str(pid)],
                                             capture_output=True).returncode != 0)
        self.cgpu("start", "s", "--", "true")
        self.cgpu("wait", "s", "--poll", "1")

    def test_kill_records_lost_job_whose_process_is_gone(self):
        self.cgpu("up", "s", "T4")
        self.cgpu("start", "s", "--", "sleep", "300")
        subprocess.run(["colab", "restart-kernel", "-s", "s"], env=self.env, check=True)
        jdir = self.vm() / self.record()["dir"].lstrip("/")
        pid = int((jdir / "pid").read_text())
        os.killpg(pid, signal.SIGKILL)
        time.sleep(3)
        (jdir / "beat").write_text(repr(time.time() - 1000))
        r = self.cgpu("kill", "s")
        self.assertIn("is gone", r.stdout)
        self.cgpu("start", "s", "--", "true")
        self.cgpu("wait", "s", "--poll", "1")


if __name__ == "__main__":
    unittest.main()
