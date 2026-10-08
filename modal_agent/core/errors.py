import os
import re


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
