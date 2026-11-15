import numpy as np
import pytest

from app import db, mixer, phases, sound

from conftest import GENRES, NOW

FAMILY_OF = {a: f for f, artists in GENRES.items() for a in artists}


@pytest.fixture(autouse=True)
def three_families(monkeypatch):
    """The test library has three families; a real one needs at least 6 phases (see phases.K_RANGE)."""
    monkeypatch.setattr(phases, "K_RANGE", (3, 6))


def fake_vec(family: str, seed: int) -> np.ndarray:
    """Songs of a family share a direction plus some noise, like real fingerprints of one style."""
    base = {"hip hop": 0, "edm": 1, "rock": 2, "ballad": 3}[family]
    rng = np.random.default_rng(seed)
    v = rng.normal(0, 0.1, sound.DIM)  # same-family similarity ~0.9, like closely related real songs
    v[base * 100:(base + 1) * 100] += 1.0
    return v


def family_of_phase(phase_id: int) -> str:
    """The family most songs of a phase belong to."""
    from collections import Counter
    members = [r["artist"] for r in db.query(
        "SELECT t.artist FROM cluster_tracks ct JOIN tracks t ON t.video_id = ct.video_id WHERE ct.cluster_id = ?",
        (phase_id,))]
    return Counter(FAMILY_OF[a] for a in members).most_common(1)[0][0]


def fingerprint_library(overrides=None):
    overrides = overrides or {}
    for n, r in enumerate(db.query("SELECT video_id, artist FROM tracks")):
        family = overrides.get(r["video_id"], FAMILY_OF.get(r["artist"]))
        if family:
            sound.store(r["video_id"], fake_vec(family, n), [family])


def test_windows():
    assert sound.windows(20) == [(0.0, 20)]
    assert sound.windows(200) == [(45.0, 55.0), (95.0, 105.0), (145.0, 155.0)]


def test_sound_beats_tags_and_artist(library):
    # Kygo2 sounds like hip hop: tags and artist say EDM, the sound decides.
    fingerprint_library({"Kygo2": "hip hop"})
    phases.compute(NOW)
    by_track = phases.track_cluster()
    assert family_of_phase(by_track["Kygo2"]) == "hip hop"
    assert family_of_phase(by_track["Kygo1"]) == "edm"


def test_unanalysed_song_borrows_sound_of_similar_songs(library):
    fingerprint_library()
    db.write("DELETE FROM track_sound WHERE video_id = 'Kygo3'")
    phases.compute(NOW)
    assert family_of_phase(phases.track_cluster()["Kygo3"]) == "edm"


def test_low_coverage_falls_back_to_tags(library):
    sound.store("Kend0", fake_vec("edm", 1), [])  # one fingerprint is not enough to trust sound
    phases.compute(NOW)
    by_track = phases.track_cluster()
    assert by_track["Kend0"] == by_track["Kend1"]


def test_new_songs_are_listened_to(library):
    fingerprint_library()
    result = phases.compute(NOW)
    edm = phases.track_cluster()["Kygo0"]
    assert result[0].get("sound_styles") is not None

    class YT:
        def get_watch_playlist(self, videoId, radio, limit):
            return {"tracks": [{"videoId": f"cand{i}", "title": f"C{i}", "artists": [{"name": f"Unknown {i}"}]}
                               for i in range(4)]}

    heard = []

    def listen(vid):  # cand0 and cand2 sound like EDM, the others like rock
        heard.append(vid)
        sound.store(vid, fake_vec("edm" if vid in ("cand0", "cand2") else "rock", 99 + len(heard)), [])

    found = mixer.find_new(YT(), edm, ["Kygo0"], 2, set(), None, listen)
    assert [f["video_id"] for f in found] == ["cand0", "cand2"]
    assert heard == ["cand0", "cand1", "cand2"]  # stops listening once enough fit
