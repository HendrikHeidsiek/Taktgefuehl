"""The steps the CLI, the web interface and the background worker share."""
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import ai, costs, db, enrich, history, mixer, phases, report, sound, ytm

# Held while the phases are recomputed and while a mix is built from them, so a mix never reads phases
# that are being replaced. Recomputing takes seconds; everything else runs alongside.
phases_lock = threading.Lock()


def data_path() -> Path:
    return Path(os.environ.get("DATA_PATH", Path(__file__).resolve().parent.parent / "data"))


@contextmanager
def phases_stable(log=print):
    if not phases_lock.acquire(blocking=False):
        log("Wartet, bis die Phasen fertig berechnet sind …")
        phases_lock.acquire()
    try:
        yield
    finally:
        phases_lock.release()


def sync(log=print, likes: bool = True) -> None:
    yt = ytm.client(data_path())
    report.step(log, "Verlauf abgleichen")
    log(f"{history.sync_history(yt)} neue Wiedergaben")
    if marked := history.mark_mix_plays():
        log(f"davon {marked} aus Taktgefühl-Mixen (zählen kaum für Phasen und Auswahl)")
    if likes:
        report.step(log, "Likes abgleichen")
        log(f"{history.sync_likes(yt)} Songs mit Like")
    db.set_state("last_sync", int(time.time()))


def enrich_all(yt, limit=None, retag=False, log=print, listen=True) -> None:
    """With an Anthropic key the AI tags songs (artists as fallback), otherwise MusicBrainz tags artists;
    YTM related artists fill the remaining gaps. Then new songs get their sound fingerprint."""
    claude = ai.client(data_path())
    costs.refresh_if_stale(claude, log)
    if claude:
        report.step(log, "Songs einordnen (KI)")
        log(f"{ai.enrich_tracks(claude, limit, retag, log)} Songs eingeordnet ({ai.model()})")
        report.step(log, "Interpreten einordnen (KI)")
        log(f"{ai.enrich_ai(claude, limit, retag, log)} Interpreten eingeordnet, für Songs, die die KI nicht kennt")
    else:
        report.step(log, "Interpreten nachschlagen (MusicBrainz)")
        log(f"{enrich.enrich_mb(limit=limit, log=log)} Interpreten mit Genres")
    if yt:
        report.step(log, "Über ähnliche Interpreten ergänzen")
        log(f"{enrich.fill_from_related(yt, limit)} Interpreten ergänzt")
    if listen:
        listen_new(limit, log)


def listen_new(limit=None, log=print) -> None:
    if not sound.available():
        report.warn(log, "Klanganalyse nicht verfügbar, Phasen nur aus Tags")
        return
    todo = sound.pending(limit)
    report.step(log, "Klanganalyse", len(todo))
    if todo:
        log(f"{len(todo)} Songs werden angehört, etwa {len(todo) * 10 // 60 + 1} min")
        done = sound.analyze_many(todo, log)
        log(f"{done} von {len(todo)} Songs angehört"
            + (". Übersprungene versucht der Server in 14 Tagen erneut." if done < len(todo) else ""))
    else:
        log("Alle Songs sind schon angehört")


def compute_phases(log=print) -> list[dict]:
    report.step(log, "Phasen berechnen")
    history.mark_mix_plays()  # catches plays from before a takeout import or the first push
    with phases_stable(log):
        result = phases.compute()
    claude = ai.client(data_path())
    if claude and result and ai.name_phases(claude):
        result = phases.summary()
    log(f"{len(result)} Phasen")
    return result


def listener():
    """For mixer.build: analyses one new song's sound, or None without sound analysis."""
    if not sound.available():
        return None
    return lambda vid: sound.analyze_many([vid], log=lambda m: None)


def build_mix(name: str, yt, push: bool, log=print) -> dict:
    """Builds one mix (and updates its playlist when push is set) while the phases stay as they are."""
    with phases_stable(log):
        result = mixer.build(name, yt, claude=ai.client(data_path()), listen=listener())
    # Kept before the push, so a refused upload does not throw away the selection.
    mixer.save_result(result, pushed=False)
    if push:
        mixer.push(yt, result)
        mixer.save_result(result, pushed=True)
    return result


def push_mixes(yt, log=print, force: bool = False) -> None:
    """Renews the mixes whose interval is up (all of them with force)."""
    mixes = db.query("SELECT * FROM mixes ORDER BY name")
    names = [m["name"] for m in mixes if force or mixer.due(m)]
    report.step(log, "Mixe erneuern", len(names))
    for m in mixes:
        if m["name"] not in names:
            at = mixer.next_refresh(m)
            log(f"„{m['name']}“: " + ("nur von Hand" if at is None else
                                       f"erst wieder am {time.strftime('%d.%m.', time.localtime(at))} dran"))
    for n, name in enumerate(names, 1):
        result = build_mix(name, yt, push=True, log=log)
        log(f"„{name}“: {len(result['video_ids'])} Songs, {result['new']} neu, in YouTube Music aktualisiert")
        report.progress(log, n, len(names))


def rebuild(log=print, mixes: bool = True) -> None:
    """The daily run: likes, tags, phases and (optionally) all mixes."""
    yt = ytm.client(data_path())
    report.step(log, "Likes abgleichen")
    log(f"{history.sync_likes(yt)} Songs mit Like")
    enrich_all(yt, log=log)
    compute_phases(log)
    if mixes:
        push_mixes(yt, log)
    db.set_state("last_rebuild", int(time.time()))
