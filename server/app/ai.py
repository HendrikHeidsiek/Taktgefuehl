"""Optional Claude integration: tags songs and artists (genre, mood, sound, energy) and names the phases.

Songs come first: the listener often loves one song of an artist and nothing else, so a song's own
character matters more than its artist's. Artist tags only stand in for songs the model doesn't know.

Active when ANTHROPIC_API_KEY is set or data/anthropic.key exists (python -m app.auth anthropic).
Only artist names, a few song titles and phase summaries leave the server; no account data.
"""
import json
import os
import time
from pathlib import Path

from . import costs, db, phases, report

DEFAULT_MODEL = os.environ.get("AI_MODEL", "claude-opus-5-5")
BATCH = 40
KEY_FILE = "anthropic.key"
RENAME_AFTER = 0.4  # share of a phase's songs that must have changed before the AI names it again
ENERGY = {1: "sehr ruhig", 2: "ruhig", 3: "mittel", 4: "energiegeladen", 5: "sehr energiegeladen"}

TAG_SCHEMA = {
    "type": "object",
    "properties": {"artists": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "known": {"type": "boolean"},
            "genres": {"type": "array", "items": {"type": "string"}},
            "moods": {"type": "array", "items": {"type": "string"}},
            "energy": {"type": "integer"},
            "language": {"type": "string"},
        },
        "required": ["name", "known", "genres", "moods", "energy", "language"],
        "additionalProperties": False,
    }}},
    "required": ["artists"],
    "additionalProperties": False,
}

TRACK_SCHEMA = {
    "type": "object",
    "properties": {"songs": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "known": {"type": "boolean"},
            "genres": {"type": "array", "items": {"type": "string"}},
            "moods": {"type": "array", "items": {"type": "string"}},
            "sound": {"type": "array", "items": {"type": "string"}},
            "energy": {"type": "integer"},
            "language": {"type": "string"},
        },
        "required": ["id", "known", "genres", "moods", "sound", "energy", "language"],
        "additionalProperties": False,
    }}},
    "required": ["songs"],
    "additionalProperties": False,
}

NAME_SCHEMA = {
    "type": "object",
    "properties": {"phases": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}, "description": {"type": "string"}},
        "required": ["id", "name", "description"],
        "additionalProperties": False,
    }}},
    "required": ["phases"],
    "additionalProperties": False,
}

TAG_PROMPT = """Ordne diese Interpreten musikalisch ein. Hinter jedem Namen stehen Songs aus der Bibliothek \
des Hörers, damit du gleichnamige Interpreten unterscheiden kannst.

Pro Interpret:
- genres: 2 bis 4 Genres auf Englisch in der üblichen Schreibweise von Musikdatenbanken, klein geschrieben, \
vom passendsten zum allgemeinsten (z. B. "german hip hop", "conscious hip hop", "hip hop"). \
Gleiche Musik bekommt gleiche Begriffe, damit sich Interpreten vergleichen lassen.
- moods: 2 bis 3 Stimmungen auf Deutsch, je ein Adjektiv (z. B. "melancholisch", "verspielt", "aggressiv").
- energy: 1 (sehr ruhig) bis 5 (sehr energiegeladen).
- language: Hauptsprache der Texte als ISO-Code ("de", "en", ...) oder "instrumental".
- known: false, wenn du den Interpreten nicht sicher kennst. Dann leite die Angaben nur vorsichtig aus den \
Songtiteln ab oder lass genres und moods leer.

Gib die Namen exakt so zurück, wie sie hier stehen.

{lines}"""

TRACK_PROMPT = """Ordne diese Songs musikalisch ein, jeden für sich. Es geht um den einzelnen Song, nicht um \
den Interpreten: Ein Hörer mag oft genau einen Song eines Interpreten, weil er etwas Besonderes hat, \
und den Rest nicht. Beschreibe also, was diesen Song ausmacht, auch wenn er vom üblichen Stil des \
Interpreten abweicht.

Pro Song (Zeile: [id] Interpret – Titel):
- genres: 2 bis 4 Genres auf Englisch in der üblichen Schreibweise von Musikdatenbanken, klein geschrieben, \
vom passendsten zum allgemeinsten (z. B. "melodic deep house", "deep house", "house"). \
Gleiche Musik bekommt gleiche Begriffe, damit sich Songs vergleichen lassen.
- moods: 2 bis 3 Stimmungen auf Deutsch, je ein Adjektiv (z. B. "melancholisch", "euphorisch", "verspielt").
- sound: 2 bis 3 prägende Klangmerkmale auf Deutsch, je 1 bis 3 Wörter, aus einem einheitlichen Wortschatz \
(z. B. "warme synths", "gefühlvoller gesang", "treibender bass", "sprechgesang", "akustische gitarre").
- energy: 1 (sehr ruhig) bis 5 (sehr energiegeladen).
- language: Hauptsprache des Textes als ISO-Code ("de", "en", ...) oder "instrumental".
- known: false, wenn du den Song nicht sicher kennst. Dann lass genres, moods und sound leer, \
statt vom Interpreten oder Titel zu raten.

Gib die id exakt so zurück, wie sie in eckigen Klammern steht.

{lines}"""

NAME_PROMPT = """Ein Hörer hat diese Hör-Phasen, automatisch aus seinem YouTube-Music-Verlauf gruppiert. \
Gib jeder Phase einen kurzen deutschen Namen (2 bis 4 Wörter, z. B. "Nachdenklicher Deutschrap" oder \
"Melodischer Deep House") und eine Beschreibung in einem Satz, was die Phase klanglich ausmacht. \
Die Namen sollen sich deutlich voneinander unterscheiden.

{phases}"""


def api_key(data_path: Path) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    f = data_path / KEY_FILE
    return f.read_text().strip() if f.exists() else None


def client(data_path: Path):
    key = api_key(data_path)
    if not key:
        return None
    import anthropic
    return anthropic.Anthropic(api_key=key)


def model() -> str:
    """The model chosen in the app, else AI_MODEL / the default."""
    return db.get_state("ai_model") or DEFAULT_MODEL


def ask(claude, prompt: str, schema: dict, purpose: str = "Sonstiges") -> dict | None:
    """One structured request, booked under purpose (costs.py). None if the model declined.
    Effort and the server-side fallback are only sent to models that support them (per the model list;
    unknown models get both)."""
    name = model()
    info = costs.model_info(name) or {"effort_low": True, "fallback": True}
    output_config = {"format": {"type": "json_schema", "schema": schema}}
    if info["effort_low"]:
        output_config["effort"] = "low"
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if info["fallback"] else {}
    response = claude.beta.messages.create(
        model=name,
        max_tokens=16000,
        output_config=output_config,
        messages=[{"role": "user", "content": prompt}],
        **extra,
    )
    costs.book_response(response, purpose)
    if response.stop_reason == "refusal":
        return None
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def tags_from_ai(entry: dict) -> dict[str, float]:
    """Genres weigh most (the first one fully), moods and energy less; all end up as clustering dimensions."""
    tags: dict[str, float] = {}
    for i, g in enumerate(entry["genres"][:4]):
        tags[g.strip().lower()] = 10.0 - 2 * i
    for m in entry["moods"][:3]:
        tags[f"stimmung: {m.strip().lower()}"] = 3.0
    for k in entry.get("sound", [])[:3]:
        tags[f"klang: {k.strip().lower()}"] = 3.0
    if entry["genres"]:
        tags[f"energie: {ENERGY.get(min(5, max(1, entry['energy'])), 'mittel')}"] = 3.0
    return tags


def tag_tracks(claude, items: list[tuple[str, str, str]], log=print,
               purpose: str = "Songs einordnen") -> dict[str, dict[str, float]]:
    """Tags songs given as (video_id, artist, title). Returns {video_id: tags} for the songs the model knows."""
    out = {}
    for start in range(0, len(items), BATCH):
        batch = items[start:start + BATCH]
        lines = "\n".join(f"- [{vid}] {artist or '?'} – {title}" for vid, artist, title in batch)
        result = ask(claude, TRACK_PROMPT.format(lines=lines), TRACK_SCHEMA, purpose)
        if result is None:
            report.warn(log, f"Block {start // BATCH + 1} mit {len(batch)} Songs vom Modell abgelehnt, ohne KI-Tags")
            continue
        for entry in result["songs"]:
            if entry["known"] and entry["genres"]:
                out[entry["id"]] = tags_from_ai(entry)
    return out


def enrich_tracks(claude, limit: int | None = None, retag: bool = False, log=print) -> int:
    """Tags played or liked songs, most played first. Unknown songs get '{}' (artist tags stand in)."""
    rows = db.query(
        "SELECT t.video_id, t.artist, t.title, COUNT(p.id) AS plays FROM tracks t "
        "LEFT JOIN plays p ON p.video_id = t.video_id WHERE (? OR t.tags IS NULL) "
        "GROUP BY t.video_id HAVING plays > 0 OR t.liked = 1 ORDER BY plays DESC, t.video_id",
        (1 if retag else 0,))[:limit]
    items = [(r["video_id"], r["artist"], r["title"]) for r in rows]
    tagged = 0
    for start in range(0, len(items), BATCH * 5):
        chunk = items[start:start + BATCH * 5]
        tags = tag_tracks(claude, chunk, log)
        db.write_many([("UPDATE tracks SET tags = ? WHERE video_id = ?", (json.dumps(tags.get(vid, {})), vid))
                       for vid, _, _ in chunk])
        tagged += len(tags)
        report.progress(log, start + len(chunk), len(items), f"  {start + len(chunk)}/{len(items)} Songs eingeordnet")
    return tagged


def pending_artists(retag: bool = False) -> list[tuple[str, list[str]]]:
    """Artists (most played first) not yet tagged by the AI that are still needed, i.e. have songs
    without their own tags; each with up to 3 song titles."""
    rows = db.query(
        "SELECT t.artist, GROUP_CONCAT(t.title, ' | ') AS titles, COUNT(p.id) AS plays "
        "FROM tracks t LEFT JOIN plays p ON p.video_id = t.video_id LEFT JOIN artists a ON a.name = t.artist "
        "WHERE t.artist IS NOT NULL AND (? OR a.source IS NULL OR a.source != 'ai') "
        "AND (t.tags IS NULL OR t.tags = '{}') "
        "GROUP BY t.artist COLLATE NOCASE ORDER BY plays DESC, t.artist", (1 if retag else 0,))
    return [(r["artist"], (r["titles"] or "").split(" | ")[:3]) for r in rows]


def enrich_ai(claude, limit: int | None = None, retag: bool = False, log=print) -> int:
    """Tags artists in batches. Returns how many got tags."""
    todo = pending_artists(retag)[:limit]
    now = int(time.time())
    tagged = 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start:start + BATCH]
        lines = "\n".join(f"- {name}: {', '.join(titles)}" for name, titles in batch)
        result = ask(claude, TAG_PROMPT.format(lines=lines), TAG_SCHEMA, "Interpreten einordnen")
        if result is None:
            report.warn(log, f"Block {start // BATCH + 1} mit {len(batch)} Interpreten vom Modell abgelehnt")
            continue
        by_name = {e["name"].lower(): e for e in result["artists"]}
        statements = []
        for name, _ in batch:
            entry = by_name.get(name.lower())
            if not entry:
                continue
            tags = tags_from_ai(entry) if entry["known"] or entry["genres"] else {}
            tagged += bool(tags)
            # Unknown to the AI: keep what MusicBrainz had, only mark it as looked at.
            statements.append((
                "INSERT INTO artists(name, tags, looked_up_at, source) VALUES(?,?,?,'ai') ON CONFLICT(name) DO UPDATE "
                "SET tags = CASE WHEN excluded.tags != '{}' THEN excluded.tags ELSE tags END, "
                "looked_up_at = excluded.looked_up_at, source = CASE WHEN excluded.tags != '{}' THEN 'ai' ELSE source END",
                (name, json.dumps(tags), now)))
        db.write_many(statements)
        report.progress(log, min(start + BATCH, len(todo)), len(todo),
                        f"  {min(start + BATCH, len(todo))}/{len(todo)} Interpreten eingeordnet")
    return tagged


def name_phases(claude, force: bool = False) -> int:
    """Names phases without a custom name whose songs changed by more than RENAME_AFTER since the last
    naming (all members count, not just the top songs: a phase can keep its favourites and still lose
    half its character)."""
    todo = []
    for r in db.query("SELECT id, data FROM clusters WHERE active = 1 AND custom_name = 0"):
        data = json.loads(r["data"])
        members = {m["video_id"] for m in db.query("SELECT video_id FROM cluster_tracks WHERE cluster_id = ?", (r["id"],))}
        before = set(data.get("named_members", []))
        same = len(members & before) / len(members | before) if members | before else 0
        if force or same < 1 - RENAME_AFTER:
            top = [f"{s['artist']} – {s['title']}" for s in phases.top_songs(r["id"], 8)]
            todo.append((r["id"], data, top, sorted(members)))
    if not todo:
        return 0
    listing = "\n\n".join(
        f"Phase {cid}: meistgehörte Songs {'; '.join(top)}. Wichtigste Interpreten: "
        f"{', '.join(phases.top_artists(cid, 8))}. Häufigste Merkmale: {', '.join(data.get('top_tags', []))}."
        + (f" Klangstile laut Audio-Analyse: {', '.join(data['sound_styles'])}." if data.get("sound_styles") else "")
        for cid, data, top, _ in todo)
    result = ask(claude, NAME_PROMPT.format(phases=listing), NAME_SCHEMA, "Phasen benennen")
    if result is None:
        return 0
    named = {p["id"]: p for p in result["phases"]}
    statements = []
    for cid, data, top, members in todo:
        if cid in named:
            data.pop("named_for", None)
            data.update(description=named[cid]["description"], named_members=members)
            statements.append(("UPDATE clusters SET name = ?, data = ? WHERE id = ? AND custom_name = 0",
                               (named[cid]["name"], json.dumps(data), cid)))
    db.write_many(statements)
    return len(statements)
