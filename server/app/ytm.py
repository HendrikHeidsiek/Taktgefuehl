"""Thin layer over ytmusicapi, so the rest of the code (and the tests) only see the calls we use."""
from pathlib import Path

AUTH_FILE = "browser.json"


class NotAuthenticated(RuntimeError):
    pass


class SessionExpired(NotAuthenticated):
    def __init__(self):
        super().__init__("Die Anmeldung bei YouTube Music ist abgelaufen. In der App unter Einstellungen → "
                         "YouTube Music neu verbinden.")


def signed_in(yt) -> bool:
    """A signed-out session answers account requests without the account menu."""
    try:
        yt.get_account_info()
        return True
    except Exception:
        return False


def call(yt, fn, *args, **kwargs):
    """Runs a ytmusicapi call; if it fails because the session expired, says so (SessionExpired) and
    remembers it for the app, instead of surfacing ytmusicapi's parser error."""
    from . import db
    try:
        result = fn(*args, **kwargs)
    except Exception:
        if not signed_in(yt):
            db.set_state("youtube_expired", True)
            raise SessionExpired()
        raise
    db.set_state("youtube_expired", False)
    return result


def method(data_path: Path) -> str | None:
    """How the server signs in: 'oauth' (device code, preferred), 'browser' (copied headers) or None."""
    from . import oauth
    if (data_path / oauth.TOKEN_FILE).exists() and oauth.client_config(data_path):
        return "oauth"
    return "browser" if (data_path / AUTH_FILE).exists() else None


def connected(data_path: Path) -> bool:
    return method(data_path) is not None


def client(data_path: Path):
    from ytmusicapi import YTMusic
    from . import oauth

    # English labels, because history.py parses the "played" texts ("Today", "Yesterday", ...).
    kind = method(data_path)
    if kind == "oauth":
        return YTMusic(str(data_path / oauth.TOKEN_FILE), oauth_credentials=oauth.credentials(data_path),
                       language="en")
    if kind == "browser":
        return YTMusic(str(data_path / AUTH_FILE), language="en")
    raise NotAuthenticated("YouTube Music ist nicht verbunden. In der App unter Einstellungen verbinden "
                           "(oder lokal: python -m app.auth).")


def replace_playlist(yt, playlist_id: str | None, title: str, description: str, video_ids: list[str]) -> str:
    """Creates the playlist on first use, afterwards swaps its content. Returns the playlist id."""
    if playlist_id:
        try:
            current = yt.get_playlist(playlist_id, limit=None)
        except Exception:
            playlist_id = None  # deleted in YouTube Music in the meantime
        else:
            items = [t for t in current.get("tracks", []) if t.get("setVideoId")]
            if items:
                yt.remove_playlist_items(playlist_id, items)
            yt.edit_playlist(playlist_id, description=description)
            if video_ids:
                yt.add_playlist_items(playlist_id, video_ids, duplicates=False)
            return playlist_id
    result = yt.create_playlist(title, description, privacy_status="PRIVATE", video_ids=video_ids)
    if not isinstance(result, str):
        raise RuntimeError(f"Playlist konnte nicht angelegt werden: {result}")
    return result
