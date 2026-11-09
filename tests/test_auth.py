from core.auth import MAX_AGE, issue, valid


def test_device_expiry_tamper_and_rotation():
    token = issue("owner", now=100)
    assert valid(token, "owner", now=101)
    assert not valid(token, "owner", now=100 + MAX_AGE)
    assert not valid(token, "changed", now=101)
    assert not valid(token + "bad", "owner", now=101)
    assert not valid("malformed", "owner", now=101)
