"""Offline behavioral checks for the codec, emitter, and generated scripts."""

from contextlib import contextmanager
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from probe import decode_observations
from probe_runtime import Codec, emit_signal


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
                    elapsed = Decimal(signal["sleep_seconds"]) + Decimal("42.1")
                    self.assertEqual(codec.observe(elapsed, signal["outcome"], "42.1", 1), digit)

    def test_fraction_bounds_and_endpoint(self):
        codec = Codec()
        for value in ("0", "0.000000001", "0.3333333333333333333333", "0.5", "0.999999999", "1"):
            interval = codec.decode(codec.encode(value))
            self.assertLessEqual(Decimal(interval["lower"]), Decimal(value))
            if interval["upper_inclusive"]:
                self.assertLessEqual(Decimal(value), Decimal(interval["upper"]))
            else:
                self.assertLess(Decimal(value), Decimal(interval["upper"]))
        self.assertEqual(codec.encode(1), [31] * 5)

    def test_leading_zero_and_capacity_boundaries(self):
        codec = Codec(digits=3)
        self.assertEqual(codec.encode(32, "integer"), [0, 1, 0])
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
                     (217 + 900, "missing", 217, 100), (217 + 8 * 1800, "missing", 217, 100),
                     (0, "missing", 217, 100), (217, "missing", 217, 900)):
            with self.assertRaises(ValueError):
                codec.observe(*args)

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
                        emit_signal(codec, digit, False, valid, invalid, sleep=waits.append)
                    self.assertFalse(output.exists())
                else:
                    emit_signal(codec, digit, False, valid, invalid, sleep=waits.append)
                    if signal["outcome"] == "missing":
                        self.assertFalse(output.exists())
                    else:
                        expected = "invalid" if signal["outcome"] == "format" else signal["outcome"]
                        self.assertEqual(output.read_text(), expected)
                self.assertEqual(waits, [float(signal["sleep_seconds"])])
            waits = []
            emit_signal(codec, None, True, valid, invalid, sleep=waits.append)
            self.assertEqual(output.read_text(), "valid:0")
            self.assertEqual(waits, [])

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
            subprocess.run([sys.executable, str(script), "prepare", "--competition", "example",
                            "--kernel", "owner/example-probe", "--adapter", str(adapter),
                            "--output", str(run), "--digits", "2", "--mode", "integer"],
                           check=True, capture_output=True, text=True)
            manifest = json.loads((run / "manifest.json").read_text())
            for entry in manifest["probes"]:
                metadata = json.loads((run / entry["folder"] / "kernel-metadata.json").read_text())
                self.assertEqual(metadata["title"], metadata["id"].split("/")[1])
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
                                     "elapsed_seconds": str(Decimal(signal["sleep_seconds"]) + 217),
                                     "outcome": signal["outcome"]})
                # Run hidden entrypoint in isolation, injecting a no-wait emitter.
                source = notebook.read_text().replace('return True', 'return False').replace(
                    'raise AssertionError("Toy run must not compute quantity")', 'return 73')
                namespace = {"__name__": "probe_test"}
                exec(compile(source, str(notebook), "exec"), namespace)
                emitted = []
                namespace["emit_signal"] = lambda *args: emitted.append(args[1])
                namespace["__name__"] = "__main__"
                entrypoint = source[source.rfind('if __name__ == "__main__":'):]
                exec(entrypoint, namespace)
                self.assertEqual(emitted, [[2, 9][entry["index"]]])
            self.assertEqual(decode_observations(manifest, codec, observations[::-1], 217, 100)["integer"], 73)
            for bad in (observations[:1], observations + [observations[0]],
                        [{**observations[0], "description": "different-run"}, observations[1]]):
                with self.assertRaises(ValueError):
                    decode_observations(manifest, codec, bad, 217, 100)
            encoded = subprocess.run([sys.executable, str(script), "encode", "--manifest",
                                      str(run / "manifest.json"), "--value", "73"],
                                     check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(encoded.stdout)["digits"], [2, 9])
            observed = root / "observations.json"
            observed.write_text(json.dumps(observations))
            decoded = subprocess.run([sys.executable, str(script), "decode", "--manifest",
                                      str(run / "manifest.json"), "--observations", str(observed),
                                      "--overhead-seconds", "217", "--tolerance-seconds", "100"],
                                     check=True, capture_output=True, text=True)
            self.assertEqual(json.loads(decoded.stdout)["integer"], 73)


if __name__ == "__main__":
    unittest.main()
