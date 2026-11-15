import json

from app import db, enrich

from conftest import add_play, add_track, NOW


def fake_get(path, params):
    if path == "artist":
        q = params["query"]
        if "Kendrick" in q:
            return {"artists": [{"id": "mb-k", "name": "Kendrick Lamar", "score": 100}]}
        if "Unknown" in q:
            return {"artists": [{"id": "mb-x", "name": "Someone Else", "score": 95}]}
        return {"artists": []}
    if path == "artist/mb-k":
        return {"genres": [{"name": "Hip Hop", "count": 8}, {"name": "conscious hip hop", "count": 3}],
                "tags": [{"name": "seen live", "count": 4}]}
    raise AssertionError(path)


def test_tags_fall_back_to_filtered_tags():
    assert enrich.tags_from_mb({"tags": [{"name": "seen live", "count": 5}, {"name": "trap", "count": 2}]}) \
        == {"trap": 1.0}


def test_enrich_mb_and_related(data):
    add_track("k1", "Kendrick Lamar")
    add_track("u1", "Unknown Rapper")
    add_play("k1", NOW)
    assert enrich.enrich_mb(get=fake_get, log=lambda m: None) == 1
    tags = json.loads(db.one("SELECT tags FROM artists WHERE name = 'kendrick lamar'")["tags"])
    assert tags == {"hip hop": 8.0, "conscious hip hop": 3.0}
    # name mismatch -> stored as a miss, not looked up again right away
    assert db.one("SELECT tags FROM artists WHERE name = 'Unknown Rapper'")["tags"] == "{}"
    assert enrich.pending_artists(NOW) == []

    class FakeYT:
        def get_artist(self, channel_id):
            return {"related": {"results": [{"title": "Kendrick Lamar"}, {"title": "Nobody"}]}}

    assert enrich.fill_from_related(FakeYT()) == 1
    filled = json.loads(db.one("SELECT tags FROM artists WHERE name = 'Unknown Rapper'")["tags"])
    assert max(filled, key=filled.get) == "hip hop"
