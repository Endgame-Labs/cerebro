#!/usr/bin/env -S uv run python
"""Print deterministic observations from the bundled compiler."""
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixture"))
from compiler import compile_expression

print("Acorn 0.3.2 numeric fixture, checked 24 November 2026.")
for expression, expected, negative, printed in [
    ("-0", -0.0, True, "-0"), ("-2 * 0", -0.0, True, "-0"),
    ("-0 * 3", -0.0, True, "-0"), ("2 + 3 * 4", 14.0, False, "14"),
    ("1.0000000001", 1.0000000001, False, "1"),
]:
    value, output = compile_expression(expression)
    sign = math.copysign(1.0, value) < 0
    assert value == expected and sign == negative, expression
    assert output == printed, expression
    print(f"Expression {expression!r}: internal={value!r}, printed={output!r}, negative_sign={sign}.")
try:
    compile_expression("f(1)")
except ValueError:
    print("Call syntax f(1) is rejected without evaluation.")
else:
    raise AssertionError("Calls must be rejected")
print("PASS: five numeric checks and one rejected call. R02 precision loss still reproduces. PR-119 is not implemented.")
