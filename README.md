# Hippocampus

Memory storage, provenance, retrieval and context assembly for
`Jev-like System 1 → Hippocampus → arbitrary LLM/System 2`.

## Ver1.0 development foundation

[`v1/`](v1/README.ja.md) contains the independent Ver1.0 development foundation:
immutable Raw snapshots, common initial/daily analysis, verified staging,
transactional publication, context membership, authorization-aware retrieval,
separate selection/final-input observations, and a standalone development UI.

**Ver1.0 is not an accepted production release.** It accepts explicitly fictional
data only. P6 system acceptance is incomplete and P7 real-data intake is closed.
It neither imports existing databases nor routes through the old API. New keys,
an audit epoch, a database and explicit service configuration are required.

- [Ver1.0 contracts, setup and remaining work (Japanese)](v1/README.ja.md)
- [Design and implementation plan, revision 2 (Japanese)](docs/v1-context-database-extension-plan.ja.md)
- [Foundation implementation record, 2026-09-24 (Japanese)](docs/v1-foundation-implementation.ja.md)
- [Publication scope and validation, 2026-11-25 (Japanese)](docs/github-sync-20261125.ja.md)

From the repository root, install `v1/requirements.txt` and `httpx` for tests:

```text
python -m pip install -r v1/requirements.txt httpx
python -m unittest discover -s tests -p "test_v1_*.py" -v
```

The tests create isolated fictional workspaces. They do not assess real-model
semantic quality. See the development guide before starting a new workspace.

## Ver0.2 public memory core

Canonical memory service for `Jev-like System 1 → Hippocampus → arbitrary LLM/System 2`.
This release deliberately removes the old API contract. The old implementation remains
as a migration/reference asset; it is not loaded by `app:app`.

- Separate retrieval weight, generalization weight, salience and target-specific confidence.
- Persistent source events, versioned memories, branch-aware retrieval and association edges.
- Authenticated principal/host boundaries, optimistic versions and idempotent commands.
- Separate selected, compiled, host-finalized and dispatch observations. Model use stays unknown.
- Weak next-turn correction events and optional temporary, scoped retrieval modifiers.
- Scoped functional control, explicit expression sandbox transitions and unresolved-item lifecycle.
- Signed audit/checkpoints, explicit one-way migration, offline restore and explicit principal erasure.
- No Hippocampus token/character cap or automatic memory deletion.
- Provisional dictionary claims with immutable sources, evidence, manual reviews and revision history.
- Explicit extraction proposals with verified original spans, immutable lineage and separate extraction confidence.
- Bounded counterevidence scans of canonical sources, expiring execution leases and explicit result/dependency inspection.

Generalization learning is disabled (`0 / unevaluated`). The initial frontend and nightly
reassessment use deterministic rules; no runtime model call is required. Feedback effects
and edge learning are separately opt-in experimental policies.

## Setup

Install `requirements.txt`, choose a **new** database, and create local credentials:

```text
python scripts/create_v02_credentials.py --auth-file data/server-auth.json --client-file data/client-auth.json --user-id demo-ui --principal demo-owner --host-id demo-host
```

The registry generator grants `read/write/sync/observe`, not review/control/learn/admin.
Protect the client registry using the operating system's file permissions. `.env.example`
shows the new settings. Export them in the service environment or use:

```text
python -m uvicorn app:app --host 127.0.0.1 --port 8091 --env-file .env
```

The service requires `HIPPOCAMPUS_V02_DB` and `HIPPOCAMPUS_V02_AUTH_FILE`. It does not
load or convert `HIPPOCAMPUS_DB`. API documentation is generated at `/docs`; only
`/health` is public. All memory routes are under `/api/v0.2` and require a bearer token.

## Operations and validation

Read the [Ver0.2 contracts and operations guide](docs/ver0.2-implementation.ja.md) before
migration, cutover, backup restore or explicit erasure. Private application
adapters are deployed separately and are not part of this public memory-core branch.

The Ver0.2 core uses **database schema 5**. Existing schema 1, 2, 3 or 4 databases must be backed up and
explicitly upgraded while writers are stopped, before starting this code. Startup does
not migrate them automatically. See the [multiresolution episode guide](docs/episodic-memory-multiresolution.ja.md),
[K3 search and review guide](docs/knowledge-countersearch-k3.ja.md) and
[K4 retrieval and injection guide](docs/knowledge-retrieval-k4.ja.md).
Dictionary claims support structured proposals, authorized review, internal counterevidence
search and separately configured `disabled / shadow / enabled` retrieval modes.
External counterevidence search is not connected. Episode generation requires explicit
model and worker configuration; its structural tests do not establish summary quality.
Schema upgrade and semantic-memory candidate import are separate explicit operations.
These historical operations target Ver0.2 only; there is no migration into Ver1.0.

```text
python scripts/migrate_v02.py --source old.db --destination new.db --principal demo-owner
python -m unittest discover -s tests -p "test_v02.py" -v
python -m unittest discover -s tests -p "test_v02_evaluation.py" -v
python -m unittest discover -s tests -p "test_knowledge*.py" -v
python -m unittest discover -s tests -p "test_extraction*.py" -v
python -m unittest discover -s tests -p "test_countersearch*.py" -v
```

Migration is plan-only unless `--apply` is supplied. Tests use fictional temporary data.
The historical test suite contains intentionally obsolete API and weight contracts;
it is not the acceptance suite for Ver0.2. See [archived v2 documentation](docs/archive/README-v2.md)
for historical behavior only.
