"""Credentials from the macOS clipboard, without printing them:

  python -m app.auth                         YouTube Music request headers -> data/browser.json (local)
  python -m app.auth anthropic               Anthropic API key -> data/anthropic.key (local)
  python -m app.auth send https://server     YouTube Music request headers -> the Taktgefühl server

Pasting into the terminal fails on macOS, because a terminal line takes at most 1024 characters and
the cookie header is longer. So we read the clipboard directly and never print the values.
"""
import getpass
import json
import subprocess
import sys
import urllib.error
import urllib.request

from ytmusicapi.auth.browser import setup_browser

from .pipeline import data_path
from .ytm import AUTH_FILE


def drop_pseudo_headers(raw: str) -> str:
    """Removes Chrome's ":authority", ":path" ... and, in its one-value-per-line format, their value."""
    out, skip_next = [], False
    for line in raw.splitlines():
        if skip_next:
            skip_next = False
        elif line.startswith(":"):
            skip_next = ": " not in line
        else:
            out.append(line)
    return "\n".join(out)


def save_anthropic_key() -> None:
    """python -m app.auth anthropic: stores the API key from the clipboard in data/anthropic.key."""
    key = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout.strip()
    if not key.startswith("sk-ant-") or any(c.isspace() for c in key):
        sys.exit("In der Zwischenablage liegt kein Anthropic-API-Key (beginnt mit sk-ant-).")
    path = data_path()
    path.mkdir(parents=True, exist_ok=True)
    target = path / "anthropic.key"
    target.write_text(key + "\n")
    target.chmod(0o600)
    print(f"Gespeichert in {target} (endet auf …{key[-4:]}).")
    print("Tipp: Zwischenablage jetzt mit etwas anderem überschreiben.")


def _call(method: str, url: str, body: dict, token: str | None = None) -> dict:
    # Own user agent: Cloudflare in front of the server blocks Python's default one (error 1010).
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": "Taktgefuehl-Mac/1.0",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        try:
            detail = json.load(e).get("detail")
        except Exception:
            detail = None
        sys.exit(f"Server: {detail or e}")
    except urllib.error.URLError as e:
        sys.exit(f"Server nicht erreichbar: {e.reason}")


def send_to_server(server: str) -> None:
    """python -m app.auth send https://server: the headers go to the server, which tests and stores them."""
    raw = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout
    if "cookie" not in raw.lower():
        sys.exit("In der Zwischenablage stehen keine Anfrage-Header mit Cookie.")
    base = server.rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://" + base
    token = _call("POST", f"{base}/api/auth/login", {"password": getpass.getpass("Taktgefühl-Passwort: "),
                                                      "device": "Mac (Anmeldehilfe)"})["token"]
    try:
        _call("PUT", f"{base}/api/credentials/youtube", {"headers": raw}, token)
    finally:
        _call("POST", f"{base}/api/auth/logout", {}, token)
    print("YouTube Music ist auf dem Server verbunden.")
    print("Tipp: Zwischenablage jetzt mit etwas anderem überschreiben.")


def main() -> None:
    if sys.argv[1:] == ["anthropic"]:
        return save_anthropic_key()
    if len(sys.argv) == 3 and sys.argv[1] == "send":
        return send_to_server(sys.argv[2])
    raw = drop_pseudo_headers(subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout)
    path = data_path()
    path.mkdir(parents=True, exist_ok=True)
    target = path / AUTH_FILE
    try:
        setup_browser(str(target), raw)
    except Exception as e:
        sys.exit(f"Das waren keine passenden Header: {e}")
    target.chmod(0o600)
    names = sorted(json.loads(target.read_text()))
    print(f"Gespeichert in {target} ({len(names)} Header, u. a. "
          f"{', '.join(n for n in names if n in ('authorization', 'cookie', 'x-goog-authuser'))}).")
    print("Tipp: Zwischenablage jetzt mit etwas anderem überschreiben.")


if __name__ == "__main__":
    main()
