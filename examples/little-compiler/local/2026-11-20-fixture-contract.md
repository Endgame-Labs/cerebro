Component: Parser
Date: 2026-11-20

# Fixture scope

Mira Vale approved fixture/compiler.py as a reproducible subset of Acorn 0.3.2 for onboarding. It supports numeric literals, parentheses, unary signs, addition, and multiplication. It rejects names, calls, and other syntax. The fixture compares signed zeros by sign bit because ordinary equality hides the R01 failure. Run uv run python scripts/reproduce.py from the workspace.
