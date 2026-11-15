import json
import sqlite3
import threading
import time
from pathlib import Path

_local = threading.local()
_path: Path | None = None
_write_lock = threading.Lock()

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS tracks(
  video_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  artist TEXT,
  artist_id TEXT,
  album TEXT,
  duration INTEGER,
  liked INTEGER NOT NULL DEFAULT 0,
  added_at INTEGER NOT NULL
);
-- One row per play. approx=1 means the time was guessed (first sync, Takeout without time).
CREATE TABLE IF NOT EXISTS plays(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  video_id TEXT NOT NULL REFERENCES tracks(video_id),
  at INTEGER NOT NULL,
  approx INTEGER NOT NULL DEFAULT 0,
  source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS plays_at_idx ON plays(at);
CREATE INDEX IF NOT EXISTS plays_video_idx ON plays(video_id);
CREATE UNIQUE INDEX IF NOT EXISTS plays_takeout_idx ON plays(video_id, at) WHERE source = 'takeout';
-- Genre tags per artist name; tags is a JSON object {genre: weight}. looked_up_at marks misses too.
CREATE TABLE IF NOT EXISTS artists(
  name TEXT PRIMARY KEY COLLATE NOCASE,
  mbid TEXT,
  tags TEXT NOT NULL DEFAULT '{}',
  looked_up_at INTEGER NOT NULL
);
-- Taste groups found by phases.compute(); names set by the user survive recomputation.
CREATE TABLE IF NOT EXISTS clusters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  custom_name INTEGER NOT NULL DEFAULT 0,
  data TEXT NOT NULL DEFAULT '{}',
  active INTEGER NOT NULL DEFAULT 1,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS cluster_tracks(
  cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  video_id TEXT NOT NULL REFERENCES tracks(video_id),
  weight REAL NOT NULL,
  PRIMARY KEY(cluster_id, video_id)
);
CREATE INDEX IF NOT EXISTS cluster_tracks_video_idx ON cluster_tracks(video_id);
CREATE TABLE IF NOT EXISTS mixes(
  name TEXT PRIMARY KEY COLLATE NOCASE,
  weights TEXT NOT NULL,
  length INTEGER NOT NULL DEFAULT 50,
  novelty REAL NOT NULL DEFAULT 0.2,
  max_minutes INTEGER NOT NULL DEFAULT 15,
  refresh_days INTEGER NOT NULL DEFAULT 7,
  playlist_id TEXT,
  built_at INTEGER
);
-- Song list of each mix as pushed to YouTube Music, to tell plays from those playlists (history.mark_mix_plays).
CREATE TABLE IF NOT EXISTS mix_pushes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  mix TEXT NOT NULL COLLATE NOCASE,
  at INTEGER NOT NULL,
  video_ids TEXT NOT NULL
);
-- Sound fingerprint per song (sound.py): 1280 float32 values; ok=0 marks a failed try (retried later).
CREATE TABLE IF NOT EXISTS track_sound(
  video_id TEXT PRIMARY KEY,
  vec BLOB,
  styles TEXT NOT NULL DEFAULT '[]',
  model TEXT NOT NULL,
  ok INTEGER NOT NULL,
  at INTEGER NOT NULL
);
-- Login sessions of the web interface; only the SHA-256 of the cookie value is stored.
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  last_used INTEGER NOT NULL
);
-- One row per AI request (costs.py): tokens and cost in US dollars, NULL while the price is unknown.
CREATE TABLE IF NOT EXISTS ai_usage(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at INTEGER NOT NULL,
  model TEXT NOT NULL,
  purpose TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  cache_write INTEGER NOT NULL DEFAULT 0,
  cache_read INTEGER NOT NULL DEFAULT 0,
  cost REAL
);
CREATE INDEX IF NOT EXISTS ai_usage_at_idx ON ai_usage(at);
CREATE TABLE IF NOT EXISTS state(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def init(data_path: Path) -> None:
    global _path
    data_path.mkdir(parents=True, exist_ok=True)
    _path = data_path / "music.db"
    conn().executescript(SCHEMA)
    # Columns added with the AI tagging. artists.source: 'mb', 'related' or 'ai'. tracks.tags: the song's own
    # tags (JSON) when the AI knows it, else NULL and the artist's tags stand in.
    # mixes.max_minutes: longest song a mix takes (hour-long DJ sets and study loops stay out), 0 = no limit.
    # mixes.refresh_days: how often the daily run renews the mix, 0 = only by hand.
    # plays.via: name of the Taktgefühl mix the play came from (history.mark_mix_plays), NULL = chosen yourself.
    for table, column in (("artists", "source TEXT"), ("tracks", "tags TEXT"),
                          ("mixes", "max_minutes INTEGER NOT NULL DEFAULT 15"),
                          ("mixes", "refresh_days INTEGER NOT NULL DEFAULT 7"), ("plays", "via TEXT")):
        if column.split()[0] not in {r["name"] for r in query(f"PRAGMA table_info({table})")}:
            write(f"ALTER TABLE {table} ADD COLUMN {column}")
    # Mixes pushed before mix_pushes existed: their last pushed song list stands in from the push on.
    if not one("SELECT 1 FROM mix_pushes"):
        for m in query("SELECT name, built_at FROM mixes WHERE built_at IS NOT NULL"):
            result = get_state(f"mix_result:{m['name']}")
            if result and result.get("pushed"):
                write("INSERT INTO mix_pushes(mix, at, video_ids) VALUES(?,?,?)",
                      (m["name"], m["built_at"], json.dumps([s["video_id"] for s in result["songs"]])))


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _path:
        c = sqlite3.connect(_path, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        _local.conn = c
        _local.path = _path
    return c


def write(sql: str, params=()) -> sqlite3.Cursor:
    with _write_lock:
        c = conn()
        cur = c.execute(sql, params)
        c.commit()
        return cur


def write_many(statements: list[tuple[str, tuple]]) -> None:
    with _write_lock:
        c = conn()
        with c:
            for sql, params in statements:
                c.execute(sql, params)


def query(sql: str, params=()) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def one(sql: str, params=()) -> sqlite3.Row | None:
    return conn().execute(sql, params).fetchone()


def get_state(key: str, default=None):
    row = one("SELECT value FROM state WHERE key = ?", (key,))
    return json.loads(row["value"]) if row else default


def set_state(key: str, value) -> None:
    write("INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
          (key, json.dumps(value)))


def seconds(text) -> int | None:
    """"3:45" or "1:02:33" -> seconds; watch tracks only carry the length as text."""
    try:
        value = 0
        for part in str(text).split(":"):
            value = value * 60 + int(part)
        return value or None
    except (TypeError, ValueError):
        return None


def track_statement(item: dict, liked: bool | None = None) -> tuple[str, tuple]:
    """Upsert for a ytmusicapi playlistItem / watch track. Keeps the liked flag unless one is given."""
    artists = item.get("artists") or []
    first = artists[0] if artists else {}
    album = item.get("album") or {}
    duration = item.get("duration_seconds") or seconds(item.get("length") or item.get("duration"))
    params = (item["videoId"], item.get("title") or "?", first.get("name"), first.get("id"),
              album.get("name") if isinstance(album, dict) else None, duration,
              1 if liked else 0, int(time.time()))
    if liked is None:
        sql = ("INSERT INTO tracks(video_id, title, artist, artist_id, album, duration, liked, added_at) "
               "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET title = excluded.title, "
               "artist = COALESCE(excluded.artist, artist), artist_id = COALESCE(excluded.artist_id, artist_id), "
               "album = COALESCE(excluded.album, album), duration = COALESCE(excluded.duration, duration)")
    else:
        sql = ("INSERT INTO tracks(video_id, title, artist, artist_id, album, duration, liked, added_at) "
               "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET title = excluded.title, "
               "artist = COALESCE(excluded.artist, artist), artist_id = COALESCE(excluded.artist_id, artist_id), "
               "album = COALESCE(excluded.album, album), duration = COALESCE(excluded.duration, duration), "
               "liked = excluded.liked")
    return sql, params
