# Hippocampus Memory Service

External long-term memory service for local LLM chat frontends.

Hippocampus is a small FastAPI + SQLite prototype that gives a chat model a
retrievable "memory" layer without putting every past conversation into the
prompt. It stores raw turns, daily summaries, episodic memories, active project
memories, and explicit user memories as separate layers, then builds a compact
`<memory_context>` block before the model responds.

The design is inspired by human episodic memory and consolidation, but the
implementation stays deliberately practical: keyword-first retrieval,
evidence-aware summaries, and clear separation between confirmed memories and
tentative inferred memories.

## What It Does

- Stores chat turns and compact long-term memories in SQLite.
- Uses SQLite FTS5, explicit keywords, and scoring for fast local retrieval.
- Keeps explicit "remember this" instructions as confirmed persistent memories.
- Keeps inferred memories tentative so guesses do not become facts.
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
OPENWEBUI_DB=../data/webui.db
```

`LMSTUDIO_BASE_URL` may point to any OpenAI-compatible local chat completions
server. LLM extraction is optional; the service also has a lightweight
heuristic fallback.

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

## OpenWebUI Integration

See [docs/openwebui-hook.md](docs/openwebui-hook.md).

The intended pattern is:

1. Take the latest user message.
2. Call `POST /context/build`.
3. Append `memory_context` to the system message.
4. Continue normally if the memory service is unavailable.

## Main Endpoints

- `GET /health`
- `POST /seed`
- `POST /memory/ingest`
- `POST /learn/openwebui/chat/{chat_id}`
- `POST /memory/remember`
- `POST /memory/persistent/duplicates`
- `POST /memory/consolidate`
- `POST /memory/retrieve`
- `POST /context/build`
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

## Status

Prototype. The current implementation is intentionally small and local-first.
The next natural steps are a review UI, scheduled consolidation, better
frontend hooks, and optional embedding-based associative retrieval.

## License

MIT
