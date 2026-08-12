# Repository Scope and Data Safety

These rules apply to all work under this directory.

## Branch policy

- `main` contains only the public Hippocampus memory service described in
  `docs/public-memory-architecture.ja.md`.
- Public scope includes memory storage, retrieval, decay, recall, consolidation,
  temporal context, provenance, integrity verification, review, deletion, APIs,
  documentation, and tests for those features.
- Deployment-specific orchestration, private application adapters, hardware
  integrations, and other project-specific behavior belong on `Private`.
- Do not merge `Private` wholesale into `main`. Move a change to `main` only
  after reviewing it as an independently public, memory-domain feature.
- `docs/langgraph-integration-plan.ja.md` is a local/private integration plan and
  must not be committed to `main`.

## Data policy

The following must not be committed to any branch, including `Private`:

- Real conversation logs, raw chat imports, and user memory exports.
- SQLite databases, journals, WAL files, and database backups.
- Generated images, audio, video, archives, and runtime output.
- Personal attachments, recordings, caches, temporary files, and local secrets.
- Local `.env` files, credentials, tokens, private keys, or machine-specific
  user paths.

Only fictional or deliberately sanitized fixtures may be committed. Keep them
small, label them as demo data, and review them manually before publication.

## Before committing or pushing

1. Confirm the current branch.
2. Review every added and modified path, including untracked files.
3. On `main`, confirm that each change belongs to the public memory scope.
4. Run `python scripts/check_public_tree.py`.
5. Inspect staged content for names, chat IDs, local paths, credentials, and raw
   conversation text before the final commit.

If branch state or publication scope is unclear, do not push until it has been
resolved explicitly.
