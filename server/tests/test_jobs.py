import threading
import time

from app import db, jobs, pipeline, report, sound

from conftest import add_track


def wait(lane, cond=lambda s: not s["running"]):
    for _ in range(200):
        s = jobs.status(lane)
        if cond(s):
            return s
        time.sleep(0.02)
    raise AssertionError(f"lane {lane} did not get there: {s}")


def test_download_errors_read_plainly():
    raw = "\x1b[0;31mERROR:\x1b[0m [youtube] 4T1Ym8m08Ds: Sign in to confirm your age. This video may be"
    assert report.clean(raw) == "Sign in to confirm your age. This video may be"
    assert report.download_reason(raw).startswith("altersbeschränkt")
    assert report.download_reason("[0;31mERROR:[0m unable to download video data: HTTP Error 403: Forbidden") \
        == "YouTube verweigert den Download"
    assert report.download_reason("ERROR: [youtube] abcdefghijk: Video unavailable") == "nicht mehr verfügbar"
    assert report.download_reason("something odd") == "anderer Fehler"
    # plain callables (print, lambdas) still get a line
    lines = []
    report.skip(lines.append, "privates Video", "A – B")
    report.progress(lines.append, 3, 10)  # nothing to say without text
    assert lines == ["A – B: privates Video"]


def test_job_keeps_steps_progress_and_skips(data):
    add_track("v1", "Künstlerin", title="Song Eins")

    def fail(vid, workdir):
        raise RuntimeError("\x1b[0;31mERROR:\x1b[0m [youtube] v1: Sign in to confirm your age")

    def fn(log):
        report.step(log, "Klanganalyse", 2)
        sound.analyze_many(["v1", "v2"], log, analyze_fn=fail)
        report.step(log, "Phasen berechnen")
        log("3 Phasen")

    sound_pause, sound.PAUSE = sound.PAUSE, 0
    try:
        assert jobs.start("rebuild", fn)
        s = wait("main")
    finally:
        sound.PAUSE = sound_pause
    first, second = s["steps"]
    assert (first["title"], first["done"], first["total"]) == ("Klanganalyse", 2, 2)
    assert list(first["skipped"].values()) == [2] and first["finished"] and second["finished"]
    skips = [e for e in s["entries"] if e["kind"] == "skip"]
    assert skips[0]["text"] == "Künstlerin – Song Eins" and skips[0]["detail"].startswith("altersbeschränkt")
    assert skips[0]["step"] == 0 and skips[1]["text"] == "v2"  # unknown song: its id
    assert [e["kind"] for e in s["entries"]][-2:] == ["step", "info"]
    # the last run survives a restart
    jobs._state["main"] = jobs._empty()
    again = jobs.status("main")
    assert again["name"] == "rebuild" and again["steps"][0]["skipped"] == first["skipped"]
    assert not again["running"]


def test_mix_lane_runs_beside_a_long_rebuild(data):
    release = threading.Event()
    assert jobs.start("rebuild", lambda log: release.wait(5))
    assert not jobs.start("sync", lambda log: None)  # main lane busy
    assert jobs.start("mix", lambda log: log("fertig"), detail={"mix": "Abend"})
    s = wait("mix")
    assert s["entries"][-1]["text"] == "fertig" and jobs.status("main")["running"]
    release.set()
    wait("main")


def test_mix_waits_while_phases_are_recomputed(data):
    def build(log):
        with pipeline.phases_stable(log):
            log("gebaut")

    pipeline.phases_lock.acquire()
    try:
        assert jobs.start("mix", build)
        s = wait("mix", lambda s: any("Wartet" in e["text"] for e in s["entries"]))
        assert s["running"]
    finally:
        pipeline.phases_lock.release()
    assert wait("mix")["entries"][-1]["text"] == "gebaut"
