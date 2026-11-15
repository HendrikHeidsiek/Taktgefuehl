import json
from datetime import datetime

from app import db, history, takeout

from conftest import NOW


def test_new_plays_cases():
    prev = list("abcdefgh")
    assert history.new_plays(prev, prev) == []
    assert history.new_plays(prev, list("xyabcdef")) == ["x", "y"]
    # replayed song moves to the top instead of appearing twice
    assert history.new_plays(prev, list("dabcefgh")) == ["d"]
    assert history.new_plays(prev, list("xdabcefg")) == ["x", "d"]
    # more new plays than the list holds
    assert history.new_plays(prev, list("stuvwxyz")) == list("stuvwxyz")
    assert history.new_plays([], list("ab")) == ["a", "b"]


def test_approx_time():
    assert history.approx_time("Today", NOW) == NOW
    assert history.approx_time("Yesterday", NOW) == NOW - 86400
    assert history.approx_time("March 2025", NOW) == int(datetime(2025, 3, 15).timestamp())
    assert history.approx_time("???", NOW) < NOW


class FakeYT:
    def __init__(self, history_ids):
        self.history_ids = history_ids

    def get_history(self):
        return [{"videoId": v, "title": v.upper(), "artists": [{"name": "A", "id": "UCA"}], "played": "Today"}
                for v in self.history_ids]

    def get_liked_songs(self, limit=100):
        return {"tracks": [{"videoId": "b", "title": "B", "artists": [{"name": "A"}]}]}


def test_sync_history_and_likes(data):
    yt = FakeYT(list("abc"))
    assert history.sync_history(yt, NOW) == 3
    assert db.one("SELECT COUNT(*) AS n FROM plays WHERE approx = 1")["n"] == 3
    yt.history_ids = list("dbac")
    assert history.sync_history(yt, NOW + 600) == 2  # d new, b replayed
    rows = db.query("SELECT video_id FROM plays WHERE approx = 0 ORDER BY at DESC")
    assert [r["video_id"] for r in rows] == ["d", "b"]
    assert history.sync_likes(yt) == 1
    assert db.one("SELECT liked FROM tracks WHERE video_id = 'b'")["liked"] == 1


def test_takeout_import(data, tmp_path):
    f = tmp_path / "watch-history.json"
    f.write_text(json.dumps([
        {"header": "YouTube Music", "title": "Watched Song One", "titleUrl": "https://music.youtube.com/watch?v=abc",
         "subtitles": [{"name": "Artist X - Topic"}], "time": "2025-05-01T10:00:00.000Z"},
        {"header": "YouTube", "title": "Watched Cat video", "titleUrl": "https://www.youtube.com/watch?v=cat",
         "time": "2025-05-01T11:00:00Z"},
        {"header": "YouTube Music", "title": "„Lied“ angesehen", "titleUrl": "https://music.youtube.com/watch?v=def",
         "subtitles": [{"name": "Künstlerin"}], "time": "2025-05-02T10:00:00Z"},
    ]), encoding="utf-8")
    assert takeout.import_file(f) == 2
    assert takeout.import_file(f) == 2  # second import adds nothing
    assert db.one("SELECT COUNT(*) AS n FROM plays")["n"] == 2
    t = db.one("SELECT title, artist FROM tracks WHERE video_id = 'abc'")
    assert (t["title"], t["artist"]) == ("Song One", "Artist X")
    assert db.one("SELECT title FROM tracks WHERE video_id = 'def'")["title"] == "Lied"


def test_expired_session_is_named_and_remembered(data):
    import pytest
    from app import ytm

    class SignedOut(FakeYT):
        def get_liked_songs(self, limit=100):
            raise KeyError("Unable to find 'twoColumnBrowseResultsRenderer'")

        def get_account_info(self):
            raise KeyError("Unable to find 'header'")

    with pytest.raises(ytm.SessionExpired) as e:
        history.sync_likes(SignedOut([]))
    assert "abgelaufen" in str(e.value)
    assert db.get_state("youtube_expired") is True

    class Hiccup(SignedOut):  # still signed in: the original error stays
        def get_account_info(self):
            return {"accountName": "x"}

    with pytest.raises(KeyError):
        history.sync_likes(Hiccup([]))
    history.sync_likes(FakeYT([]))
    assert db.get_state("youtube_expired") is False


def test_plays_from_a_mix_playlist_are_marked(data):
    from conftest import add_play, add_track
    for vid, artist in (("m1", "A"), ("m2", "B"), ("m3", "A"), ("m4", "C"), ("o1", "D"), ("o2", "E")):
        add_track(vid, artist)
    history.record_push("Abend", ["m1", "m2", "m3", "m4"], NOW)
    # before the push (beyond the grace hour): the same songs in a row are your own plays
    for n, vid in enumerate(("m1", "m2", "m3")):
        add_play(vid, NOW - 2 * 3600 + n * 200)
    # a single mix song between your own songs stays yours
    for n, vid in enumerate(("o1", "m4", "o2")):
        add_play(vid, NOW + 1000 + n * 200)
    # shuffled run of three by two artists: from the playlist
    for n, vid in enumerate(("m3", "m1", "m2")):
        add_play(vid, NOW + 5000 + n * 200)
    assert history.mark_mix_plays() == 3
    marked = [r["video_id"] for r in db.query("SELECT video_id FROM plays WHERE via = 'Abend' ORDER BY at")]
    assert marked == ["m3", "m1", "m2"]
    assert history.mark_mix_plays() == 0  # idempotent

    # three songs by one artist in a row (an album) do not count as the playlist
    history.record_push("Album", ["m1", "m3", "o1"], NOW + 9000)
    for n, vid in enumerate(("o1", "m1", "m3")):
        add_play(vid, NOW + 10000 + n * 200)
    db.write("UPDATE tracks SET artist = 'A' WHERE video_id = 'o1'")
    assert history.mark_mix_plays() == 0


def test_old_pushes_are_dropped_but_the_newest_stays(data):
    history.record_push("Abend", ["a"], NOW - 200 * 86400)
    history.record_push("Morgen", ["b"], NOW - 200 * 86400)
    history.record_push("Morgen", ["c"], NOW)
    rows = db.query("SELECT mix, video_ids FROM mix_pushes ORDER BY mix")
    assert [(r["mix"], r["video_ids"]) for r in rows] == [("Abend", '["a"]'), ("Morgen", '["c"]')]
