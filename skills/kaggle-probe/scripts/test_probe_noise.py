import unittest

from probe_noise import timing_budget, MIN_TIMING_DIGITS
from probe_runtime import Codec, LogScale, pack_digits


class NoiseTests(unittest.TestCase):
    def test_budget_scales_with_measured_noise_and_keeps_floor_until_enough_digits(self):
        few = [{"gpu": False, "lower": -12.0, "upper": 52.0}, {"gpu": True, "lower": 0.0, "upper": 30.0}]
        cpu = timing_budget(few, False)
        self.assertEqual((cpu["overhead_seconds"], cpu["tolerance_seconds"], cpu["min_resolution_seconds"]),
                         (20, 200, 800))  # 3 x 64 s spread is under the floor
        quiet = [{"gpu": False, "lower": 0.0, "upper": 30.0}] * MIN_TIMING_DIGITS
        self.assertEqual(timing_budget(quiet, False)["min_resolution_seconds"], 360)
        noisy = few + [{"gpu": False, "lower": 100.0, "upper": 130.0}]
        self.assertEqual(timing_budget(noisy, False)["tolerance_seconds"], 426)
        self.assertEqual(timing_budget([], True), {"gpu": True, "digits": 0, "tolerance_seconds": 200,
                                                   "min_resolution_seconds": 800})

    def test_calibration_digit_round_trips_and_refuses_reserved_crash_slot(self):
        codec = Codec("8", "0.5", 1, 3)
        self.assertEqual(codec.calibration_digit("valid:0@0"), 47)
        for spec in ("missing@3", "valid:2@15", "exception@1"):
            signal = codec.signal(codec.calibration_digit(spec))
            self.assertEqual(f"{signal['outcome']}@{signal['bucket']}", spec)
        for spec in ("exception@0", "valid:3@0", "missing@16"):
            with self.assertRaises(ValueError):
                codec.calibration_digit(spec)

    def test_log_scale_and_pack_digits(self):
        scale = LogScale(30, 300, 63)
        self.assertEqual(scale.code(101), 33)
        for value in (31.0, 64.0, 101.0, 224.0, 299.0):
            lower, upper = scale.interval(scale.code(value))
            self.assertTrue(lower <= value < upper)
        self.assertEqual(scale.interval(0)[0], 0.0)
        self.assertEqual(pack_digits([1, 2], 10), 12)
        with self.assertRaises(ValueError):
            pack_digits([10], 10)


if __name__ == "__main__":
    unittest.main()
