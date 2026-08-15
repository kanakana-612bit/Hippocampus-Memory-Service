# OpenWebUI Hook

Hippocampus can be connected to OpenWebUI by calling `POST /memory/ingest` and
then `POST /context/build` inside the request pipeline before the model response
is generated.

The service returns a prompt-ready block:

```text
<memory_context>
Use these retrieved memories only when relevant to the user's current request.
Confirmed memories may be treated as stable user-provided context.
Inferred memories are tentative: do not present them as certain facts, and avoid over-personalizing from them.
...
</memory_context>
```

Recommended behavior:

- Send the previous assistant turn and latest user turn to `/memory/ingest`.
- Use stable source event IDs so retries do not create duplicate traces.
- Leave `auto_capture=true`; automatically extracted items remain inferred
  short-term candidates.
- Call Hippocampus with the latest user message as `query`.
- Add the returned `memory_context` to the system message.
- Keep a short timeout, such as 2-3 seconds.
- If the memory service is unavailable, continue the chat without memory.
- Do not log the full memory context unless you are debugging a private local setup.

Example request:

```json
{
  "query": "Do you remember our keyword-search plan?",
  "limit": 6,
  "char_budget": 2500,
  "include_recent_raw": false,
  "conversation_id": "openwebui-chat-id"
}
```

Example environment switches for an OpenWebUI integration:

```env
HIPPOCAMPUS_HOOK_ENABLED=1
HIPPOCAMPUS_AUTO_CAPTURE_ENABLED=1
HIPPOCAMPUS_URL=http://127.0.0.1:8091
HIPPOCAMPUS_TIMEZONE=UTC
HIPPOCAMPUS_TIMEOUT_SECONDS=2.5
HIPPOCAMPUS_CONTEXT_LIMIT=6
HIPPOCAMPUS_CONTEXT_CHAR_BUDGET=2500
HIPPOCAMPUS_ATTRIBUTION_GATE_ENABLED=1
HIPPOCAMPUS_ATTRIBUTION_GATE_MODE=risk_based
HIPPOCAMPUS_ATTRIBUTION_TIMEOUT_SECONDS=5.0
HIPPOCAMPUS_ATTRIBUTION_GATE_FAIL_CLOSED=1
HIPPOCAMPUS_TEMPORAL_GATE_ENABLED=1
```

`HIPPOCAMPUS_ATTRIBUTION_GATE_MODE` accepts `risk_based`, `always`, or `off`.
The risk-based mode buffers only ordinary responses that appear likely to use
past conversation or retrieved memory. Tool execution and internal task
responses remain on their existing path.

Example ingestion payload:

```json
{
  "conversation_id": "openwebui-chat-id",
  "messages": [
    {
      "id": "previous-assistant-event-id",
      "role": "assistant",
      "content": "Previous assistant response",
      "actor_role": "assistant",
      "source_channel": "openwebui_hook",
      "content_origin": "original"
    },
    {
      "id": "current-user-event-id",
      "role": "user",
      "content": "その通りです。次に監査設計を進めたい。",
      "actor_id": "stable-user-id-if-available",
      "actor_role": "user",
      "source_channel": "openwebui_hook",
      "content_origin": "original"
    }
  ],
  "auto_capture": true,
  "idempotency_key": "stable-request-key"
}
```

The ingestion request should be best-effort. If it fails or times out, continue
the chat and still attempt context retrieval. Do not send system prompts, tool
payloads, or hidden internal task messages to the memory service.

Use the authenticated user's stable ID as `actor_id` when it is already
available to the frontend. Do not infer or synthesize an identity. The `role`,
`actor_role`, and `source_channel` fields are independently preserved so a
later summary cannot silently turn assistant-authored text into a user claim.
Reusing an existing source event ID with different content, conversation, role,
or actor attribution is rejected as a provenance conflict.

## Response Attribution Gate

`POST /context/build` returns `attribution_evidence` together with the prompt
block. The prompt asks the model to attach `[[event:ID]]` or `[[memory:ID]]` to
claims about who previously said, requested, preferred, believed, or proposed
something. These markers are machine-readable evidence references and are
removed before the final response is displayed.

For a gated response, the OpenWebUI integration buffers the draft and calls
`POST /response/candidates/select`. Hippocampus applies two narrow checks:

- a matching audited original event can verify a direct speech claim;
- a confirmed explicit memory can support a request, preference, belief, or
  proposal, but does not by itself prove an exact quotation;
- assistant-authored or derived evidence cannot be presented as a user-authored
  statement;
- missing or weak evidence remains `unverified`.
- explicit date claims are checked against the configured current time and
  timezone;
- a past date cannot be presented as a pending future reminder, while a
  correctly worded historical event remains allowed.

If the first candidate is contradicted or unverified, OpenWebUI requests one
regeneration with narrow correction feedback and validates it again. If the
retry still fails, it displays a neutral fallback rather than the rejected
draft. `HIPPOCAMPUS_ATTRIBUTION_GATE_FAIL_CLOSED=1` applies the same behavior
when the validator itself is unavailable during a gated response.

The gates are not general factuality checkers and do not review the whole
answer. They only check actor attribution and explicit temporal consistency.
This narrow scope keeps the final decision deterministic and allows Phase 4
and Phase 5 evidence to be reused directly. Optional SLM claim extraction is
enabled in the Hippocampus service with `HIPPOCAMPUS_CLAIM_SLM_ENABLED=1`; the
SLM structures claims but never decides whether they are valid. Set
`HIPPOCAMPUS_SLM_PROVIDER=ollama`, select the local model with
`HIPPOCAMPUS_SLM_MODEL`, and use `POST /slm/preload` when the model should stay
resident. Both user input and assistant drafts can be sent to
`POST /slm/claims/extract` with the corresponding `source_role`.

When the frontend has an original timestamp, send it as `event_time` together
with its IANA `timezone`. If it does not, omit it; Hippocampus records the value
as an ingestion-time fallback instead of pretending it came from the source.

OpenWebUI stores data in a `DATA_DIR`. If you run OpenWebUI from a Python
entrypoint, make sure `DATA_DIR` points to the intended database directory.
Starting with the wrong `DATA_DIR` can make OpenWebUI look like a fresh install
because it opens an empty database.
