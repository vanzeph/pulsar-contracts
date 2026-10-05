"""Unit tests for the Pulsar cross-repo topology gate.

The tests synthesise miniature repositories (pyproject + one source file) in
``tmp_path`` and assert both the legal topology and a representative set of
illegal mutations, mirroring the Repository Boundaries / Module Boundaries
tables of the Pulsar architecture baseline.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

TOOL_PATH = Path(__file__).resolve().parents[1] / "check_topology.py"


def load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_topology", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # required for dataclass field resolution
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def tool() -> ModuleType:
    return load_tool()


def req(repo: str, anchor: str) -> str:
    """PEP 508 direct reference: the only legal cross-repo dependency form."""
    return f"{repo} @ git+https://github.com/vanzeph/{repo}.git@{anchor}"


FULL_SHA_C = "f" * 40
FULL_SHA_0 = "0" * 40


def make_repo(
    root: Path,
    repo: str,
    *,
    dependencies: list[str] | None = None,
    extra_pyproject: str = "",
    src_imports: list[str] | None = None,
    src_body: str = "",
    omit_pyproject: bool = False,
    packages: list[str] | None = None,
) -> Path:
    """Materialise a miniature repository named ``repo`` under ``root``."""
    repo_dir = root / repo
    import_name = repo.replace("-", "_")
    pkg_dir = repo_dir / "src" / import_name
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(
        "".join(f"import {mod}\n" for mod in (src_imports or [])) + src_body,
        encoding="utf-8",
    )
    if not omit_pyproject:
        dep_lines = "\n".join(f'    "{d}",' for d in (dependencies or []))
        pkg_lines = "\n".join(
            f'    "{p}",'
            for p in (packages if packages is not None else [f"src/{import_name}"])
        )
        (repo_dir / "pyproject.toml").write_text(
            f"""
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{repo}"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
{dep_lines}
]

[tool.hatch.build.targets.wheel]
packages = [
{pkg_lines}
]
{extra_pyproject}
""",
            encoding="utf-8",
        )
    return repo_dir


def make_workspace(
    root: Path,
    *,
    contracts_dep: bool = True,
    app_deps: list[str] | None = None,
    ui_imports: list[str] | None = None,
    core_imports: list[str] | None = None,
    core_dependencies: list[str] | None = None,
) -> Path:
    """Build the six-repository workspace; every knob intentionally defaults
    to the *legal* topology so tests mutate one aspect at a time."""
    anchored = req("pulsar-contracts", FULL_SHA_C)
    make_repo(
        root,
        "pulsar-contracts",
        dependencies=["pydantic>=2"],
    )
    core_dep = [anchored] if contracts_dep else []
    make_repo(
        root,
        "pulsar-core",
        dependencies=core_dependencies if core_dependencies is not None else core_dep,
        src_imports=core_imports if core_imports is not None else (["pulsar_contracts"] if contracts_dep else []),
    )
    make_repo(root, "pulsar-data", dependencies=list(core_dep))
    make_repo(root, "pulsar-exec", dependencies=list(core_dep))
    if app_deps is None:
        app_deps = [
            anchored,
            req("pulsar-core", FULL_SHA_0),
            req("pulsar-data", FULL_SHA_0),
            req("pulsar-exec", FULL_SHA_0),
        ]
    make_repo(root, "pulsar-app", dependencies=app_deps)
    make_repo(root, "pulsar-ui", src_imports=ui_imports)
    return root


# ---------------------------------------------------------------------------
# legal topologies
# ---------------------------------------------------------------------------


def test_legal_workspace_passes(tool: ModuleType, tmp_path: Path) -> None:
    root = make_workspace(tmp_path)
    reports = tool.check_workspace(root)
    assert all(r.ok for r in reports), [r.violations for r in reports]
    assert len(reports) == 6


def test_app_subset_of_allowed_deps_is_legal(tool: ModuleType, tmp_path: Path) -> None:
    # app depending on contracts alone (early bootstrap state) stays legal:
    # the topology constrains the edge set, it does not force completeness.
    root = make_workspace(tmp_path, app_deps=[req("pulsar-contracts", FULL_SHA_C)])
    reports = tool.check_workspace(root)
    app = next(r for r in reports if r.repo == "pulsar-app")
    assert app.ok, app.violations


def test_workspace_mode_requires_all_six(tool: ModuleType, tmp_path: Path) -> None:
    make_workspace(tmp_path)
    (tmp_path / "pulsar-ui").rename(tmp_path / "somewhere-else")
    with pytest.raises(SystemExit, match="missing repositories"):
        tool.check_workspace(tmp_path)


# ---------------------------------------------------------------------------
# edge rules
# ---------------------------------------------------------------------------


def test_core_cannot_depend_on_data(tool: ModuleType, tmp_path: Path) -> None:
    root = make_workspace(
        tmp_path,
        core_dependencies=[
            req("pulsar-contracts", FULL_SHA_C),
            req("pulsar-data", FULL_SHA_0),
        ],
    )
    report = tool.check_repo(root / "pulsar-core")
    assert not report.ok
    assert any("forbidden cross-repo dependency" in v for v in report.violations)


def test_data_cannot_depend_on_core(tool: ModuleType, tmp_path: Path) -> None:
    root = make_workspace(tmp_path)
    # overwrite pulsar-data's pyproject with a dependency on pulsar-core
    make_repo(
        root,
        "pulsar-data",
        dependencies=[req("pulsar-core", FULL_SHA_0)],
    )
    report = tool.check_repo(root / "pulsar-data")
    assert not report.ok
    assert any("forbidden cross-repo dependency: pulsar-data -> pulsar-core" in v for v in report.violations)


def test_ui_cannot_import_contracts(tool: ModuleType, tmp_path: Path) -> None:
    root = make_workspace(tmp_path, ui_imports=["pulsar_contracts"])
    report = tool.check_repo(root / "pulsar-ui")
    assert not report.ok
    assert any("forbidden cross-repo import: pulsar_contracts" in v for v in report.violations)


def test_core_cannot_import_exec_even_if_allowed_dep_declared(
    tool: ModuleType, tmp_path: Path
) -> None:
    root = make_workspace(tmp_path, core_imports=["pulsar_contracts", "pulsar_exec"])
    report = tool.check_repo(root / "pulsar-core")
    assert not report.ok
    assert any("forbidden cross-repo import: pulsar_exec" in v for v in report.violations)


# ---------------------------------------------------------------------------
# import purity
# ---------------------------------------------------------------------------


def test_undeclared_pulsar_import_is_rejected(tool: ModuleType, tmp_path: Path) -> None:
    # imports contracts but never declares it
    root = make_workspace(tmp_path, core_dependencies=[])
    make_repo(root, "pulsar-core", dependencies=[], src_imports=["pulsar_contracts"])
    report = tool.check_repo(root / "pulsar-core")
    assert not report.ok
    assert any("import purity" in v for v in report.violations)


# ---------------------------------------------------------------------------
# versioned git references
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "f" * 40,          # full commit hash
        "f385fbd",         # short commit hash (>= 7 hex)
        "v0.1.0",          # released tag
        "v1.2.3rc1",       # pre-release tag
    ],
)
def test_anchored_git_refs_are_accepted(tool: ModuleType, tmp_path: Path, ref: str) -> None:
    repo = make_repo(
        tmp_path, "pulsar-core", dependencies=[req("pulsar-contracts", ref)]
    )
    report = tool.check_repo(repo)
    assert not any("git reference" in v or "anchor" in v for v in report.violations), report.violations


@pytest.mark.parametrize(
    "req",
    [
        "git+https://github.com/vanzeph/pulsar-contracts.git",        # bare URL
        req("pulsar-contracts", "main").replace("main", "main"),     # branch
        req("pulsar-contracts", "HEAD"),                             # symbolic
        "pulsar-contracts @ git+https://github.com/someone/pulsar-contracts.git@" + FULL_SHA_C,  # foreign org
        "pulsar-contracts>=0.1",                                     # registry form
    ],
)
def test_non_reproducible_refs_are_rejected(tool: ModuleType, tmp_path: Path, req: str) -> None:
    repo = make_repo(tmp_path, "pulsar-core", dependencies=[req])
    report = tool.check_repo(repo)
    assert not report.ok, req


# ---------------------------------------------------------------------------
# banned third-party SDKs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sdk", ["akshare", "baostock", "tushare", "xtquant"])
def test_core_cannot_import_sdk(tool: ModuleType, tmp_path: Path, sdk: str) -> None:
    repo = make_repo(
        tmp_path,
        "pulsar-core",
        dependencies=[req("pulsar-contracts", FULL_SHA_C)],
        src_imports=["pulsar_contracts", sdk],
    )
    report = tool.check_repo(repo)
    assert not report.ok
    assert any(f"banned import '{sdk}'" in v for v in report.violations)


def test_contracts_cannot_import_network_client(tool: ModuleType, tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "pulsar-contracts", src_imports=["requests"])
    report = tool.check_repo(repo)
    assert not report.ok


def test_data_may_import_akshare_and_requests(tool: ModuleType, tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        "pulsar-data",
        dependencies=[req("pulsar-contracts", FULL_SHA_C)],
        src_imports=["pulsar_contracts", "akshare", "requests"],
    )
    report = tool.check_repo(repo)
    assert report.ok, report.violations


def test_data_cannot_import_broker_sdk(tool: ModuleType, tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        "pulsar-data",
        dependencies=[req("pulsar-contracts", FULL_SHA_C)],
        src_imports=["xtquant"],
    )
    report = tool.check_repo(repo)
    assert not report.ok


def test_exec_may_import_broker_sdk_but_not_data_sdk(tool: ModuleType, tmp_path: Path) -> None:
    anchored = req("pulsar-contracts", FULL_SHA_C)
    ok_repo = make_repo(
        tmp_path, "pulsar-exec", dependencies=[anchored], src_imports=["xtquant"]
    )
    assert tool.check_repo(ok_repo).ok
    bad_repo = make_repo(
        tmp_path / "bad", "pulsar-exec", dependencies=[anchored], src_imports=["akshare"]
    )
    assert not tool.check_repo(bad_repo).ok


# ---------------------------------------------------------------------------
# repo structure
# ---------------------------------------------------------------------------


def test_multiple_wheel_packages_rejected(tool: ModuleType, tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        "pulsar-core",
        dependencies=[req("pulsar-contracts", FULL_SHA_C)],
        packages=["src/pulsar_core", "src/pulsar_extra"],
    )
    report = tool.check_repo(repo)
    assert not report.ok
    assert any("exactly one package" in v for v in report.violations)


def test_missing_src_layout_rejected(tool: ModuleType, tmp_path: Path) -> None:
    repo = make_repo(
        tmp_path,
        "pulsar-core",
        dependencies=[req("pulsar-contracts", FULL_SHA_C)],
        packages=["pulsar_core"],
    )
    report = tool.check_repo(repo)
    assert not report.ok


def test_unknown_distribution_rejected(tool: ModuleType, tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "pulsar-quant")
    with pytest.raises(SystemExit, match="unknown distribution"):
        tool.check_repo(repo)


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


def test_cli_repo_mode_exit_codes(tool: ModuleType, tmp_path: Path) -> None:
    good = make_repo(
        tmp_path / "good",
        "pulsar-core",
        dependencies=[req("pulsar-contracts", FULL_SHA_C)],
        src_imports=["pulsar_contracts"],
    )
    proc = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--repo", str(good)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "topology gate" in proc.stdout

    bad = make_workspace(tmp_path / "bad", ui_imports=["pulsar_contracts"])
    proc = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--workspace", str(bad)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "forbidden cross-repo import: pulsar_contracts" in proc.stdout
