import json

import pytest
from fastapi.testclient import TestClient

from app import db, main, mixer, phases, ytm

from conftest import NOW
from test_phases_mixer import FakeYT


@pytest.fixture
def client(library, data, monkeypatch):
    monkeypatch.setenv("DATA_PATH", str(data))
    monkeypatch.setenv("SCHEDULE", "0")
    monkeypatch.setenv("ADMIN_PASSWORD", "geheim-genug")
    monkeypatch.setattr(main, "DATA", data)
    monkeypatch.setattr(main, "login_guard", main.LoginGuard())
    phases.compute(NOW)
    with TestClient(main.app) as c:
        token = c.post("/api/auth/login", json={"password": "geheim-genug"}).json()["token"]
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


def test_login_and_guard(client):
    anon = TestClient(main.app)
    assert anon.get("/api/status").status_code == 401
    assert anon.get("/api/health").json()["app"] == "taktgefuehl"
    for _ in range(main.LoginGuard.IP_MAX):
        assert anon.post("/api/auth/login", json={"password": "falsch"}).status_code == 401
    blocked = anon.post("/api/auth/login", json={"password": "geheim-genug"})
    assert blocked.status_code == 429 and "Fehlversuche" in blocked.json()["detail"]
    assert client.get("/api/status").status_code == 200  # existing session unaffected


def test_password_change_logs_out_other_sessions(client):
    other = TestClient(main.app)
    other.headers["Authorization"] = "Bearer " + other.post(
        "/api/auth/login", json={"password": "geheim-genug"}).json()["token"]
    assert client.put("/api/auth/password", json={"old_password": "x" * 8, "new_password": "neu-und-lang"}).status_code == 403
    assert client.put("/api/auth/password", json={"old_password": "geheim-genug",
                                                  "new_password": "neu-und-lang"}).status_code == 200
    assert other.get("/api/status").status_code == 401
    assert client.get("/api/status").status_code == 200


def test_status_phases_and_rename(client):
    s = client.get("/api/status").json()
    assert s["counts"]["phases"] == 3 and s["youtube"] is False and s["ai"]["connected"] is False
    ps = client.get("/api/phases").json()
    assert "centroid" not in ps[0] and len(ps[0]["top_songs"]) == 8
    assert client.patch(f"/api/phases/{ps[0]['id']}", json={"name": "Mein Ding"}).status_code == 200
    assert client.get("/api/phases").json()[0]["name"] == "Mein Ding"


def wait_for_job(client, lane="main"):
    import time
    for _ in range(100):
        if not client.get(f"/api/jobs?lane={lane}").json()["running"]:
            return
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_mixes_crud_and_build(client, data, monkeypatch):
    ps = {p["name"].split(" /")[0]: p["id"] for p in client.get("/api/phases").json()}
    bad = client.put("/api/mixes/Abend", json={"weights": {"9999": 50}})
    assert bad.status_code == 422
    r = client.put("/api/mixes/Abend", json={"weights": {str(ps["Hip Hop"]): 30, str(ps["Electronic"]): 70},
                                            "length": 20, "novelty": 0.2})
    assert r.status_code == 200
    assert sorted(round(p["share"], 2) for p in r.json()["parts"]) == [0.3, 0.7]

    assert client.post("/api/mixes/Abend/build", json={}).status_code == 409  # YouTube not connected
    yt = FakeYT()
    monkeypatch.setattr(ytm, "client", lambda path: yt)
    (data / ytm.AUTH_FILE).write_text("{}")
    started = client.post("/api/mixes/Abend/build", json={"push": True})
    assert started.status_code == 202 and started.json()["name"] == "mix"
    assert started.json()["detail"] == {"mix": "Abend", "push": True}  # the app picks the job up by this
    wait_for_job(client, "mix")
    job = client.get("/api/jobs?lane=mix").json()
    assert job["error"] is None and [st["title"] for st in job["steps"]] == [
        "Songs auswählen", "Zu YouTube Music schicken"]
    assert client.get("/api/status").json()["mix_job"]["name"] == "mix"
    built = client.get("/api/mixes/Abend/result").json()
    assert len(built["songs"]) == 20 and sum(s["new"] for s in built["songs"]) == 4 and built["pushed"]
    assert built["mix"]["playlist_url"].endswith("PL1")
    assert client.delete("/api/mixes/Abend").status_code == 200
    assert client.get("/api/mixes").json() == []


def test_refused_playlist_upload_is_reported_and_keeps_the_songs(client, data, monkeypatch):
    ps = {p["name"].split(" /")[0]: p["id"] for p in client.get("/api/phases").json()}
    client.put("/api/mixes/Abend", json={"weights": {str(ps["Hip Hop"]): 100}, "length": 20, "novelty": 0.2})
    (data / ytm.AUTH_FILE).write_text("{}")

    class Refusing(FakeYT):
        signed_in = True

        def create_playlist(self, *args, **kwargs):
            raise Exception("Server returned HTTP 401: Unauthorized. You must be signed in to perform this operation.")

        def get_account_info(self):
            if not self.signed_in:
                raise KeyError("header")
            return {"accountName": "x"}

    yt = Refusing()
    monkeypatch.setattr(ytm, "client", lambda path: yt)
    client.post("/api/mixes/Abend/build", json={"push": True})
    wait_for_job(client, "mix")
    assert "Schreiben der Playlist" in client.get("/api/jobs?lane=mix").json()["error"]
    kept = client.get("/api/mixes/Abend/result").json()
    assert len(kept["songs"]) == 20 and not kept["pushed"]

    yt.signed_in = False  # the session is gone altogether: say so, and the app shows its banner
    client.post("/api/mixes/Abend/build", json={"push": True})
    wait_for_job(client, "mix")
    assert "abgelaufen" in client.get("/api/jobs?lane=mix").json()["error"]
    assert client.get("/api/status").json()["youtube_expired"] is True


def test_rebuild_keeps_the_new_songs_for_the_app(client, data, monkeypatch):
    from app import pipeline
    ps = {p["name"].split(" /")[0]: p["id"] for p in client.get("/api/phases").json()}
    client.put("/api/mixes/Abend", json={"weights": {str(ps["Hip Hop"]): 100}, "length": 20, "novelty": 0.2,
                                        "max_minutes": 20})
    assert client.get("/api/mixes").json()[0]["max_minutes"] == 20
    yt = FakeYT()
    pipeline.push_mixes(yt, log=lambda *a, **k: None)
    kept = client.get("/api/mixes/Abend/result").json()
    assert kept["pushed"] and [s["video_id"] for s in kept["songs"]] == yt.playlists["PL1"]


def test_mixes_are_renewed_by_their_interval(client):
    from app import pipeline
    ps = {p["name"].split(" /")[0]: p["id"] for p in client.get("/api/phases").json()}
    for name, days in (("Woche", 7), ("Hand", 0)):
        client.put(f"/api/mixes/{name}", json={"weights": {str(ps["Hip Hop"]): 100}, "length": 20, "refresh_days": days})
    mixes = {m["name"]: m for m in client.get("/api/mixes").json()}
    assert mixes["Woche"]["next_refresh"] == 0 and mixes["Hand"]["next_refresh"] is None

    lines = []
    pipeline.push_mixes(FakeYT(), log=lambda text, *a, **k: lines.append(str(text)))
    assert any("„Hand“: nur von Hand" in line for line in lines)
    woche = db.one("SELECT * FROM mixes WHERE name = 'Woche'")
    assert woche["built_at"] and db.one("SELECT built_at FROM mixes WHERE name = 'Hand'")["built_at"] is None

    lines.clear()
    pipeline.push_mixes(FakeYT(), log=lambda text, *a, **k: lines.append(str(text)))  # the next day: not yet
    assert db.one("SELECT built_at FROM mixes WHERE name = 'Woche'")["built_at"] == woche["built_at"]
    assert any("„Woche“: erst wieder am" in line for line in lines)
    db.write("UPDATE mixes SET built_at = built_at - 7 * 86400 + 3600 WHERE name = 'Woche'")  # built a week ago, a bit later in the day
    assert mixer.due(db.one("SELECT * FROM mixes WHERE name = 'Woche'"))


def test_takeout_upload(client):
    entries = [{"header": "YouTube Music", "title": "Watched Starbuster", "time": "2025-03-01T20:00:00Z",
                "titleUrl": "https://music.youtube.com/watch?v=starbuster", "subtitles": [{"name": "Fontaines D.C. - Topic"}]}]
    r = client.post("/api/takeout", content=json.dumps(entries))
    assert r.json() == {"imported": 1}
    assert db.one("SELECT artist FROM tracks WHERE video_id = 'starbuster'")["artist"] == "Fontaines D.C."
    assert client.post("/api/takeout", content="<html>nope</html>").status_code == 422


def test_credentials_are_checked_and_write_only(client, data):
    r = client.put("/api/credentials/youtube", json={"headers": "accept: */*\nuser-agent: test browser"})
    assert r.status_code == 422
    assert not (data / ytm.AUTH_FILE).exists()
    assert "headers" not in json.dumps(client.get("/api/status").json())


def test_jobs_endpoint(client):
    assert client.post("/api/jobs/sync").status_code == 409  # not connected
    assert client.post("/api/jobs/nope").status_code == 404


def test_start_page_and_versioned_apk(client, tmp_path, monkeypatch):
    anon = TestClient(main.app)
    monkeypatch.setattr(main, "DOWNLOAD", tmp_path)
    monkeypatch.setattr(main, "APK", tmp_path / "taktgefuehl.apk")
    page = anon.get("/").text
    assert "Noch nicht auf diesem Server" in page and "Betreiber" not in page and "{{" not in page
    assert "github.com/HendrikHeidsiek/Taktgefuehl" in page
    monkeypatch.setenv("OPERATOR_NAME", "example.org")
    monkeypatch.setenv("OPERATOR_URL", "https://example.org")
    assert 'Betreiber dieser Seite: <a href="https://example.org"' in anon.get("/").text
    (tmp_path / "taktgefuehl.apk").write_bytes(b"x" * 2048)
    (tmp_path / "app-version.json").write_text('{"version": "0.2.0", "build": 3}')
    page = anon.get("/").text
    assert 'href="/taktgefuehl-0.2.0.apk"' in page and "Version 0.2.0" in page
    r = anon.get("/taktgefuehl.apk", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/taktgefuehl-0.2.0.apk"
    assert anon.get("/taktgefuehl-0.1.0.apk", follow_redirects=False).headers["location"] == "/taktgefuehl-0.2.0.apk"
    assert anon.get("/taktgefuehl-0.2.0.apk").content == b"x" * 2048
    assert anon.get("/api/app").json()["android"]["url"] == "/taktgefuehl-0.2.0.apk"
    assert anon.get("/qr/app.svg").headers["content-type"].startswith("image/svg")


def test_ai_model_choice_and_costs(client):
    assert client.put("/api/ai/model", json={"model": "claude-made-up"}).status_code == 422
    assert client.post("/api/ai/models/refresh").status_code == 409  # no key
    c = client.get("/api/ai/costs").json()
    assert c["total"] == 0 and c["months"] == [] and c["currency"] == "USD"
