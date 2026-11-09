import os
import re
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def conversation_event(correlation_id, stage, status, *, source_id=None, action=None):
    logger.info('conversation_event correlation=%s stage=%s status=%s source_id=%s action=%s',
        correlation_id, stage, status, source_id or '-', action or '-')


class AnswerValidationError(ValueError):
    """The provider did not produce a usable, grounded answer within the budget."""


class IndexUnavailableError(RuntimeError):
    """Saved notes exist, but their search index is unavailable."""


class AnswerPathError(RuntimeError):
    """Safe diagnostic metadata; exception text may contain private provider input."""
    def __init__(self, stage, correlation_id, cause):
        super().__init__('The answer could not be completed. Please try again later.')
        self.stage = stage
        self.correlation_id = correlation_id
        self.error_type = type(cause).__name__
        status = getattr(cause, 'status_code', None)
        self.status_code = status if type(status) is int else None
        self.provider_failures = []
        failures = getattr(cause, 'failures', [])
        for entry in (failures[:8] if isinstance(failures, (list, tuple)) else []):
            if isinstance(entry, tuple) and len(entry) == 2 and isinstance(entry[1], Exception):
                error = entry[1]
                code = getattr(error, 'status_code', None)
                self.provider_failures.append({'type': type(error).__name__, 'status': code if type(code) is int else None})


def log_answer_failure(logger, error, *, stage, correlation_id):
    """Log no exception text/traceback, queries, prompts, credentials or user IDs."""
    if isinstance(error, AnswerPathError):
        stage, correlation_id = error.stage, error.correlation_id
        error_type, status = error.error_type, error.status_code
    else:
        error_type, status = type(error).__name__, getattr(error, 'status_code', None)
    logger.error('answer_failure correlation=%s stage=%s error_type=%s status=%s provider_failures=%s',
        correlation_id, stage, error_type, status if type(status) is int else 'unavailable',
        error.provider_failures if isinstance(error, AnswerPathError) else [])


def redact(error):
    text = str(error)
    for name in ("TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY", "DASHBOARD_PASSWORD", "TELEGRAM_WEBHOOK_SECRET"):
        value = os.environ.get(name)
        if value:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"https?://\S+", "[URL redacted]", text)
    text = re.sub(r"(?:sk-[\w-]+|\d{6,}:[\w-]+)", "[redacted]", text)
    return text[:2000]


def classify(error, stage="generation"):
    status = getattr(error, "status_code", None)
    if stage in {"download", "media", "storage", "index", "telegram", "configuration"}:
        return stage
    if status in {402, 429, 500, 502, 503, 504} or hasattr(error, "failures"):
        return "provider"
    return "validation"


def user_message(kind):
    return {
        "download": "I couldn't open this post. If you can still view it, try sending it again.",
        "provider": "I couldn't finish this one because the AI service is busy. I've kept it for retry.",
        "media": "I couldn't read this media. I've kept it for retry.",
        "configuration": "The assistant needs an owner configuration update.",
        "storage": "I couldn't save this note. Please try again later.",
    }.get(kind, "I couldn't finish this one. I've kept it for retry.")
