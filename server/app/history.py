"""Builds a lasting play log from YouTube Music's short history list.

get_history() only returns the last few hundred songs, without times, and a replayed song moves to
the top instead of appearing twice. So we poll regularly, compare with the previous snapshot and
store everything that newly appeared at the top as a play "now".
"""
import json
import re
import time
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime

from . import db, ytm

SNAPSHOT_KEY = "history_snapshot"
ALIGN = 5  # how many following items must match to accept an alignment
SPACING = 180  # seconds between plays that arrive in one poll, to keep their order
MIN_RUN = 3  # plays in a row, all on one mix playlist, that count as listening to that playlist
GRACE = 3600  # play times are estimated per poll: a play shortly before a push may belong to it
KEEP_PUSHES = 90 * 86400  # older song lists are dropped (the newest of each mix always stays)

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}


def new_plays(previous: list[str], current: list[str]) -> list[str]:
    """Video ids at the top of `current` that were played since `previous` was taken, newest first."""
    if not previous:
        return list(current)
    for i in range(len(current) + 1):
        head = set(current[:i])
        rest = [v for v in previous if v not in head]
        window = min(ALIGN, len(current) - i, len(rest))
        if window == 0:
            # Nothing left to compare: either all of current is new, or previous is exhausted.
            if i == len(current) or not rest:
                return current[:i]
            continue
        if current[i:i + window] == rest[:window]:
            return current[:i]
    return list(current)


def approx_time(label: str | None, now: int) -> int:
    """Rough timestamp for the "played" labels of the first sync."""
    day = 86400
    label = (label or "").strip().lower()
    if label == "today":
        return now
    if label == "yesterday":
        return now - day
    if label == "this week":
        return now - 3 * day
    if label == "last week":
        return now - 10 * day
    if label == "this month":
        return now - 15 * day
    if label == "last month":
        return now - 45 * day
    m = re.fullmatch(r"([a-z]+)\s+(\d{4})", label)
    if m and m.group(1) in _MONTHS:
        return int(datetime(int(m.group(2)), _MONTHS[m.group(1)], 15).timestamp())
    return now - 60 * day


def sync_history(yt, now: int | None = None) -> int:
    """Polls the history once. Returns the number of plays stored."""
    now = now or int(time.time())
    items = [i for i in ytm.call(yt, yt.get_history) if i.get("videoId")]
    current = [i["videoId"] for i in items]
    previous = db.get_state(SNAPSHOT_KEY)
    statements = [db.track_statement(i) for i in items]
    if previous is None:
        # First run: everything is history of unknown age, timed from the labels.
        for n, item in enumerate(items):
            at = approx_time(item.get("played"), now) - n * SPACING
            statements.append(("INSERT INTO plays(video_id, at, approx, source) VALUES(?,?,1,'history')",
                               (item["videoId"], at)))
        stored = len(items)
    else:
        fresh = new_plays(previous, current)
        for n, video_id in enumerate(fresh):
            statements.append(("INSERT INTO plays(video_id, at, approx, source) VALUES(?,?,0,'history')",
                               (video_id, now - n * SPACING)))
        stored = len(fresh)
    db.write_many(statements)
    db.set_state(SNAPSHOT_KEY, current)
    return stored


def record_push(mix: str, video_ids: list[str], at: int) -> None:
    """Remembers the songs a mix playlist had from this moment on (mixer.push)."""
    db.write("INSERT INTO mix_pushes(mix, at, video_ids) VALUES(?,?,?)", (mix, at, json.dumps(video_ids)))
    db.write("DELETE FROM mix_pushes WHERE at < ? AND id NOT IN (SELECT MAX(id) FROM mix_pushes GROUP BY mix)",
             (at - KEEP_PUSHES,))


def mark_mix_plays() -> int:
    """Marks plays that came from a Taktgefühl playlist (plays.via = the mix's name). Returns how many
    plays were newly marked.

    The history doesn't say where a song was played from, otherwise the mixes would feed on themselves:
    every play from a mix makes its songs and its phase shares look more loved. A playlist leaves a trace,
    though: several songs in a row (by at least two artists) that are all on the playlist as it was at
    the time, in any order, so shuffle too. A single song that happens to be on a mix stays your own play.
    """
    pushes: dict[str, list[tuple[int, set[str]]]] = defaultdict(list)
    for r in db.query("SELECT mix, at, video_ids FROM mix_pushes ORDER BY at, id"):
        pushes[r["mix"].lower()].append((r["at"], set(json.loads(r["video_ids"]))))
    if not pushes:
        return 0
    plays = db.query("SELECT p.id, p.video_id, p.at, p.via, LOWER(t.artist) AS artist FROM plays p "
                     "LEFT JOIN tracks t ON t.video_id = p.video_id WHERE p.approx = 0 AND p.at >= ? "
                     "ORDER BY p.at, p.id", (min(v[0][0] for v in pushes.values()) - GRACE,))
    names = {r["mix"].lower(): r["mix"] for r in db.query("SELECT mix FROM mix_pushes")}
    marks: dict[int, str] = {}
    for key, versions in pushes.items():
        times = [at for at, _ in versions]

        def on_list(play) -> bool:
            for at in (play["at"], play["at"] + GRACE):
                i = bisect_right(times, at) - 1
                if i >= 0 and play["video_id"] in versions[i][1]:
                    return True
            return False

        run: list = []
        for play in [*plays, None]:
            if play is not None and on_list(play):
                run.append(play)
                continue
            if len(run) >= MIN_RUN and len({p["artist"] for p in run}) >= 2:
                for p in run:
                    marks.setdefault(p["id"], names[key])
            run = []
    unmarked = {p["id"] for p in plays if p["via"] is None}
    fresh = [(mix, pid) for pid, mix in marks.items() if pid in unmarked]
    db.write_many([("UPDATE plays SET via = ? WHERE id = ? AND via IS NULL", f) for f in fresh])
    return len(fresh)


def sync_likes(yt) -> int:
    """Mirrors the "Liked songs" playlist into tracks.liked. Returns the number of liked songs."""
    liked = [t for t in ytm.call(yt, yt.get_liked_songs, limit=None).get("tracks", []) if t.get("videoId")]
    statements = [("UPDATE tracks SET liked = 0 WHERE liked = 1", ())]
    statements += [db.track_statement(t, liked=True) for t in liked]
    db.write_many(statements)
    return len(liked)
