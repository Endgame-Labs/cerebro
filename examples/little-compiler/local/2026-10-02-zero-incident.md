Component: Constant folding
Date: 2026-10-02

# R01: West became east

The Constant folding shortcut shipped in 0.3.1 returned +0.0 for -2 * 0. The direction indicator displayed east instead of west in 6 of 80 replay cases. Eli Moss initially blamed Parser for normalizing -0. This was an incident hypothesis, not a verified cause.
