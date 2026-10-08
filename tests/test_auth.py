import importlib
from core.auth import COOKIE, MAX_AGE, issue, valid
from starlette.testclient import TestClient


def test_device_expiry_tamper_and_rotation():
    token = issue("owner", now=100)
    assert valid(token, "owner", now=101)
    assert not valid(token, "owner", now=100 + MAX_AGE)
    assert not valid(token, "changed", now=101)
    assert not valid(token + "bad", "owner", now=101)
    assert not valid("malformed", "owner", now=101)


def test_cookie_endpoint(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "owner")
    server = importlib.import_module("dashboard_server")
    with TestClient(server.app, base_url="https://testserver") as client:
        token = issue("owner")
        assert client.post("/api/device", json={"token": token}, headers={"Origin": "https://evil.example"}).status_code == 403
        response = client.post("/api/device", json={"token": token}, headers={"Origin": "https://testserver"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
        assert COOKIE in cookie
        assert client.post("/api/device", json={"token": "invalid"}, headers={"Origin": "https://testserver"}).status_code == 403
        response = client.post("/api/device", json={"token": ""}, headers={"Origin": "https://testserver"})
        assert "Max-Age=0" in response.headers["set-cookie"]
