"""Content identity for loaded logic; display edits do not change its version."""
from __future__ import annotations

import ast
import hashlib
import json

DISPLAY_FIELDS = frozenset({"name", "description", "tags", "label"})


def digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                   allow_nan=False).encode("utf-8"),
    ).hexdigest()


class _Logic(ast.NodeTransformer):
    def generic_visit(self, node):
        node = super().generic_visit(node)
        if (isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr)):
            value = node.body[0].value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                node.body = node.body[1:]
        return node

    def visit_Assign(self, node):
        if (any(isinstance(target, ast.Name) and target.id == "META" for target in node.targets)
                and isinstance(node.value, ast.Dict)):
            pairs = [(key, value) for key, value in zip(node.value.keys, node.value.values, strict=True)
                     if not isinstance(key, ast.Constant) or key.value not in DISPLAY_FIELDS]
            node.value.keys = [key for key, _ in pairs]
            node.value.values = [value for _, value in pairs]
        return self.generic_visit(node)


def logic_version(code: str, dependencies: dict[str, str] | None = None) -> str:
    def normalized(source):
        return ast.dump(_Logic().visit(ast.parse(source)), include_attributes=False)

    return digest({
        "code": normalized(code),
        "dependencies": {name: normalized(source) for name, source in (dependencies or {}).items()},
    })


def runtime_identity(strategy, overrides: dict) -> str:
    """Small, I/O-free marker for matching live results to loaded configuration."""
    from app.strategy.engine import StrategyEngine

    return digest({
        "logic": getattr(strategy, "logic_version", ""),
        "params": StrategyEngine.resolve_params(strategy, overrides=overrides),
        "overrides": {key: value for key, value in overrides.items()
                      if key not in DISPLAY_FIELDS and key != "params"},
        "meta": {key: value for key, value in strategy.meta.items() if key not in DISPLAY_FIELDS},
    })
