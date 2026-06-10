"""Anthropic Messages API client for TradeRay agents.

Capabilities:
  - Async, single shared client (`AsyncAnthropic`).
  - Adaptive thinking for Claude Opus 4.7 reasoning depth.
  - Multi-modal user payloads — accepts plain text OR a list of content blocks
    (e.g. the image+text array produced by `vision_utils.build_vision_message`).
  - System-prompt prompt caching (the agent persona is frozen across the
    trading session; cache_control=ephemeral keeps the cost flat).
  - Robust JSON extraction from outputs that mix `<thinking>...</thinking>`
    reasoning with `<output>{...}</output>` JSON, code fences, or stray prose.
  - Tenacity-backed retry on transient API failures (timeouts, 429, 5xx).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import tenacity
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    RateLimitError,
)

from config import settings
from core.logger import get_logger

log = get_logger(__name__)
_retry_logger = logging.getLogger("traderay.llm.retry")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LLMResponseError(Exception):
    """Model returned content we could not parse into the expected JSON shape."""


# ---------------------------------------------------------------------------
# Singleton client
# ---------------------------------------------------------------------------

_client: AsyncAnthropic | None = None


def get_client() -> AsyncAnthropic:
    """Return the process-wide AsyncAnthropic.

    `max_retries=0` so tenacity owns the retry policy with our logging /
    backoff — otherwise the SDK would retry silently and we'd lose visibility.
    """
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key, max_retries=0)
    return _client


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

# Preferred path: model emitted <output>{...}</output> per our prompt contract.
_OUTPUT_TAG_RE = re.compile(r"<output>\s*(\{[\s\S]*?\})\s*</output>", re.IGNORECASE)

# Fallback: greedy {...} grab — used only if the model dropped the <output> tags.
_FALLBACK_JSON_RE = re.compile(r"\{[\s\S]*\}")

# Strip the <thinking> reasoning block first so we don't accidentally parse
# JSON that the model wrote inside its reasoning ("if I returned {...}").
_THINKING_RE = re.compile(r"<thinking>[\s\S]*?</thinking>", re.IGNORECASE)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the trade-decision JSON out of a model response.

    Order of attempts:
      1. Match `<output>{...}</output>` (our prompt contract).
      2. Strip <thinking>...</thinking> reasoning, then grab the largest {...}.
      3. Strip ```json fences if present.

    Raises LLMResponseError if no parseable JSON object can be recovered.
    """
    if not text or not text.strip():
        raise LLMResponseError("empty model response")

    # Attempt 1: explicit <output> tag (the contract we ask for in prompts.py)
    m = _OUTPUT_TAG_RE.search(text)
    if m:
        candidate = m.group(1)
    else:
        # Attempt 2: kill thinking block, then greedy match
        cleaned = _THINKING_RE.sub("", text).strip()
        m = _FALLBACK_JSON_RE.search(cleaned)
        if not m:
            raise LLMResponseError(
                f"no JSON object found in response (first 300 chars): {text[:300]!r}"
            )
        candidate = m.group(0)

    # Attempt 3: strip ``` fences
    candidate = candidate.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`").strip()
        if candidate.lower().startswith("json"):
            candidate = candidate[4:].lstrip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError as e:
        raise LLMResponseError(
            f"JSON parse failed: {e.msg} at pos {e.pos} — raw: {candidate[:300]!r}"
        ) from e


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

def _is_retryable(exc: BaseException) -> bool:
    """Retry transient failures only — not bad-request / auth / quota errors."""
    if isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError)):
        return True
    if isinstance(exc, APIStatusError):
        # 5xx server errors and explicit 429 rate limits (RateLimitError covers
        # most cases, but defensive check here too).
        return exc.status_code >= 500 or exc.status_code == 429
    return False


_retry_decorator = tenacity.retry(
    stop=tenacity.stop_after_attempt(4),
    wait=tenacity.wait_exponential(multiplier=1, min=2, max=30),
    retry=tenacity.retry_if_exception(_is_retryable),
    before_sleep=tenacity.before_sleep_log(_retry_logger, logging.WARNING),
    reraise=True,
)


# ---------------------------------------------------------------------------
# The one entrypoint agents use
# ---------------------------------------------------------------------------

# Models that support `thinking: {"type": "adaptive"}`. Haiku 4.5 does NOT —
# sending the param there 400s, so we omit thinking entirely for it.
_ADAPTIVE_THINKING_PREFIXES: tuple[str, ...] = (
    "claude-opus-4-6",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-fable",
)


def _supports_adaptive_thinking(model: str) -> bool:
    return model.startswith(_ADAPTIVE_THINKING_PREFIXES)


@_retry_decorator
async def call_agent(
    *,
    system_prompt: str,
    user_content: str | list[dict[str, Any]],
    max_tokens: int = 2500,
    cache_system: bool = True,
    label: str = "agent",
    model: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Call Anthropic's Messages API and return BOTH the parsed JSON
    decision AND a usage dict (for cost tracking).

    Args:
        system_prompt: The frozen agent persona (will be prompt-cached).
        user_content: Either a plain string (single text block) OR a list
            of content blocks — typically the output of
            `vision_utils.build_vision_message(...)` containing one image
            block followed by one text block.
        max_tokens: Cap on output tokens.
        cache_system: If True, system block carries cache_control=ephemeral.
        label: Free-text tag for logging / observability (e.g. "quant:BTCUSDT").
        model: Override model id for this call (per-agent routing). Falls
            back to settings.anthropic_model when None.

    Returns:
        A tuple `(parsed_json, usage_dict)` where `usage_dict` contains:
            input_tokens, output_tokens, cache_read_input_tokens,
            cache_creation_input_tokens, model, stop_reason.

        Callers that want to ignore usage can do:
            parsed, _ = await call_agent(...)

    Raises:
        LLMResponseError: model output could not be parsed.
        anthropic.APIError (subclasses): unrecoverable API failures.
    """
    client = get_client()

    system_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": system_prompt,
            **({"cache_control": {"type": "ephemeral"}} if cache_system else {}),
        }
    ]

    # Normalize content: string → single text block; list → use as-is.
    if isinstance(user_content, str):
        message_content: list[dict[str, Any]] = [
            {"type": "text", "text": user_content}
        ]
    else:
        message_content = user_content

    model_id = model or settings.anthropic_model

    request_kwargs: dict[str, Any] = {
        "model": model_id,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": [{"role": "user", "content": message_content}],
    }
    if _supports_adaptive_thinking(model_id):
        request_kwargs["thinking"] = {"type": "adaptive"}

    response = await client.messages.create(**request_kwargs)

    # Concatenate all visible text blocks (we ignore Anthropic's internal
    # `thinking` blocks — the model's own <thinking> in plain text is what
    # our prompt asks for and what we strip in extract_json).
    text_parts = [b.text for b in response.content if b.type == "text"]
    raw = "\n".join(text_parts).strip()

    # Build the usage dict BEFORE attempting to parse — token costs are real
    # even when the model's output is malformed. We log + return this regardless.
    usage = response.usage
    usage_dict: dict[str, Any] = {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_input_tokens": int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        ),
        "cache_creation_input_tokens": int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
        "model": model_id,
        "stop_reason": response.stop_reason,
    }

    log.info(
        "llm.call",
        label=label,
        model=usage_dict["model"],
        input_tokens=usage_dict["input_tokens"],
        output_tokens=usage_dict["output_tokens"],
        cache_read=usage_dict["cache_read_input_tokens"],
        cache_write=usage_dict["cache_creation_input_tokens"],
        stop_reason=usage_dict["stop_reason"],
    )

    try:
        parsed = extract_json(raw)
    except LLMResponseError as e:
        log.error("llm.parse_failed", label=label, err=str(e), preview=raw[:300])
        raise

    return parsed, usage_dict


__all__ = ["call_agent", "extract_json", "get_client", "LLMResponseError"]
