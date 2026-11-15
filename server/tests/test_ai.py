import json
from types import SimpleNamespace

from app import ai, db, phases

from conftest import NOW, add_play, add_track


class FakeClaude:
    """Answers like the API: one text block with JSON, or a refusal."""

    def __init__(self, answer, refuse=False):
        self.answer = answer
        self.refuse = refuse
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0, iterations=None)
        if self.refuse:
            return SimpleNamespace(stop_reason="refusal", content=[], usage=usage, model=kwargs["model"])
        text = json.dumps(self.answer(kwargs["messages"][0]["content"]))
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)],
                               usage=usage, model=kwargs["model"])


def tagger(prompt):
    out = []
    for line in prompt.splitlines():
        if not line.startswith("- "):
            continue
        name = line[2:].split(":")[0]
        if name == "Nobody Knows":
            out.append({"name": name, "known": False, "genres": [], "moods": [], "energy": 3, "language": "de"})
        else:
            out.append({"name": name, "known": True, "genres": ["german hip hop", "hip hop"],
                        "moods": ["Verspielt"], "energy": 4, "language": "de"})
    return {"artists": out}


def test_enrich_ai_tags_and_keeps_mb_tags_for_unknown(data):
    add_track("m1", "Marsimoto", title="Grüner Samt")
    add_track("n1", "Nobody Knows")
    add_play("m1", NOW)
    db.write("INSERT INTO artists(name, tags, looked_up_at, source) VALUES('Nobody Knows', ?, 0, 'mb')",
             (json.dumps({"trap": 2.0}),))
    claude = FakeClaude(tagger)
    assert ai.enrich_ai(claude, log=lambda m: None) == 1
    request = claude.requests[0]
    assert "Grüner Samt" in request["messages"][0]["content"]
    assert request["fallbacks"] == "default"
    assert request["output_config"]["format"]["type"] == "json_schema"

    tags = json.loads(db.one("SELECT tags FROM artists WHERE name = 'Marsimoto'")["tags"])
    assert tags["german hip hop"] == 10 and tags["hip hop"] == 8
    assert tags["stimmung: verspielt"] == 3 and tags["energie: energiegeladen"] == 3
    row = db.one("SELECT tags, source FROM artists WHERE name = 'Nobody Knows'")
    assert json.loads(row["tags"]) == {"trap": 2.0} and row["source"] == "mb"
    # tagged once: nothing left except the unknown one
    assert [a for a, _ in ai.pending_artists()] == ["Nobody Knows"]


def test_refusal_skips_batch(data):
    add_track("m1", "Marsimoto")
    assert ai.enrich_ai(FakeClaude(tagger, refuse=True), log=lambda m: None) == 0


def test_mood_tags_get_no_umbrella():
    expanded = phases.expand({"german hip hop": 10, "stimmung: emotional": 3})
    assert "hip hop" in expanded and "rock" not in expanded  # "emo" is inside "emotional"


def test_name_phases_once_and_keeps_custom_names(library):
    result = phases.compute(NOW)
    phases.rename(result[-1]["id"], "Meine Rock-Ecke")

    def namer(prompt):
        ids = [int(l.split(":")[0].split()[1]) for l in prompt.splitlines() if l.startswith("Phase ")]
        return {"phases": [{"id": i, "name": f"KI-Name {i}", "description": "Beschreibung."} for i in ids]}

    claude = FakeClaude(namer)
    assert ai.name_phases(claude) == 2
    names = {c["name"] for c in phases.summary()}
    assert "Meine Rock-Ecke" in names and sum(n.startswith("KI-Name") for n in names) == 2
    assert ai.name_phases(claude) == 0  # same artists, no new request
    assert len(claude.requests) == 1
    phases.compute(NOW + 86400)
    assert {c["name"] for c in phases.summary()} == names  # recompute keeps AI names


def test_song_tags_beat_artist_tags(library):
    # A Kygo song that is really hip hop belongs to the hip hop phase, the rest of Kygo stays EDM.
    db.write("UPDATE tracks SET tags = ? WHERE video_id = 'Kygo1'",
             (json.dumps({"german hip hop": 10, "hip hop": 8}),))
    result = phases.compute(NOW)
    by_track = phases.track_cluster()
    hip = next(c["id"] for c in result if "kendrick lamar" in [a.lower() for a in c["artists"]])
    assert by_track["Kygo1"] == hip
    assert by_track["Kygo2"] != hip


def test_new_songs_must_fit_the_phase_as_songs(library):
    from app import mixer

    result = phases.compute(NOW)
    edm = next(c["id"] for c in result if "kygo" in [a.lower() for a in c["artists"]])

    class YT:
        def get_watch_playlist(self, videoId, radio, limit):
            return {"tracks": [
                {"videoId": "fits", "title": "Warm House", "artists": [{"name": "Some DJ"}]},
                {"videoId": "kygo-rock", "title": "Kygo goes rock", "artists": [{"name": "Kygo"}]},
            ]}

    def song_tagger(prompt):
        songs = {"fits": ["deep house", "house"], "kygo-rock": ["alternative rock", "rock"]}
        return {"songs": [{"id": vid, "known": True, "genres": g, "moods": [], "sound": [], "energy": 3,
                           "language": "en"} for vid, g in songs.items()]}

    found = mixer.find_new(YT(), edm, ["Kygo0"], 2, set(), FakeClaude(song_tagger))
    assert [f["video_id"] for f in found] == ["fits"]
    assert json.loads(db.one("SELECT tags FROM tracks WHERE video_id = 'kygo-rock'")["tags"])["rock"] == 8


def test_unknown_song_falls_back_to_artist(library):
    db.write("UPDATE tracks SET tags = '{}' WHERE video_id = 'Kygo1'")
    phases.compute(NOW)
    assert "Kygo1" in phases.track_cluster()
