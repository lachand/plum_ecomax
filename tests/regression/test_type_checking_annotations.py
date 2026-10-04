"""A name imported only under `if TYPE_CHECKING:` must not be used in an
unquoted annotation in a module without `from __future__ import annotations`.

Python 3.13 evaluates annotations when the def/class body runs, so such a
name raises NameError at import time; Python 3.14 (PEP 649) defers
evaluation and hides the bug, which is how it shipped once: the tests passed
locally on 3.14 and failed in CI on 3.13. This static check catches it on
whichever Python the tests run on.
"""

from __future__ import annotations

import ast
from pathlib import Path

COMPONENT_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "plum_ecomax"


def _type_checking_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.If) and (
            (isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING")
            or (isinstance(node.test, ast.Attribute) and node.test.attr == "TYPE_CHECKING")
        ):
            for stmt in ast.walk(node):
                if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                    names.update((a.asname or a.name).split(".")[0] for a in stmt.names)
    return names


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    )


def _annotations(tree: ast.Module):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs, a.vararg, a.kwarg]:
                if arg is not None and arg.annotation is not None:
                    yield arg.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def test_type_checking_only_names_are_not_used_in_unquoted_annotations():
    offenders = []
    for path in sorted(COMPONENT_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        guarded = _type_checking_names(tree)
        if not guarded or _has_future_annotations(tree):
            continue
        for annotation in _annotations(tree):
            for name in ast.walk(annotation):  # a quoted annotation is a str Constant: no Name
                if isinstance(name, ast.Name) and name.id in guarded:
                    offenders.append(f"{path.name}:{name.lineno} uses {name.id!r} unquoted")
    assert not offenders, "\n".join(offenders)
