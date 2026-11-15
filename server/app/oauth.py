"""Signing in to YouTube Music with a device code (OAuth), like a TV does.

The app shows a short code, you confirm it at google.com/device in a real browser, the server gets a
token and refreshes it by itself. No headers to copy, and unlike browser cookies the token is not
rotated away when another client uses it. Needs your own Google OAuth client ("TVs and Limited Input
devices", YouTube Data API enabled), stored once in data/oauth-client.json.
"""
import json
import os
import threading
import time
from pathlib import Path

CLIENT_FILE = "oauth-client.json"
TOKEN_FILE = "oauth.json"

_lock = threading.Lock()
_pending: dict | None = None  # the running device flow: device_code, user_code, expires_at, interval


class OAuthError(RuntimeError):
    pass


def client_config(data_path: Path) -> tuple[str, str] | None:
    """(client id, client secret) from the environment or data/oauth-client.json."""
    cid, secret = os.environ.get("YT_CLIENT_ID"), os.environ.get("YT_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    f = data_path / CLIENT_FILE
    if not f.exists():
        return None
    d = json.loads(f.read_text())
    return d["client_id"], d["client_secret"]


def save_client(data_path: Path, client_id: str, client_secret: str) -> None:
    f = data_path / CLIENT_FILE
    f.write_text(json.dumps({"client_id": client_id.strip(), "client_secret": client_secret.strip()}))
    f.chmod(0o600)


def credentials(data_path: Path):
    from ytmusicapi import OAuthCredentials

    config = client_config(data_path)
    if not config:
        raise OAuthError("Der Google-Zugang (Client-ID und Secret) ist noch nicht eingerichtet.")
    return OAuthCredentials(*config)


def start(data_path: Path) -> dict:
    """Asks Google for a device code. Returns what the app shows: code, address, how long it is valid."""
    global _pending
    from ytmusicapi.exceptions import YTMusicError

    try:
        code = credentials(data_path).get_code()
    except YTMusicError as e:
        raise OAuthError(f"Google lehnt den Zugang ab: {e}") from e
    if "device_code" not in code:
        raise OAuthError(f"Google hat keinen Code geliefert: {code.get('error_description') or code.get('error') or code}")
    with _lock:
        _pending = {"device_code": code["device_code"], "user_code": code["user_code"],
                    "verification_url": code["verification_url"], "interval": int(code.get("interval", 5)),
                    "expires_at": int(time.time()) + int(code["expires_in"])}
        return public(_pending)


def public(p: dict) -> dict:
    return {"user_code": p["user_code"], "verification_url": p["verification_url"],
            "interval": p["interval"], "expires_in": max(0, p["expires_at"] - int(time.time()))}


def poll(data_path: Path, verify=None) -> str:
    """One look whether the code was confirmed: 'pending', 'ok', or OAuthError. On 'ok' the token is
    stored in data/oauth.json; verify(path) may test it first (and raise if it does not work)."""
    global _pending
    with _lock:
        p = dict(_pending) if _pending else None
    if not p:
        raise OAuthError("Es läuft keine Anmeldung. Bitte neu starten.")
    if time.time() > p["expires_at"]:
        raise OAuthError("Der Code ist abgelaufen. Bitte neu starten.")
    creds = credentials(data_path)
    raw = creds.token_from_code(p["device_code"])
    error = raw.get("error")
    if error in ("authorization_pending", "slow_down"):
        return "pending"
    if error == "access_denied":
        raise OAuthError("Die Anmeldung wurde bei Google abgelehnt.")
    if error or "refresh_token" not in raw:
        raise OAuthError(f"Google: {raw.get('error_description') or error or 'kein Token erhalten'}")
    target = data_path / TOKEN_FILE
    tmp = data_path / (TOKEN_FILE + ".neu")
    store_token(creds, raw, tmp)
    try:
        if verify:
            verify(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, target)
    with _lock:
        _pending = None
    return "ok"


def store_token(creds, raw: dict, path: Path) -> None:
    """Writes the token in ytmusicapi's own format (it refreshes and rewrites the file itself later)."""
    from ytmusicapi.auth.oauth import RefreshingToken

    token = RefreshingToken(credentials=creds, access_token=raw["access_token"], refresh_token=raw["refresh_token"],
                            scope=raw["scope"], token_type=raw["token_type"],
                            expires_in=raw.get("refresh_token_expires_in", raw["expires_in"]))
    token.update(raw)
    token.local_cache = path  # writes the file
    path.chmod(0o600)
