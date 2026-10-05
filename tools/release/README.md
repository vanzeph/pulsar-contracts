# Pulsar release lockfiles and versioned-reference conventions

This directory ships the reproducible-install artefacts for the six Pulsar
repositories (`github.com/vanzeph`):

| repository | distribution | role |
|---|---|---|
| pulsar-contracts | `pulsar-contracts` | port protocols + immutable domain objects (root, no pulsar deps) |
| pulsar-core | `pulsar-core` | deterministic engine; depends on contracts |
| pulsar-data | `pulsar-data` | MarketDataPort adapters; depends on contracts |
| pulsar-exec | `pulsar-exec` | ExecutionPort adapters; depends on contracts |
| pulsar-app | `pulsar-app` | runtime assembly; may depend on the four above |
| pulsar-ui | `pulsar-ui` | read-only dashboard; depends on NO pulsar package |

## Files

- `pulsar.constraints.txt` — the six Pulsar packages pinned as **anchored git
  references**. Use it as a pip constraints file (`pip install -c`) so that any
  resolution of a `pulsar-*` package inside a workspace resolves to exactly
  these commits, even when repos are installed editable.
- `requirements-lock.txt` — a complete pinned environment (Pulsar git refs +
  every transitive PyPI dependency). Install this file alone to reproduce the
  whole system.

## Versioned-reference convention

Cross-repo dependencies MUST be PEP 508 direct references of the form:

```
pulsar-contracts @ git+https://github.com/vanzeph/pulsar-contracts.git@<anchor>
```

where `<anchor>` is either

- a **commit hash** (7–40 hex chars, e.g. `f385fbd`), used while a repository
  has not cut tags yet, or
- a **release tag** (`vX.Y.Z`, e.g. `v0.1.0`) once the repository is tagged.

Floating references (no anchor, branch names, `HEAD`) are rejected by the
topology gate (`tools/check_topology.py`, see below) because they are not
reproducible. When a repository you depend on moves forward: bump the anchor,
run that repository's test suite, then update the lockfiles here.

## Reproducible install (clean room)

```bash
python3 -m venv /tmp/pulsar-release
/tmp/pulsar-release/bin/pip install --upgrade pip
/tmp/pulsar-release/bin/pip install -r requirements-lock.txt
/tmp/pulsar-release/bin/python - <<'PY'
import pulsar_contracts, pulsar_core, pulsar_data, pulsar_exec, pulsar_app, pulsar_ui
print("all six packages import:",
      pulsar_contracts.__version__, pulsar_core.__version__,
      pulsar_data.__version__, pulsar_exec.__version__,
      pulsar_app.__version__, pulsar_ui.__version__)
PY
```

No GitHub credentials are required: every reference is a public HTTPS git URL.

## Local development (editable workspace)

Clone the six repositories side by side, then:

```bash
python3 -m venv .venv
.venv/bin/pip install -c pulsar.constraints.txt \
    -e pulsar-contracts -e pulsar-core -e pulsar-data \
    -e pulsar-exec -e pulsar-app -e pulsar-ui
```

The constraints file keeps the family consistent while `pip` builds the
editable installs from your checkouts.

## Re-locking

When a pinned commit must move forward:

```bash
# 1. update the six lines in pulsar.constraints.txt to the new anchors
python3 -m venv /tmp/pulsar-relock
/tmp/pulsar-relock/bin/pip install --upgrade pip
/tmp/pulsar-relock/bin/pip install -r pulsar.constraints.txt
# 2. regenerate the full pin set
/tmp/pulsar-relock/bin/pip freeze --exclude-editable > requirements-lock.txt
# 3. smoke-test the new lock in a *fresh* venv (see clean-room recipe above)
```

Commit both files together with the constraints change.

## CI topology gate

Every repository runs `pulsar-contracts/tools/check_topology.py --repo .` in
CI, fetching the tool from a **pinned commit** of this repository (the same
versioned-reference convention, applied to tooling). The gate asserts:

1. **Edge rules** — declared and imported pulsar dependencies stay within
   `core/data/exec -> contracts`, `app -> {contracts, core, data, exec}`,
   `ui -> none`, `contracts -> none`.
2. **Import purity** — a package only imports pulsar modules it declares in
   `[project].dependencies`.
3. **Versioned references** — every pulsar dependency is an anchored
   `git+https://github.com/vanzeph/<repo>.git@<...>` reference.
4. **Repo structure** — one package per repository, at `src/<import_name>`.
5. **Banned imports** — per-repository deny lists (e.g. pulsar-core may never
   import a data-source/broker SDK or a network client).

Run the full six-repository check locally with:

```bash
python tools/check_topology.py --workspace /path/to/dir-containing-all-six
```
