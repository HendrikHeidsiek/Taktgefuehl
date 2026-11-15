"""Backfill from Google Takeout: "YouTube and YouTube Music/history/watch-history.json"."""
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db

_TITLE_PATTERNS = [r"^Watched (.+)$", r"^„?(.+?)“? angesehen$", r"^Listened to (.+)$"]


def _clean_title(raw: str) -> str:
    for pattern in _TITLE_PATTERNS:
        m = re.match(pattern, raw)
        if m:
            return m.group(1).strip()
    return raw


def _artist(entry: dict) -> str | None:
    subtitles = entry.get("subtitles") or []
    if not subtitles:
        return None
    name = subtitles[0].get("name") or ""
    return re.sub(r"\s+-\s+Topic$", "", name).strip() or None


def parse(path: Path) -> list[dict]:
    """YouTube Music entries as {videoId, title, artist, at}; plain YouTube videos are skipped."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for e in entries:
        if e.get("header") != "YouTube Music":
            continue
        url = e.get("titleUrl") or ""
        video_id = (parse_qs(urlparse(url).query).get("v") or [None])[0]
        if not video_id or not e.get("time"):
            continue
        at = int(datetime.fromisoformat(e["time"].replace("Z", "+00:00")).timestamp())
        out.append({"videoId": video_id, "title": _clean_title(e.get("title") or "?"),
                    "artist": _artist(e), "at": at})
    return out


def import_file(path: Path) -> int:
    """Stores the plays (re-importing the same file is harmless). Returns the number of entries read."""
    entries = parse(path)
    statements = []
    for e in entries:
        # OR IGNORE: titles from the API are better than the Takeout ones, keep them.
        statements.append(("INSERT OR IGNORE INTO tracks(video_id, title, artist, added_at) VALUES(?,?,?,?)",
                           (e["videoId"], e["title"], e["artist"], e["at"])))
        statements.append(("INSERT OR IGNORE INTO plays(video_id, at, approx, source) VALUES(?,?,0,'takeout')",
                           (e["videoId"], e["at"])))
    db.write_many(statements)
    return len(entries)
