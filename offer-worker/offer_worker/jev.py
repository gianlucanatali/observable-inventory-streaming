"""Jev (TypeSafe AI) decision client behind a small interface.

The request shape is compatible with TypeSafe's official Choice/API docs:
``state`` and ``model`` are top-level fields, and ``questions`` maps a caller-
chosen id to a typed question whose Choice ``criteria`` maps option ids to
descriptions. The request shape is covered by the public API documentation;
live account access and response behaviour still need a deployment check.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import httpx
from ddtrace import tracer
from ddtrace.llmobs import LLMObs
from ddtrace.llmobs.decorators import llm

QUESTION = "offer"


class JevError(Exception):
    """Base. `reason` is the bounded decision_reason label."""
    reason = "error"


class JevTimeout(JevError):
    reason = "timeout"


class JevRateLimited(JevError):
    reason = "rate_limited"


@dataclass(frozen=True)
class JevChoice:
    choice: str
    confidence: float
    probabilities: dict[str, float] | None = None


class JevClient(Protocol):
    def choose(self, state: str, instructions: str, criteria: dict[str, str]) -> JevChoice: ...


class HttpJevClient:
    def __init__(self, url: str, api_key: str, model: str, timeout_ms: int, client: httpx.Client | None = None):
        self._url = url
        self._model = model
        # httpx applies this to connect, read, write and pool separately (not one overall deadline): worst case a
        # few times the value, typically one round trip.
        self.timeout_ms = timeout_ms  # the worker updates this from demo:config before each call
        self._http = client or httpx.Client()
        self._headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    @llm(model_name="jev-latest", model_provider="typesafe", name="offer.jev.call")
    def choose(self, state: str, instructions: str, criteria: dict[str, str]) -> JevChoice:
        body = {"state": state, "model": self._model,
                "questions": {QUESTION: {"type": "choice", "instructions": instructions, "criteria": criteria}}}
        with tracer.trace("offer.jev.call", service="jev", resource="POST systemone", span_type="http") as span:
            span.set_tag("jev.options", len(criteria))
            try:
                r = self._http.post(self._url, json=body, headers=self._headers,
                                    timeout=httpx.Timeout(self.timeout_ms / 1000.0))
            except httpx.TimeoutException as e:
                raise JevTimeout(f"Jev call timed out: {e}") from e
            except httpx.HTTPError as e:
                raise JevError(f"Jev call failed: {type(e).__name__}: {e}") from e
            span.set_tag("http.status_code", r.status_code)
            if r.status_code == 429:
                raise JevRateLimited("Jev answered 429")
            if r.status_code != 200:
                raise JevError(f"Jev answered HTTP {r.status_code}")
            try:
                ans = r.json()["answers"][QUESTION]
                choice, conf = ans["choice"], float(ans["confidence"])
            except (ValueError, KeyError, TypeError) as e:
                raise JevError(f"Jev response has an unexpected shape ({type(e).__name__}: {e}); check the Jev deployment assumptions in jev.py") from e
            raw_probabilities = ans.get("probabilities", {})
            probabilities = {
                option: float(value) for option, value in raw_probabilities.items()
                if option in criteria and not isinstance(value, bool) and isinstance(value, (int, float))
            } if isinstance(raw_probabilities, dict) else {}
            if LLMObs.enabled:
                LLMObs.annotate(
                    input_data=[{"role": "user", "content": json.dumps(
                        {"state": state, "instructions": instructions, "criteria": criteria}, sort_keys=True)}],
                    output_data=[{"role": "assistant", "content": json.dumps(
                        {"choice": choice, "confidence": conf, "probabilities": probabilities}, sort_keys=True)}],
                )
            span.set_tag("jev.confidence", conf)
            return JevChoice(choice, conf, probabilities)
