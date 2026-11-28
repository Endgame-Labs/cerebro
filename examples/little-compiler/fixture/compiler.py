"""Numeric subset of the fictional Acorn 0.3.2 compiler."""
import ast


def fold(node):
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = fold(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mult)):
        left, right = fold(node.left), fold(node.right)
        return left + right if isinstance(node.op, ast.Add) else left * right
    raise ValueError("Use numeric literals, unary signs, addition, or multiplication.")


def compile_expression(text):
    value = fold(ast.parse(text, mode="eval").body)
    return value, format(value, ".8g")
