import random

import pytest

from app import db, mixer, phases, ytm

from conftest import GENRES, NOW


def _family(cluster):
    for family, artists in GENRES.items():
        if set(a.lower() for a in artists) >= set(a.lower() for a in cluster["artists"]):
            return family
    return None


def test_phases_found_and_edm_is_active(library):
    result = phases.compute(NOW)
    families = {_family(c): c for c in result}
    assert set(families) == {"hip hop", "edm", "rock"}, [c["artists"] for c in result]
    assert families["edm"]["status"] == "aktive Phase"
    assert families["hip hop"]["status"] in ("ruht", "läuft mit")
    weekly = families["hip hop"]["weekly_share"]
    assert weekly[0] > weekly[-1]


def test_ids_and_custom_names_survive_recompute(library):
    first = {_family(c): c["id"] for c in phases.compute(NOW)}
    assert phases.rename(first["hip hop"], "Rap-Zeit")
    second = {_family(c): c for c in phases.compute(NOW + 86400)}
    assert second["hip hop"]["id"] == first["hip hop"]
    assert second["hip hop"]["name"] == "Rap-Zeit"
    assert phases.resolve("rap") == first["hip hop"]


class FakeYT:
    def __init__(self):
        self.playlists = {}
        self.calls = []

    def get_watch_playlist(self, videoId, radio, limit):
        artist = "Unheard DJ" if videoId[:4] in ("Avic", "Mart", "Fred", "Kygo", "Lane") else "Kendrick Lamar"
        tracks = [{"videoId": f"new-{videoId}-{i}", "title": f"New {i}", "artists": [{"name": f"{artist} {i % 4}"}]}
                  for i in range(10)]
        # a song by a rock artist must never sneak into an EDM mix
        tracks.insert(0, {"videoId": f"rock-{videoId}", "title": "R", "artists": [{"name": "Muse"}]})
        return {"tracks": tracks}

    def create_playlist(self, title, description, privacy_status, video_ids):
        self.playlists["PL1"] = list(video_ids)
        return "PL1"

    def get_playlist(self, playlist_id, limit=None):
        return {"tracks": [{"videoId": v, "setVideoId": "s" + v} for v in self.playlists[playlist_id]]}

    def remove_playlist_items(self, playlist_id, items):
        self.calls.append(("remove", len(items)))
        self.playlists[playlist_id] = []

    def edit_playlist(self, playlist_id, description):
        self.calls.append(("edit", description))

    def add_playlist_items(self, playlist_id, ids, duplicates):
        self.playlists[playlist_id] += ids


def test_mix_30_70_with_new_songs_and_push(library):
    phases.compute(NOW)
    weights = mixer.parse_weights("Hip=30,Electronic=70")
    mixer.save_mix("Abend", weights, length=40, novelty=0.2)
    yt = FakeYT()
    result = mixer.build("Abend", yt, now=NOW, rng=random.Random(1))
    ids = result["video_ids"]
    assert len(ids) == len(set(ids)) == 40
    assert not any(v.startswith("rock-") for v in ids)
    assert result["new"] == round(12 * 0.2) + round(28 * 0.2)

    edm = {v for v in library["edm"]}
    hip = {v for v in library["hip hop"]}
    known = [v for v in ids if not v.startswith("new-")]
    assert sum(v in edm for v in known) > sum(v in hip for v in known)
    # evenly spread: every block of 10 songs has some hip hop
    for block in range(4):
        part = ids[block * 10:(block + 1) * 10]
        assert any(v in hip or v.startswith("new-Kend") or v.startswith("new-J._C") or v.startswith("new-Trav")
                   or v.startswith("new-Cro") or v.startswith("new-Kont") for v in part)

    playlist_id = mixer.push(yt, result, now=NOW)
    assert yt.playlists[playlist_id] == ids
    second = mixer.build("Abend", yt, now=NOW, rng=random.Random(2))
    assert mixer.push(yt, second, now=NOW) == playlist_id  # same playlist, new content
    assert yt.playlists[playlist_id] == second["video_ids"]
    assert ("remove", 40) in yt.calls


def test_offline_build_and_bad_weights(library):
    phases.compute(NOW)
    with pytest.raises(ValueError):
        mixer.parse_weights("Gibtsnicht=100")
    mixer.save_mix("Nur Rock", mixer.parse_weights("Rock=1"), length=30, novelty=0.5)
    result = mixer.build("Nur Rock", None, now=NOW, rng=random.Random(3))
    assert result["new"] == 0
    assert len(result["video_ids"]) == 18  # only 18 rock songs exist
    assert sum(result["short"].values()) == 12


def test_split_adds_up():
    assert sum(mixer.split(50, {1: 1 / 3, 2: 1 / 3, 3: 1 / 3}).values()) == 50
    assert mixer.split(10, {1: 0.3, 2: 0.7}) == {1: 3, 2: 7}


def test_replace_playlist_recreates_deleted_playlist():
    class YT(FakeYT):
        def get_playlist(self, playlist_id, limit=None):
            raise Exception("404")

    yt = YT()
    assert ytm.replace_playlist(yt, "PLgone", "T", "D", ["a"]) == "PL1"


def test_long_songs_stay_out_unless_allowed(library):
    phases.compute(NOW)
    mixer.save_mix("Rock", mixer.parse_weights("Rock=1"), length=30, novelty=0.5)
    long_set = library["rock"][0]
    db.write("UPDATE tracks SET duration = 3600 WHERE video_id = ?", (long_set,))

    class LongRadio(FakeYT):
        def get_watch_playlist(self, videoId, radio, limit):
            tracks = super().get_watch_playlist(videoId, radio, limit)["tracks"]
            for t in tracks:
                t["length"] = "1:02:33" if t["title"] == "New 0" else "3:45"
            return {"tracks": tracks}

    assert db.seconds("1:02:33") == 3753 and db.seconds("3:45") == 225 and db.seconds(None) is None
    ids = mixer.build("Rock", LongRadio(), now=NOW, rng=random.Random(4))["video_ids"]
    assert long_set not in ids
    assert not any(v.startswith("new-") and v.endswith("-0") for v in ids)
    assert db.one("SELECT duration FROM tracks WHERE video_id LIKE 'new-%-1'")["duration"] == 225

    mixer.save_mix("Rock", mixer.parse_weights("Rock=1"), length=30, novelty=0, max_minutes=0)
    assert long_set in mixer.build("Rock", None, now=NOW, rng=random.Random(4))["video_ids"]


def test_listening_to_a_mix_does_not_feed_back(library):
    """Two weeks of a rock mix on repeat: rock must not become the active phase, and the mix's songs
    must not climb in the next build because they were played from it."""
    from conftest import DAY, add_play
    from app import history

    phases.compute(NOW)
    rock = library["rock"][::3]  # two songs each of the three rock artists
    mixer.save_mix("Rock", mixer.parse_weights("Rock=1"), length=10, novelty=0)
    pushed_at = NOW - 15 * DAY
    history.record_push("Rock", rock, pushed_at)
    for day in range(14):
        for n in range(30):
            add_play(rock[n % 6], pushed_at + DAY + day * DAY + 8000 + n * 200)  # after the day's own plays
    assert history.mark_mix_plays() >= 14 * 30  # plus a rock play of your own right before a run

    families = {_family(c): c for c in phases.compute(NOW)}
    assert families["rock"]["status"] != "aktive Phase"
    assert families["edm"]["status"] == "aktive Phase"

    scores = {t["video_id"]: t["score"] for t in mixer.known_tracks(families["rock"]["id"], NOW)}
    others = [s for v, s in scores.items() if v not in rock]
    assert max(scores[v] for v in rock if v[-1] != "0") <= max(others) + 0.01  # song 0 of an artist is liked
