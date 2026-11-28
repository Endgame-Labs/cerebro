Component: Constant folding
Date: 2026-10-04

# R01: The parser was innocent

Mira Vale traced -2 * 0 through Parser and found the negative operand intact. Constant folding alone replaced the multiplication with positive zero. This corrects the 2 October parser hypothesis. The observed wrong result was +0.0, not -0.0. D02 already forbade that rewrite.
