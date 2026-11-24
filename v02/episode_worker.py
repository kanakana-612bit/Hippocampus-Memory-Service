"""Opt-in episodic maintenance client; never started by importing the service.

All scope and source resolution stays behind the canonical authenticated API.
Generation is outside database transactions. Only content-free status is printed
by the CLI; retryable commits retain their exact request and idempotency key.
"""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.parse
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .episode_contracts import JobCommit, JobPrepare
from .episode_generation import (EpisodeGenerator, GenerationError, ModelConfig,
    TransportError, endpoint_url, json_request)


JST = timezone(timedelta(hours=9), name="Asia/Tokyo")


def daily_due_day(at: datetime) -> str:
    """Latest completed calendar day eligible at 03:00 JST, including downtime.

    Eligibility is not a semantic boundary. The service includes outstanding older
    closed chunks; repeated calls also catch late backlog without local day markers.
    """
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("An aware scheduling timestamp is required")
    return ((at.astimezone(JST) - timedelta(hours=3)).date() - timedelta(days=1)).isoformat()


class CanonicalEpisodeClient:
    def __init__(self, url: str, token: str, *, timeout=30.0):
        self.url = endpoint_url(url) + "/api/v0.2"
        if not token:
            raise ValueError("An explicit canonical API credential is required")
        self._token, self.timeout = token, timeout

    def request(self, method, route, payload=None):
        return json_request(self.url + route, method=method, payload=payload,
                            token=self._token, timeout=self.timeout)


class EpisodeWorker:
    def __init__(self, client, generator, scope: dict, *, reason="scheduled_episode_derivation"):
        # Scope parsing is shared with the canonical contract, not inferred from text.
        from .contracts import Scope
        self.client, self.generator = client, generator
        self.scope = Scope.model_validate(scope).model_dump()
        self.reason = reason

    def _command(self, route, body):
        """One retry only for uncertain transport, with the identical command."""
        for attempt in range(2):
            try:
                return self.client.request("POST", route, body)
            except TransportError as exc:
                if attempt or exc.code != "connection_failure":
                    raise

    def _path_revision(self):
        query = urllib.parse.urlencode({"conversation_id": self.scope["conversation_id"]})
        result = self.client.request("GET", "/dialogue/path?" + query)
        try:
            revision = result["data"]["revision_key"]
            if not isinstance(revision, str) or not revision:
                raise ValueError()
            return revision
        except (KeyError, TypeError, ValueError):
            raise GenerationError("canonical_path_manifest_invalid") from None

    def seal(self):
        return self._command("/episodes/chunks/seal", {"scope": self.scope,
            "path_revision": self._path_revision(), "reason": self.reason,
            "idempotency_key": "episode-seal:" + uuid.uuid4().hex})

    def process(self, operation: str, *, day: str | None = None, source_refs=()):
        request = JobPrepare(scope=self.scope, path_revision=self._path_revision(),
            operation=operation, day=day, source_refs=source_refs,
            reason=self.reason, idempotency_key="episode-prepare:" + uuid.uuid4().hex)
        prepared = self._command("/episodes/jobs/prepare", request.model_dump(mode="json"))
        if prepared.get("status") == "no_work":
            return {"operation": operation, "status": "no_work"}
        try:
            job_id = prepared["job_id"]
            manifest_digest = prepared["manifest_digest"]
            bundle = prepared["input_bundle"]
            if (not isinstance(job_id, str) or not job_id
                    or prepared["operation"] != operation or not isinstance(bundle, dict)):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise GenerationError("canonical_job_manifest_invalid") from None
        # Never interpolate the returned identifier as a URL path or query delimiter.
        route = "/episodes/jobs/" + urllib.parse.quote(job_id, safe="") + "/commit"
        proposal = self.generator.generate(operation, bundle)
        body = JobCommit(manifest_digest=manifest_digest, proposal=proposal,
            generator=self.generator.metadata(), reason=self.reason,
            idempotency_key="episode-commit:" + uuid.uuid4().hex).model_dump(mode="json")
        committed = self._command(route, body)
        expected_candidates = len(proposal.structured_candidates) + sum(
            len(getattr(summary, field)) for summary in proposal.summaries
            for field in ("decision_candidates", "stance_candidates", "intent_candidates", "unresolved_candidates"))
        expected = {"summary_refs": len(proposal.summaries), "episode_refs": len(proposal.episodes),
                    "candidate_refs": expected_candidates}
        from .knowledge_contracts import RecordRef
        try:
            if (committed["job_id"] != job_id or committed["raw_changed"] is not False
                    or committed["knowledge_promoted"] is not False):
                raise ValueError()
            returned_ids = []
            for field, count in expected.items():
                references = committed[field]
                if not isinstance(references, list) or len(references) != count:
                    raise ValueError()
                returned_ids.extend(RecordRef.model_validate(ref).id for ref in references)
            if len(set(returned_ids)) != len(returned_ids):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise GenerationError("canonical_commit_acknowledgement_invalid") from None
        # A command acknowledgement is not a certificate of summary correctness.
        return {"operation": operation, "status": "committed",
            "summaries": len(proposal.summaries), "episodes": len(proposal.episodes),
            "structured_candidates": expected_candidates}

    def run_once(self, *, at: datetime | None = None, operation="all", day=None,
                 source_refs=()):
        at = at or datetime.now(timezone.utc)
        if operation not in {"all", "provisional", "daily", "regenerate"}:
            raise ValueError("Unknown maintenance operation")
        if day is not None and date.fromisoformat(day).isoformat() != day:
            raise ValueError("day must be an ISO calendar date")
        if operation != "regenerate":
            self.seal()
        results = []
        if operation in {"all", "provisional"}:
            results.append(self.process("provisional"))
        if operation in {"all", "daily"}:
            results.append(self.process("daily", day=day or daily_due_day(at)))
        if operation == "regenerate":
            results.append(self.process("regenerate", source_refs=source_refs))
        return {"status": "complete", "results": results,
                "semantic_correctness": "not_attested"}


def _client_config(filename, user_id):
    # The only file read is the explicitly supplied credential configuration.
    config = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
    if "users" in config:
        users = config["users"]
        if user_id is None:
            if len(users) != 1:
                raise ValueError("Select one explicit user for the maintenance scope")
            user_id = next(iter(users))
        entry = users[user_id]
        return CanonicalEpisodeClient(config["url"], entry["token"])
    if user_id is not None:
        raise ValueError("A standalone credential file does not select users")
    return CanonicalEpisodeClient(config["url"], config["token"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-file", required=True)
    parser.add_argument("--user-id")
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--context-key", default="conversation")
    parser.add_argument("--sandbox-id")
    parser.add_argument("--model-url", required=True, help="Explicit OpenAI-compatible /v1 endpoint")
    parser.add_argument("--model", required=True)
    parser.add_argument("--model-api-key-env", help="Name of the environment variable containing a provider key")
    parser.add_argument("--model-timeout", type=float, default=120.0)
    parser.add_argument("--max-output-tokens", type=int, default=8192)
    parser.add_argument("--operation", choices=["all", "provisional", "daily", "regenerate"], default="all")
    parser.add_argument("--day", help="Explicit prior processing day for a manual daily replay")
    parser.add_argument("--source-ref", action="append", default=[], help="Versioned regeneration source ID:VERSION")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--loop", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if args.poll_seconds < 1 or args.poll_seconds > 3600:
        parser.error("poll-seconds must be between 1 and 3600")
    if args.loop and (args.day or args.operation == "regenerate"):
        parser.error("Explicit day and regeneration runs require --once")
    if args.operation == "regenerate" and not args.source_ref:
        parser.error("Regeneration requires explicit versioned sources")
    if args.source_ref and args.operation != "regenerate":
        parser.error("source-ref is only for regeneration")
    if args.day and args.operation not in {"all", "daily"}:
        parser.error("day is only for daily organization")
    try:
        source_refs = []
        for value in args.source_ref:
            identity, version = value.rsplit(":", 1)
            source_refs.append({"id": identity, "version": int(version)})
        api_key = os.environ.get(args.model_api_key_env) if args.model_api_key_env else None
        if args.model_api_key_env and not api_key:
            raise ValueError("Configured model key environment variable is unset")
        generator = EpisodeGenerator(ModelConfig(args.model_url, args.model,
            api_key=api_key, timeout=args.model_timeout, max_tokens=args.max_output_tokens))
        worker = EpisodeWorker(_client_config(args.client_file, args.user_id), generator,
            {"conversation_id": args.conversation_id, "context_key": args.context_key,
             "sandbox_id": args.sandbox_id})
    except (KeyError, TypeError, ValueError, OSError):
        print(json.dumps({"status": "failed", "code": "configuration_invalid"}))
        return 2
    while True:
        try:
            result = worker.run_once(operation=args.operation, day=args.day, source_refs=source_refs)
        except (GenerationError, TransportError) as exc:
            result = {"status": "failed", "code": exc.code if isinstance(exc, TransportError) else str(exc)}
            if isinstance(exc, TransportError) and exc.status is not None:
                result["http_status"] = exc.status
        except Exception:
            # Do not stringify validation exceptions; they can contain source text.
            result = {"status": "failed", "code": "worker_failure"}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.once:
            return 0 if result["status"] == "complete" else 1
        # Auth and scope failures require operator action, not repeated provider calls.
        if result.get("http_status") in {401, 403}:
            return 1
        try:
            time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
