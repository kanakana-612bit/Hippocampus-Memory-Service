# OpenWebUI Hook: Advisory Context

Historical Context Compiler v2 integration notes. The routes and private bridge
below are not provided by the current public `app:app` (Ver0.2), or by Ver1.0.
See [Ver0.2](ver0.2-implementation.ja.md) and [Ver1.0](../v1/README.ja.md)
for their independent contracts. Deployment adapters are maintained separately.

The current integration does not inspect or reject generated conversation.
The main model receives source-aware context and interpretation cautions.
Legacy semantic gates and realtime SLM calls are not part of this pipeline.

## Components

- `openwebui_adapter.py`: pure conversion of OpenWebUI's saved history tree to
  immutable event versions and an ordered selected path; final budget checking.
- `openwebui_bridge.py`: optional asynchronous OpenWebUI glue. It imports
  OpenWebUI only inside the synchronization function.
- `POST /dialogue/sync`: stores dialogue, selects the path, then captures only
  canonical user events. Completed assistant turns are stored immediately.
- `POST /context/build`: returns `prompt_text`, `compiled_context`,
  `context_compiler_version`, and `budget_report`.
- `GET /dialogue/transcript`: searches canonical original dialogue, not audit
  drafts. Optional `query` performs an exact case-insensitive substring search.
- `GET /status/context-compiler`: shows the mode and configured budget.

The bridge is a source-level integration, not an automatically installed
OpenWebUI plugin. Back up the installed host files before adapting these call
sites. OpenWebUI upgrades can change them. Make this checkout importable in the
OpenWebUI process through its Python module search path.

## Call Sites

1. Before model generation, call `await openwebui_bridge.build(form_data, metadata)`.
   Append a nonempty result to the system message. Pass the saved chat ID and
   active user message ID in metadata. Internal tasks must set `metadata.task`.
2. After all file/tool/system-prompt additions, call
   `openwebui_bridge.final_budget(form_data, metadata)`.
   Do not let an old gate buffer or regenerate the response.
3. After persisting a completed assistant message (both streaming and
   non-streaming completion), call
   `await openwebui_bridge.completed(content, metadata)`.
4. After persisting UI history changes, call
   `await openwebui_bridge.sync_saved(chat_id)`. This covers manual branch
   selection without another user message.

The bridge reads the history's parent IDs and current leaf. It stores displayed
sibling branches but only selects the active chain. Incomplete and legacy
validation-failed outputs do not become dialogue evidence. Candidate bodies
remain separate from original dialogue. Repeated identical synchronization is
idempotent; actual branch switches append a new path revision.

The supplied bridge targets a single OpenWebUI worker. Custom multi-worker
hosts should serialize selection updates or send `expected_revision` to
`/dialogue/sync` and refresh/retry on HTTP 409. Candidate storage is not a
replacement for a conversation access-control layer.

## Configuration

Set these in the service and host environments as appropriate:

```env
HIPPOCAMPUS_HOOK_ENABLED=1
HIPPOCAMPUS_AUTO_CAPTURE_ENABLED=1
HIPPOCAMPUS_URL=http://127.0.0.1:8091
HIPPOCAMPUS_TIMEOUT_SECONDS=8
HIPPOCAMPUS_TIMEZONE=UTC
HIPPOCAMPUS_LOGICAL_ACTOR_ID=assistant:primary
HIPPOCAMPUS_RESPONSE_GATE_MODE=advisory
HIPPOCAMPUS_EXPERIMENTAL_GATES_ENABLED=0
HIPPOCAMPUS_SLM_PRELOAD_ON_START=0
HIPPOCAMPUS_CLAIM_SLM_ENABLED=0
HIPPOCAMPUS_TEMPORAL_SLM_ENABLED=0
HIPPOCAMPUS_ATTRIBUTION_GATE_ENABLED=0
HIPPOCAMPUS_TEMPORAL_GATE_ENABLED=0
HIPPOCAMPUS_RUNTIME_CONTEXT_LIMIT=10322
HIPPOCAMPUS_RESERVED_GENERATION_TOKENS=1800
HIPPOCAMPUS_TEMPLATE_OVERHEAD_RESERVE=1000
HIPPOCAMPUS_CONTEXT_BUDGET_RATIO=0.25
HIPPOCAMPUS_CONTEXT_MIN_TOKENS=800
HIPPOCAMPUS_CONTEXT_MAX_TOKENS=3000
HIPPOCAMPUS_CANDIDATE_AUDIT_ENABLED=0
```

The capacity is a deployment setting, not a permanent model limit. The host
must use the same budget settings as the service. The initial estimator uses
UTF-8 byte counts conservatively; it does not measure server templates.
The separate 1000-token reserve covers expected template overhead. Multimodal
server token accounting is not implemented by this text-chat adapter.

A larger explicit `max_tokens` request is included in the generation reserve.
If context and history exceed capacity, whole records/old turns are dropped;
the current user turn is not silently sliced. If fixed instructions and the
current turn alone exceed capacity, the host reports `context_budget_exceeded`.
This is a capacity failure, not a semantic rejection.

## Minimal Sync Example

All values below are fictional:

```json
{
  "conversation_id": "demo-chat",
  "revision_key": "demo-selection-1",
  "auto_capture": true,
  "active_event_ids": ["demo-user-1", "demo-assistant-1"],
  "messages": [
    {
      "id": "demo-user-1",
      "role": "user",
      "content": "Could these dates refer to different events?",
      "source_channel": "openwebui_hook",
      "meta": {"logical_actor_id": "demo-user"}
    },
    {
      "id": "demo-assistant-1",
      "role": "assistant",
      "content": "Which event does each date describe?",
      "source_channel": "openwebui_hook",
      "meta": {
        "logical_actor_id": "assistant:primary",
        "model_provenance": {"model_id": "demo-model"}
      }
    }
  ]
}
```

Then send `query` and `conversation_id` to `/context/build`, with
`budget.existing_input_tokens` representing the host's existing request.
The returned JSON context labels originals, derived summaries and actors.
Several date expressions produce a caution, not a contradiction verdict.

## Failure and Audit Behavior

If synchronization fails, do not inject an outdated canonical path. Continue
the conversation without memory and retry synchronization on a later event.
Do not mark an unavailable compiler as a semantic validation failure.
No mandatory evidence markers or automatic regeneration loop are used.

Optional `/debug/candidates` routes require
`HIPPOCAMPUS_CANDIDATE_AUDIT_ENABLED=1`. Their separate SQLite file is debug
storage only. Never send these results to the ordinary context compiler.
Unshown outputs from another host may be recorded there explicitly; this
integration does not request extra candidate generations.

Keep OpenWebUI's `DATA_DIR` pointed at the existing data directory when
restarting. A wrong directory can look like a fresh account installation.
Keep both services on loopback or behind appropriate authentication.
Read [the migration guide](context-compiler-migration.ja.md) before upgrading.
