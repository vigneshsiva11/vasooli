"""Mechanical proof that Stage 7 cannot write.

Two independent checks, because they fail in different ways:

1. **The HTTP surface.** Walk the live app's OpenAPI schema and assert that every
   Stage 7 path exposes GET and nothing else. Route enumeration goes through
   `app.openapi()` rather than `app.routes`, which is the only view that reflects
   what is actually served.
2. **The source.** Parse this package's own modules and assert none of them CALLS a
   Motor write method. A GET handler that called an update would pass the first check
   and fail this one.

Neither is a substitute for the other, and neither writes anything itself.

The source check parses to an AST rather than grepping, for a specific reason: the
modules in this package discuss the write methods they avoid, in prose. A textual
scan flags those docstrings and a scan tuned to stop flagging them stops being
evidence of anything. An AST sees calls, and a sentence is not a call.
"""

from __future__ import annotations

import ast
from pathlib import Path

#: Paths this stage owns. Anything in the schema starting with one of these must be
#: GET-only.
STAGE_7_PREFIXES: tuple[str, ...] = ("/metrics", "/audit-trail")

#: Every Motor/PyMongo method that mutates. `create_index` is included: an index is a
#: write, and a read-only stage has no business creating one even though it changes
#: no document.
WRITE_METHODS: frozenset[str] = frozenset(
    {
        "insert_one",
        "insert_many",
        "update_one",
        "update_many",
        "replace_one",
        "delete_one",
        "delete_many",
        "find_one_and_update",
        "find_one_and_replace",
        "find_one_and_delete",
        "bulk_write",
        "create_index",
        "create_indexes",
        "drop_index",
        "drop_indexes",
        "drop",
        "rename",
        "rename_collection",
        "aggregate",
    }
)

#: Aggregation stages that write. Checked as string literals, since they reach Mongo
#: as data rather than as a method name. `aggregate` is in `WRITE_METHODS` above for
#: the same reason — this stage uses no pipeline at all, so any pipeline is a finding
#: worth looking at even when its stages are read-only.
WRITE_STAGES: frozenset[str] = frozenset({"$out", "$merge"})


def check_http_surface(openapi_schema: dict) -> list[str]:
    """Return a list of violations: Stage 7 paths exposing a non-GET method.

    Args:
        openapi_schema: the output of `app.openapi()`.
    """
    violations: list[str] = []
    for path, operations in openapi_schema.get("paths", {}).items():
        if not path.startswith(STAGE_7_PREFIXES):
            continue
        for method in operations:
            if method.lower() != "get":
                violations.append(f"{method.upper()} {path}")
    return sorted(violations)


def stage_7_paths(openapi_schema: dict) -> list[str]:
    """Return every Stage 7 path in the schema, so the count can be asserted too.

    An empty list means the router is not mounted, which would make
    `check_http_surface` pass for the wrong reason.
    """
    return sorted(
        path
        for path in openapi_schema.get("paths", {})
        if path.startswith(STAGE_7_PREFIXES)
    )


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Return the `id()` of every string node that is a docstring.

    Docstrings are excluded from the string-literal scan so that a module explaining
    which pipeline stages it refuses to use is not reported as using them.
    """
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            found.add(id(body[0].value))
    return found


def _source_files(roots: tuple[Path, ...]) -> list[Path]:
    """Expand directories to their `.py` files, keeping explicit files as given."""
    files: list[Path] = []
    for root in roots:
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
        elif root.is_file():
            files.append(root)
    return files


def default_roots() -> tuple[Path, ...]:
    """Every file that makes up Stage 7: this package, its route module, its models.

    All three, not just the package: a write introduced in the route handler or in a
    model validator would be just as much a write, and scanning only the package would
    miss it.
    """
    here = Path(__file__).resolve().parent
    return (
        here,
        here.parent / "routes" / "metrics.py",
        here.parent / "models" / "metrics.py",
    )


def check_source(*roots: Path) -> list[str]:
    """Return a list of violations: a write call in this stage's own source.

    Reports the file, line, and offending name for each. `verify_readonly.py` is
    checked for write CALLS like every other module — it declares the method names as
    a frozenset and calls none of them. It is exempt only from the string-literal
    scan, because it is the file that declares `WRITE_STAGES` and would otherwise
    report its own declaration.
    """
    declaring_file = Path(__file__).resolve().name
    violations: list[str] = []
    for file in _source_files(roots or default_roots()):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        docstrings = _docstring_nodes(tree)
        scan_literals = file.name != declaring_file

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.attr
                    if isinstance(func, ast.Attribute)
                    else func.id
                    if isinstance(func, ast.Name)
                    else None
                )
                if name in WRITE_METHODS:
                    violations.append(
                        f"{file.name}:{node.lineno}: calls {name}()"
                    )
            elif (
                scan_literals
                and isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in WRITE_STAGES
                and id(node) not in docstrings
            ):
                violations.append(
                    f"{file.name}:{node.lineno}: names pipeline stage {node.value!r}"
                )
    return sorted(violations)


def read_methods_used(*roots: Path) -> list[str]:
    """Return the distinct Motor methods this stage does call, for the record.

    A read-only claim is stronger when it also says what the code DOES do: seeing
    `find` and `sort` and nothing else is the positive form of the same evidence.
    """
    motor_methods = {"find", "find_one", "sort", "to_list", "distinct", "count_documents"}
    used: set[str] = set()
    for file in _source_files(roots or default_roots()):
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in motor_methods:
                    used.add(node.func.attr)
    return sorted(used)
