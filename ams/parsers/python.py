"""Phase 1 parser for Python / FastAPI, built on the stdlib `ast` module.

Extracts Pydantic models, dataclasses and Enums plus FastAPI routes into the
normalized schema. Purely deterministic: the code is parsed, never imported.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from ams.schema import Endpoint, Entity, Field, Param, ServiceSchema

SCALARS = {
    "str": "string", "int": "integer", "float": "decimal", "Decimal": "decimal", "bool": "boolean",
    "datetime": "datetime", "date": "date", "UUID": "uuid", "dict": "object", "Any": "object",
}
COLLECTIONS = {"list", "List", "set", "Set", "tuple", "Sequence"}
MODEL_BASES = {"BaseModel", "SQLModel"}
ENUM_BASES = {"Enum", "StrEnum", "IntEnum"}
VERBS = {"get", "post", "put", "patch", "delete"}


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def canonical(ann: ast.AST | None, known: set[str]) -> tuple[str, bool]:
    """(canonical type, optional) for a type annotation."""
    if ann is None:
        return "object", False
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):  # forward reference
        return canonical(ast.parse(ann.value, mode="eval").body, known)
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):  # X | None
        parts = [p for p in (ann.left, ann.right) if not (isinstance(p, ast.Constant) and p.value is None)]
        inner, _ = canonical(parts[0], known) if parts else ("object", False)
        return inner, len(parts) == 1
    if isinstance(ann, ast.Subscript):
        outer = _name(ann.value)
        if outer == "Optional":
            return canonical(ann.slice, known)[0], True
        if outer in COLLECTIONS:
            return f"list<{canonical(ann.slice, known)[0]}>", False
        if outer == "Annotated":
            return canonical(ann.slice.elts[0], known)  # type: ignore[attr-defined]
        return "object", False
    simple = _name(ann)
    if simple in SCALARS:
        return SCALARS[simple], False
    return (f"ref:{simple}", False) if simple in known else ("object", False)


def _alias(value: ast.AST | None) -> str | None:
    """alias= from `Field(alias="x")`."""
    if isinstance(value, ast.Call) and _name(value.func) == "Field":
        for kw in value.keywords:
            if kw.arg in ("alias", "serialization_alias") and isinstance(kw.value, ast.Constant):
                return str(kw.value.value)
    return None


def _entity(cls: ast.ClassDef, rel: str, known: set[str]) -> Entity | None:
    bases = {_name(b) for b in cls.bases}
    if bases & ENUM_BASES:
        values = [t.id for s in cls.body if isinstance(s, ast.Assign) for t in s.targets if isinstance(t, ast.Name)]
        return Entity(cls.name, [], rel, cls.lineno, kind="enum", values=values)
    is_dataclass = any(_name(d.func if isinstance(d, ast.Call) else d) == "dataclass" for d in cls.decorator_list)
    if not (bases & MODEL_BASES or bases & known or is_dataclass):
        return None
    fields = []
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id.startswith("_") or _name(stmt.annotation) == "ClassVar":
                continue
            ctype, optional = canonical(stmt.annotation, known)
            fields.append(Field(stmt.target.id, _alias(stmt.value) or stmt.target.id, ctype,
                                ast.unparse(stmt.annotation), optional or stmt.value is not None))
    return Entity(cls.name, fields, rel, cls.lineno)


def _endpoints(fn: ast.FunctionDef | ast.AsyncFunctionDef, rel: str, known: set[str]) -> list[Endpoint]:
    out = []
    for dec in fn.decorator_list:
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in VERBS):
            continue
        path = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else "/"
        model = next((kw.value for kw in dec.keywords if kw.arg == "response_model"), fn.returns)
        ret = canonical(model, known)[0] if model is not None else None
        path_vars = set(re.findall(r"{(\w+)}", path))
        params = []
        for arg in fn.args.args:
            ctype = canonical(arg.annotation, known)[0]
            loc = "path" if arg.arg in path_vars else "body" if ctype.startswith("ref:") else "query"
            params.append(Param(arg.arg, loc, ctype))
        returns = ret.removeprefix("ref:").replace("list<ref:", "list<") if ret else None
        out.append(Endpoint(dec.func.attr.upper(), path, fn.name, returns, params, rel, fn.lineno))
    return out


def _port(trees: list[tuple[str, ast.Module]]) -> int:
    for _, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _name(node.func) == "getenv" and len(node.args) == 2:
                key, default = node.args
                if isinstance(key, ast.Constant) and key.value == "PORT" and isinstance(default, ast.Constant):
                    return int(default.value)
    return 8000


def parse(root: Path, name: str | None = None) -> ServiceSchema:
    skip = {".venv", "venv", "tests", "__pycache__", "ams_generated"}
    files = [p for p in sorted(root.rglob("*.py")) if not skip & set(p.relative_to(root).parts)]
    trees = [(p.relative_to(root).as_posix(), ast.parse(p.read_text(encoding="utf8"))) for p in files]

    # two passes so a model can reference another one defined later
    known = {n.name for _, t in trees for n in ast.walk(t) if isinstance(n, ast.ClassDef)}
    entities, endpoints = [], []
    for rel, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                ent = _entity(node, rel, known)
                if ent:
                    entities.append(ent)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                endpoints += _endpoints(node, rel, known)
    return ServiceSchema(name or root.name, "python", "fastapi", root.as_posix(), _port(trees), entities, endpoints)
