"""OpenRouter client.

Every call is logged to `model_calls` before it returns: request, response,
tokens, cost, latency, seed. Two reasons this is not optional here - the
prompt-sensitivity band needs to re-run identical requests under varied
instructions, and the released dataset needs to show exactly what was asked.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .. import config
from ..db import insert


class ModelError(RuntimeError):
    pass


@dataclass
class ModelSpec:
    """A pinned model + sampling parameters. Pinned means the string includes
    a dated or versioned id wherever the provider offers one."""

    model: str
    temperature: float = 1.0
    top_p: float = 1.0
    max_tokens: int = 700
    seed: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        if self.seed is not None:
            body["seed"] = self.seed
        body.update(self.extra)
        return body


@dataclass
class Completion:
    content: str
    model: str
    call_id: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cost_usd: float | None
    latency_ms: int
    raw: dict[str, Any]


def cache_key(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class OpenRouter:
    def __init__(self, api_key: str | None = None, timeout: float = 120.0):
        self.api_key = api_key or config.OPENROUTER_API_KEY
        self._timeout = timeout

    @property
    def headers(self) -> dict[str, str]:
        if not self.api_key:
            raise ModelError("OPENROUTER_API_KEY is not set (see .env.example)")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": config.OPENROUTER_APP_URL,
            "X-Title": config.OPENROUTER_APP_TITLE,
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------- calling

    async def complete(
        self,
        spec: ModelSpec,
        messages: list[dict[str, str]],
        *,
        conn: sqlite3.Connection | None = None,
        purpose: str = "turn",
        debate_id: int | None = None,
        agent_config_id: int | None = None,
        attempts: int = 4,
    ) -> Completion:
        payload = spec.payload(messages)
        key = cache_key(payload)
        started = time.perf_counter()
        last_error: Exception | None = None
        data: dict[str, Any] = {}

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for attempt in range(attempts):
                try:
                    resp = await client.post(
                        f"{config.OPENROUTER_BASE}/chat/completions",
                        headers=self.headers,
                        json=payload,
                    )
                    if resp.status_code in (429, 500, 502, 503, 504):
                        raise ModelError(f"{resp.status_code}: {resp.text[:400]}")
                    resp.raise_for_status()
                    data = resp.json()
                    if "error" in data:
                        raise ModelError(str(data["error"])[:400])
                    break
                except Exception as exc:  # noqa: BLE001 - retried below
                    last_error = exc
                    if attempt == attempts - 1:
                        self._log(
                            conn, purpose, spec, payload, None, key, debate_id,
                            agent_config_id, int((time.perf_counter() - started) * 1000),
                            error=repr(exc),
                        )
                        raise ModelError(f"OpenRouter call failed: {exc}") from exc
                    time.sleep(1.5 * (2**attempt))

        latency_ms = int((time.perf_counter() - started) * 1000)
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError) as exc:
            raise ModelError(f"Unexpected response shape: {json.dumps(data)[:400]}") from exc

        usage = data.get("usage") or {}
        call_id = self._log(
            conn, purpose, spec, payload, data, key, debate_id, agent_config_id,
            latency_ms, content=content,
        )
        return Completion(
            content=content.strip(),
            model=data.get("model", spec.model),
            call_id=call_id,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cost_usd=usage.get("cost"),
            latency_ms=latency_ms,
            raw=data,
        )

    # ------------------------------------------------------------- logging

    @staticmethod
    def _log(
        conn: sqlite3.Connection | None,
        purpose: str,
        spec: ModelSpec,
        payload: dict[str, Any],
        response: dict[str, Any] | None,
        key: str,
        debate_id: int | None,
        agent_config_id: int | None,
        latency_ms: int,
        content: str | None = None,
        error: str | None = None,
    ) -> int | None:
        if conn is None:
            return None
        usage = (response or {}).get("usage") or {}
        return insert(
            conn,
            "model_calls",
            debate_id=debate_id,
            agent_config_id=agent_config_id,
            purpose=purpose,
            model=spec.model,
            request_json=json.dumps(payload, ensure_ascii=False),
            response_json=json.dumps(response, ensure_ascii=False) if response else None,
            content=content,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            cost_usd=usage.get("cost"),
            latency_ms=latency_ms,
            seed=spec.seed,
            cache_key=key,
            error=error,
        )
