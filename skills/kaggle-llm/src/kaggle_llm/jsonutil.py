"""JSON without JavaScript's nonstandard NaN/Infinity literals."""
import json
import math


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON value {value} is not supported")


def loads(text):
    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            return _reject_constant(value)
        return result
    return json.loads(text, parse_constant=_reject_constant, parse_float=finite_float)
