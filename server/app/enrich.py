"""Genre tags per artist: MusicBrainz first, YouTube Music's "related artists" as fallback."""
import json
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict

from . import db, report

MB_URL = "https://musicbrainz.org/ws/2"
USER_AGENT = "MusicPhasen/0.1 (self-hosted personal app)"
MB_INTERVAL = 1.1  # MusicBrainz allows one request per second
RETRY_AFTER = 30 * 86400  # look up misses again after a month

# Tags that say nothing about the sound.
_NOISE = {"seen live", "favorites", "favourite", "male vocalists", "female vocalists", "german", "american",
          "british", "english", "french", "deutsch", "usa", "uk", "rapper", "singer", "producer", "dj",
          "composer", "spotify", "under 2000 listeners", "fixme", "check"}

_last_request = 0.0


def _get(path: str, params: dict, attempts: int = 5) -> dict:
    """GET with the rate limit; on 503 (MusicBrainz is throttling) wait longer and try again."""
    global _last_request
    url = f"{MB_URL}/{path}?{urllib.parse.urlencode({**params, 'fmt': 'json'})}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(attempts):
        wait = MB_INTERVAL - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code != 503 or attempt == attempts - 1:
                raise
            time.sleep(int(e.headers.get("Retry-After") or 0) or 2 * 2 ** attempt)
        finally:
            _last_request = time.monotonic()
    raise RuntimeError("unreachable")


def _norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return "".join(ch for ch in s if ch.isalnum())


def tags_from_mb(data: dict) -> dict[str, float]:
    """Genres count fully, free-form tags half (they are noisier)."""
    tags: dict[str, float] = {}
    for g in data.get("genres") or []:
        if g.get("count", 0) > 0:
            tags[g["name"].lower()] = float(g["count"])
    if not tags:
        for t in data.get("tags") or []:
            name = t["name"].lower()
            if t.get("count", 0) > 0 and name not in _NOISE and not name.isdigit():
                tags[name] = t["count"] * 0.5
    return tags


def lookup_mb(name: str, get=_get) -> tuple[str | None, dict[str, float]]:
    found = get("artist", {"query": f'artist:"{name}"', "limit": 5}).get("artists") or []
    target = _norm(name)
    match = next((a for a in found if a.get("score", 0) >= 90 and
                  (_norm(a["name"]) == target or any(_norm(x.get("name", "")) == target
                                                     for x in a.get("aliases") or []))), None)
    if not match:
        return None, {}
    detail = get(f"artist/{match['id']}", {"inc": "genres+tags"})
    return match["id"], tags_from_mb(detail)


def pending_artists(now: int) -> list[str]:
    rows = db.query(
        "SELECT t.artist, COUNT(p.id) AS plays FROM tracks t LEFT JOIN plays p ON p.video_id = t.video_id "
        "LEFT JOIN artists a ON a.name = t.artist "
        "WHERE t.artist IS NOT NULL AND (a.name IS NULL OR (a.tags = '{}' AND a.looked_up_at < ?)) "
        "GROUP BY t.artist COLLATE NOCASE ORDER BY plays DESC", (now - RETRY_AFTER,))
    return [r["artist"] for r in rows]


def enrich_mb(limit: int | None = None, get=_get, log=print) -> int:
    """Looks up artists without tags, most played first. Returns how many got tags."""
    now = int(time.time())
    names = pending_artists(now)[:limit]
    hits = 0
    for n, name in enumerate(names, 1):
        try:
            mbid, tags = lookup_mb(name, get)
        except Exception as e:  # network hiccup: try this artist again next run
            report.warn(log, f"{name}: MusicBrainz nicht erreichbar ({report.clean(e)[:80]}), nächstes Mal wieder")
            continue
        db.write("INSERT INTO artists(name, mbid, tags, looked_up_at) VALUES(?,?,?,?) ON CONFLICT(name) "
                 "DO UPDATE SET mbid = excluded.mbid, tags = excluded.tags, looked_up_at = excluded.looked_up_at",
                 (name, mbid, json.dumps(tags), now))
        hits += bool(tags)
        report.progress(log, n, len(names), f"  {n}/{len(names)} Interpreten nachgeschlagen" if n % 25 == 0 else None)
    return hits


def fill_from_related(yt, limit: int | None = None) -> int:
    """Artists MusicBrainz knows nothing about inherit the averaged tags of their YTM related artists."""
    rows = db.query(
        "SELECT a.name, MAX(t.artist_id) AS artist_id FROM artists a JOIN tracks t ON t.artist = a.name "
        "WHERE a.tags = '{}' AND t.artist_id IS NOT NULL GROUP BY a.name")
    known = {r["name"].lower(): json.loads(r["tags"]) for r in db.query("SELECT name, tags FROM artists WHERE tags != '{}'")}
    filled = 0
    for row in rows[:limit]:
        try:
            related = (yt.get_artist(row["artist_id"]).get("related") or {}).get("results") or []
        except Exception:
            continue
        summed: dict[str, float] = defaultdict(float)
        used = 0
        for r in related:
            tags = known.get((r.get("title") or "").lower())
            if not tags:
                continue
            total = sum(tags.values())
            for k, v in tags.items():
                summed[k] += v / total
            used += 1
        if used:
            tags = {k: round(v / used * 10, 3) for k, v in summed.items()}
            db.write("UPDATE artists SET tags = ? WHERE name = ?", (json.dumps(tags), row["name"]))
            filled += 1
    return filled
