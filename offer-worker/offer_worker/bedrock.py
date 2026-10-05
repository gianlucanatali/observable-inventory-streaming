"""Bedrock text lane: one short descriptive line for FIXED terms. The structured card owns every fact.

Uses the Bedrock Runtime Converse API (boto3 `bedrock-runtime`, `converse`), credentials from the instance role.
Before the first run, confirm model access in the account, region availability of BEDROCK_MODEL_ID in eu-west-1 and expected latency.
"""
from __future__ import annotations

import re
from typing import Protocol

from ddtrace import tracer

MAX_CHARS = 200
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_FORBIDDEN = re.compile(r"[€$£%]|\b(?:eur|euro|euros|usd|dollars?|free|gratis|save|discount(?:ed)?|off|code|coupon|voucher)\b", re.I)


class TextError(Exception):
    reason = "error"


class TextTimeout(TextError):
    reason = "timeout"


class TextWriter(Protocol):
    def write(self, facts: str) -> str: ...


def allowed_numbers(*texts: str) -> set[str]:
    """Numbers the text may repeat: those that appear in the product facts (e.g. 'EU 42'). Never prices or discounts."""
    out: set[str] = set()
    for t in texts:
        out.update(_NUMBER.findall(t))
    return out


def validate_text(text: str, allowed: set[str]) -> str | None:
    """Return None if acceptable, else a short reject cause (logged; the metric label is just invalid_text)."""
    t = text.strip()
    if not t:
        return "empty"
    if "\n" in t or len(t) > MAX_CHARS:
        return "not_one_short_line"
    if _FORBIDDEN.search(t):
        return "money_or_discount_wording"
    extra = [n for n in _NUMBER.findall(t) if n not in allowed]
    if extra:
        return f"numbers_not_in_facts:{','.join(extra)}"
    return None


SYSTEM = ("You write one short, friendly sentence (max 25 words) for a shop notification. Describe only what the facts say. "
          "Never mention prices, discounts, percentages, numbers, currencies, stock quantities or deadlines. "
          "Do not invent product features. Output the sentence only.")


class BedrockWriter:
    def __init__(self, region: str, model_id: str, timeout_ms: int, client=None):
        if client is None:
            import boto3
            from botocore.config import Config
            t = timeout_ms / 1000.0
            client = boto3.client("bedrock-runtime", region_name=region,
                                  config=Config(connect_timeout=t, read_timeout=t, retries={"max_attempts": 1}))
        self._c = client
        self._model = model_id

    def write(self, facts: str) -> str:
        with tracer.trace("offer.bedrock.call", service="bedrock", resource="converse", span_type="http") as span:
            span.set_tag("bedrock.model_id", self._model)
            try:
                r = self._c.converse(
                    modelId=self._model, system=[{"text": SYSTEM}],
                    messages=[{"role": "user", "content": [{"text": facts}]}],
                    inferenceConfig={"maxTokens": 80, "temperature": 0.2},
                )
                return r["output"]["message"]["content"][0]["text"]
            except (KeyError, IndexError, TypeError) as e:
                raise TextError(f"unexpected Bedrock response shape: {type(e).__name__}: {e}") from e
            except Exception as e:  # botocore ClientError, ReadTimeoutError, EndpointConnectionError, ...
                name = type(e).__name__
                if "Timeout" in name:
                    raise TextTimeout(f"Bedrock call timed out: {e}") from e
                raise TextError(f"Bedrock call failed: {name}: {e}") from e
