"""Portable timing/outcome codec; also embedded verbatim in generated notebooks."""

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP, localcontext
from pathlib import Path
import time


def decimal(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("Values must be finite")
    return number


@dataclass(frozen=True)
class Codec:
    total_hours: str = "4"
    resolution_hours: str = "0.5"
    digits: int = 5
    valid_outcomes: int = 1

    def __post_init__(self):
        total, step = decimal(self.total_hours), decimal(self.resolution_hours)
        if total <= 0 or step <= 0:
            raise ValueError("Time window and resolution must be positive")
        ratio = total / step
        if ratio != int(ratio) or ratio < 1:
            raise ValueError("Time window must be an integer multiple of resolution")
        for value in (self.digits, self.valid_outcomes):
            if type(value) is not int or value < 1:
                raise ValueError("digits and valid_outcomes must be positive integers")

    @property
    def buckets(self):
        return int(decimal(self.total_hours) / decimal(self.resolution_hours))

    @property
    def step_seconds(self):
        return decimal(self.resolution_hours) * 3600

    @property
    def base(self):
        return self.buckets * (3 + self.valid_outcomes)

    @property
    def capacity(self):
        return self.base ** self.digits

    def encode(self, value, mode="fraction"):
        value = decimal(value)
        with localcontext() as context:
            context.prec = max(50, len(str(self.capacity)) + len(value.as_tuple().digits) + 10)
            if mode == "fraction":
                if not 0 <= value <= 1:
                    raise ValueError("Fraction must be in [0, 1]")
                number = min(self.capacity - 1, int((value * self.capacity).to_integral_value(rounding=ROUND_FLOOR)))
            elif mode == "integer":
                if value != int(value) or not 0 <= value < self.capacity:
                    raise ValueError(f"Integer must be in [0, {self.capacity - 1}]")
                number = int(value)
            else:
                raise ValueError("mode must be fraction or integer")
        result = [0] * self.digits
        for index in range(self.digits - 1, -1, -1):
            number, result[index] = divmod(number, self.base)
        return result

    def decode(self, digits, mode="fraction"):
        if len(digits) != self.digits:
            raise ValueError(f"Expected {self.digits} digits")
        number = 0
        for digit in digits:
            self.signal(digit)  # validate range and type
            number = number * self.base + digit
        if mode == "integer":
            return {"integer": number}
        if mode != "fraction":
            raise ValueError("mode must be fraction or integer")
        with localcontext() as context:
            context.prec = max(50, len(str(self.capacity)) + 10)
            return {
                "lower": str(Decimal(number) / self.capacity),
                "upper": str(Decimal(number + 1) / self.capacity),
                "upper_inclusive": number == self.capacity - 1,
                "resolution": str(Decimal(1) / self.capacity),
            }

    def signal(self, digit):
        if type(digit) is not int or not 0 <= digit < self.base:
            raise ValueError(f"Digit must be an integer in [0, {self.base - 1}]")
        outcome_index, bucket = divmod(digit, self.buckets)
        outcome = ("exception", "missing", "format")[outcome_index] if outcome_index < 3 else f"valid:{outcome_index - 3}"
        return {"digit": digit, "bucket": bucket, "sleep_seconds": str(bucket * self.step_seconds), "outcome": outcome}

    def observe(self, elapsed_seconds, outcome, overhead_seconds, tolerance_seconds):
        overhead, tolerance = decimal(overhead_seconds), decimal(tolerance_seconds)
        if overhead < 0 or not 0 <= tolerance < self.step_seconds / 2:
            raise ValueError("Overhead must be nonnegative; tolerance must be less than half a bucket")
        elapsed = decimal(elapsed_seconds)
        if elapsed < 0:
            raise ValueError("Elapsed time must be nonnegative")
        corrected = elapsed - overhead
        bucket = int((corrected / self.step_seconds).to_integral_value(rounding=ROUND_HALF_UP))
        if not 0 <= bucket < self.buckets:
            raise ValueError("Observed time is outside the configured buckets")
        if abs(corrected - bucket * self.step_seconds) > tolerance:
            raise ValueError("Observed time is too far from a calibrated bucket")
        known = {"exception": 0, "missing": 1, "format": 2}
        known.update({f"valid:{i}": i + 3 for i in range(self.valid_outcomes)})
        if outcome not in known:
            raise ValueError(f"Unknown outcome: {outcome!r}; do not infer a valid outcome from an unknown error")
        return known[outcome] * self.buckets + bucket


def emit_signal(codec, digit, is_toy, write_valid, write_invalid, submission_file="submission.csv", sleep=time.sleep):
    """Emit one digit. Inject sleep in local tests to avoid real delays."""
    output = Path(submission_file)
    output.unlink(missing_ok=True)
    if is_toy:
        write_valid(0)
        if not output.is_file():
            raise ValueError("Valid writer did not create the submission file")
        return
    signal = codec.signal(digit)
    sleep(float(signal["sleep_seconds"]))
    outcome = signal["outcome"]
    if outcome == "exception":
        raise RuntimeError("KAGGLE_PROBE_EXCEPTION")
    if outcome == "missing":
        return
    if outcome == "format":
        write_invalid()
    else:
        write_valid(int(outcome.split(":")[1]))
    if not output.is_file():
        raise ValueError("Submission writer did not create the expected file")
