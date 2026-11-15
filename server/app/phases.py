"""Finds taste groups ("phases") among the songs you play and tracks when each one is active.

Songs, not artists: a single song you love says nothing about the rest of its artist's work.
1. Every played or liked song becomes a vector: its sound fingerprint (sound.py, ~64 %) and its tags
   (ai.py; the artist's when the song is unknown, ~36 %). Its weight comes only from plays and likes.
2. Ward clustering groups songs that sound alike, with the number of phases that separates best
   (tags only, without fingerprints: average-linkage clustering on cosine distance).
3. Tiny groups are folded into their nearest big one, so the result stays a handful of phases.
4. Groups keep their id (and their name) across recomputations by song overlap.
5. For each group we store weekly listening shares; a group whose recent share is clearly above
   its long-term share is a currently active phase.
"""
import json
import math
import time
from collections import defaultdict

import numpy as np
from sklearn.cluster import AgglomerativeClustering
from sklearn.metrics import silhouette_score

from . import db, sound

DISTANCE = 0.75  # cosine distance at which groups stop merging; higher = fewer, broader phases (tags only)
K_RANGE = (6, 14)  # with sound: Ward clustering, number of phases chosen by the clearest separation (fewer
                   # than 6 lumps ambient and big beat together on a real library)
IMPUTE_NEIGHBOURS = 5  # songs not analysed yet borrow the sound of their most tag-similar analysed songs
SOUND_WEIGHT, TAG_WEIGHT = 0.8, 0.6  # sound counts ~64 %, tags ~36 % of the combined similarity
MIN_SOUND_COVERAGE = 0.3  # below this share of analysed songs, phases use tags alone
MIN_SHARE = 0.04  # groups below this share of all listening are folded into a neighbour...
MIN_MERGE_SIMILARITY = 0.3  # ...if they are at least this similar,
MIN_KEEP = 0.01  # otherwise they stay a small phase of their own if they reach this share
FIT_SIMILARITY = 0.35  # a new song must be at least this close to a phase's centre to join its mixes
CENTROID_DIMS = 40
UMBRELLA_WEIGHT = 1.0
# Umbrella genre -> substrings of genre tags that belong to it.
UMBRELLAS = {
    "hip hop": ("hip hop", "hip-hop", "rap", "trap", "drill", "grime", "boom bap"),
    "electronic": ("house", "techno", "trance", "edm", "electro", "dubstep", "drum and bass", "garage",
                   "bass music", "big room", "electronic", "hardstyle", "future bass", "synthwave"),
    "rock": ("rock", "grunge", "punk", "metal", "emo", "shoegaze"),
    "pop": ("pop",),
    "r&b": ("r&b", "rnb", "soul", "neo soul"),
    "jazz": ("jazz", "swing", "bebop"),
    "classical": ("classical", "baroque", "romantic era", "opera", "orchestral"),
    "folk": ("folk", "singer-songwriter", "americana", "country"),
    "latin": ("reggaeton", "latin", "salsa", "bachata", "dembow"),
    "reggae": ("reggae", "dancehall", "dub"),
}
MIX_PLAY_WEIGHT = 0.25  # a play from a Taktgefühl mix says much less about your taste than one you chose
MATCH_OVERLAP = 0.3  # weighted song overlap needed to treat a new group as an old one
RECENT_DAYS = 14
WEEKS = 26
DAY = 86400


def track_weights(now: int) -> dict[str, float]:
    """Plays with a slow decay (half-life one year) plus a bonus for a like. Plays from the mixes count
    little: they come from the mixes, not from you, and would otherwise confirm the mixes' own picks."""
    weights: dict[str, float] = defaultdict(float)
    for r in db.query("SELECT video_id, at, via FROM plays"):
        weights[r["video_id"]] += (MIX_PLAY_WEIGHT if r["via"] else 1) * 0.5 ** (max(0, now - r["at"]) / DAY / 365)
    for r in db.query("SELECT video_id FROM tracks WHERE liked = 1"):
        weights[r["video_id"]] += 3
    return weights


def track_tags() -> dict[str, dict[str, float]]:
    """The song's own tags, or its artist's when the song has none."""
    artist_tags = {r["name"].lower(): json.loads(r["tags"])
                   for r in db.query("SELECT name, tags FROM artists WHERE tags != '{}'")}
    out = {}
    for r in db.query("SELECT video_id, artist, tags FROM tracks"):
        # '{}' means the AI did not know the song: the artist stands in, like for untagged songs.
        tags = json.loads(r["tags"] or "{}") or artist_tags.get((r["artist"] or "").lower())
        if tags:
            out[r["video_id"]] = tags
    return out


def impute_sound(keys: list[str], sounds: dict[str, np.ndarray], t: np.ndarray) -> np.ndarray:
    """Rows of sound fingerprints for keys. A song without its own borrows the mean of its most
    tag-similar analysed songs, so all songs live in one space while the analysis catches up."""
    own = [i for i, k in enumerate(keys) if k in sounds]
    out = np.zeros((len(keys), sound.DIM))
    if not own:
        return out
    known, known_tags = np.stack([sounds[keys[i]] for i in own]), t[own]
    for i, k in enumerate(keys):
        if k in sounds:
            out[i] = sounds[k]
        elif t[i].any():
            nn = np.argsort(-(known_tags @ t[i]))[:IMPUTE_NEIGHBOURS]
            v = known[nn].mean(axis=0)
            out[i] = v / (np.linalg.norm(v) or 1)
    return out


def sound_styles() -> dict[str, list[str]]:
    return {r["video_id"]: json.loads(r["styles"]) for r in db.query("SELECT video_id, styles FROM track_sound WHERE ok = 1")}


def expand(tags: dict[str, float]) -> dict[str, float]:
    """Adds umbrella genres, so "conscious hip hop" and "german hip hop" share a dimension."""
    top = max(tags.values())
    out: dict[str, float] = defaultdict(float)
    for t, v in tags.items():
        v = math.sqrt(v / top)
        out[t] = max(out[t], v)
        if ":" in t:  # "stimmung: …" / "energie: …" from the AI tags have no umbrella
            continue
        for umbrella, keywords in UMBRELLAS.items():
            if t != umbrella and any(k in t for k in keywords):
                out[umbrella] = max(out[umbrella], v * UMBRELLA_WEIGHT)
    return out


def vectors(tags_by_key: dict[str, dict[str, float]]) -> tuple[list[str], list[str], np.ndarray]:
    """Rows = keys (songs), columns = tags incl. umbrellas, L2-normalised."""
    keys = sorted(tags_by_key)
    expanded = {k: expand(tags_by_key[k]) if tags_by_key[k] else {} for k in keys}
    vocab = sorted({t for tags in expanded.values() for t in tags})
    col = {t: i for i, t in enumerate(vocab)}
    m = np.zeros((len(keys), len(vocab)))
    for i, k in enumerate(keys):
        for t, v in expanded[k].items():
            m[i, col[t]] = v
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    m = np.divide(m, norms, out=np.zeros_like(m), where=norms > 0)
    return keys, vocab, m


def ward_labels(m: np.ndarray) -> np.ndarray:
    """Ward clustering with the number of groups that separates best (silhouette). Average linkage chains
    real libraries into one giant group, because real songs sound more alike than hand-picked ones."""
    best = None
    for k in range(K_RANGE[0], min(K_RANGE[1], len(m) - 1) + 1):
        labels = AgglomerativeClustering(n_clusters=k, linkage="ward").fit_predict(m)
        score = silhouette_score(m, labels, metric="cosine")
        if best is None or score > best[0]:
            best = (score, labels)
    return best[1] if best else np.zeros(len(m), dtype=int)


def cluster(m: np.ndarray, w: np.ndarray, sound_based: bool = False) -> list[list[int]]:
    """Index groups of the rows of m, biggest (by weight w) first."""
    if len(m) == 1:
        return [[0]]
    if sound_based:
        labels = ward_labels(m)
    else:
        labels = AgglomerativeClustering(n_clusters=None, metric="cosine", linkage="average",
                                         distance_threshold=DISTANCE).fit_predict(m)
    groups: dict[int, list[int]] = defaultdict(list)
    for i, label in enumerate(labels):
        groups[label].append(i)
    ordered = sorted(groups.values(), key=lambda g: -w[g].sum())
    total = w.sum()

    def centroid(g):
        c = (m[g] * w[g, None]).sum(axis=0)
        return c / (np.linalg.norm(c) or 1)

    n_big = max(1, sum(w[g].sum() / total >= MIN_SHARE for g in ordered))
    big = ordered[:n_big]
    centroids = [centroid(b) for b in big]
    for g in ordered[n_big:]:
        sims = [float(centroid(g) @ c) for c in centroids]
        best = int(np.argmax(sims))
        if sims[best] >= MIN_MERGE_SIMILARITY:
            big[best] = big[best] + g
        elif w[g].sum() / total >= MIN_KEEP:
            big.append(g)
            centroids.append(centroid(g))
    return sorted(big, key=lambda g: -w[g].sum())


def _name(top_tags: list[str]) -> str:
    genres = [t for t in top_tags if ":" not in t]
    return " / ".join(t.title() for t in genres[:2]) or "Unbenannt"


def _overlap(a: dict[str, float], b: dict[str, float]) -> float:
    keys = a.keys() | b.keys()
    lo = sum(min(a.get(k, 0), b.get(k, 0)) for k in keys)
    hi = sum(max(a.get(k, 0), b.get(k, 0)) for k in keys)
    return lo / hi if hi else 0.0


def compute(now: int | None = None) -> list[dict]:
    """Recomputes the groups and their timelines. Returns summary(), the new state."""
    now = now or int(time.time())
    weights = track_weights(now)
    played = [k for k, v in weights.items() if v > 0]
    tags = track_tags()
    sounds = {k: v / (np.linalg.norm(v) or 1) for k, v in sound.vectors(played).items()}
    use_sound = len(sounds) >= MIN_SOUND_COVERAGE * len(played)
    keys = sorted(k for k in played if k in tags or (use_sound and k in sounds))
    if not keys:
        return []
    _, vocab, t = vectors({k: tags.get(k, {}) for k in keys})
    w = np.array([weights[k] for k in keys])
    if use_sound:
        snd = impute_sound(keys, sounds, t)
        m = np.hstack([SOUND_WEIGHT * snd, TAG_WEIGHT * t])
        m /= np.linalg.norm(m, axis=1, keepdims=True)
        groups = cluster(m, w, sound_based=True)
    else:
        snd = None
        groups = cluster(t, w)
    styles = sound_styles()

    new = []
    for g in groups:
        c = (t[g] * w[g, None]).sum(axis=0)
        c /= np.linalg.norm(c) or 1
        order = np.argsort(-c)
        group = {"members": {keys[i]: float(w[i]) for i in g},
                 "top_tags": [vocab[i] for i in order[:8] if c[i] > 0],
                 "centroid": {vocab[i]: round(float(c[i]), 4) for i in order[:CENTROID_DIMS] if c[i] > 0}}
        if snd is not None:
            sc = (snd[g] * w[g, None]).sum(axis=0)
            group["sound_centroid"] = [round(float(x), 4) for x in sc / (np.linalg.norm(sc) or 1)]
            counts: dict[str, float] = defaultdict(float)
            for i in g:
                for rank, style in enumerate(styles.get(keys[i], [])[:3]):
                    counts[style] += w[i] / (rank + 1)
            group["sound_styles"] = [k for k, _ in sorted(counts.items(), key=lambda x: -x[1])[:5]]
        new.append(group)

    old = {}
    for r in db.query("SELECT id, name, custom_name, data FROM clusters WHERE active = 1"):
        members = {t["video_id"]: t["weight"] for t in
                   db.query("SELECT video_id, weight FROM cluster_tracks WHERE cluster_id = ?", (r["id"],))}
        old[r["id"]] = {"name": r["name"], "custom": r["custom_name"], "members": members,
                        "data": json.loads(r["data"])}

    pairs = sorted(((_overlap(n["members"], o["members"]), i, oid)
                    for i, n in enumerate(new) for oid, o in old.items()), reverse=True)
    taken_new, taken_old = set(), set()
    for score, i, oid in pairs:
        if score < MATCH_OVERLAP or i in taken_new or oid in taken_old:
            continue
        new[i]["id"] = oid
        taken_new.add(i)
        taken_old.add(oid)

    db.write("UPDATE clusters SET active = 0 WHERE active = 1")
    statements = []
    for g in new:
        data = {k: g[k] for k in ("top_tags", "centroid", "sound_centroid", "sound_styles") if k in g}
        if "id" in g:
            o = old[g["id"]]
            # Names from the user or the AI (ai.name_phases) stay; the AI renames when the core songs change.
            kept = {k: o["data"][k] for k in ("description", "named_for", "named_members") if k in o["data"]}
            name = o["name"] if o["custom"] or kept else _name(g["top_tags"])
            statements.append(("UPDATE clusters SET name = ?, data = ?, active = 1, updated_at = ? WHERE id = ?",
                               (name, json.dumps({**data, **kept}), now, g["id"])))
            statements.append(("DELETE FROM cluster_tracks WHERE cluster_id = ?", (g["id"],)))
        else:
            g["id"] = db.write("INSERT INTO clusters(name, data, updated_at) VALUES(?,?,?)",
                               (_name(g["top_tags"]), json.dumps(data), now)).lastrowid
        statements += [("INSERT INTO cluster_tracks(cluster_id, video_id, weight) VALUES(?,?,?)", (g["id"], k, wt))
                       for k, wt in g["members"].items()]
    db.write_many(statements)
    _store_timelines(now)
    return summary()


def track_cluster() -> dict[str, int]:
    return {r["video_id"]: r["cluster_id"] for r in db.query(
        "SELECT ct.video_id, ct.cluster_id FROM cluster_tracks ct JOIN clusters c ON c.id = ct.cluster_id "
        "WHERE c.active = 1")}


def best_cluster(tags: dict[str, float], vec: np.ndarray | None = None) -> tuple[int | None, float]:
    """The phase whose centre is closest to a song, and the similarity. Sound and tags are weighted like
    in compute() when both exist on both sides; otherwise whatever is there decides."""
    song = expand(tags) if tags else {}
    norm = math.sqrt(sum(v * v for v in song.values())) or 1
    if vec is not None:
        vec = vec / (np.linalg.norm(vec) or 1)
    ws, wt = SOUND_WEIGHT ** 2, TAG_WEIGHT ** 2
    best, best_sim = None, 0.0
    for r in db.query("SELECT id, data FROM clusters WHERE active = 1"):
        data = json.loads(r["data"])
        parts = []
        if song:
            parts.append((wt, sum(v * data.get("centroid", {}).get(t, 0) for t, v in song.items()) / norm))
        if vec is not None and data.get("sound_centroid"):
            parts.append((ws, float(vec @ np.array(data["sound_centroid"]))))
        if not parts:
            continue
        sim = sum(w * x for w, x in parts) / sum(w for w, _ in parts)
        if sim > best_sim:
            best, best_sim = r["id"], sim
    return best, best_sim


def _store_timelines(now: int) -> None:
    """Shares count only what you played yourself: listening to a 30/70 mix would otherwise make the
    phases look 30/70, and the active phase would be whatever the mixes push."""
    by_track = track_cluster()
    week = 7 * DAY
    start = now - WEEKS * week
    weekly = defaultdict(lambda: [0] * WEEKS)
    recent: dict[int, int] = defaultdict(int)
    total: dict[int, int] = defaultdict(int)
    for r in db.query("SELECT video_id, at FROM plays WHERE via IS NULL"):
        cid = by_track.get(r["video_id"])
        if cid is None:
            continue
        total[cid] += 1
        if r["at"] >= start:
            weekly[cid][min(WEEKS - 1, (r["at"] - start) // week)] += 1
        if r["at"] >= now - RECENT_DAYS * DAY:
            recent[cid] += 1
    all_total = sum(total.values()) or 1
    all_recent = sum(recent.values())
    week_sums = [sum(weekly[c][i] for c in weekly) for i in range(WEEKS)]
    for r in db.query("SELECT id, data FROM clusters WHERE active = 1"):
        cid = r["id"]
        data = json.loads(r["data"])
        data["share_total"] = total[cid] / all_total
        data["share_recent"] = recent[cid] / all_recent if all_recent else None
        data["weekly_share"] = [round(weekly[cid][i] / week_sums[i], 3) if week_sums[i] else None
                                for i in range(WEEKS)]
        data["plays"] = total[cid]
        db.write("UPDATE clusters SET data = ? WHERE id = ?", (json.dumps(data), cid))


def status(share_recent: float | None, share_total: float) -> str:
    if share_recent is None:
        return "keine aktuellen Daten"
    ratio = share_recent / share_total if share_total else 0
    if share_recent >= 0.2 and ratio >= 1.3:
        return "aktive Phase"
    if ratio <= 0.4:
        return "ruht"
    return "läuft mit"


def top_artists(cluster_id: int, n: int = 6) -> list[str]:
    return [r["artist"] for r in db.query(
        "SELECT t.artist, SUM(ct.weight) AS w FROM cluster_tracks ct JOIN tracks t ON t.video_id = ct.video_id "
        "WHERE ct.cluster_id = ? AND t.artist IS NOT NULL GROUP BY t.artist COLLATE NOCASE ORDER BY w DESC LIMIT ?",
        (cluster_id, n))]


def top_songs(cluster_id: int, n: int = 5) -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT t.video_id, t.artist, t.title FROM cluster_tracks ct JOIN tracks t ON t.video_id = ct.video_id "
        "WHERE ct.cluster_id = ? ORDER BY ct.weight DESC LIMIT ?", (cluster_id, n))]


def summary() -> list[dict]:
    out = []
    for r in db.query("SELECT id, name, data FROM clusters WHERE active = 1"):
        data = json.loads(r["data"])
        data.pop("centroid", None)
        data.pop("sound_centroid", None)
        data.pop("named_members", None)
        songs = db.one("SELECT COUNT(*) AS n FROM cluster_tracks WHERE cluster_id = ?", (r["id"],))["n"]
        out.append({"id": r["id"], "name": r["name"], "artists": top_artists(r["id"]), "songs": songs,
                    "status": status(data.get("share_recent"), data.get("share_total", 0)), **data})
    return sorted(out, key=lambda c: -c.get("share_total", 0))


def rename(cluster_id: int, name: str) -> bool:
    return db.write("UPDATE clusters SET name = ?, custom_name = 1 WHERE id = ? AND active = 1",
                    (name, cluster_id)).rowcount > 0


def resolve(ref: str) -> int | None:
    """Phase by id or (case-insensitive) name prefix."""
    if ref.isdigit():
        row = db.one("SELECT id FROM clusters WHERE id = ? AND active = 1", (int(ref),))
        return row["id"] if row else None
    rows = db.query("SELECT id, name FROM clusters WHERE active = 1")
    for match in ([r["id"] for r in rows if r["name"].lower() == ref.lower()],
                  [r["id"] for r in rows if r["name"].lower().startswith(ref.lower())]):
        if len(match) == 1:
            return match[0]
    return None
