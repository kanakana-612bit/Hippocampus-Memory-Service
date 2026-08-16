# Hippocampus Memory Service

External long-term memory service for local LLM chat frontends.

Hippocampus is a small FastAPI + SQLite service that gives a chat model a
retrievable "memory" layer without putting every past conversation into the
prompt. It separates decaying short-term traces from canonical long-term
memories, then builds a compact `<memory_context>` block before the model
responds.

The design is inspired by human episodic memory and consolidation, but the
implementation stays deliberately practical: keyword-first retrieval,
evidence-aware summaries, and clear separation between confirmed memories and
tentative inferred memories.

## What It Does

- Stores chat turns and compact long-term memories in SQLite.
- Uses SQLite FTS5, explicit keywords, and scoring for fast local retrieval.
- Keeps explicit "remember this" instructions as confirmed persistent memories.
- Automatically creates short-term candidates from affect, repetition,
  unfinished plans, preferences, procedures, corrections, and confirmations.
- Separates event, receive, persist, and source timestamps and normalizes them
  to UTC without discarding the source timezone.
- Builds compact temporal context and supports as-of, historical, future, and
  current-memory retrieval.
- Keeps validity windows and supersession history instead of silently
  overwriting an older memory.
- Records actor attribution, source channel, content origin, extractor, and
  derivation edges independently from memory content.
- Maintains an append-only SHA-256 audit chain and verifies current records
  against their latest audited snapshots.
- Signs audit heads with Ed25519, anchors checkpoints outside SQLite, and
  detects a database rollback against the last external anchor.
- Checks response candidates that attribute a past statement, request, or
  preference to someone, and rejects actor claims contradicted by provenance.
- Produces signed SQLite backups, verifies them before restore, and records a
  rollback restore as an explicit history branch.
- Keeps inferred memories tentative so guesses do not become facts.
- Separates activation, salience, stability, retention, and epistemic confidence.
- Consolidates short-term traces into episodic, semantic, prospective,
  or procedural long-term memory.
- Builds prompt-ready context for OpenWebUI or other local chat frontends.
- Can import an OpenWebUI chat branch for initial learning.
- Can optionally use an OpenAI-compatible local LLM endpoint for memory extraction.

## Non-goals

- This is not a hosted memory service.
- This is not an embedding database, although embeddings can be added later.
- This is not production privacy infrastructure.
- This should not be used for other people without review/delete controls.

## Quick Start

```powershell
cd hippocampus
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
Copy-Item .env.example .env
.\.venv\Scripts\python -m uvicorn app:app --host 127.0.0.1 --port 8091 --reload
```

Open the API docs:

```text
http://127.0.0.1:8091/docs
```

Linux/macOS:

```bash
cd hippocampus
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
./.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8091 --reload
```

## Configuration

Copy `.env.example` to `.env` and adjust as needed:

```env
HIPPOCAMPUS_DB=./data/hippocampus.db
LMSTUDIO_BASE_URL=http://127.0.0.1:1234/v1
HIPPOCAMPUS_LLM_MODEL=local-memory-extractor
HIPPOCAMPUS_TIMEZONE=UTC
HIPPOCAMPUS_SLM_PROVIDER=ollama
HIPPOCAMPUS_SLM_BASE_URL=http://127.0.0.1:11434
HIPPOCAMPUS_SLM_MODEL=qwen3.5:4b
HIPPOCAMPUS_SLM_KEEP_ALIVE=-1
HIPPOCAMPUS_SLM_PRELOAD_ON_START=0
HIPPOCAMPUS_CLAIM_SLM_ENABLED=0
OPENWEBUI_DB=../data/webui.db
HIPPOCAMPUS_SECURITY_DIR=./data/.hippocampus-security
HIPPOCAMPUS_BACKUP_DIR=./data/backups
# HIPPOCAMPUS_KEY_PASSPHRASE=replace-with-a-local-secret
```

`LMSTUDIO_BASE_URL` may point to any OpenAI-compatible local chat completions
server. LLM extraction is optional; the service also has a lightweight
heuristic fallback.

## Optional Resident SLM

Hippocampus can use a small Ollama model to structure attribution claims while
leaving the final actor and provenance decision deterministic. Install Ollama,
pull a local model, enable `HIPPOCAMPUS_CLAIM_SLM_ENABLED=1`, and keep
`HIPPOCAMPUS_SLM_KEEP_ALIVE=-1` to retain the model in memory after preload.
Set `HIPPOCAMPUS_SLM_PRELOAD_ON_START=1` when the service should load it in a
background thread after every restart.

```powershell
ollama pull qwen3.5:4b
Invoke-RestMethod -Method Post http://127.0.0.1:8091/slm/preload
Invoke-RestMethod -Method Post http://127.0.0.1:8091/slm/claims/extract `
  -ContentType 'application/json' `
  -Body '{
    "source_role":"assistant",
    "content":"The user previously said that option A was preferred.",
    "event_ids":["example-event"],
    "validate_attribution":true
  }'
```

`GET /status/slm` reports whether the server, configured model, and resident
model are available. The extraction response contains the public machine format
`subject`, `predicate`, `content`, and `evidence_marker`, plus `gate_claims` for
the existing attribution validator. An evidence marker is accepted only when it
appears in the input or is supplied by the caller; the SLM cannot authorize an
invented source ID. The route applies the lightweight attribution-risk filter
before inference by default; pass `"risk_filter": false` only for extractor
evaluation cases that intentionally bypass that first stage.

To use an already resident model on an LM Studio or another OpenAI-compatible
server, set `HIPPOCAMPUS_SLM_PROVIDER=openai` and set
`HIPPOCAMPUS_SLM_MODEL` to its served model ID. The structured claim route sends
an OpenAI-compatible `response_format=json_schema`; preload and keep-alive are
then managed by the remote model server.

## Seed Demo Data

The bundled seed file contains fictional demo memories only.

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8091/seed `
  -ContentType 'application/json' `
  -Body '{"overwrite": false}'
```

## Retrieve Memories

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8091/memory/retrieve `
  -ContentType 'application/json' `
  -Body '{"query":"keyword search for technical support","limit":5}'
```

## Build Prompt Context

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8091/context/build `
  -ContentType 'application/json' `
  -Body '{"query":"Do you remember the keyword-search plan?","limit":4,"char_budget":2500}'
```

The response includes:

- `memory_context`: prompt-ready block wrapped in `<memory_context>`.
- `injection`: suggested placement and budget metadata.
- `retrieved`: debug details and scoring components.

## Explicit Memories

Use this route for user-confirmed memories. Confirmed persistent memories are
treated differently from inferred memories.

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8091/memory/remember `
  -ContentType 'application/json' `
  -Body '{
    "content": "For technical support, prefer keyword-searchable evidence.",
    "category": "retrieval_preference",
    "scope": "user",
    "dedupe": true,
    "update_existing": true
  }'
```

## Automatic Candidate Capture

`POST /memory/ingest` stores incoming turns and evaluates user messages with a
lightweight deterministic extractor. It does not require an LLM call. Automatic
results are written only to `memory_traces` with
`acquisition_mode=automatic` and `epistemic_status=inferred`; they never become
confirmed long-term facts during ingestion.

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8091/memory/ingest `
  -ContentType 'application/json' `
  -Body '{
    "conversation_id":"example-chat",
    "messages":[
      {"id":"turn-17","role":"user","content":"次に監査設計を検討する必要がある。"}
    ],
    "auto_capture":true,
    "capture_threshold":0.50,
    "idempotency_key":"example-chat-turn-17"
  }'
```

The response reports created, reinforced, and skipped candidates. Stable source
event IDs prevent replayed turns from being recorded twice. Repeated matching
messages reinforce an existing active trace and increase its occurrence and
continuity scores. Set `auto_capture=false` when importing material that already
has a separate extraction pipeline.

The same key can instead be supplied with the `Idempotency-Key` HTTP header.
Reusing a key with a different payload is rejected.

## Nightly Reassessment

The real-time extractor is followed by an optional nightly reassessment. A
configurable user-activity gap first creates deterministic session boundaries.
Within each session, explicit transition expressions are treated as candidates
and an available LLM classifies genuine semantic topic changes. Topic segments
are then divided by an estimated token budget with overlapping context.

Assistant and system events are supplied as context, but a memory candidate may
only cite eligible user source event IDs. Boundary and candidate IDs are checked
deterministically, and every accepted result starts as an inferred short-term
trace.

```powershell
python scripts/run_nightly_maintenance.py `
  --since-hours 36 `
  --model your-long-context-model `
  --session-gap-minutes 90 `
  --context-tokens 12000 `
  --overlap-turns 2 `
  --auto-consolidate
```

Set `HIPPOCAMPUS_NIGHTLY_MODEL` to keep the nightly model independent from the
resident real-time claim extractor. Use `POST /memory/segments/detect` for a
segmentation-only run. `GET /memory/boundaries`, `GET /memory/segments`, and
`GET /status/segmentation` expose persisted derived boundaries and segments.
Transition expressions are never treated as proof of a topic change when the
LLM classifier is available; they are only high-precision candidate signals.

Use `POST /memory/nightly/extract` for extraction only or
`POST /memory/nightly/run` for extraction, decay/consolidation, and a signed
checkpoint. `GET /status/nightly` reports recent jobs, decisions, gate latency,
trace/source edge mismatches, estimated input tokens, segment counts, and model
call counts. These metrics allow context budgets to be tuned without publishing
raw conversations.

## Initial Learning From OpenWebUI

Hippocampus can import an OpenWebUI chat from `webui.db`, store raw turns, and
create tentative daily/project/episodic memories.

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8091/learn/openwebui/chat/<chat-id> `
  -ContentType 'application/json' `
  -Body '{"branch":"current","create_memories":true,"overwrite_seeded":false}'
```

With a local OpenAI-compatible LLM extractor:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8091/learn/openwebui/chat/<chat-id> `
  -ContentType 'application/json' `
  -Body '{"branch":"current","create_memories":true,"use_llm":true,"model":"your-local-model"}'
```

Generated memories are marked as inferred and tentative by default.

## Layered Memory Model

Hippocampus uses two physical persistence layers:

- `memory_traces`: proto/candidate short-term traces. Activation decays over
  time; recall, repetition, salience, and continuity can keep a trace active or
  send it to review.
- `memories`: consolidated long-term memory. `memory_type` is one of
  `episodic`, `semantic`, `prospective`, or `procedural` in the public scope.

The following axes remain independent:

- `activation`: current recall readiness; increases on recall and decays with time.
- `salience`: emotional, novel, repeated, or unfinished importance.
- `stability`: strength of consolidation across time and reuse.
- `retention_score`: operational score used for review/archive thresholds.
- `epistemic_confidence`: confidence that an interpretation or world hypothesis
  is true. Emotional intensity never raises this automatically.

Explicit user instructions are stored with `acquisition_mode=user_explicit`,
`epistemic_status=confirmed`, and `epistemic_confidence=1.0`. Automatically
extracted memories remain `inferred` unless reviewed or confirmed.

For source material, `observation_statement` records what was actually reported
while `world_hypothesis` records an interpretation. This keeps reported content
separate from an inferred claim about the world.

## Temporal Awareness

Phase 3 keeps four timestamps separate:

- `event_time`: when the event or utterance occurred.
- `received_at`: when Hippocampus received it.
- `persisted_at`: when it was first persisted.
- `source_time`: the timestamp stated by an imported source.

Inputs are normalized to UTC for comparison while `timezone` and `time_source`
preserve interpretation context. Historical imports therefore keep their
original event time instead of appearing to have happened during import.

`POST /context/build` now includes a compact `<temporal_context>` with the
current local time, elapsed time since the latest event, and the gap before that
event. `GET /temporal/context` exposes the same information directly.

Long-term retrieval accepts `as_of` and `temporal_scope` (`current`,
`historical`, `future`, or `all`). `auto` is also available on retrieval and
context requests; queries about earlier or future periods automatically broaden
the search. Memories can use `valid_from`, `valid_until`, and `superseded_by`.
Use `POST /memories/{memory_id}/supersede` to close an old validity interval and
activate its replacement without deleting history.

The deterministic extractor recognizes clear day/week/month expressions such
as `today`, `tomorrow`, `next week`, `今日`, `明日`, `来週`, and explicit dates for
prospective candidates. Ambiguous temporal language is left unresolved.

## Provenance And Audit

Phase 4 records `actor_id`, `actor_role`, `source_channel`, `content_origin`,
`extractor`, and typed `derived_from` references for raw messages, short-term
traces, and long-term memories. This keeps source attribution separate from the
claim itself and exposes a provenance graph through
`GET /provenance/{object_type}/{object_id}`.

Creation, update, recall, consolidation, maintenance, supersession, and
forgetting operations append canonical events to `audit_events`. Every event
contains the previous event hash, payload digest, current-object digest, and
its time and attribution metadata. Database triggers reject updates and deletes
on the audit ledger and provenance edges.

`GET /audit/verify` performs two independent checks: the event hash chain, and
the current state of each object against its latest audited state digest. The
ledger stores digests rather than duplicate private memory content. A valid
chain proves consistency only from the Phase 4 baseline inside this database.
It does not prove that a recorded claim is true, and an unsigned local chain
cannot detect every whole-database rollback or tail replacement by itself.

## Signed Checkpoints And Restore

Phase 5 creates an Ed25519 signing identity whose private key is stored outside
SQLite. `POST /audit/checkpoints` signs the current verified audit head and
atomically replaces a public anchor document outside the database. On startup,
Hippocampus never overwrites a valid anchor that refers to a checkpoint missing
from the current database; `GET /audit/checkpoints/verify` reports that state as
a rollback instead.

`POST /audit/keys/rotate` creates a new key and records a transition signed by
both the old and new keys. Public keys, rotations, checkpoints, branches, and
branch adoptions are append-only. The default private-key protection relies on
filesystem access controls. Set `HIPPOCAMPUS_KEY_PASSPHRASE` before the first
start to encrypt newly generated private-key files.

Create and verify a signed online backup with `POST /backups` and
`POST /backups/verify`. `POST /restores/plan` compares a verified backup with
the current checkpoint lineage without changing the live database. Applying a
restore is deliberately offline-only:

```powershell
python scripts/restore_backup.py hippocampus-manual-<timestamp>.db
# Stop Hippocampus after reviewing the plan.
python scripts/restore_backup.py hippocampus-manual-<timestamp>.db --apply --service-stopped
```

The restore tool keeps a recovery copy of the replaced database. Restoring an
older or divergent backup creates a new branch with both the local fork
checkpoint and the previously canonical external checkpoint recorded.

The default anchor is outside SQLite but still on the same host. Put
`HIPPOCAMPUS_ANCHOR_PATH` on separately protected or replicated storage when
the threat model includes whole-host compromise. Signatures establish
continuity and source attribution, not whether a memory claim is true. Keep
checkpoint, rotation, backup, and restore endpoints behind a trusted local or
authenticated administrative boundary.

## Trace Lifecycle

Create a short-term candidate:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8091/memory/traces `
  -ContentType 'application/json' `
  -Body '{
    "trace_stage":"candidate",
    "candidate_memory_type":"episodic",
    "content":"The user appeared strongly interested in layered memory.",
    "keywords":["layered memory"],
    "activation":0.8,
    "salience":0.9,
    "epistemic_status":"inferred",
    "epistemic_confidence":0.7
  }'
```

Recall or consolidate it:

```text
POST /memory/traces/{trace_id}/recall
POST /memory/traces/{trace_id}/consolidate
```

Run decay and review maintenance from an external scheduler:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8091/memory/maintenance `
  -ContentType 'application/json' `
  -Body '{"daily_decay_rate":0.90,"auto_consolidate":false}'
```

Confirmed or pinned memories are never automatically archived. Maintenance does
not change epistemic confidence merely because a memory was recalled.

## OpenWebUI Integration

See [docs/openwebui-hook.md](docs/openwebui-hook.md).

The intended pattern is:

1. Take the latest user message.
2. Call `POST /context/build`.
3. Append `memory_context` to the system message.
4. Continue normally if the memory service is unavailable.

## Response Attribution Gate

The attribution gate is deliberately narrower than a general fact checker. It
examines only response fragments that claim who previously said, requested,
preferred, believed, or proposed something. Each claim is checked against the
audited actor and origin of its supporting event or memory.

The gate returns one of three decisions:

- `verified`: the actor, proposition, and audited source agree.
- `contradicted`: the source belongs to another actor, is derived material used
  as a direct quotation, or fails integrity checks.
- `unverified`: no sufficiently strong source was supplied.

Prompt context carries compact `[[event:ID]]` and `[[memory:ID]]` references so
a model can identify the evidence it used. The public API can validate one
candidate or select among several. If no candidate passes, it requests one
attribution-safe regeneration instead of displaying a known-bad attribution.

This does not prove that the response is factually correct. It only checks
whether the response is entitled to assign a remembered proposition to the
claimed actor. The deterministic extractor intentionally abstains on ambiguous
language; applications should keep such claims unverified rather than silently
promoting them to user instructions.

## Public Memory Architecture

The public Japanese overview of the memory architecture is available at
[docs/public-memory-architecture.ja.md](docs/public-memory-architecture.ja.md).
It focuses on memory organization, temporal context, provenance, integrity,
forgetting, and privacy without describing deployment-specific integrations.

## Main Endpoints

- `GET /health`
- `GET /status/phase1`
- `GET /status/phase2`
- `GET /status/phase3`
- `GET /status/phase4`
- `GET /status/phase5`
- `GET /status/attribution-gate`
- `GET /temporal/context`
- `GET /audit/verify`
- `GET /audit/events`
- `POST/GET /audit/checkpoints`
- `GET /audit/checkpoints/verify`
- `GET /audit/keys`
- `POST /audit/keys/rotate`
- `GET /audit/branches`
- `POST /backups`
- `POST /backups/verify`
- `POST /restores/plan`
- `GET /provenance/{object_type}/{object_id}`
- `POST /seed`
- `POST /memory/ingest`
- `POST /learn/openwebui/chat/{chat_id}`
- `POST /memory/remember`
- `POST /memory/persistent/duplicates`
- `POST /memory/consolidate`
- `POST /memory/retrieve`
- `POST /context/build`
- `POST /attribution/validate`
- `POST /response/candidates/select`
- `POST /temporal/validate`
- `GET /status/hardening`
- `GET /status/nightly`
- `GET /status/segmentation`
- `POST /memory/traces`
- `GET /memory/traces`
- `GET/PATCH/DELETE /memory/traces/{trace_id}`
- `POST /memory/traces/{trace_id}/recall`
- `POST /memory/traces/{trace_id}/review`
- `POST /memory/traces/{trace_id}/consolidate`
- `POST /memory/maintenance`
- `POST /memory/nightly/extract`
- `POST /memory/nightly/run`
- `POST /memory/segments/detect`
- `GET /memory/boundaries`
- `GET /memory/segments`
- `POST /memories/retrieve`
- `GET /memories`
- `GET /memories/{memory_id}/evidence`
- `POST /memories/{memory_id}/supersede`
- `GET/PATCH/DELETE /memories/{memory_id}`
- `GET /memory?memory_type=episodic`
- `GET /memory/{memory_type}/{memory_id}`
- `PATCH /memory/{memory_type}/{memory_id}`
- `POST /memory/merge`
- `DELETE /memory/{memory_type}/{memory_id}`
- `GET /export`

## Privacy

Read [docs/privacy.md](docs/privacy.md) before publishing or syncing a real
deployment. Do not commit real memory databases, logs, raw chat imports, or
exports from private conversations.

The repository includes `scripts/check_public_tree.py` and a GitHub Actions
publication guard. They reject common database, log, export, generated-media,
backup, secret, and personal-path artifacts from every branch. Project-specific
code belongs on `Private`; runtime data belongs in neither branch.

## Status

Memory-domain phases 1 through 5, the two-stage extraction pipeline, request
idempotency, response attribution gate, explicit-date temporal gate, hybrid
conversation segmentation, and the segmented nightly runner are implemented. Existing
episodic/project/persistent rows are projected into the canonical long-term
table at startup while legacy routes remain available. Back up a real SQLite
database before first migration.

Run the test suite with the service environment:

```powershell
python -m unittest discover -s tests -v
```

The next steps are broader operational evaluation, relative-time claim
extraction, administrative authorization, and a review UI. Embedding retrieval
remains optional; keyword + FTS5 is the default.

## License

MIT
