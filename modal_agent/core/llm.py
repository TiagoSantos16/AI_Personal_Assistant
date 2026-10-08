import base64
import logging
import os
import re
import time
import random
from functools import lru_cache
from contextvars import ContextVar

from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

from langchain_openai import ChatOpenAI

from core.config import OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 2
RETRYABLE_STATUS = {408, 500, 502, 503, 504}
NETWORK_ERROR_TYPES = (ConnectionError, TimeoutError)
NETWORK_ERROR_NAMES = {"APIConnectionError", "APITimeoutError", "RemoteProtocolError"}
NETWORK_HINTS = ("timed out", "timeout", "connection reset", "connection aborted", "temporary failure in name resolution")

_STATUS_RE = re.compile(r"Error code: (\d+)")


class ProviderChainError(Exception):
    """Raised when every model in a chain fails, keeping one error per model."""

    def __init__(self, failures: list[tuple[str, Exception]]):
        self.failures = failures
        lines = [f"{model}: {err}" for model, err in failures]
        super().__init__("All AI models failed:\n" + "\n".join(lines))


QUOTA_STATUSES = {402, 429}

_RESET_HEADER_RE = re.compile(r"x-ratelimit-reset['\"]?\s*:\s*['\"]?(\d{10,13})", re.IGNORECASE)


def _epoch_to_seconds(raw: str) -> float | None:
    """Unix epoch (seconds or milliseconds) -> seconds from now."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    if value > 10_000_000_000:  # millisecond epochs are ~1.7e12 today
        value = value / 1000
    seconds = value - time.time()
    return seconds if seconds > 0 else None


def _retry_after_seconds(raw: str) -> float | None:
    """Retry-After header: delay-seconds or HTTP-date."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.isdigit():
        seconds = int(raw)
        return seconds if seconds > 0 else None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (dt - datetime.now(timezone.utc)).total_seconds()
    return seconds if seconds > 0 else None


def _extract_reset_seconds(exc: Exception) -> float | None:
    """Seconds until OpenRouter says the rate limit resets, or None if unknown.

    Checks Retry-After, X-RateLimit-Reset (epoch ms or s), and the error body
    (where the SDK often embeds the metadata headers dict as text).
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        get = headers.get if hasattr(headers, "get") else lambda *_: None
        seconds = _retry_after_seconds(get("Retry-After") or get("retry-after") or "")
        if seconds is not None:
            return seconds
        for name in ("X-RateLimit-Reset", "x-ratelimit-reset"):
            seconds = _epoch_to_seconds(get(name) or "")
            if seconds is not None:
                return seconds

    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        metadata = body.get("error", {}).get("metadata", {}) if isinstance(body.get("error"), dict) else {}
        meta_headers = metadata.get("headers", {}) if isinstance(metadata, dict) else {}
        if isinstance(meta_headers, dict):
            for name in ("X-RateLimit-Reset", "x-ratelimit-reset"):
                seconds = _epoch_to_seconds(str(meta_headers.get(name) or ""))
                if seconds is not None:
                    return seconds

    match = _RESET_HEADER_RE.search(str(exc))
    if match:
        return _epoch_to_seconds(match.group(1))
    return None


def _is_quota_failure(exc: Exception) -> bool:
    status = _status_of(exc)
    if status in QUOTA_STATUSES:
        return True
    if status is None:
        lowered = str(exc).lower()
        return "rate limit" in lowered or "rate_limit" in lowered
    return False


def is_quota_error(exc: Exception) -> bool:
    """True when the failure is an out-of-credits / rate-limit condition."""
    if isinstance(exc, ProviderChainError):
        return bool(exc.failures) and all(_is_quota_failure(err) for _, err in exc.failures)
    return _is_quota_failure(exc)


def quota_reset_seconds(exc: Exception) -> float | None:
    """Earliest known reset across the failure(s), in seconds from now."""
    if isinstance(exc, ProviderChainError):
        candidates = [
            s for s in (_extract_reset_seconds(err) for _, err in exc.failures) if s is not None
        ]
        return min(candidates) if candidates else None
    return _extract_reset_seconds(exc)


def _status_of(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    match = _STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def is_retryable(exc: Exception) -> bool:
    status = _status_of(exc)
    if status == 429:
        # Shared-pool congestion clears in seconds; a daily cap has a reset
        # timestamp hours away and is pointless to retry within this call.
        if any(word in str(exc).lower() for word in ("daily", "quota", "credits", "per day")):
            return False
        reset = _extract_reset_seconds(exc)
        return reset is not None and reset <= 10
    if status is not None:
        return status in RETRYABLE_STATUS
    if isinstance(exc, NETWORK_ERROR_TYPES) or exc.__class__.__name__ in NETWORK_ERROR_NAMES:
        return True
    lowered = str(exc).lower()
    return any(hint in lowered for hint in NETWORK_HINTS)


CALL_CONTEXT = ContextVar("call_context", default=None)


def response_text(response):
    value = response.content
    if isinstance(value, list):
        value = "\n".join(block.get("text", "") for block in value if isinstance(block, dict))
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Empty model response")
    if response.response_metadata.get("finish_reason") in {"length", "max_tokens"}:
        raise ValueError("Truncated model response")
    return value.strip()


def invoke_with_retry(llms: list[ChatOpenAI], content, *, step="generation", output=1200, deadline=None, structured=False):
    from core.accounting import record
    context = CALL_CONTEXT.get() or {}
    deadline = deadline or context.get("deadline") or time.monotonic() + 180
    failures: list[tuple[str, Exception]] = []
    for llm in llms:
        for attempt in range(RETRY_ATTEMPTS):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Generation deadline exhausted")
            started = time.monotonic()
            payload = {"model": llm.model_name}
            try:
                # SDK request kwargs override the cached client's defaults too.
                options = {"timeout": min(float(os.environ.get("GENERATIVE_TIMEOUT_SECONDS", "45")), remaining), "max_tokens": output}
                if structured:
                    options["response_format"] = {"type": "json_object"}
                response = llm.invoke(content, **options)
                payload.update(response.response_metadata.get("accounting", {}))
                response_text(response)
                if context.get("record"):
                    context["record"](record(payload, step, time.monotonic() - started))
                return response
            except Exception as exc:
                body = getattr(exc, "body", None)
                if isinstance(body, dict):
                    payload.update({key: body[key] for key in ("id", "model", "provider", "usage") if key in body})
                if context.get("record"):
                    context["record"](record(payload, step, time.monotonic() - started, False))
                failures.append((llm.model_name, exc))
                if not is_retryable(exc) or attempt + 1 == RETRY_ATTEMPTS:
                    break
                delay = min(10, _extract_reset_seconds(exc) or 1) + random.uniform(0, .3)
                if delay + .1 >= deadline - time.monotonic():
                    break
                time.sleep(delay)
    raise ProviderChainError(failures)


def build_vision_messages(prompt: str, image_paths: list[str]) -> list[dict]:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        encoded = _encoded(path, os.path.getmtime(path))
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
    return [{"role": "user", "content": content}]


@lru_cache(maxsize=64)
def _encoded(path, modified):
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode()


class AccountingChatOpenAI(ChatOpenAI):
    """Keep OpenRouter charge metadata otherwise dropped by LangChain."""
    def _create_chat_result(self, response, generation_info=None):
        payload = response if isinstance(response, dict) else response.model_dump()
        result = super()._create_chat_result(response, generation_info)
        compact = {key: payload.get(key) for key in ("id", "model", "provider", "usage")}
        for generation in result.generations:
            generation.message.response_metadata["accounting"] = compact
        return result


@lru_cache(maxsize=16)
def _chat_model(model: str) -> ChatOpenAI:
    return AccountingChatOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.environ["OPENROUTER_API_KEY"],
        model=model,
        temperature=0.3,
        timeout=45,
        max_retries=0,
        max_tokens=1200,
    )


def get_llm(model_chain: list[str]) -> list[ChatOpenAI]:
    """One client per candidate model, tried in order by invoke_with_retry."""
    return [_chat_model(m) for m in model_chain]
