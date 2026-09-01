import base64
import logging
import os
import re
import time

from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

from langchain_openai import ChatOpenAI

from core.config import OPENROUTER_BASE_URL

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 2.0
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
        reset = _extract_reset_seconds(exc)
        return reset is None or reset <= 30
    if status is not None:
        return status in RETRYABLE_STATUS
    if isinstance(exc, NETWORK_ERROR_TYPES) or exc.__class__.__name__ in NETWORK_ERROR_NAMES:
        return True
    lowered = str(exc).lower()
    return any(hint in lowered for hint in NETWORK_HINTS)


def _retry_delay(failures: list[tuple[str, Exception]], attempt: int) -> float:
    hints = [
        s
        for _, exc in failures
        if _status_of(exc) == 429
        for s in [_extract_reset_seconds(exc)]
        if s is not None and s <= 30
    ]
    if hints:
        return max(1.0, min(hints) + 1)
    return RETRY_BASE_DELAY * (2 ** (attempt - 1))


def invoke_with_retry(llms: list[ChatOpenAI], content):
    failures: list[tuple[str, Exception]] = []
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        failures = []
        for llm in llms:
            try:
                return llm.invoke(content)
            except Exception as exc:
                failures.append((llm.model_name, exc))
        if not any(is_retryable(exc) for _, exc in failures) or attempt == RETRY_ATTEMPTS:
            break
        delay = _retry_delay(failures, attempt)
        logger.warning(
            f"All {len(llms)} models failed; retrying in {delay:.0f}s (attempt {attempt}/{RETRY_ATTEMPTS})"
        )
        time.sleep(delay)
    raise ProviderChainError(failures)


def build_vision_messages(prompt: str, image_paths: list[str]) -> list[dict]:
    content: list[dict] = [{"type": "text", "text": prompt}]
    for path in image_paths:
        with open(path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{encoded}"}})
    return [{"role": "user", "content": content}]


def _chat_model(model: str) -> ChatOpenAI:
    return ChatOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.environ["OPENROUTER_API_KEY"],
        model=model,
        temperature=0.3,
        timeout=60,
    )


def get_llm(model_chain: list[str]) -> list[ChatOpenAI]:
    """One client per candidate model, tried in order by invoke_with_retry."""
    return [_chat_model(m) for m in model_chain]