"""Builds playlists that mix phases by percentage, with a share of new songs, and pushes them to YTM."""
import json
import math
import random
import time
from collections import defaultdict

from . import ai, db, history, phases, sound, ytm

DAY = 86400
MAX_PER_ARTIST = 3
RADIO_SEEDS = 5
CANDIDATE_FACTOR = 3  # radio songs collected per new song wanted, before checking the fit
TEMPERATURE = 0.8  # randomness when picking known songs; 0 = always the top-scored ones


def save_mix(name: str, weights: dict[int, float], length: int = 50, novelty: float = 0.2,
             max_minutes: int = 15, refresh_days: int = 7) -> None:
    """max_minutes: longest song the mix takes, 0 = no limit. refresh_days: renewal interval, 0 = by hand."""
    db.write("INSERT INTO mixes(name, weights, length, novelty, max_minutes, refresh_days) VALUES(?,?,?,?,?,?) "
             "ON CONFLICT(name) DO UPDATE SET weights = excluded.weights, length = excluded.length, "
             "novelty = excluded.novelty, max_minutes = excluded.max_minutes, refresh_days = excluded.refresh_days",
             (name, json.dumps({str(k): v for k, v in weights.items()}), length, novelty, max_minutes, refresh_days))


SLACK = 6 * 3600  # the daily run starts at slightly different times; a mix built yesterday evening counts as a day old


def next_refresh(mix) -> int | None:
    """When the daily run renews the mix next; None = only by hand, 0 = at the next run."""
    if not mix["refresh_days"]:
        return None
    if not mix["built_at"]:
        return 0
    return mix["built_at"] + mix["refresh_days"] * DAY - SLACK


def due(mix, now: int | None = None) -> bool:
    at = next_refresh(mix)
    return at is not None and at <= (now or int(time.time()))


def parse_weights(spec: str) -> dict[int, float]:
    """"Hip-Hop=30,EDM=70" (names, name prefixes or ids) -> {cluster_id: 0.3, ...}."""
    raw: dict[int, float] = {}
    for part in spec.split(","):
        ref, _, value = part.rpartition("=")
        cid = phases.resolve(ref.strip())
        if cid is None:
            raise ValueError(f"Unbekannte oder mehrdeutige Phase: {ref.strip()!r}")
        raw[cid] = raw.get(cid, 0) + float(value)
    total = sum(raw.values())
    if total <= 0:
        raise ValueError("Die Anteile müssen zusammen mehr als 0 ergeben.")
    return {k: v / total for k, v in raw.items()}


def split(total: int, weights: dict[int, float]) -> dict[int, int]:
    """Largest-remainder rounding, so the counts add up to total."""
    exact = {k: total * w for k, w in weights.items()}
    counts = {k: math.floor(v) for k, v in exact.items()}
    for k in sorted(exact, key=lambda k: exact[k] - counts[k], reverse=True)[:total - sum(counts.values())]:
        counts[k] += 1
    return counts


def known_tracks(cluster_id: int, now: int, max_seconds: int | None = None) -> list[dict]:
    """Songs of the phase with a score; max_seconds leaves out longer ones (unknown length counts as fine).
    The score counts only plays you chose yourself, so a song doesn't climb by being in the mixes; plays
    from the mixes still count as recently played. A song only heard in a mix comes back once you like it."""
    rows = db.query(
        "SELECT t.video_id, t.artist, t.liked, COUNT(CASE WHEN p.via IS NULL THEN p.id END) AS plays, "
        "MAX(p.at) AS last_at, COUNT(CASE WHEN p.via IS NULL AND p.at >= ? THEN p.id END) AS plays_90d "
        "FROM tracks t JOIN cluster_tracks ct ON ct.video_id = t.video_id AND ct.cluster_id = ? "
        "LEFT JOIN plays p ON p.video_id = t.video_id WHERE t.duration IS NULL OR t.duration <= ? "
        "GROUP BY t.video_id HAVING plays > 0 OR t.liked = 1",
        (now - 90 * DAY, cluster_id, max_seconds or 1 << 40))
    out = []
    for r in rows:
        score = math.log1p(r["plays"]) + 0.5 * math.log1p(r["plays_90d"]) + 1.5 * r["liked"]
        if r["last_at"] and now - r["last_at"] < 2 * DAY:
            score -= 2
        elif r["last_at"] and now - r["last_at"] < 7 * DAY:
            score -= 0.7
        out.append({"video_id": r["video_id"], "artist": (r["artist"] or "").lower(), "score": score})
    return out


def pick_known(pool: list[dict], n: int, rng: random.Random, exclude: set[str]) -> list[dict]:
    """Weighted random pick (Gumbel trick) with at most MAX_PER_ARTIST songs per artist."""
    keyed = sorted(pool, key=lambda t: -(t["score"] - TEMPERATURE * math.log(-math.log(rng.random() or 1e-12))))
    picked, per_artist = [], defaultdict(int)
    for t in keyed:
        if len(picked) == n:
            break
        if t["video_id"] in exclude or per_artist[t["artist"]] >= MAX_PER_ARTIST:
            continue
        picked.append(t)
        per_artist[t["artist"]] += 1
    return picked


def _artist_fits(artist: str, cluster_id: int, by_track: dict[str, int]) -> bool:
    """Fallback without song tags: reject only artists whose known songs all sit in other phases."""
    clusters = {by_track[r["video_id"]] for r in db.query(
        "SELECT video_id FROM tracks WHERE artist = ? COLLATE NOCASE", (artist,)) if r["video_id"] in by_track}
    return not clusters or cluster_id in clusters


def find_new(yt, cluster_id: int, seeds: list[str], n: int, exclude: set[str], claude=None,
             listen=None, max_seconds: int | None = None) -> list[dict]:
    """Unheard songs from the YTM radio of the seeds that fit the phase.

    Each candidate is judged as a song: its AI tags and, with listen, its sound fingerprint (fetched on
    demand, one after the other, until enough fit) must be closest to this phase's centre. One loved song
    of an artist does not make the rest of the artist welcome. Without either, an artist rule stands in.
    listen(video_id) analyses a song and stores its fingerprint (sound.analyze_many).
    """
    if n <= 0 or not seeds:
        return []
    heard = {r["video_id"] for r in db.query("SELECT DISTINCT video_id FROM plays")}
    candidates, per_artist = {}, defaultdict(int)
    for seed in seeds[:RADIO_SEEDS]:
        try:
            radio = yt.get_watch_playlist(videoId=seed, radio=True, limit=40).get("tracks", [])
        except Exception:
            continue
        for t in radio:
            vid = t.get("videoId")
            artist = ((t.get("artists") or [{}])[0].get("name") or "").lower()
            if not vid or vid in heard or vid in exclude or vid in candidates or per_artist[artist] >= 2:
                continue
            if max_seconds and (db.seconds(t.get("length")) or 0) > max_seconds:
                continue
            candidates[vid] = t
            per_artist[artist] += 1
        if len(candidates) >= n * CANDIDATE_FACTOR:
            break
    if not candidates:
        return []
    db.write_many([db.track_statement(t) for t in candidates.values()])
    tags = {}
    if claude:
        items = [(vid, (t.get("artists") or [{}])[0].get("name"), t.get("title") or "?") for vid, t in candidates.items()]
        tags = ai.tag_tracks(claude, items, log=lambda m: None, purpose="Mix: neue Songs prüfen")
        db.write_many([("UPDATE tracks SET tags = ? WHERE video_id = ?", (json.dumps(tags.get(vid, {})), vid))
                       for vid in candidates])
    by_track = phases.track_cluster()
    known = sound.vectors(list(candidates))
    found = []
    for vid, t in candidates.items():
        artist = ((t.get("artists") or [{}])[0].get("name") or "").lower()
        vec = known.get(vid)
        if vec is None and listen:
            listen(vid)
            vec = sound.vectors([vid]).get(vid)
        if vid in tags or vec is not None:
            best, sim = phases.best_cluster(tags.get(vid, {}), vec)
            fits = best == cluster_id and sim >= phases.FIT_SIMILARITY
        else:
            fits = _artist_fits(artist, cluster_id, by_track)
        if fits:
            found.append({"video_id": vid, "artist": artist, "new": True})
            if len(found) == n:
                break
    return found


def interleave(per_cluster: dict[int, list[dict]], weights: dict[int, float]) -> list[dict]:
    """Spreads the phases evenly over the playlist instead of block by block."""
    queues = {k: list(v) for k, v in per_cluster.items() if v}
    placed: dict[int, int] = defaultdict(int)
    out = []
    while queues:
        k = max(queues, key=lambda c: weights[c] * (len(out) + 1) - placed[c])
        out.append(queues[k].pop(0))
        placed[k] += 1
        if not queues[k]:
            del queues[k]
    return out


def _spread_new(known: list[dict], new: list[dict]) -> list[dict]:
    """Puts new songs between known ones at even distances, starting with a known song."""
    if not new:
        return known
    out, step = [], (len(known) + len(new)) / len(new)
    slots = {int(step * (i + 0.5)) for i in range(len(new))}
    ki, ni = iter(known), iter(new)
    for pos in range(len(known) + len(new)):
        item = next(ni, None) if pos in slots else next(ki, None)
        if item is None:
            item = next(ki, None) or next(ni, None)
        if item:
            out.append(item)
    return out


def build(name: str, yt=None, now: int | None = None, rng: random.Random | None = None, claude=None,
          listen=None) -> dict:
    """Builds the mix. Without yt, no new songs (radio needs YouTube Music); claude and listen
    (see find_new) check whether they fit."""
    now = now or int(time.time())
    rng = rng or random.Random()
    mix = db.one("SELECT * FROM mixes WHERE name = ?", (name,))
    if not mix:
        raise ValueError(f"Mix {name!r} gibt es nicht.")
    weights = {int(k): v for k, v in json.loads(mix["weights"]).items()}
    counts = split(mix["length"], weights)
    max_seconds = mix["max_minutes"] * 60 or None
    used: set[str] = set()
    per_cluster: dict[int, list[dict]] = {}
    short = {}
    for cid, n in counts.items():
        n_new = round(n * mix["novelty"]) if yt else 0
        pool = known_tracks(cid, now, max_seconds)
        known = pick_known(pool, n - n_new, rng, used)
        used |= {t["video_id"] for t in known}
        new = find_new(yt, cid, [t["video_id"] for t in known], n_new, used, claude, listen, max_seconds) if yt else []
        used |= {t["video_id"] for t in new}
        if len(known) + len(new) < n:
            extra = pick_known(pool, n - len(known) - len(new), rng, used)
            known += extra
            used |= {t["video_id"] for t in extra}
        if len(known) + len(new) < n:
            short[cid] = n - len(known) - len(new)
        per_cluster[cid] = _spread_new(known, new)
    ordered = interleave(per_cluster, weights)
    return {"name": mix["name"], "video_ids": [t["video_id"] for t in ordered],
            "new": sum(1 for t in ordered if t.get("new")), "short": short, "weights": weights}


def push(yt, result: dict, now: int | None = None) -> str:
    now = now or int(time.time())
    mix = db.one("SELECT * FROM mixes WHERE name = ?", (result["name"],))
    names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM clusters")}
    parts = ", ".join(f"{round(w * 100)} % {names.get(cid, cid)}" for cid, w in result["weights"].items())
    description = f"{parts}. Automatisch erstellt am {time.strftime('%d.%m.%Y', time.localtime(now))}."
    try:
        playlist_id = ytm.call(yt, ytm.replace_playlist, yt, mix["playlist_id"], f"Taktgefühl: {mix['name']}",
                               description, result["video_ids"])
    except ytm.SessionExpired:
        raise
    except Exception as e:
        if "401" in str(e):  # still signed in for reading, but writing is refused
            raise RuntimeError("YouTube Music lässt das Schreiben der Playlist nicht zu (401), obwohl Lesen noch "
                               "geht. Bitte YouTube Music neu verbinden, am besten aus einem privaten Fenster.") from e
        raise
    db.write("UPDATE mixes SET playlist_id = ?, built_at = ? WHERE name = ?", (playlist_id, now, mix["name"]))
    history.record_push(mix["name"], result["video_ids"], now)
    return playlist_id


def song_list(ids: list[str]) -> list[dict]:
    rows = {r["video_id"]: r for r in db.query(
        f"SELECT video_id, title, artist FROM tracks WHERE video_id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
    new = {t for t in ids if not db.one("SELECT 1 FROM plays WHERE video_id = ?", (t,))
           and not db.one("SELECT 1 FROM tracks WHERE video_id = ? AND liked = 1", (t,))}
    return [{"video_id": v, "title": rows[v]["title"] if v in rows else v,
             "artist": rows[v]["artist"] if v in rows else None, "new": v in new} for v in ids]


def save_result(result: dict, pushed: bool) -> None:
    """Keeps the songs of the last build, which the app shows (GET /api/mixes/{name}/result)."""
    db.set_state(f"mix_result:{result['name']}", {
        "at": int(time.time()), "pushed": pushed, "songs": song_list(result["video_ids"]),
        "short": {str(k): v for k, v in result["short"].items()}})
