import json

import pytest

from app import oauth, ytm


class FakeCreds:
    def __init__(self, answers):
        self.answers = list(answers)
        self.client_id, self.client_secret = "id", "secret"

    def get_code(self):
        return {"device_code": "dev-1", "user_code": "ABCD-EFGH", "verification_url": "https://www.google.com/device",
                "expires_in": 1800, "interval": 5}

    def token_from_code(self, device_code):
        assert device_code == "dev-1"
        return self.answers.pop(0)

    def refresh_token(self, refresh_token):
        return {"access_token": "fresh", "expires_in": 3600}


TOKEN = {"access_token": "at", "refresh_token": "rt", "scope": "https://www.googleapis.com/auth/youtube",
         "token_type": "Bearer", "expires_in": 3599}


@pytest.fixture
def creds(data, monkeypatch):
    oauth.save_client(data, "id.apps.googleusercontent.com", "secret")
    fake = FakeCreds([{"error": "authorization_pending"}, {"error": "slow_down"}, TOKEN])
    monkeypatch.setattr(oauth, "credentials", lambda path: fake)
    return fake


def test_device_flow_stores_token_and_switches_client(data, creds):
    shown = oauth.start(data)
    assert shown["user_code"] == "ABCD-EFGH" and "device_code" not in shown
    assert oauth.poll(data) == "pending"
    assert oauth.poll(data) == "pending"
    checked = []
    assert oauth.poll(data, verify=lambda p: checked.append(p)) == "ok"
    stored = json.loads((data / oauth.TOKEN_FILE).read_text())
    assert stored["refresh_token"] == "rt" and stored["expires_at"] > 0
    assert checked and not checked[0].exists()  # verified before it replaced anything
    assert ytm.method(data) == "oauth"
    with pytest.raises(oauth.OAuthError):  # the flow is finished
        oauth.poll(data)


def test_failed_verification_keeps_old_login(data, creds):
    (data / ytm.AUTH_FILE).write_text("{}")
    oauth.start(data)
    creds.answers = [TOKEN]

    def broken(path):
        raise KeyError("no history")

    with pytest.raises(KeyError):
        oauth.poll(data, verify=broken)
    assert not (data / oauth.TOKEN_FILE).exists()
    assert ytm.method(data) == "browser"


def test_denied_and_expired(data, creds, monkeypatch):
    oauth.start(data)
    creds.answers = [{"error": "access_denied"}]
    with pytest.raises(oauth.OAuthError, match="abgelehnt"):
        oauth.poll(data)
    oauth.start(data)
    monkeypatch.setattr(oauth.time, "time", lambda: 10 ** 11)
    with pytest.raises(oauth.OAuthError, match="abgelaufen"):
        oauth.poll(data)


def test_without_client_config(data):
    assert oauth.client_config(data) is None
    with pytest.raises(oauth.OAuthError, match="eingerichtet"):
        oauth.start(data)
