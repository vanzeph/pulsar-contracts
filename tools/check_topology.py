#!/usr/bin/env python3
"""Cross-repo dependency topology gate for the Pulsar repository family.

Pulsar is a six-repository layout (github.com/vanzeph):

    pulsar-contracts   port protocols + immutable domain objects (root)
    pulsar-core        engine: depends on contracts only
    pulsar-data        MarketDataPort adapters: depends on contracts only
    pulsar-exec        ExecutionPort adapters: depends on contracts only
    pulsar-app         runtime assembly: may depend on all four above
    pulsar-ui          read-only dashboard: depends on NO pulsar package

This tool statically asserts, per repository:

  1. Edge rules  -- every declared pulsar-* dependency and every ``pulsar_*``
     import inside the packaged source tree stays inside the allowed edge set
     taken from the Pulsar architecture baseline:
         core / data / exec -> contracts
         app                -> contracts, core, data, exec
         ui                 -> (none)
         contracts          -> (none)
  2. Import purity -- the package only imports pulsar modules it also
     declares in ``[project].dependencies`` (no undeclared cross-repo use).
  3. Versioned references -- every pulsar-* dependency is a direct git
     reference ``git+https://github.com/vanzeph/<repo>.git@<anchor>`` where
     ``<anchor>`` is a commit hash (7-40 hex) or a ``vX.Y.Z`` tag.  Floating
     refs (branch names, HEAD, no anchor) are rejected: cross-repo references
     must be reproducible.
  4. Repo structure -- the wheel builds exactly one package, laid out at
     ``src/<import_name>``, matching the distribution name.
  5. Banned imports -- repository-specific deny lists, e.g. pulsar-core may
     never import a data-source or broker SDK, and never a network client.

Usage:
    python tools/check_topology.py --repo /path/to/pulsar-core
    python tools/check_topology.py --workspace /path/to/dir-with-all-six

Exit code 0 = topology OK, 1 = violations found (printed on stdout).

The script is stdlib-only (ast + tomllib) so any checkout with Python >= 3.11
can run it without installing anything.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

GITHUB_ORG_URL = "https://github.com/vanzeph"

#: The six repositories of the Pulsar family, by distribution name.
ALL_REPOS: tuple[str, ...] = (
    "pulsar-contracts",
    "pulsar-core",
    "pulsar-data",
    "pulsar-exec",
    "pulsar-app",
    "pulsar-ui",
)

#: Data-source SDKs: only pulsar-data adapters may import these.
DATA_SOURCE_SDKS: frozenset[str] = frozenset(
    {"akshare", "baostock", "tushare", "rqdatac", "rqdata", "jqdatasdk", "mootdx"}
)

#: Broker / gateway SDKs: only pulsar-exec adapters may import these.
BROKER_SDKS: frozenset[str] = frozenset({"xtquant", "easytrader"})

#: HTTP client libraries: contracts (pure interfaces), core (deterministic
#: engine) and ui (artefact consumer) must not open network sessions.
NETWORK_CLIENTS: frozenset[str] = frozenset(
    {"requests", "httpx", "aiohttp", "urllib3"}
)

ALL_SDKS: frozenset[str] = DATA_SOURCE_SDKS | BROKER_SDKS


@dataclass(frozen=True)
class RepoRule:
    """Topology rules that apply to a single repository."""

    #: pulsar-* distribution names this repo may depend on.
    allowed_deps: frozenset[str]
    #: top-level module names this repo's packaged source may never import.
    banned_imports: frozenset[str]


def _other_pulsar_imports(repo: str) -> frozenset[str]:
    own = dist_to_import(repo)
    return frozenset(
        dist_to_import(r) for r in ALL_REPOS if dist_to_import(r) != own
    )


REPO_RULES: dict[str, RepoRule] = {
    "pulsar-contracts": RepoRule(
        allowed_deps=frozenset(),
        banned_imports=frozenset(),
    ),
    "pulsar-core": RepoRule(
        allowed_deps=frozenset({"pulsar-contracts"}),
        banned_imports=frozenset(),
    ),
    "pulsar-data": RepoRule(
        allowed_deps=frozenset({"pulsar-contracts"}),
        banned_imports=frozenset(),
    ),
    "pulsar-exec": RepoRule(
        allowed_deps=frozenset({"pulsar-contracts"}),
        banned_imports=frozenset(),
    ),
    "pulsar-app": RepoRule(
        allowed_deps=frozenset(
            {"pulsar-contracts", "pulsar-core", "pulsar-data", "pulsar-exec"}
        ),
        banned_imports=frozenset({"pulsar_ui"}),
    ),
    "pulsar-ui": RepoRule(
        allowed_deps=frozenset(),
        banned_imports=frozenset(),
    ),
}

# Repository-specific banned third-party modules, layered on top of the
# pulsar edge rules.  Every repo is denied the *other* pulsar modules via the
# edge rules themselves; these lists constrain third-party SDKs.
REPO_BANNED_THIRD_PARTY: dict[str, frozenset[str]] = {
    # pure contract library: no SDKs, no network clients
    "pulsar-contracts": ALL_SDKS | NETWORK_CLIENTS,
    # engine: no data-source or broker SDK, no network client
    "pulsar-core": ALL_SDKS | NETWORK_CLIENTS,
    # data adapters: broker SDKs belong to pulsar-exec
    "pulsar-data": BROKER_SDKS,
    # execution adapters: market-data SDKs belong to pulsar-data
    "pulsar-exec": DATA_SOURCE_SDKS,
    # assembly point: plugins ship their own SDKs inside data/exec
    "pulsar-app": ALL_SDKS,
    # read-only artefact consumer: no SDKs, no network clients
    "pulsar-ui": ALL_SDKS | NETWORK_CLIENTS,
}


def dist_to_import(dist_name: str) -> str:
    """Map a distribution name to its import package (PEP 503 normalisation
    is overkill here: the Pulsar family only uses the ``-`` -> ``_`` form)."""
    return dist_name.replace("-", "_")


# --------------------------------------------------------------------------
# pyproject parsing
# --------------------------------------------------------------------------

_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_GIT_REF_RE = re.compile(
    r"git\+https://github\.com/vanzeph/(?P<repo>[A-Za-z0-9._-]+)\.git@(?P<ref>[^\s;]+)"
)
_ANCHOR_RE = re.compile(r"^(?:[0-9a-fA-F]{7,40}|v\d+\.\d+\.\d+[0-9A-Za-z.+-]*)$")


def requirement_name(req: str) -> str | None:
    """Best-effort extraction of the distribution name from a PEP 508 string
    or a direct reference."""
    match = _REQ_NAME_RE.match(req)
    return match.group(1) if match else None


def is_pulsar_dist(name: str) -> bool:
    return name in ALL_REPOS


def check_git_anchor(req: str, dist_name: str) -> list[str]:
    """Assert ``req`` is an anchored, first-party git reference."""
    problems: list[str] = []
    match = _GIT_REF_RE.search(req)
    if match is None:
        problems.append(
            f"dependency '{dist_name}' must be a direct git reference of the form "
            f"'git+{GITHUB_ORG_URL}/{dist_name}.git@<commit-or-vtag>' "
            f"(floating refs are not reproducible); got: {req!r}"
        )
        return problems
    if match.group("repo") != dist_name:
        problems.append(
            f"dependency '{dist_name}' points at foreign repository "
            f"'{match.group('repo')}'; cross-repo references must stay inside "
            f"github.com/vanzeph"
        )
    ref = match.group("ref")
    if not _ANCHOR_RE.match(ref):
        problems.append(
            f"dependency '{dist_name}' uses non-reproducible anchor {ref!r}; "
            f"use a commit hash (7-40 hex) or a vX.Y.Z tag"
        )
    return problems


@dataclass
class PyProject:
    """The subset of pyproject.toml the gate cares about."""

    dist_name: str
    dependencies: list[str] = field(default_factory=list)
    optional_dependencies: dict[str, list[str]] = field(default_factory=dict)
    wheel_packages: list[str] = field(default_factory=list)


def load_pyproject(path: Path) -> PyProject:
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    project = doc.get("project", {})
    hatch = doc.get("tool", {}).get("hatch", {})
    packages = (
        hatch.get("build", {}).get("targets", {}).get("wheel", {}).get("packages", [])
    )
    return PyProject(
        dist_name=project.get("name", ""),
        dependencies=list(project.get("dependencies", [])),
        optional_dependencies={
            extra: list(reqs)
            for extra, reqs in project.get("optional-dependencies", {}).items()
        },
        wheel_packages=list(packages),
    )


# --------------------------------------------------------------------------
# source scanning
# --------------------------------------------------------------------------


@dataclass
class ScanResult:
    imported_modules: dict[str, list[str]] = field(default_factory=dict)  # module -> files

    def add(self, module: str, rel_file: str) -> None:
        self.imported_modules.setdefault(module, []).append(rel_file)


def scan_imports(package_root: Path) -> ScanResult:
    """AST-scan every ``*.py`` under the packaged source tree and record the
    top-level module of each absolute import."""
    result = ScanResult()
    for py in sorted(package_root.rglob("*.py")):
        rel = str(py.relative_to(package_root))
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        except SyntaxError as exc:  # pragma: no cover - defensive
            raise SystemExit(f"cannot parse {py}: {exc}") from exc
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                # ``import a.b`` binds top-level module ``a``
                for alias in node.names:
                    result.add(alias.name.split(".")[0], rel)
            elif isinstance(node, ast.ImportFrom):
                # ``from a.b import x`` -> ``a``; relative imports (level > 0)
                # stay inside the package and are not recorded
                if node.level == 0 and node.module:
                    result.add(node.module.split(".")[0], rel)
    return result


# --------------------------------------------------------------------------
# per-repo check
# --------------------------------------------------------------------------


@dataclass
class RepoReport:
    repo: str
    path: Path
    violations: list[str] = field(default_factory=list)
    declared_pulsar_deps: list[str] = field(default_factory=list)
    imported_pulsar_modules: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def check_repo(repo_path: Path) -> RepoReport:
    repo_path = repo_path.resolve()
    pyproject_path = repo_path / "pyproject.toml"
    if not pyproject_path.is_file():
        raise SystemExit(f"no pyproject.toml under {repo_path}")

    pyproject = load_pyproject(pyproject_path)
    dist = pyproject.dist_name
    rule = REPO_RULES.get(dist)
    if rule is None:
        raise SystemExit(
            f"{repo_path} declares unknown distribution {dist!r}; "
            f"expected one of {sorted(ALL_REPOS)}"
        )
    report = RepoReport(repo=dist, path=repo_path)

    own_import = dist_to_import(dist)
    allowed_imports = {dist_to_import(d) for d in rule.allowed_deps}
    banned = set(rule.banned_imports) | set(REPO_BANNED_THIRD_PARTY.get(dist, frozenset()))

    # ---- rule 4: repo structure -------------------------------------
    expected_pkg_dir = f"src/{own_import}"
    if len(pyproject.wheel_packages) != 1:
        report.violations.append(
            f"repo structure: [tool.hatch.build.targets.wheel].packages must list "
            f"exactly one package; got {pyproject.wheel_packages!r}"
        )
    elif pyproject.wheel_packages[0] != expected_pkg_dir:
        report.violations.append(
            f"repo structure: package must live at {expected_pkg_dir!r}; "
            f"got {pyproject.wheel_packages[0]!r}"
        )

    package_root = repo_path / expected_pkg_dir
    if not package_root.is_dir():
        report.violations.append(
            f"repo structure: packaged source directory {expected_pkg_dir!r} "
            f"does not exist"
        )
        return report

    # ---- rule 1 + 3: declared dependencies ---------------------------
    declared: list[str] = list(pyproject.dependencies)
    for extra, reqs in pyproject.optional_dependencies.items():
        declared.extend(reqs)
    for req in declared:
        if req.strip().startswith(("git+", "http://", "https://")):
            report.violations.append(
                f"dependency {req!r} must use the PEP 508 direct-reference form "
                f"'<name> @ git+{GITHUB_ORG_URL}/<repo>.git@<anchor>'"
            )
            continue
        name = requirement_name(req)
        if name is None:
            report.violations.append(f"cannot parse requirement {req!r}")
            continue
        if not is_pulsar_dist(name):
            continue
        report.declared_pulsar_deps.append(name)
        if name == dist:
            report.violations.append(f"self-dependency on {name!r}")
        elif name not in rule.allowed_deps:
            report.violations.append(
                f"forbidden cross-repo dependency: {dist} -> {name} "
                f"(allowed: {sorted(rule.allowed_deps) or 'none'})"
            )
        report.violations.extend(check_git_anchor(req, name))

    declared_imports = {
        dist_to_import(n)
        for n in map(requirement_name, pyproject.dependencies)
        if n and is_pulsar_dist(n)
    }

    # ---- rule 1 + 2 + 5: source imports ------------------------------
    scan = scan_imports(package_root)
    for module, files in sorted(scan.imported_modules.items()):
        cross_repo = module.startswith("pulsar_") and module != own_import
        if cross_repo:
            report.imported_pulsar_modules.append(module)
        if module in banned:
            sample = ", ".join(files[:3])
            report.violations.append(
                f"banned import '{module}' in {sample}"
                + (" ..." if len(files) > 3 else "")
            )
        elif cross_repo:
            if module not in allowed_imports:
                report.violations.append(
                    f"forbidden cross-repo import: {module} "
                    f"(allowed pulsar imports: {sorted(allowed_imports) or 'none'})"
                )
            elif module not in declared_imports:
                report.violations.append(
                    f"import purity: {module} is imported but not declared in "
                    f"[project].dependencies"
                )

    return report


# --------------------------------------------------------------------------
# workspace check
# --------------------------------------------------------------------------


def check_workspace(root: Path) -> list[RepoReport]:
    root = root.resolve()
    missing = [r for r in ALL_REPOS if not (root / r).is_dir()]
    if missing:
        raise SystemExit(
            f"workspace {root} is missing repositories: {missing}; "
            f"expected one checkout per repo of {list(ALL_REPOS)}"
        )
    return [check_repo(root / repo) for repo in ALL_REPOS]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print_reports(reports: list[RepoReport]) -> int:
    failed = [r for r in reports if not r.ok]
    for report in reports:
        status = "OK " if report.ok else "FAIL"
        declared = ",".join(sorted(report.declared_pulsar_deps)) or "-"
        print(
            f"[{status}] {report.repo:<18} deps->[{declared}] "
            f"({'no violations' if report.ok else f'{len(report.violations)} violation(s)'})"
        )
        for violation in report.violations:
            print(f"       - {violation}")
    if failed:
        print(
            f"\ntopology gate: {len(failed)}/{len(reports)} repository(ies) "
            f"violate the Pulsar dependency topology"
        )
        return 1
    print("\ntopology gate: all repositories conform to the Pulsar dependency topology")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--repo", type=Path, help="check a single repository checkout")
    group.add_argument(
        "--workspace", type=Path, help="check a directory containing all six checkouts"
    )
    args = parser.parse_args(argv)

    if args.repo is not None:
        reports = [check_repo(args.repo)]
    else:
        reports = check_workspace(args.workspace)
    return _print_reports(reports)


if __name__ == "__main__":
    sys.exit(main())
