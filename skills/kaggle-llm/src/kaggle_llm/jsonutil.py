"""JSON without JavaScript's nonstandard NaN/Infinity literals."""
import json


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON value {value} is not supported")


def loads(text):
    return json.loads(text, parse_constant=_reject_constant)
