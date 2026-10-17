"""Offline behavioral checks for the codec, emitter, and generated scripts."""

from contextlib import contextmanager
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from probe import decode_observations
from probe_runtime import Codec, SqrtScale, dataset_dir, emit_signal, pack_slots, unpack_slots


@contextmanager
def working_directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class ProbeTests(unittest.TestCase):
    def test_integer_roundtrip_and_timing_for_every_digit(self):
        for codec in (Codec(digits=1), Codec("0.3", "0.1", 2, 2)):
            for value in range(codec.capacity):
                digits = codec.encode(value, "integer")
                self.assertEqual(codec.decode(digits, "integer"), {"integer": value})
                for digit in digits:
                    signal = codec.signal(digit)
                    elapsed = Decimal(signal["target_seconds"]) + Decimal("42.1")
                    self.assertEqual(codec.observe(elapsed, signal["outcome"], "42.1", 1), digit)

    def test_separate_allowance_keeps_grid_and_crash_slot(self):
        legacy = Codec("8", "0.5", 5, 1)
        self.assertEqual(Codec("8", "0.5", 5, 1, "0.5"), Codec(**{**legacy.__dict__, "allowance_hours": "0.5"}))
        self.assertEqual((legacy.buckets, legacy.base, legacy.signal(47)["target_seconds"]), (16, 63, "1800.0"))
        codec = Codec("8", "0.25", 2, 4, "0.5")
        self.assertEqual((codec.buckets, codec.base), (31, 216))
        self.assertEqual(codec.allowance_seconds, 1800)
        for digit in range(codec.base):
            signal = codec.signal(digit)
            self.assertEqual(Decimal(signal["target_seconds"]), 1800 + signal["bucket"] * 900)
            self.assertEqual(codec.observe(Decimal(signal["target_seconds"]) + 37, signal["outcome"], 37, 300), digit)
        self.assertEqual(codec.signal(codec.base - 1)["target_seconds"], "28800.00")
        with self.assertRaisesRegex(ValueError, "Reserved crash"):
            codec.observe(1800 + 37, "exception", 37, 300)
        for bad in (("8", "0.25", 1, 1, "0.3"), ("8", "0.25", 1, 1, "9"), ("8", "0.25", 1, 1, "0")):
            with self.assertRaises(ValueError):
                Codec(*bad)

    def test_fraction_bounds_and_endpoint(self):
        codec = Codec()
        for value in ("0", "0.000000001", "0.3333333333333333333333", "0.5", "0.999999999", "1"):
            interval = codec.decode(codec.encode(value))
            self.assertLessEqual(Decimal(interval["lower"]), Decimal(value))
            if interval["upper_inclusive"]:
                self.assertLessEqual(Decimal(value), Decimal(interval["upper"]))
            else:
                self.assertLess(Decimal(value), Decimal(interval["upper"]))
        self.assertEqual(codec.encode(1), [30] * 5)

    def test_leading_zero_and_capacity_boundaries(self):
        codec = Codec(digits=3)
        self.assertEqual(codec.encode(31, "integer"), [0, 1, 0])
        self.assertEqual(codec.encode(0, "integer"), [0, 0, 0])
        for value, mode in ((codec.capacity, "integer"), (-1, "integer"), (1.5, "integer"),
                            ("1.0000001", "fraction"), (-0.1, "fraction"), ("NaN", "fraction")):
            with self.assertRaises(ValueError):
                codec.encode(value, mode)
        with self.assertRaises(ValueError):
            Codec("4", "0.3")

    def test_unknown_errors_and_bad_timing_are_rejected(self):
        codec = Codec()
        for args in ((217, "unknown", 217, 100), (217, "timeout", 217, 100),
                     (217 + 900, "missing", 217, 100), (217 + 9 * 1800, "missing", 217, 100),
                     (0, "missing", 217, 100), (217, "missing", 217, 900)):
            with self.assertRaises(ValueError):
                codec.observe(*args)

    def test_crash_slot_is_reserved_but_zero_remains_encodable(self):
        codec = Codec()
        self.assertEqual(codec.base, 31)
        self.assertEqual(codec.capacity, 28629151)
        with self.assertRaisesRegex(ValueError, "Reserved crash"):
            codec.observe(1800 + 42, "exception", 42, 10)
        with self.assertRaises(ValueError):
            codec.observe(42, "exception", 42, 10)
        signal = codec.signal(0)
        self.assertEqual(signal["outcome"], "exception")
        self.assertEqual(signal["target_seconds"], "3600.0")
        self.assertEqual(codec.observe(3642, "exception", 42, 10), 0)

    def test_compute_and_writer_time_do_not_shift_the_deadline(self):
        codec = Codec(digits=1)
        for computed_at in (10, 500, 1700):
            waits = []
            current = [computed_at]
            with tempfile.TemporaryDirectory() as folder, working_directory(folder):
                def write_valid(index):
                    current[0] += 20
                    Path("submission.csv").write_text("valid")

                emit_signal(codec, 3 * codec.buckets - 1, False, write_valid, None,
                            started_at=0, clock=lambda: current[0], sleep=waits.append)
            self.assertEqual(waits, [1800 - computed_at - 20])

    def test_calculation_failure_and_timeout_use_reserved_slot(self):
        runtime = Path(__file__).with_name("probe_runtime.py").read_text()
        with tempfile.TemporaryDirectory() as folder:
            for computation in ('raise ValueError("bad quantity")',
                                'return 999999', 'time.sleep(10)'):
                adapter = f'''\ndef is_toy(): return False
def quantity(): {computation}
def write_valid_submission(index): raise AssertionError("unexpected writer")
def write_invalid_submission(): raise AssertionError("unexpected writer")
'''
                # 180ms computation allowance; watchdog must stop a 10s sleep.
                source = 'import time\n_started = time.monotonic()\n' + runtime
                source += f'\nrun_probe(Codec("0.0002", "0.00005", 1), 0, "integer", {adapter!r}, "submission.csv", _started)\n'
                notebook = Path(folder) / "probe.py"
                notebook.write_text(source)
                started = time.monotonic()
                result = subprocess.run([sys.executable, str(notebook)], cwd=folder,
                                        capture_output=True, text=True, timeout=3)
                elapsed = time.monotonic() - started
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("KAGGLE_PROBE_EXCEPTION", result.stderr)
                self.assertGreater(elapsed, 0.16)
                self.assertLess(elapsed, 2)

    def test_emitter_branches_without_sleeping(self):
        codec = Codec(digits=1, valid_outcomes=2)
        with tempfile.TemporaryDirectory() as folder, working_directory(folder):
            output = Path("submission.csv")
            for digit in range(codec.base):
                waits = []
                output.write_text("stale")
                valid = lambda index: output.write_text(f"valid:{index}")
                invalid = lambda: output.write_text("invalid")
                signal = codec.signal(digit)
                if signal["outcome"] == "exception":
                    with self.assertRaisesRegex(RuntimeError, "KAGGLE_PROBE_EXCEPTION"):
                        emit_signal(codec, digit, False, valid, invalid, started_at=0, clock=lambda: 0, sleep=waits.append)
                    self.assertFalse(output.exists())
                else:
                    emit_signal(codec, digit, False, valid, invalid, started_at=0, clock=lambda: 0, sleep=waits.append)
                    if signal["outcome"] == "missing":
                        self.assertFalse(output.exists())
                    else:
                        expected = "invalid" if signal["outcome"] == "format" else signal["outcome"]
                        self.assertEqual(output.read_text(), expected)
                self.assertEqual(waits, [float(signal["target_seconds"])])
            waits = []
            emit_signal(codec, None, True, valid, invalid, started_at=0, clock=lambda: 0, sleep=waits.append)
            self.assertEqual(output.read_text(), "valid:0")
            self.assertEqual(waits, [])

    def test_sqrt_scale_intervals_cover_every_count(self):
        scale = SqrtScale(15)
        self.assertEqual([scale.code(c) for c in (0, 1, 2, 4, 45, 46, 10**6)], [0, 2, 3, 4, 13, 14, 14])
        for count in range(200):
            lower, upper = scale.interval(scale.code(count))
            self.assertTrue(lower <= count <= upper)
        self.assertEqual(scale.interval(14), (46, float("inf")))
        with self.assertRaisesRegex(ValueError, "never emitted"):
            scale.interval(1)

    def test_slots_round_trip_and_reject_overflow(self):
        radices = [15, 4, 3]
        seen = {pack_slots([a, b, c], radices) for a in range(15) for b in range(4) for c in range(3)}
        self.assertEqual(seen, set(range(180)))
        self.assertEqual(unpack_slots(pack_slots([14, 2, 1], radices), radices), [14, 2, 1])
        with self.assertRaises(ValueError):
            pack_slots([15, 0, 0], radices)
        with self.assertRaises(ValueError):
            unpack_slots(180, radices)

    def test_dataset_dir_override_and_absence(self):
        with tempfile.TemporaryDirectory() as folder:
            os.environ["PROBE_DATASETS"] = json.dumps({"owner/data": folder})
            try:
                self.assertEqual(dataset_dir("owner/data/3"), Path(folder))
                self.assertIsNone(dataset_dir("owner/not-attached-anywhere-xyz"))
            finally:
                os.environ.pop("PROBE_DATASETS")

    def test_prepared_script_runs_standalone_and_decoder_orders_indices(self):
        script = Path(__file__).with_name("probe.py")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            adapter = root / "adapter.py"
            adapter.write_text('''
def is_toy():
    return True
def quantity():
    raise AssertionError("Toy run must not compute quantity")
def write_valid_submission(index):
    Path("submission.csv").write_text("valid toy")
def write_invalid_submission():
    raise AssertionError("Toy run must not write invalid submission")
''')
            run = root / "run"
            rejected = subprocess.run([sys.executable, str(script), "prepare", "--competition", "example",
                                       "--kernel", "owner/example-probe", "--adapter", str(adapter),
                                       "--output", str(run), "--runtime-limit-hours", "3"],
                                      capture_output=True, text=True)
            self.assertEqual(rejected.returncode, 2)
            self.assertFalse(run.exists())
            base = [sys.executable, str(script), "prepare", "--competition", "example",
                    "--kernel", "owner/example-probe", "--adapter", str(adapter), "--mode", "integer"]
            unmeasured = subprocess.run(base + ["--output", str(run), "--digits", "2"],
                                        capture_output=True, text=True)
            self.assertIn("--noise-runs", unmeasured.stderr)
            calib = root / "calib"
            subprocess.run(base + ["--output", str(calib), "--digits", "1", "--calibrate", "valid:0@0"],
                           check=True, capture_output=True, text=True)
            calib_manifest = json.loads((calib / "manifest.json").read_text())
            self.assertEqual(calib_manifest["calibrate"], "valid:0@0")
            (calib / "watch").mkdir()
            (calib / "watch" / "observations.json").write_text(json.dumps([{
                "index": 0, "description": calib_manifest["probes"][0]["description"], "outcome": "valid:0",
                "elapsed_lower_seconds": "2000", "elapsed_seconds": "2027"}]))
            gpu = subprocess.run(base + ["--output", str(run), "--digits", "2", "--gpu", "--noise-runs", str(calib)],
                                 capture_output=True, text=True)
            self.assertIn("No measured timing for GPU", gpu.stderr)
            unpinned = subprocess.run(base + ["--output", str(run), "--digits", "2", "--noise-runs", str(calib),
                                              "--dataset", "owner/data"], capture_output=True, text=True)
            self.assertIn("owner/slug/version", unpinned.stderr)
            subprocess.run(base + ["--output", str(run), "--digits", "2", "--noise-runs", str(calib),
                                   "--dataset", "owner/data/3"], check=True, capture_output=True, text=True)
            manifest = json.loads((run / "manifest.json").read_text())
            for entry in manifest["probes"]:
                metadata = json.loads((run / entry["folder"] / "kernel-metadata.json").read_text())
                self.assertEqual(metadata["title"], metadata["id"].split("/")[1])
                self.assertEqual(metadata["dataset_sources"], ["owner/data/3"])
            codec = Codec(**manifest["codec"])
            observations = []
            for entry, digit in zip(manifest["probes"], [2, 9]):
                notebook = run / entry["folder"] / "probe.py"
                # A separate Python process from the generated folder has no skill on sys.path.
                subprocess.run([sys.executable, str(notebook)], cwd=notebook.parent,
                               check=True, capture_output=True, text=True)
                self.assertEqual((notebook.parent / "submission.csv").read_text(), "valid toy")
                signal = codec.signal(digit)
                observations.append({"index": entry["index"], "description": entry["description"],
                                     "elapsed_seconds": str(Decimal(signal["target_seconds"]) + 217),
                                     "outcome": signal["outcome"]})
                # Run hidden entrypoint in isolation, injecting a no-wait emitter.
                source = notebook.read_text().replace('return True', 'return False').replace(
                    'raise AssertionError("Toy run must not compute quantity")', 'return 71')
                namespace = {"__name__": "probe_test"}
                exec(compile(source, str(notebook), "exec"), namespace)
                emitted = []
                namespace["emit_signal"] = lambda *args, **kwargs: emitted.append(args[1])
                namespace["__name__"] = "__main__"
                entrypoint = source[source.rfind('if __name__ == "__main__":'):]
                exec(entrypoint, namespace)
                self.assertEqual(emitted, [[2, 9][entry["index"]]])
            self.assertEqual(decode_observations(manifest, codec, observations[::-1], 217, 100)["integer"], 71)
            for bad in (observations[:1], observations + [observations[0]],
                        [{**observations[0], "description": "different-run"}, observations[1]],
                        [{**observations[0], "issues": ["uncalibrated"]}, observations[1]],
                        [{**observations[0], "elapsed_lower_seconds": "0"}, observations[1]]):
                with self.assertRaises(ValueError):
                    decode_observations(manifest, codec, bad, 217, 100)
            encoded = subprocess.run([sys.executable, str(script), "encode", "--manifest",
                                      str(run / "manifest.json"), "--value", "71"],
                                     check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(encoded.stdout)["digits"], [2, 9])
            observed = root / "observations.json"
            observed.write_text(json.dumps(observations))
            decoded = subprocess.run([sys.executable, str(script), "decode", "--manifest",
                                      str(run / "manifest.json"), "--observations", str(observed),
                                      "--overhead-seconds", "217", "--tolerance-seconds", "100"],
                                     check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(decoded.stdout)["integer"], 71)
            # Budget from the calibration: overhead 214 s, tolerance 200 s (floor); one digit still missing.
            observed.write_text(json.dumps(observations[:1]))
            partial = subprocess.run([sys.executable, str(script), "decode", "--manifest",
                                      str(run / "manifest.json"), "--observations", str(observed),
                                      "--noise-runs", str(calib), str(run), "--partial"],
                                     check=True, capture_output=True, text=True)
            partial = json.loads(partial.stdout)
            self.assertEqual((partial["overhead_seconds"], partial["tolerance_seconds"]), (214, 200))
            self.assertEqual(partial["digits"][0]["digit"], 2)
            self.assertEqual(partial["undecoded"], [1])


if __name__ == "__main__":
    unittest.main()
