from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"


def _keep_alive_value(value: str) -> str | int:
    normalized = value.strip()
    try:
        return int(normalized)
    except ValueError:
        return normalized


class StructuredSLMClient:
    """Small structured-output client for a local Ollama server."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        keep_alive: str | int | None = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("HIPPOCAMPUS_SLM_BASE_URL")
            or DEFAULT_OLLAMA_BASE_URL
        ).rstrip("/")
        self.model = model or os.getenv("HIPPOCAMPUS_SLM_MODEL") or "qwen3.5:4b"
        self.timeout = int(timeout or os.getenv("HIPPOCAMPUS_SLM_TIMEOUT_SECONDS", "180"))
        configured_keep_alive = str(
            keep_alive
            if keep_alive is not None
            else os.getenv("HIPPOCAMPUS_SLM_KEEP_ALIVE", "-1")
        )
        self.keep_alive = _keep_alive_value(configured_keep_alive)

    def _request(
        self,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            data=(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
                if payload is not None
                else None
            ),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST" if payload is not None else "GET",
        )
        try:
            raw = urllib.request.urlopen(request, timeout=timeout or self.timeout).read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Ollama is unavailable at {self.base_url}: {exc.reason}") from exc
        return json.loads(raw.decode("utf-8"))

    def structured_chat(
        self,
        *,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        temperature: float = 0.0,
        max_tokens: int = 1200,
    ) -> dict[str, Any]:
        started = time.monotonic()
        response = self._request(
            "/api/chat",
            payload={
                "model": self.model,
                "messages": messages,
                "stream": False,
                "format": schema,
                "think": False,
                "keep_alive": self.keep_alive,
                "options": {
                    "temperature": temperature,
                    "num_predict": max_tokens,
                },
            },
        )
        content = str((response.get("message") or {}).get("content") or "")
        return {
            "content": content,
            "model": str(response.get("model") or self.model),
            "provider": "ollama",
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
            "load_duration_ms": round(float(response.get("load_duration") or 0) / 1_000_000, 3),
            "prompt_eval_count": int(response.get("prompt_eval_count") or 0),
            "eval_count": int(response.get("eval_count") or 0),
        }

    def status(self) -> dict[str, Any]:
        try:
            version = self._request("/api/version", timeout=5)
            tags = self._request("/api/tags", timeout=10)
            running = self._request("/api/ps", timeout=10)
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            return {
                "provider": "ollama",
                "available": False,
                "base_url": self.base_url,
                "model": self.model,
                "configured_keep_alive": self.keep_alive,
                "error": str(exc),
            }
        local_models = [str(item.get("name") or item.get("model") or "") for item in tags.get("models") or []]
        loaded_models = [str(item.get("name") or item.get("model") or "") for item in running.get("models") or []]
        return {
            "provider": "ollama",
            "available": True,
            "base_url": self.base_url,
            "version": version.get("version"),
            "model": self.model,
            "model_installed": self.model in local_models,
            "model_loaded": self.model in loaded_models,
            "configured_keep_alive": self.keep_alive,
            "local_models": local_models,
            "loaded_models": loaded_models,
        }

    def preload(self) -> dict[str, Any]:
        started = time.monotonic()
        response = self._request(
            "/api/generate",
            payload={
                "model": self.model,
                "prompt": "",
                "stream": False,
                "keep_alive": self.keep_alive,
            },
        )
        return {
            "provider": "ollama",
            "model": str(response.get("model") or self.model),
            "loaded": True,
            "keep_alive": self.keep_alive,
            "duration_ms": round((time.monotonic() - started) * 1000.0, 3),
            "load_duration_ms": round(float(response.get("load_duration") or 0) / 1_000_000, 3),
        }
