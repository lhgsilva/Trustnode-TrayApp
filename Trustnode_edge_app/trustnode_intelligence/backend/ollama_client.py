"""Thin HTTP client for the configured AI endpoint.

We target Ollama's OpenAI-compatible chat completions endpoint
(/v1/chat/completions). The same client works against:
  - Ollama (default, free, self-hosted)
  - vLLM running an OpenAI-compatible server
  - OpenAI API (drop in api.openai.com + your key)
  - Anthropic via a small adapter (TODO future)

We use httpx synchronously for simplicity. The router wraps calls in
asyncio.to_thread so the event loop isn't blocked.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import httpx

_log = logging.getLogger("trustnode.intelligence.ai")


class AIBackendError(Exception):
    """User-safe AI backend error.

    The exception message is deliberately generic (no provider name, no URL,
    no raw response body). The full details are written to the admin log for
    diagnostics but never surfaced to the customer-facing chat.
    """

    def __init__(self, user_message: str, *, http_status: int | None = None):
        super().__init__(user_message)
        self.http_status = http_status


def _bucket_http_error(status: int, body: str = "") -> str:
    body_l = (body or "").lower()
    # Distinguish "quota exhausted" from generic rate-limit — both are 429
    # upstream, but the customer action differs. Quota-exhausted needs the
    # admin to raise the limit; rate-limit is transient. We never leak the
    # provider name — the message is provider-agnostic on purpose.
    if status == 429:
        if "insufficient_quota" in body_l or "quota" in body_l or "billing" in body_l:
            return (
                "The AI assistant has reached its usage quota. "
                "Please contact your administrator to raise the limit."
            )
        return "The AI assistant is temporarily rate-limited. Please try again in a moment."
    if status in (401, 403):
        return "The AI assistant is not authorised. Please contact your administrator."
    if status == 404:
        return "The AI assistant is temporarily unavailable. Please contact your administrator."
    if 500 <= status <= 599:
        return "The AI assistant is temporarily unavailable. Please try again shortly."
    return "The AI assistant is temporarily unavailable. Please contact your administrator."


class OllamaClient:
    def __init__(self, endpoint_url: str, model: str, auth_token: str = "",
                 timeout_s: float = 60.0):
        # Operator 2026-07-02: hard cap at 60s. Beyond this, an unresponsive
        # AI upstream (Ollama loading a big model, OpenAI degraded) will
        # tie up the FastAPI threadpool and wedge ALL other Intelligence
        # endpoints (delete-chat, list-chats, status). 60s is generous for
        # a chat completion; if it takes longer, we bail with a friendly
        # "temporarily unavailable" message and free the thread.
        self.endpoint_url = endpoint_url.rstrip("/")
        self.model = model
        self.auth_token = auth_token
        self.timeout_s = timeout_s

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.auth_token:
            h["Authorization"] = f"Bearer {self.auth_token}"
        return h

    def chat(self, messages: List[Dict[str, Any]],
             tools: Optional[List[Dict[str, Any]]] = None,
             temperature: float = 0.0,
             max_tokens: int = 2048) -> Dict[str, Any]:
        # Operator 2026-06-30: default temperature=0 for deterministic
        # tool-call selection. With 0.2 the model occasionally retried
        # the same call with slightly-different args (token waste).
        # Engineering Q&A wants deterministic + reproducible anyway —
        # there is no creative-writing use case here.
        """Single non-streaming chat completion. Returns OpenAI-shape response.
        Caller is responsible for the tool-call loop."""
        url = f"{self.endpoint_url}/v1/chat/completions"
        body: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            resp = httpx.post(url, json=body, headers=self._headers(), timeout=self.timeout_s)
        except httpx.HTTPError as exc:
            _log.warning("AI endpoint unreachable: %s", exc)
            raise AIBackendError(
                "The AI assistant is temporarily unavailable. Please try again shortly."
            ) from exc
        if resp.status_code >= 400:
            _log.warning(
                "AI endpoint HTTP %s from %s (model=%s): %s",
                resp.status_code, url, self.model, resp.text[:1000],
            )
            raise AIBackendError(
                _bucket_http_error(resp.status_code, resp.text),
                http_status=resp.status_code,
            )
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            _log.warning("AI endpoint returned invalid JSON: %s", exc)
            raise AIBackendError(
                "The AI assistant returned an unexpected response. Please try again shortly."
            ) from exc
