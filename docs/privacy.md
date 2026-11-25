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

## Publication controls

Install guards in each clone before committing, using your GitHub noreply identity:

```sh
python scripts/install_publication_hooks.py --name YOUR_HANDLE --email YOUR_NOREPLY
python -m unittest discover -s tests -p test_publication_privacy.py
python scripts/check_commit_privacy.py check HEAD
```

The installer changes repository-local settings only. Reinstall after moving the clone
or Python interpreter. Do not disable the hooks to get a failed publication through.

- `pre-commit` checks the effective author/committer identities and the full staged
  Git tree. Editing a worktree file after staging cannot hide the staged bytes.
- `commit-msg` rejects non-noreply addresses in messages, including attribution trailers.
- `pre-push` checks proposed branch/tag objects, every reachable commit and their
  trees. It checks tagger identities and annotated tag messages too. A clean tip
  does not excuse private content or identities in an ancestor.
- The guards reject incomplete history, nonregular entries, unreadable/non-UTF8
  blobs, NUL bytes, LFS pointers, runtime artifacts, recognizable media/archive
  headers, audio data URIs and recognized Base64-encoded media/archive payloads.
  No size-based or documentation-directory skip is used.
- Content may use reserved example/test addresses; commit/tag metadata permits
  only GitHub noreply addresses. Errors suppress offending values and paths.
- CI fetches full history, exercises the guards using fictional data and checks
  the published ref (including annotated tags). It runs after upload and cannot
  prevent initial disclosure by an unguarded client or API.

All commit creation must use native Git with these hooks, or an explicitly reviewed
interface that sets both author and committer and validates the object before upload.
Repository-local settings do not change a connector's authenticated-account defaults.
After a push, compare remote metadata and tree with the validated local commit.

Static scanning is not a semantic personal-data classifier, a speaker identification
system or a universal decoder. It cannot prove fictional provenance or detect every
encrypted, hidden or dynamically generated payload. Review fixture and prompt changes
manually; do not access real logs or recordings to perform that review.

References: [Git hooks](https://git-scm.com/docs/githooks),
[GitHub workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).
