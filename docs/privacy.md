# Privacy Notes

Hippocampus is designed to store conversational memory. That makes it useful,
but it also means it can contain private or sensitive information.

Before publishing, syncing, or sharing a deployment:

- Do not commit `data/`, `*.db`, `*.db-wal`, `*.db-shm`, logs, or exports from a real user.
- Do not commit generated images, audio, video, recordings, archives, or runtime output.
- Treat `raw_messages` as the most sensitive table because it can contain full turns.
- Prefer compact summaries and keywords for long-term memory.
- Keep inferred memories tentative. Do not let the system turn guesses into facts.
- Provide a review and delete path before using this with other people.
- If you import OpenWebUI chats, inspect generated memories before pinning them.
- Run `python scripts/check_public_tree.py` before pushing. The same check runs in CI.
- Never commit Phase 5 private signing keys, passphrases, external anchors, or
  signed backup files. Keep the security and backup directories outside source
  control and protect checkpoint, key-rotation, backup, and restore operations
  as administrative functions.

The bundled `seed_memories.json` contains fictional demo data only.
