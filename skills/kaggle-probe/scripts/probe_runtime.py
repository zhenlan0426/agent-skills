"""Portable timing/outcome codec; also embedded verbatim in generated notebooks."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP, localcontext
import math
from pathlib import Path
import signal as signals
import time


def decimal(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Expected a finite number, got {value!r}") from exc
    if not number.is_finite():
        raise ValueError("Values must be finite")
    return number


@dataclass(frozen=True)
class Codec:
    total_hours: str = "4"
    resolution_hours: str = "0.5"
    digits: int = 5
    valid_outcomes: int = 1
    # Computation allowance before the first deadline; None keeps it equal to one resolution step.
    allowance_hours: str = None

    def __post_init__(self):
        total, step = decimal(self.total_hours), decimal(self.resolution_hours)
        if total <= 0 or step <= 0:
            raise ValueError("Time window and resolution must be positive")
        allowance = self.allowance_seconds / 3600
        if allowance <= 0 or allowance > total:
            raise ValueError("Computation allowance must be positive and within the time window")
        ratio = (total - allowance) / step
        if ratio != int(ratio):
            raise ValueError("Time window minus allowance must be an integer multiple of resolution")
        for value in (self.digits, self.valid_outcomes):
            if type(value) is not int or value < 1:
                raise ValueError("digits and valid_outcomes must be positive integers")

    @property
    def buckets(self):
        return int((decimal(self.total_hours) * 3600 - self.allowance_seconds) / self.step_seconds) + 1

    @property
    def allowance_seconds(self):
        hours = self.resolution_hours if self.allowance_hours is None else self.allowance_hours
        return decimal(hours) * 3600

    def target_seconds(self, bucket):
        return self.allowance_seconds + bucket * self.step_seconds

    @property
    def step_seconds(self):
        return decimal(self.resolution_hours) * 3600

    @property
    def base(self):
        # Exception in bucket zero is reserved for a failed computation.
        return self.buckets * (3 + self.valid_outcomes) - 1

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
        outcome_index, bucket = divmod(digit + 1, self.buckets)
        outcome = ("exception", "missing", "format")[outcome_index] if outcome_index < 3 else f"valid:{outcome_index - 3}"
        return {"digit": digit, "bucket": bucket,
                "target_seconds": str(self.target_seconds(bucket)), "outcome": outcome}

    def calibration_digit(self, spec):
        """Digit that emits `outcome@bucket`, e.g. `valid:0@0`; the reserved crash slot is refused."""
        outcome, bucket = spec.rsplit("@", 1)
        channels = ("exception", "missing", "format")
        channel = channels.index(outcome) if outcome in channels else 3 + int(outcome.split(":")[1])
        digit = channel * self.buckets + int(bucket) - 1
        if not 0 <= digit < self.base or (self.signal(digit)["outcome"], self.signal(digit)["bucket"]) != (
                outcome, int(bucket)):
            raise ValueError(f"{spec} is not an emittable signal (exception@0 is reserved for crashes)")
        return digit


    def observe(self, elapsed_seconds, outcome, overhead_seconds, tolerance_seconds):
        overhead, tolerance = decimal(overhead_seconds), decimal(tolerance_seconds)
        if overhead < 0 or not 0 <= tolerance < self.step_seconds / 2:
            raise ValueError("Overhead must be nonnegative; tolerance must be less than half a bucket")
        elapsed = decimal(elapsed_seconds)
        if elapsed < 0:
            raise ValueError("Elapsed time must be nonnegative")
        corrected = elapsed - overhead - self.allowance_seconds
        bucket = int((corrected / self.step_seconds).to_integral_value(rounding=ROUND_HALF_UP))
        if not 0 <= bucket < self.buckets:
            raise ValueError("Observed time is outside the configured buckets")
        if abs(corrected - bucket * self.step_seconds) > tolerance:
            raise ValueError("Observed time is too far from a calibrated bucket")
        known = {"exception": 0, "missing": 1, "format": 2}
        known.update({f"valid:{i}": i + 3 for i in range(self.valid_outcomes)})
        if outcome not in known:
            raise ValueError(f"Unknown outcome: {outcome!r}; do not infer a valid outcome from an unknown error")
        symbol = known[outcome] * self.buckets + bucket
        if symbol == 0:
            raise ValueError("Reserved crash signal: the hidden computation failed; this is not digit zero")
        return symbol - 1


class LogScale:
    """Map a positive statistic to an integer code in [0, levels - 1] on a log grid from lo to hi."""

    def __init__(self, lo, hi, levels):
        self.lo, self.hi, self.levels = float(lo), float(hi), int(levels)
        self.step = math.log(self.hi / self.lo) / (self.levels - 1)

    def code(self, value):
        return min(max(round(math.log(float(value) / self.lo) / self.step), 0), self.levels - 1)

    def interval(self, code):
        """Values that encode to `code`; the end codes also absorb everything clipped beyond lo/hi."""
        lower = 0.0 if code == 0 else self.lo * math.exp((code - 0.5) * self.step)
        upper = math.inf if code == self.levels - 1 else self.lo * math.exp((code + 0.5) * self.step)
        return lower, upper


class SqrtScale:
    """Map a count to code round(2 sqrt(count)), capped at levels - 1: each step is about one Poisson SD wide.

    Code 1 is never emitted (count 0 -> 0, count 1 -> 2).
    """

    def __init__(self, levels):
        self.levels = int(levels)

    def code(self, count):
        return min(math.floor(2 * math.sqrt(count) + 0.5), self.levels - 1)

    def interval(self, code):
        """Inclusive count range that encodes to `code`; the top code is open-ended (upper = inf)."""
        if code == 0:
            return 0, 0
        lower = math.ceil((code - 0.5) ** 2 / 4)
        upper = math.inf if code == self.levels - 1 else math.ceil((code + 0.5) ** 2 / 4) - 1
        if upper < lower:
            raise ValueError(f"Code {code} is never emitted")
        return lower, upper


def pack_slots(codes, radices):
    """Mixed-radix packing of several small codes into one digit (first slot most significant)."""
    if len(codes) != len(radices):
        raise ValueError("Need one radix per code")
    value = 0
    for code, radix in zip(codes, radices):
        if not 0 <= code < radix:
            raise ValueError(f"Code {code} outside [0, {radix - 1}]")
        value = value * radix + code
    return value


def unpack_slots(value, radices):
    """Inverse of pack_slots."""
    codes = []
    for radix in reversed(radices):
        value, code = divmod(value, radix)
        codes.append(code)
    if value:
        raise ValueError("Value exceeds the product of the radices")
    return codes[::-1]


def pack_digits(codes, base):
    """Combine per-probe codes (most significant first) into one integer for `--mode integer`."""
    return pack_slots(codes, [base] * len(codes))


def dataset_dir(ref):
    """Mount folder of a dataset attached with `prepare --dataset owner/slug/version`, or None if absent.

    `PROBE_DATASETS` (JSON {"owner/slug": dir}) overrides it for local simulation.
    """
    import json
    import os
    owner, slug = ref.split("/")[:2]
    local = json.loads(os.environ.get("PROBE_DATASETS", "{}"))
    if f"{owner}/{slug}" in local:
        return Path(local[f"{owner}/{slug}"])
    root = Path("/kaggle/input")
    for path in (root / "datasets" / owner / slug, root / slug):
        if path.is_dir():
            return path
    return next((path for path in sorted(root.rglob(slug)) if path.is_dir()), None)



def emit_signal(codec, digit, is_toy, write_valid, write_invalid, submission_file="submission.csv",
                *, started_at, sleep=time.sleep, clock=time.monotonic, ready=None):
    """Prepare output before waiting for an absolute deadline from script startup."""
    output = Path(submission_file)
    output.unlink(missing_ok=True)
    if is_toy:
        write_valid(0)
        if not output.is_file():
            raise ValueError("Valid writer did not create the submission file")
        return
    signal = codec.signal(digit)
    outcome = signal["outcome"]
    if outcome == "format":
        write_invalid()
    elif outcome.startswith("valid:"):
        write_valid(int(outcome.split(":")[1]))
    if outcome in ("format",) or outcome.startswith("valid:"):
        if not output.is_file():
            raise ValueError("Submission writer did not create the expected file")
    if clock() - started_at >= float(codec.allowance_seconds):
        raise TimeoutError("Probe preparation exceeded the computation allowance")
    if ready is not None:
        ready()  # Disarm the computation watchdog only after output is ready.
    wait_until(started_at + float(signal["target_seconds"]), sleep, clock)
    if outcome == "exception":
        raise RuntimeError("KAGGLE_PROBE_EXCEPTION")


def wait_until(deadline, sleep=time.sleep, clock=time.monotonic):
    remaining = deadline - clock()
    if remaining > 0:
        sleep(remaining)


def run_probe(codec, index, mode, adapter_source, submission_file, started_at):
    """Kaggle/Linux entrypoint; bound computation failures to the reserved slot.

    Adapter imports, quantity calculation, encoding and output preparation all
    run under a real-time watchdog. No adapter code runs after the timed wait.
    """
    def timed_out(signum, frame):
        raise TimeoutError("Probe preparation exceeded the computation allowance")

    def disarm():
        signals.setitimer(signals.ITIMER_REAL, 0)

    previous = signals.signal(signals.SIGALRM, timed_out)
    armed = True
    toy = False

    def ready():
        nonlocal armed
        disarm()
        armed = False

    try:
        remaining = started_at + float(codec.allowance_seconds) - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Runtime imports exceeded the computation allowance")
        signals.setitimer(signals.ITIMER_REAL, remaining)
        namespace = globals()
        namespace["PROBE_CODEC"] = codec  # lets adapters size codes, e.g. LogScale(lo, hi, PROBE_CODEC.base)
        exec(compile(adapter_source, "adapter.py", "exec"), namespace)
        toy = bool(namespace["is_toy"]())
        digit = 0 if toy else codec.encode(namespace["quantity"](), mode)[index]
        emit_signal(codec, digit, toy, namespace["write_valid_submission"],
                    namespace["write_invalid_submission"], submission_file,
                    started_at=started_at, ready=ready)
    except BaseException:
        if armed:
            disarm()
            if not toy:
                wait_until(started_at + float(codec.allowance_seconds))
        raise
    finally:
        disarm()
        signals.signal(signals.SIGALRM, previous)
