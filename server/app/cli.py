"""Command line: python -m app.cli <command>. The web server (app.main) does the same and runs the schedule."""
import argparse
import json
import sys
import time
from pathlib import Path

from . import ai, db, mixer, phases, pipeline, takeout, ytm
from .pipeline import data_path

SPARK = " ▁▂▃▄▅▆▇█"


def sparkline(values: list) -> str:
    known = [v for v in values if v is not None]
    top = max(known, default=0) or 1
    return "".join("·" if v is None else SPARK[round(v / top * (len(SPARK) - 1))] for v in values)


def cmd_sync(args) -> None:
    pipeline.sync(likes=not args.no_likes)


def cmd_takeout(args) -> None:
    print(f"{takeout.import_file(Path(args.file))} YouTube-Music-Einträge gelesen")


def cmd_enrich(args) -> None:
    pipeline.enrich_all(None if args.no_related else ytm.client(data_path()), args.limit, args.retag,
                        listen=not args.no_sound)


def print_phases(items: list[dict]) -> None:
    if not items:
        print("Noch keine Phasen. Erst „sync“ und „enrich“, dann „phases --recompute“.")
        return
    print(f"{'ID':>3}  {'Phase':<28} {'Anteil':>6} {'2 Wo.':>6}  {'letzte 26 Wochen':<26}  Status")
    for c in items:
        recent = "–" if c.get("share_recent") is None else f"{c['share_recent']:.0%}"
        print(f"{c['id']:>3}  {c['name'][:28]:<28} {c.get('share_total', 0):>6.0%} {recent:>6}  "
              f"{sparkline(c.get('weekly_share', []))}  {c['status']}")
        if c.get("description"):
            print(f"     {c['description']}")
        print(f"     {c['songs']} Songs, v. a. {', '.join(c['artists'])}")


def cmd_phases(args) -> None:
    print_phases(pipeline.compute_phases(log=lambda m: None) if args.recompute else phases.summary())


def cmd_rename(args) -> None:
    cid = phases.resolve(args.phase)
    if cid is None or not phases.rename(cid, args.name):
        sys.exit(f"Phase {args.phase!r} nicht gefunden.")
    print(f"Phase {cid} heißt jetzt „{args.name}“.")


def cmd_mix_set(args) -> None:
    weights = mixer.parse_weights(args.weights)
    mixer.save_mix(args.name, weights, args.length, args.novelty)
    names = {c["id"]: c["name"] for c in phases.summary()}
    print(f"Mix „{args.name}“: " + ", ".join(f"{w:.0%} {names[c]}" for c, w in weights.items()))


def cmd_mix_list(args) -> None:
    names = {c["id"]: c["name"] for c in phases.summary()}
    for m in db.query("SELECT * FROM mixes ORDER BY name"):
        parts = ", ".join(f"{w:.0%} {names.get(int(c), f'(Phase {c} weg)')}" for c, w in json.loads(m["weights"]).items())
        built = time.strftime("%d.%m. %H:%M", time.localtime(m["built_at"])) if m["built_at"] else "nie"
        print(f"{m['name']}: {parts} · {m['length']} Songs · {m['novelty']:.0%} neu · zuletzt {built}")


def cmd_mix_build(args) -> None:
    yt = None if args.offline else ytm.client(data_path())
    result = mixer.build(args.name, yt, claude=ai.client(data_path()), listen=None if args.offline else pipeline.listener())
    print(f"{len(result['video_ids'])} Songs, davon {result['new']} neu")
    for cid, missing in result["short"].items():
        print(f"  Phase {cid}: {missing} Songs zu wenig im Bestand")
    if args.push:
        playlist_id = mixer.push(yt or ytm.client(data_path()), result)
        print(f"In YouTube Music: https://music.youtube.com/playlist?list={playlist_id}")
    else:
        titles = {r["video_id"]: r for r in db.query(
            f"SELECT video_id, title, artist FROM tracks WHERE video_id IN ({','.join('?' * len(result['video_ids']))})",
            result["video_ids"])}
        for n, vid in enumerate(result["video_ids"], 1):
            t = titles.get(vid)
            print(f"  {n:>2}. {t['artist'] if t else '?'} – {t['title'] if t else vid}")


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli", description="Phasen-Mixe für YouTube Music")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync", help="Verlauf und Likes von YouTube Music holen")
    s.add_argument("--no-likes", action="store_true")
    s.set_defaults(func=cmd_sync)
    s = sub.add_parser("import-takeout", help="watch-history.json aus Google Takeout einlesen")
    s.add_argument("file")
    s.set_defaults(func=cmd_takeout)
    s = sub.add_parser("enrich", help="Genres der Interpreten nachschlagen")
    s.add_argument("--limit", type=int)
    s.add_argument("--no-related", action="store_true", help="ohne YouTube-Music-Ergänzung")
    s.add_argument("--retag", action="store_true", help="mit KI alle Interpreten neu einordnen")
    s.add_argument("--no-sound", action="store_true", help="ohne Klang-Analyse")
    s.set_defaults(func=cmd_enrich)
    s = sub.add_parser("phases", help="Phasen anzeigen")
    s.add_argument("--recompute", action="store_true", help="vorher neu berechnen")
    s.set_defaults(func=cmd_phases)
    s = sub.add_parser("rename", help="Phase umbenennen")
    s.add_argument("phase")
    s.add_argument("name")
    s.set_defaults(func=cmd_rename)
    s = sub.add_parser("mix-set", help='Mix anlegen, z. B. mix-set Abend "Hip-Hop=30,EDM=70"')
    s.add_argument("name")
    s.add_argument("weights")
    s.add_argument("--length", type=int, default=50)
    s.add_argument("--novelty", type=float, default=0.2, help="Anteil neuer Songs, 0 bis 1")
    s.set_defaults(func=cmd_mix_set)
    s = sub.add_parser("mix-list", help="Mixe anzeigen")
    s.set_defaults(func=cmd_mix_list)
    s = sub.add_parser("mix-build", help="Mix erzeugen und anzeigen oder hochladen")
    s.add_argument("name")
    s.add_argument("--push", action="store_true", help="als Playlist in YouTube Music speichern")
    s.add_argument("--offline", action="store_true", help="ohne YouTube Music (keine neuen Songs)")
    s.set_defaults(func=cmd_mix_build)
    args = p.parse_args(argv)
    db.init(data_path())
    try:
        args.func(args)
    except (ytm.NotAuthenticated, ValueError) as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
