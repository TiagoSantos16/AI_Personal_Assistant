"""Bounded delivery; callers persist outcomes independently."""
import os
import time
import requests


def call(method, payload):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return False
    for attempt in range(2):
        try:
            response = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=10)
            body = response.json()
            if not isinstance(body, dict):
                return False
            if response.status_code == 429 and attempt == 0:
                delay = body.get("parameters", {}).get("retry_after", 0)
                if isinstance(delay, (int, float)) and 0 < delay <= 5:
                    time.sleep(delay)
                    continue
            return response.ok and body.get("ok") is True
        except (requests.RequestException, ValueError):
            return False  # timeout is ambiguous; never regenerate
    return False


def send(chat_id, text):
    if chat_id is None:
        return True
    # 1800 Unicode codepoints stay below Telegram's UTF-16 limit even for emoji.
    for start in range(0, len(text), 1800):
        if not call("sendMessage", {"chat_id": chat_id, "text": text[start:start + 1800]}):
            return False
    return True


def react(chat_id, message_id, clear=False):
    if chat_id is None or message_id is None:
        return True
    return call("setMessageReaction", {"chat_id": chat_id, "message_id": message_id,
        "reaction": [] if clear else [{"type": "emoji", "emoji": "👀"}]})
