"""Deterministic Telegram input validation; no external calls."""
import hmac
import re
from urllib.parse import urlsplit


def authorised(header, message, env):
    secret, owner = env.get("TELEGRAM_WEBHOOK_SECRET"), env.get("TELEGRAM_ALLOWED_USER_ID")
    sender = message.get("from") if isinstance(message, dict) else None
    return bool(secret and owner and isinstance(sender, dict) and hmac.compare_digest(str(header or "").encode(), secret.encode())
                and str(sender.get("id", "")) == owner)


def command(text):
    match = re.fullmatch(r"/([a-z]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?", text.strip(), re.S)
    return (match[1], match[2] or "") if match else (None, "")


def canonical_source(url):
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or parsed.hostname not in
        {"instagram.com", "www.instagram.com", "m.instagram.com"} or
        parsed.username or parsed.password or parsed.port not in {None, 443, 80}):
        raise ValueError("Unsupported source")
    match = re.fullmatch(r"/(reel|reels|p)/([A-Za-z0-9_-]+)/?", parsed.path)
    if not match:
        raise ValueError("Unsupported Instagram path")
    kind = "post" if match[1] == "p" else "reel"
    return {"source_id": match[2], "kind": kind,
            "url": f"https://www.instagram.com/{'p' if kind == 'post' else 'reel'}/{match[2]}/"}


def urls(message):
    text = message.get("text") or message.get("caption") or ""
    entities = message.get("entities") or message.get("caption_entities") or []
    result = []
    encoded = text.encode("utf-16-le")
    for entity in entities:
        if entity.get("type") == "text_link":
            result.append(entity.get("url", ""))
        elif entity.get("type") == "url":
            start, length = entity.get("offset", 0), entity.get("length", 0)
            if not isinstance(start, int) or not isinstance(length, int) or start < 0 or length < 1:
                continue
            try:
                result.append(encoded[start * 2:(start + length) * 2].decode("utf-16-le"))
            except UnicodeDecodeError:
                continue
    if not result:
        result = [u.rstrip(".,;!?)>]") for u in re.findall(r"https?://[^\s<>]+", text)]
    return list(dict.fromkeys(result))
