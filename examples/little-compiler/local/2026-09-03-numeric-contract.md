Decision: D01
Component: Parser
Date: 2026-09-03

# D01: Why negative zero survives parsing

Mira Vale chose binary64 numeric literals with a separate unary minus node. Parsing -0 produces -0.0. The sign matters to a downstream direction indicator that distinguishes approaching a boundary from the left. Jo Fen wanted all zero spellings normalized for prettier output, but withdrew that request after the indicator demo. Parser, Constant folding, and Number printer must preserve signed zero.
