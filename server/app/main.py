"""Taktgefühl server: JSON API for the app, plus the schedule (sync, daily rebuild). The only web page
is the start page with the Android app; everything else happens in the app.

Reachable from the internet behind a reverse proxy, so: one password (scrypt), bearer tokens stored as
hashes, a guard against password guessing, and the credentials (YouTube cookies, Anthropic key) are
write-only through the API.
"""
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import ai, costs, db, jobs, mixer, oauth, phases, pipeline, report, sound, takeout, ytm
from .auth import drop_pseudo_headers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("taktgefuehl")

VERSION = "0.2.10"
DATA = pipeline.data_path()
DOWNLOAD = Path(__file__).resolve().parent.parent / "download"
STATIC = Path(__file__).resolve().parent / "static"
SYNC_MINUTES = int(os.environ.get("SYNC_MINUTES", "30"))
REBUILD_HOUR = int(os.environ.get("REBUILD_HOUR", "5"))
SESSION_DAYS = 180
MAX_TAKEOUT = 300 * 1024 * 1024

app = FastAPI(title="Taktgefühl", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

JOB_LABELS = {"sync": "der Abgleich", "rebuild": "„Alles neu berechnen“", "phases": "die Phasenberechnung",
              "mixes": "das Erneuern der Mixe"}
JOBS = {
    "sync": lambda log: pipeline.sync(log),
    "rebuild": lambda log: pipeline.rebuild(log),
    "phases": lambda log: (pipeline.enrich_all(None, log=log), pipeline.compute_phases(log)),
    "mixes": lambda log: pipeline.push_mixes(ytm.client(DATA), log, force=True),
}


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    detail = exc.detail if isinstance(exc.detail, str) and exc.status_code != 404 else "Nicht gefunden"
    return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


# ---------------------------------------------------------------- password & sessions

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str | None) -> bool:
    try:
        _, salt, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def token_hash(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def session(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(401, "Nicht angemeldet")
    th = token_hash(token)
    now = int(time.time())
    row = db.one("SELECT last_used FROM sessions WHERE token_hash = ?", (th,))
    if not row or row["last_used"] < now - SESSION_DAYS * 86400:
        raise HTTPException(401, "Sitzung abgelaufen")
    if row["last_used"] < now - 300:
        db.write("UPDATE sessions SET last_used = ? WHERE token_hash = ?", (now, th))
    return th


class LoginGuard:
    """Slows down password guessing: 8 failures within 15 minutes block that address for 15 minutes,
    20 failures from anywhere block all logins for 5 minutes. A successful login clears the address."""

    WINDOW, IP_MAX, IP_BLOCK, ALL_MAX, ALL_BLOCK = 15 * 60, 8, 15 * 60, 20, 5 * 60

    def __init__(self):
        self.lock = threading.Lock()
        self.fails: dict[str, list[float]] = {}
        self.blocked: dict[str, float] = {}

    def wait_seconds(self, ip: str) -> int:
        with self.lock:
            until = max(self.blocked.get(ip, 0), self.blocked.get("*", 0))
        return max(0, int(until - time.time()))

    def failed(self, ip: str) -> None:
        now = time.time()
        with self.lock:
            for key, limit, block in ((ip, self.IP_MAX, self.IP_BLOCK), ("*", self.ALL_MAX, self.ALL_BLOCK)):
                recent = [t for t in self.fails.get(key, []) if now - t < self.WINDOW] + [now]
                self.fails[key] = recent
                if len(recent) >= limit:
                    self.blocked[key] = now + block
                    self.fails[key] = []
                    log.warning("Login gesperrt für %s nach %d Fehlversuchen", key, limit)

    def succeeded(self, ip: str) -> None:
        with self.lock:
            self.fails.pop(ip, None)
            self.blocked.pop(ip, None)


login_guard = LoginGuard()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return request.headers.get("x-real-ip") or (fwd.split(",")[0].strip() if fwd else "") or \
        (request.client.host if request.client else "?")


# ---------------------------------------------------------------- startup, password reset

# Forgot the password? Put "passwort-zuruecksetzen.txt" with the new password into the data folder on
# the NAS. The server picks it up within half a minute, sets it, logs out all devices and deletes the file.
RESET_FILE = DATA / "passwort-zuruecksetzen.txt"


def _apply_password_reset() -> None:
    if not RESET_FILE.is_file():
        return
    try:
        lines = RESET_FILE.read_text(encoding="utf-8-sig").strip().splitlines()
        pw = lines[0].strip() if lines else ""
    except OSError:
        pw = ""
    try:
        RESET_FILE.unlink()
    except OSError:
        log.error("Passwort-Datei konnte nicht gelöscht werden: %s", RESET_FILE)
    if len(pw) < 8:
        log.error("Passwort nicht geändert: in %s stand kein Passwort mit mindestens 8 Zeichen", RESET_FILE.name)
        return
    db.set_state("password_hash", hash_password(pw))
    db.write("DELETE FROM sessions")
    log.warning("Neues Passwort gesetzt (aus %s, Datei gelöscht), alle Geräte abgemeldet", RESET_FILE.name)


def _watch_password_reset() -> None:
    while True:
        try:
            _apply_password_reset()
        except Exception as e:  # never let this kill the thread
            log.exception("Passwort-Reset fehlgeschlagen: %s", e)
        time.sleep(20)


@app.on_event("startup")
def startup() -> None:
    db.init(DATA)
    if not db.get_state("password_hash"):
        pw = os.environ.get("ADMIN_PASSWORD") or secrets.token_urlsafe(9)
        db.set_state("password_hash", hash_password(pw))
        if os.environ.get("ADMIN_PASSWORD"):
            log.info("Passwort aus ADMIN_PASSWORD gesetzt")
        else:
            log.warning("Passwort festgelegt: %s  (bitte in der App ändern)", pw)
    threading.Thread(target=_watch_password_reset, daemon=True).start()
    if os.environ.get("SCHEDULE", "1") != "0":
        jobs.schedule(SYNC_MINUTES, REBUILD_HOUR, JOBS)


# ---------------------------------------------------------------- public

@app.get("/api/health")
def health():
    return {"ok": True, "app": "taktgefuehl", "version": VERSION}


# The Android app ships inside the image, so it always matches this server.
APK = DOWNLOAD / "taktgefuehl.apk"


def public_base(request: Request) -> str:
    """Address the server is reached under (PUBLIC_URL, else what the proxy forwards)."""
    url = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")
    if url:
        return url
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}"


def _app_info() -> dict | None:
    """Written by release.sh next to the APK: {"version": "0.1.0", "build": 1}."""
    try:
        info = json.loads((DOWNLOAD / "app-version.json").read_text())
        return {"version": str(info["version"]), "build": int(info["build"])}
    except (ValueError, KeyError, OSError):
        return None


def _apk_name() -> str:
    # Every release gets its own address, so no cache (proxy, browser) hands out an older APK.
    info = _app_info()
    return f"taktgefuehl-{info['version']}.apk" if info else "taktgefuehl.apk"


@app.get("/", include_in_schema=False)
def landing(request: Request):
    """The only page: what this is and the Android app. Everything else happens in the app."""
    info = _app_info()
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    if APK.is_file() and info:
        size = f"{APK.stat().st_size / 1024 / 1024:.0f} MB"
        meta = f"Version {html.escape(info['version'])} · {size}"
        page = page.replace("{{APK}}", "/" + _apk_name()).replace("{{META}}", meta)
    else:
        page = page.replace("{{APK}}", "#").replace("{{META}}", "Noch nicht auf diesem Server")
    name = os.environ.get("OPERATOR_NAME", "").strip()
    link = os.environ.get("OPERATOR_URL", "").strip()
    who = f'<a href="{html.escape(link)}" rel="noopener">{html.escape(name)}</a>' if link else html.escape(name)
    page = page.replace("{{OPERATOR}}", f"<span>Betreiber dieser Seite: {who}</span>" if name else "")
    page = page.replace("{{BASE}}", html.escape(public_base(request)))
    return Response(page, media_type="text/html",
                    headers={"Cache-Control": "no-cache", "X-Robots-Tag": "noindex, nofollow"})


@app.get("/qr/app.svg", include_in_schema=False)
def app_qr(request: Request):
    """QR code with the download link of this server, for the start page."""
    import io
    import segno
    buf = io.BytesIO()
    segno.make(public_base(request) + "/taktgefuehl.apk", error="m").save(
        buf, kind="svg", scale=1, border=0, dark="#1A1033", light=None, xmldecl=False)
    svg = buf.getvalue().decode().replace('class="segno"', 'shape-rendering="crispEdges"', 1)
    svg = re.sub(r'width="(\d+)" height="(\d+)"', r'viewBox="0 0 \1 \2" width="\1" height="\2"', svg, count=1)
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/app")
def app_info(response: Response):
    """Newest Android app this server offers. Public, the app asks before login too."""
    response.headers["Cache-Control"] = "no-store"
    info = _app_info()
    if not APK.is_file() or not info:
        return {"android": None}
    return {"android": {**info, "size": APK.stat().st_size, "url": "/" + _apk_name()}}


@app.api_route("/taktgefuehl.apk", methods=["GET", "HEAD"], include_in_schema=False)
def apk_latest():
    """Stable link: redirects to the current version's own address."""
    if not APK.is_file():
        raise HTTPException(404)
    return RedirectResponse("/" + _apk_name(), status_code=302, headers={"Cache-Control": "no-store"})


@app.api_route("/taktgefuehl-{version}.apk", methods=["GET", "HEAD"], include_in_schema=False)
def apk_version(version: str):
    if not APK.is_file():
        raise HTTPException(404)
    if f"taktgefuehl-{version}.apk" != _apk_name():  # an old link: send people to the current one
        return RedirectResponse("/" + _apk_name(), status_code=302, headers={"Cache-Control": "no-store"})
    return FileResponse(APK, media_type="application/vnd.android.package-archive", filename=_apk_name(),
                        headers={"Cache-Control": "public, max-age=31536000, immutable",
                                 "X-Robots-Tag": "noindex, nofollow"})


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


# ---------------------------------------------------------------- auth

class LoginIn(BaseModel):
    password: str
    device: str | None = None


class PasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8)


@app.post("/api/auth/login")
def login(body: LoginIn, request: Request):
    ip = client_ip(request)
    wait = login_guard.wait_seconds(ip)
    if wait:
        minutes = max(1, (wait + 59) // 60)
        raise HTTPException(429, f"Zu viele Fehlversuche. Bitte in {minutes} "
                                 f"{'Minute' if minutes == 1 else 'Minuten'} erneut versuchen.",
                            headers={"Retry-After": str(wait)})
    if not check_password(body.password, db.get_state("password_hash")):
        login_guard.failed(ip)
        time.sleep(0.5)
        raise HTTPException(401, "Passwort falsch")
    login_guard.succeeded(ip)
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    db.write("INSERT INTO sessions(token_hash, created_at, last_used) VALUES(?,?,?)", (token_hash(token), now, now))
    return {"token": token, "version": VERSION}


@app.post("/api/auth/logout")
def logout(th=Depends(session)):
    db.write("DELETE FROM sessions WHERE token_hash = ?", (th,))
    return {"ok": True}


@app.put("/api/auth/password")
def change_password(body: PasswordIn, th=Depends(session)):
    if not check_password(body.old_password, db.get_state("password_hash")):
        raise HTTPException(403, "Altes Passwort falsch")
    db.set_state("password_hash", hash_password(body.new_password))
    db.write("DELETE FROM sessions WHERE token_hash != ?", (th,))
    return {"ok": True}


# ---------------------------------------------------------------- status & jobs

def _count(sql: str) -> int:
    return db.one(sql)[0]


@app.get("/api/status")
def status(_=Depends(session)):
    return {
        "version": VERSION,
        "youtube": ytm.connected(DATA),
        "youtube_method": ytm.method(DATA),
        "oauth_client": oauth.client_config(DATA) is not None,
        "youtube_expired": bool(db.get_state("youtube_expired")),
        "ai": {"connected": ai.api_key(DATA) is not None, "model": ai.model(),
               "model_name": (costs.model_info(ai.model()) or {}).get("name")},
        "counts": {
            "tracks": _count("SELECT COUNT(*) FROM tracks"),
            "liked": _count("SELECT COUNT(*) FROM tracks WHERE liked = 1"),
            "plays": _count("SELECT COUNT(*) FROM plays"),
            "plays_timed": _count("SELECT COUNT(*) FROM plays WHERE approx = 0"),
            "plays_mix": _count("SELECT COUNT(*) FROM plays WHERE via IS NOT NULL"),
            "phases": _count("SELECT COUNT(*) FROM clusters WHERE active = 1"),
            "mixes": _count("SELECT COUNT(*) FROM mixes"),
            "sound": _count("SELECT COUNT(*) FROM track_sound WHERE ok = 1"),
        },
        "sound": sound.available(),
        "first_play": db.one("SELECT MIN(at) FROM plays")[0],
        "last_sync": db.get_state("last_sync"),
        "last_rebuild": db.get_state("last_rebuild"),
        "last_error": db.get_state("last_error"),
        "schedule": {"sync_minutes": SYNC_MINUTES, "rebuild_hour": REBUILD_HOUR},
        "job": jobs.status(),
        "mix_job": jobs.status("mix"),
    }


@app.post("/api/jobs/{name}")
def start_job(name: str, _=Depends(session)):
    if name not in JOBS:
        raise HTTPException(404)
    if name != "phases" and not ytm.connected(DATA):
        raise HTTPException(409, "YouTube Music ist nicht verbunden.")
    if not jobs.start(name, JOBS[name]):
        label = JOB_LABELS.get(jobs.status()["name"], "eine andere Aufgabe")
        raise HTTPException(409, f"Gerade läuft schon {label}. Bitte warten, bis das fertig ist.")
    return jobs.status()


@app.get("/api/jobs")
def job_status(lane: str = "main", _=Depends(session)):
    """The running (or last) job of a lane: 'main' for sync and rebuild, 'mix' for building one mix."""
    if lane not in jobs.LANES:
        raise HTTPException(404)
    return jobs.status(lane)


# ---------------------------------------------------------------- phases

class PhaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


@app.get("/api/phases")
def list_phases(_=Depends(session)):
    return [{**p, "top_songs": phases.top_songs(p["id"], 8)} for p in phases.summary()]


@app.patch("/api/phases/{phase_id}")
def rename_phase(phase_id: int, body: PhaseIn, _=Depends(session)):
    if not phases.rename(phase_id, body.name.strip()):
        raise HTTPException(404)
    return {"ok": True}


# ---------------------------------------------------------------- mixes

class MixIn(BaseModel):
    weights: dict[int, float]  # phase id -> share (any scale, normalised here)
    length: int = Field(50, ge=10, le=200)
    novelty: float = Field(0.2, ge=0, le=1)
    max_minutes: int = Field(15, ge=0, le=600)  # longest song, 0 = no limit
    refresh_days: int = Field(7, ge=0, le=365)  # renewal by the daily run, 0 = only by hand


class BuildIn(BaseModel):
    push: bool = False


def _mix_out(m) -> dict:
    names = {r["id"]: r["name"] for r in db.query("SELECT id, name FROM clusters WHERE active = 1")}
    weights = {int(k): v for k, v in json.loads(m["weights"]).items()}
    return {"name": m["name"], "length": m["length"], "novelty": m["novelty"], "max_minutes": m["max_minutes"],
            "refresh_days": m["refresh_days"], "next_refresh": mixer.next_refresh(m), "built_at": m["built_at"],
            "playlist_url": f"https://music.youtube.com/playlist?list={m['playlist_id']}" if m["playlist_id"] else None,
            "parts": [{"phase_id": cid, "name": names.get(cid), "share": w} for cid, w in weights.items()]}


@app.get("/api/mixes")
def list_mixes(_=Depends(session)):
    return [_mix_out(m) for m in db.query("SELECT * FROM mixes ORDER BY name")]


@app.put("/api/mixes/{name}")
def save_mix(name: str, body: MixIn, _=Depends(session)):
    name = name.strip()
    active = {r["id"] for r in db.query("SELECT id FROM clusters WHERE active = 1")}
    weights = {k: v for k, v in body.weights.items() if v > 0}
    if not name or len(name) > 60:
        raise HTTPException(422, "Name fehlt oder ist zu lang.")
    if not weights or not set(weights) <= active:
        raise HTTPException(422, "Mindestens eine bestehende Phase mit Anteil über 0 wählen.")
    total = sum(weights.values())
    mixer.save_mix(name, {k: v / total for k, v in weights.items()}, body.length, body.novelty, body.max_minutes,
                    body.refresh_days)
    return _mix_out(db.one("SELECT * FROM mixes WHERE name = ?", (name,)))


@app.post("/api/mixes/{name}/build", status_code=202)
def build_mix(name: str, body: BuildIn, _=Depends(session)):
    """Starts building the mix as a job (new songs take a while: radio plus AI check), push=true also
    updates the playlist in YouTube Music. The job keeps running when the app closes; the result is at
    GET /api/mixes/{name}/result afterwards."""
    if not db.one("SELECT 1 FROM mixes WHERE name = ?", (name,)):
        raise HTTPException(404)
    if not ytm.connected(DATA):
        raise HTTPException(409, "YouTube Music ist nicht verbunden.")

    def run(log):
        yt = ytm.client(DATA)
        report.step(log, "Songs auswählen")
        result = pipeline.build_mix(name, yt, push=False, log=log)
        log(f"{len(result['video_ids'])} Songs, davon {result['new']} neu")
        if body.push:
            report.step(log, "Zu YouTube Music schicken")
            mixer.push(yt, result)
            mixer.save_result(result, pushed=True)
            log("Playlist in YouTube Music aktualisiert")

    if not jobs.start("mix", run, detail={"mix": name, "push": body.push}):
        running = jobs.status("mix")["detail"] or {}
        raise HTTPException(409, f"Gerade wird schon der Mix „{running.get('mix', '?')}“ erstellt. Bitte kurz warten.")
    return jobs.status("mix")


@app.get("/api/mixes/{name}/result")
def mix_result(name: str, _=Depends(session)):
    """The songs of the last build of this mix."""
    mix = db.one("SELECT * FROM mixes WHERE name = ?", (name,))
    result = db.get_state(f"mix_result:{name}")
    if not mix or not result:
        raise HTTPException(404)
    return {"mix": _mix_out(mix), **result}


@app.delete("/api/mixes/{name}")
def delete_mix(name: str, _=Depends(session)):
    """Forgets the mix; the playlist stays in YouTube Music."""
    if not db.write("DELETE FROM mixes WHERE name = ?", (name,)).rowcount:
        raise HTTPException(404)
    db.write("DELETE FROM state WHERE key = ?", (f"mix_result:{name}",))
    return {"ok": True}


# ---------------------------------------------------------------- credentials & import

class YoutubeIn(BaseModel):
    headers: str = Field(min_length=20, max_length=100_000)


class KeyIn(BaseModel):
    key: str = Field(min_length=20, max_length=500)


@app.put("/api/credentials/youtube")
def set_youtube(body: YoutubeIn, _=Depends(session)):
    """Request headers copied from music.youtube.com. Tested before they replace the current ones."""
    from ytmusicapi.auth.browser import setup_browser
    from ytmusicapi import YTMusic

    DATA.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA, suffix=".json")
    os.close(fd)
    try:
        setup_browser(tmp, drop_pseudo_headers(body.headers))
        os.chmod(tmp, 0o600)
        YTMusic(tmp, language="en").get_history()
    except Exception as e:
        os.unlink(tmp)
        raise HTTPException(422, f"Diese Header funktionieren nicht: {e}")
    os.replace(tmp, DATA / ytm.AUTH_FILE)
    db.set_state("youtube_expired", False)
    return {"ok": True}


@app.delete("/api/credentials/youtube")
def delete_youtube(_=Depends(session)):
    (DATA / ytm.AUTH_FILE).unlink(missing_ok=True)
    (DATA / oauth.TOKEN_FILE).unlink(missing_ok=True)
    return {"ok": True}


class OAuthClientIn(BaseModel):
    client_id: str = Field(min_length=10, max_length=300)
    client_secret: str = Field(min_length=5, max_length=300)


@app.put("/api/youtube/oauth-client")
def set_oauth_client(body: OAuthClientIn, _=Depends(session)):
    """Your Google OAuth client (type "TVs and Limited Input devices"); write-only. Checked by asking
    Google for a device code, which also starts the sign-in."""
    old = oauth.client_config(DATA)
    oauth.save_client(DATA, body.client_id, body.client_secret)
    try:
        return oauth.start(DATA)
    except oauth.OAuthError as e:
        if old:
            oauth.save_client(DATA, *old)
        else:
            (DATA / oauth.CLIENT_FILE).unlink(missing_ok=True)
        raise HTTPException(422, str(e))


@app.post("/api/youtube/device")
def start_device_login(_=Depends(session)):
    """Starts the device-code sign-in: returns the code to confirm at the address."""
    try:
        return oauth.start(DATA)
    except oauth.OAuthError as e:
        raise HTTPException(409, str(e))


@app.post("/api/youtube/device/poll")
def poll_device_login(_=Depends(session)):
    """Checks once whether the code was confirmed; the app calls this every few seconds."""
    from ytmusicapi import YTMusic

    def verify(path):  # the token must really open YouTube Music, history included
        YTMusic(str(path), oauth_credentials=oauth.credentials(DATA), language="en").get_history()

    try:
        status = oauth.poll(DATA, verify)
    except oauth.OAuthError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(422, f"Angemeldet, aber YouTube Music antwortet nicht wie erwartet: "
                                 f"{str(e).splitlines()[0][:150]}")
    if status == "ok":
        db.set_state("youtube_expired", False)
    return {"status": status}


@app.put("/api/credentials/anthropic")
def set_anthropic(body: KeyIn, _=Depends(session)):
    import anthropic

    key = body.key.strip()
    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except anthropic.AuthenticationError:
        raise HTTPException(422, "Der Key wird von Anthropic nicht akzeptiert.")
    except anthropic.APIError as e:
        raise HTTPException(422, f"Key konnte nicht geprüft werden: {e}")
    f = DATA / ai.KEY_FILE
    f.write_text(key + "\n")
    f.chmod(0o600)
    costs.refresh_if_stale(ai.client(DATA), log=lambda *a: None)
    return {"ok": True}


# ---------------------------------------------------------------- AI model & costs

class ModelIn(BaseModel):
    model: str = Field(min_length=3, max_length=100)


def _models_out() -> dict:
    cat = costs.catalog()
    return {"selected": ai.model(), "models": cat["models"], "at": cat["at"], "prices_at": cat["prices_at"],
            "prices_error": cat["prices_error"]}


@app.get("/api/ai/models")
def ai_models(_=Depends(session)):
    """The models this key can use, with prices in dollars per million tokens (refreshed every few days)."""
    if not costs.catalog()["at"]:
        costs.refresh_if_stale(ai.client(DATA), log=lambda *a: None)
    return _models_out()


@app.post("/api/ai/models/refresh")
def ai_models_refresh(_=Depends(session)):
    claude = ai.client(DATA)
    if not claude:
        raise HTTPException(409, "Kein Anthropic-API-Key hinterlegt.")
    try:
        costs.refresh(claude, log=lambda *a: None)
    except Exception as e:
        raise HTTPException(502, f"Modellliste nicht abrufbar: {report.clean(e)[:150]}")
    return _models_out()


@app.put("/api/ai/model")
def ai_set_model(body: ModelIn, _=Depends(session)):
    if not costs.model_info(body.model):
        raise HTTPException(422, "Dieses Modell steht nicht in der Liste.")
    db.set_state("ai_model", body.model)
    return _models_out()


@app.get("/api/ai/costs")
def ai_costs(_=Depends(session)):
    """What the AI requests cost, by month, model and purpose, plus the last requests."""
    return costs.summary()


@app.delete("/api/credentials/anthropic")
def delete_anthropic(_=Depends(session)):
    (DATA / ai.KEY_FILE).unlink(missing_ok=True)
    return {"ok": True}


@app.post("/api/takeout")
async def upload_takeout(request: Request, _=Depends(session)):
    """Body: the watch-history.json from Google Takeout."""
    size = int(request.headers.get("content-length") or 0)
    if size > MAX_TAKEOUT:
        raise HTTPException(413, "Datei zu groß.")
    target = DATA / "takeout-watch-history.json"
    written = 0
    with open(target, "wb") as f:
        async for chunk in request.stream():
            written += len(chunk)
            if written > MAX_TAKEOUT:
                f.close()
                target.unlink(missing_ok=True)
                raise HTTPException(413, "Datei zu groß.")
            f.write(chunk)
    try:
        count = takeout.import_file(target)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(422, f"Das ist keine watch-history.json im JSON-Format ({e.__class__.__name__}).")
    finally:
        target.unlink(missing_ok=True)
    return {"imported": count}
