Decision: D02
Component: Constant folding
Date: 2026-09-09

# D02: Fold operations, not algebra

Eli Moss decided constant folding must evaluate the original binary64 operation. Replacing x * 0 with positive zero is unsafe for negative operands. The optimizer may fold literal-only expressions, but may not apply identities that erase signed zero. Parser supplies the unary minus node.
