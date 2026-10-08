"""Expiring device credentials; password rotation revokes all devices."""
import hashlib
import hmac
import secrets
import time

COOKIE = "assistant_device"
MAX_AGE = 30 * 86400


def issue(password, now=None):
    payload = f"{int(time.time() if now is None else now) + MAX_AGE}.{secrets.token_hex(16)}"
    signature = hmac.new(password.encode(), ("device-v1:" + payload).encode(), hashlib.sha256).hexdigest()
    return payload + "." + signature


def valid(token, password, now=None):
    if not password or not isinstance(token, str) or len(token) > 160:
        return False
    try:
        expires, nonce, signature = token.split(".")
        remaining = int(expires) - (time.time() if now is None else now)
        if not 0 < remaining <= MAX_AGE or len(nonce) != 32:
            return False
        expected = hmac.new(password.encode(), ("device-v1:" + expires + "." + nonce).encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature, expected)
    except (ValueError, TypeError):
        return False
