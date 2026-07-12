# OpenWebUI Hook

Hippocampus can be connected to OpenWebUI by calling `POST /context/build`
inside the request pipeline before the model response is generated.

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
HIPPOCAMPUS_URL=http://127.0.0.1:8091
HIPPOCAMPUS_TIMEOUT_SECONDS=2.5
HIPPOCAMPUS_CONTEXT_LIMIT=6
HIPPOCAMPUS_CONTEXT_CHAR_BUDGET=2500
```

OpenWebUI stores data in a `DATA_DIR`. If you run OpenWebUI from a Python
entrypoint, make sure `DATA_DIR` points to the intended database directory.
Starting with the wrong `DATA_DIR` can make OpenWebUI look like a fresh install
because it opens an empty database.
