Acorn 0.3.2 numeric fixture, checked 24 November 2026.
Expression '-0': internal=-0.0, printed='-0', negative_sign=True.
Expression '-2 * 0': internal=-0.0, printed='-0', negative_sign=True.
Expression '-0 * 3': internal=-0.0, printed='-0', negative_sign=True.
Expression '2 + 3 * 4': internal=14.0, printed='14', negative_sign=False.
Expression '1.0000000001': internal=1.0000000001, printed='1', negative_sign=False.
Call syntax f(1) is rejected without evaluation.
PASS: five numeric checks and one rejected call. R02 precision loss still reproduces. PR-119 is not implemented.
