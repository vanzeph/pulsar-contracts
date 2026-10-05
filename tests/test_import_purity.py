"""Import-purity checks: this library must depend only on basic libraries.

Two angles:

1. static: AST-scan every module under ``src/pulsar_contracts`` and assert
   that every import root is on the allowlist (stdlib basics + pydantic +
   pandas + the package itself);
2. runtime: import the installed package and assert no forbidden data-source
   / broker SDK or sibling pulsar package leaked into ``sys.modules``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Iterator

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "pulsar_contracts"

ALLOWED_IMPORT_ROOTS = {
    # stdlib basics
    "__future__",
    "datetime",
    "enum",
    "importlib",
    "typing",
    "zoneinfo",
    # basic third-party libraries (policy: contracts depend on nothing else)
    "pydantic",
    "pandas",
    # self
    "pulsar_contracts",
}

FORBIDDEN_RUNTIME_MODULES = {
    # data-source SDKs
    "akshare",
    "baostock",
    "tushare",
    "rqdata",
    "jqdatasdk",
    # broker SDKs
    "xtquant",
    # network clients (a contracts library performs no I/O)
    "requests",
    "httpx",
    "aiohttp",
    "urllib3",
    # sibling pulsar packages (dependency direction is one-way: they depend on us)
    "pulsar_core",
    "pulsar_data",
    "pulsar_exec",
    "pulsar_app",
    "pulsar_ui",
}


def _import_roots(tree: ast.AST) -> Iterator[str]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:  # relative import inside the package
                continue
            if node.module:
                yield node.module.split(".")[0]


def test_static_imports_stay_within_allowlist() -> None:
    offenders: dict[str, list[str]] = {}
    modules = sorted(SRC_ROOT.rglob("*.py"))
    assert modules, f"no source modules found under {SRC_ROOT}"
    for path in modules:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        illegal = sorted(set(_import_roots(tree)) - ALLOWED_IMPORT_ROOTS)
        if illegal:
            offenders[path.relative_to(path.parents[2]).as_posix()] = illegal
    assert not offenders, f"illegal imports outside basic libraries: {offenders}"


def test_runtime_import_pulls_no_forbidden_modules() -> None:
    import pulsar_contracts  # noqa: F401 - the import itself is the act under test

    loaded = FORBIDDEN_RUNTIME_MODULES.intersection(sys.modules)
    assert not loaded, f"forbidden modules imported at runtime: {sorted(loaded)}"


def test_public_surface_resolves() -> None:
    import pulsar_contracts as pc

    for name in pc.__all__:
        assert getattr(pc, name, None) is not None, name
