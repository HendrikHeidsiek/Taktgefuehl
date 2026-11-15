"""Sound fingerprints: what a song sounds like, from its audio, independent of genre labels.

For each song the audio is fetched with yt-dlp (AAC, about 4 MB), three 10 s
windows at 25 %, 50 % and 75 % of the song go through the Discogs-EffNet model (Essentia's mel input,
the model as ONNX, so no TensorFlow), and the averaged 1280-dim embedding is kept. The audio file is
deleted right away. In a test this matched the whole song's fingerprint as well as the whole song did.

Optional: without essentia/onnxruntime installed (or with SOUND=0) everything else works on tags alone.
"""
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from . import db, report

MODEL_DIR = Path(os.environ.get("MODEL_PATH", Path(__file__).resolve().parent.parent / "models"))
MODEL_FILE = "discogs-effnet-bsdynamic-1.onnx"
MODEL_NAME = "discogs-effnet-3x10"
DIM = 1280
WINDOW = 10.0  # seconds per excerpt
PAUSE = 2.0  # seconds between downloads, to stay unremarkable
RETRY_AFTER = 14 * 86400  # failed songs are tried again after two weeks
PATCH, PATCH_HOP = 128, 62  # mel frames per model patch and between patches (~2 s and ~1 s)

_lock = threading.Lock()
_session = None


def available() -> bool:
    if os.environ.get("SOUND", "1") == "0" or not (MODEL_DIR / MODEL_FILE).exists():
        return False
    try:
        import essentia  # noqa: F401
        import onnxruntime  # noqa: F401
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return True


def _model():
    global _session
    with _lock:
        if _session is None:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = int(os.environ.get("SOUND_THREADS", "2"))
            _session = ort.InferenceSession(str(MODEL_DIR / MODEL_FILE), opts)
        return _session


def windows(duration: float) -> list[tuple[float, float]]:
    """Three 10 s windows at 25/50/75 %; short songs get their middle part."""
    if duration <= 3 * WINDOW:
        return [(0.0, duration)]
    return [(max(0.0, duration * f - WINDOW / 2), min(duration, duration * f + WINDOW / 2)) for f in (.25, .5, .75)]


def embed(audio: np.ndarray, sample_rate: int = 16000) -> tuple[np.ndarray, np.ndarray]:
    """Mean embedding and mean style activations of the 3x10 s windows of a 16 kHz mono signal."""
    import essentia.standard as es

    mel = es.TensorflowInputMusiCNN()
    patches = []
    for start, end in windows(len(audio) / sample_rate):
        part = audio[int(start * sample_rate):int(end * sample_rate)].astype(np.float32)
        frames = np.array([mel(f) for f in es.FrameGenerator(part, frameSize=512, hopSize=256, startFromZero=True)])
        if len(frames) < PATCH:
            frames = np.pad(frames, ((0, PATCH - len(frames)), (0, 0)))
        patches += [frames[s:s + PATCH] for s in range(0, len(frames) - PATCH + 1, PATCH_HOP)]
    styles, embeddings = _model().run(["activations", "embeddings"],
                                      {"melspectrogram": np.stack(patches).astype(np.float32)})
    return embeddings.mean(axis=0), styles.mean(axis=0)


class _Quiet:
    """yt-dlp prints errors even when quiet; we report them ourselves, shorter."""

    def debug(self, msg):
        pass

    info = warning = error = debug


def fetch(video_id: str, workdir: Path) -> Path:
    """Audio of one song into workdir, always anonymous. Never with the account's cookies: YouTube
    rotates them when another client uses them, which signed the server out of YouTube Music."""
    from yt_dlp import YoutubeDL

    # AAC (m4a): essentia's Linux build cannot decode the Opus streams YouTube prefers.
    opts = {"format": "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]", "outtmpl": str(workdir / "%(id)s.%(ext)s"),
            "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True, "logger": _Quiet()}
    with YoutubeDL(opts) as ydl:
        ydl.download([f"https://music.youtube.com/watch?v={video_id}"])
    return next(workdir.glob(f"{video_id}.*"))


def analyze(video_id: str, workdir: Path) -> tuple[np.ndarray, np.ndarray]:
    import essentia.standard as es

    path = fetch(video_id, workdir)
    try:
        audio = es.MonoLoader(filename=str(path), sampleRate=16000, resampleQuality=4)()
    finally:
        path.unlink(missing_ok=True)
    return embed(audio)


def top_styles(activations: np.ndarray, n: int = 5) -> list[str]:
    names = json.loads((MODEL_DIR / "discogs400-styles.json").read_text())
    return [names[i].split("---")[-1].lower() for i in np.argsort(-activations)[:n]]


def store(video_id: str, vec: np.ndarray | None, styles: list[str] | None = None) -> None:
    db.write("INSERT INTO track_sound(video_id, vec, styles, model, ok, at) VALUES(?,?,?,?,?,?) "
             "ON CONFLICT(video_id) DO UPDATE SET vec = excluded.vec, styles = excluded.styles, "
             "model = excluded.model, ok = excluded.ok, at = excluded.at",
             (video_id, vec.astype(np.float32).tobytes() if vec is not None else None,
              json.dumps(styles or []), MODEL_NAME, 1 if vec is not None else 0, int(time.time())))


def vectors(video_ids=None) -> dict[str, np.ndarray]:
    rows = db.query("SELECT video_id, vec FROM track_sound WHERE ok = 1 AND model = ?", (MODEL_NAME,))
    wanted = set(video_ids) if video_ids is not None else None
    return {r["video_id"]: np.frombuffer(r["vec"], dtype=np.float32) for r in rows
            if wanted is None or r["video_id"] in wanted}


def analyze_many(video_ids: list[str], log=print, analyze_fn=None) -> int:
    """Analyzes the songs one after the other with a pause in between. Returns how many succeeded."""
    analyze_fn = analyze_fn or analyze
    names = {r["video_id"]: " – ".join(x for x in (r["artist"], r["title"]) if x) for r in db.query(
        f"SELECT video_id, artist, title FROM tracks WHERE video_id IN ({','.join('?' * len(video_ids))})",
        video_ids)} if video_ids else {}
    done = 0
    with tempfile.TemporaryDirectory(prefix="takt-sound-") as tmp:
        workdir = Path(tmp)
        for n, vid in enumerate(video_ids, 1):
            try:
                vec, acts = analyze_fn(vid, workdir)
                store(vid, vec, top_styles(acts) if acts is not None else [])
                done += 1
            except Exception as e:  # unavailable, age-restricted, region lock ...: remember, retry much later
                store(vid, None)
                reason = report.download_reason(e)
                what = names.get(vid) or vid
                report.skip(log, reason, what if reason != "anderer Fehler" else f"{what} ({report.clean(e)[:100]})")
            report.progress(log, n, len(video_ids), f"  {n}/{len(video_ids)} Songs angehört" if n % 20 == 0 else None)
            if n < len(video_ids):
                time.sleep(PAUSE)
    return done


def pending(limit: int | None = None, now: int | None = None) -> list[str]:
    """Played or liked songs without a fingerprint, most played first."""
    now = now or int(time.time())
    rows = db.query(
        "SELECT t.video_id, COUNT(p.id) AS plays FROM tracks t LEFT JOIN plays p ON p.video_id = t.video_id "
        "LEFT JOIN track_sound s ON s.video_id = t.video_id AND s.model = ? "
        "WHERE s.video_id IS NULL OR (s.ok = 0 AND s.at < ?) "
        "GROUP BY t.video_id HAVING plays > 0 OR t.liked = 1 ORDER BY plays DESC, t.video_id",
        (MODEL_NAME, now - RETRY_AFTER))
    return [r["video_id"] for r in rows][:limit]
