"""Background work in two lanes, plus the schedule that starts it.

The main lane runs the long work (sync, rebuild, phases, all mixes), one job at a time. The mix lane
builds a single mix the user asked for, so that does not have to wait for a rebuild that takes an hour;
the moment the phases are recomputed is guarded separately (pipeline.phases_lock).

Each job keeps steps with their progress and a log of typed entries (see report.py). The last finished
job of each lane is kept in the database, so the app can show what happened over night.
"""
import threading
import time
import traceback
from datetime import datetime

from . import db, report, ytm
from .pipeline import data_path

LOG_ENTRIES = 300
LANES = ("main", "mix")

_lock = threading.Lock()


def lane_of(name: str) -> str:
    return "mix" if name == "mix" else "main"


def _empty() -> dict:
    return {"name": None, "detail": None, "started": None, "finished": None, "error": None,
            "steps": [], "entries": []}


_state = {lane: _empty() for lane in LANES}


def status(lane: str = "main") -> dict:
    """The running job of the lane, or the last one (also from before a restart)."""
    with _lock:
        s = _state[lane]
        if s["name"] is None:
            s = db.get_state(f"last_job:{lane}") or s
        return _public(s)


def _public(s: dict) -> dict:
    out = {**s, "steps": [dict(x, skipped=dict(x["skipped"])) for x in s["steps"]], "entries": list(s["entries"])}
    out["running"] = s["name"] is not None and s["finished"] is None
    # Plain lines for app versions that only know the old format.
    out["log"] = [f"{time.strftime('%H:%M:%S', time.localtime(e['t']))} {e['text']}"
                  + (f": {e['detail']}" if e.get("detail") else "") for e in s["entries"]][-80:]
    return out


class Log:
    """The `log` a job function gets: callable with a line like print, plus step/progress/warn/skip."""

    def __init__(self, lane: str):
        self.lane = lane

    def _state(self) -> dict:
        return _state[self.lane]

    def _add(self, kind: str, text: str, detail: str | None = None) -> None:
        with _lock:
            s = self._state()
            s["entries"].append({"t": int(time.time()), "kind": kind, "text": text, "detail": detail,
                                 "step": len(s["steps"]) - 1 if s["steps"] else None})
            del s["entries"][:-LOG_ENTRIES]
        print(f"[{self.lane}] {text}" + (f": {detail}" if detail else ""), flush=True)

    def __call__(self, text, kind: str = "info") -> None:
        self._add(kind, str(text).strip())

    def warn(self, text: str) -> None:
        self._add("warn", text)

    def error(self, text: str) -> None:
        self._add("error", text)

    def step(self, title: str, total: int | None = None) -> None:
        now = int(time.time())
        with _lock:
            s = self._state()
            if s["steps"] and s["steps"][-1]["finished"] is None:
                s["steps"][-1]["finished"] = now
            s["steps"].append({"title": title, "total": total, "done": 0, "started": now, "finished": None,
                               "skipped": {}})
        self._add("step", title)

    def progress(self, done: int, total: int | None = None) -> None:
        with _lock:
            s = self._state()
            if s["steps"]:
                s["steps"][-1]["done"] = done
                if total is not None:
                    s["steps"][-1]["total"] = total

    def skip(self, reason: str, what: str) -> None:
        with _lock:
            s = self._state()
            if s["steps"]:
                skipped = s["steps"][-1]["skipped"]
                skipped[reason] = skipped.get(reason, 0) + 1
        self._add("skip", what, reason)


def start(name: str, fn, detail: dict | None = None) -> bool:
    """Runs fn(log) in a thread on the job's lane. False if that lane is busy. detail says what the job
    works on (e.g. which mix), so the app can pick up a running job when a screen is opened again."""
    lane = lane_of(name)
    with _lock:
        s = _state[lane]
        if s["name"] is not None and s["finished"] is None:
            return False
        _state[lane] = {**_empty(), "name": name, "detail": detail, "started": int(time.time())}
    log = Log(lane)

    def run():
        try:
            fn(log)
        except ytm.SessionExpired as e:
            _fail(log, str(e))
        except ytm.NotAuthenticated:
            _fail(log, "YouTube Music ist nicht verbunden.")
        except Exception as e:  # shown in the app; YouTube hiccups are expected
            traceback.print_exc()
            message = report.clean(e) or e.__class__.__name__
            _fail(log, message[:200] + ("…" if len(message) > 200 else ""))
        finally:
            now = int(time.time())
            with _lock:
                s = _state[lane]
                s["finished"] = now
                if s["steps"] and s["steps"][-1]["finished"] is None:
                    s["steps"][-1]["finished"] = now
                done = _public(s)
            db.set_state(f"last_job:{lane}", {k: done[k] for k in _empty()})

    threading.Thread(target=run, name=f"job-{name}", daemon=True).start()
    return True


def _fail(log: Log, message: str) -> None:
    log.error(f"Abgebrochen: {message}")
    with _lock:
        _state[log.lane]["error"] = message
    db.set_state("last_error", {"at": int(time.time()), "message": message})


def schedule(sync_minutes: int, rebuild_hour: int, jobs: dict) -> None:
    """Worker loop: sync every sync_minutes, the full rebuild once a day from rebuild_hour on."""
    def loop():
        time.sleep(20)  # let the server come up first
        while True:
            if ytm.connected(data_path()):
                last = db.get_state("last_rebuild")
                due = (datetime.now().hour >= rebuild_hour and
                       (not last or datetime.fromtimestamp(last).date() < datetime.now().date()))
                if due:
                    start("rebuild", jobs["rebuild"])
                else:
                    start("sync", jobs["sync"])
            time.sleep(sync_minutes * 60)

    threading.Thread(target=loop, name="schedule", daemon=True).start()
