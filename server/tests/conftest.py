import json
import os
import sys
from pathlib import Path

os.environ["SOUND"] = "0"  # tests never download audio; the sound path is tested with fake fingerprints

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402

DAY = 86400
NOW = 1_780_000_000

GENRES = {
    "hip hop": {"Kendrick Lamar": {"hip hop": 10, "conscious hip hop": 5, "west coast hip hop": 4},
                "J. Cole": {"hip hop": 9, "conscious hip hop": 4},
                "Travis Scott": {"hip hop": 7, "trap": 8},
                "Cro": {"hip hop": 5, "german hip hop": 6, "pop rap": 3},
                "Kontra K": {"german hip hop": 7, "hip hop": 4, "gangsta rap": 2}},
    "edm": {"Avicii": {"progressive house": 8, "house": 5, "edm": 6},
            "Martin Garrix": {"progressive house": 6, "big room house": 7, "edm": 8},
            "Fred again..": {"house": 6, "uk garage": 5, "electronic": 7},
            "Kygo": {"tropical house": 9, "edm": 5, "house": 3},
            "Lane 8": {"deep house": 7, "progressive house": 6, "electronic": 4}},
    "rock": {"Foo Fighters": {"alternative rock": 9, "rock": 8, "post-grunge": 4},
             "Muse": {"alternative rock": 8, "rock": 6, "art rock": 4},
             "Arctic Monkeys": {"indie rock": 9, "rock": 5, "garage rock": 4}},
}


@pytest.fixture
def data(tmp_path):
    db.init(tmp_path)
    return tmp_path


def add_track(video_id, artist, liked=False, title=None):
    sql, params = db.track_statement({"videoId": video_id, "title": title or video_id,
                                      "artists": [{"name": artist, "id": "UC" + artist}]}, liked=liked)
    db.write(sql, params)


def add_play(video_id, at):
    db.write("INSERT INTO plays(video_id, at, approx, source) VALUES(?,?,0,'history')", (video_id, at))


@pytest.fixture
def library(data):
    """26 weeks: a hip-hop phase (weeks 0-12), then an EDM phase (weeks 13-25), rock now and then."""
    tracks = {}
    for family, artists in GENRES.items():
        for artist, tags in artists.items():
            db.write("INSERT INTO artists(name, mbid, tags, looked_up_at) VALUES(?,?,?,?)",
                     (artist, None, json.dumps(tags), NOW))
            for n in range(6):
                vid = f"{artist[:4]}{n}".replace(" ", "_")
                add_track(vid, artist, liked=(n == 0))
                tracks.setdefault(family, []).append(vid)
    start = NOW - 26 * 7 * DAY
    for week in range(26):
        main = "hip hop" if week < 13 else "edm"
        other = "edm" if week < 13 else "hip hop"
        for day in range(7):
            t = start + (week * 7 + day) * DAY
            for k, vid in enumerate(tracks[main][(day * 3) % 30:(day * 3) % 30 + 8]):
                add_play(vid, t + k * 200)
            add_play(tracks[other][(week + day) % 30], t + 5000)
            if day == 3:
                add_play(tracks["rock"][week % 18], t + 6000)
    return tracks
