"""What a running job tells the app: the steps it goes through with their progress, and a log whose
entries say what kind they are (step, info, warning, skipped song, error), in plain German instead of
raw tool output.

The pipeline functions take any `log` callable (print in the CLI, a lambda in tests). The helpers here
use the job's richer methods when the log has them and fall back to a plain line otherwise.
"""
import re

ANSI = re.compile(r"\x1b\[[0-9;]*m|\[[0-9;]*m")


def clean(text) -> str:
    """Error text without terminal colours and yt-dlp's 'ERROR: [youtube] <id>:' prefix, one line."""
    text = ANSI.sub("", str(text)).strip().splitlines()[0] if str(text).strip() else ""
    return re.sub(r"^(ERROR:\s*)?(\[\w+\]\s*)?([\w-]{11}:\s*)?", "", text).strip()


def step(log, title: str, total: int | None = None) -> None:
    """A new section of the job, optionally with how many items it works through."""
    f = getattr(log, "step", None)
    f(title, total) if f else log(title)


def progress(log, done: int, total: int | None = None, text: str | None = None) -> None:
    """Progress of the current step; the plain log only gets the text, if any."""
    f = getattr(log, "progress", None)
    if f:
        f(done, total)
    elif text:
        log(text)


def warn(log, text: str) -> None:
    f = getattr(log, "warn", None)
    f(text) if f else log(text)


def skip(log, reason: str, what: str) -> None:
    """One item left out for a reason; the step counts the reasons for its summary."""
    f = getattr(log, "skip", None)
    f(reason, what) if f else log(f"{what}: {reason}")


# Why YouTube does not hand out a song's audio, from yt-dlp's error text.
DOWNLOAD_REASONS = [
    (("confirm your age", "age-restricted", "inappropriate"), "altersbeschränkt, ohne Anmeldung nicht abrufbar"),
    (("not a bot",), "YouTube hält den Server für einen Bot"),
    (("403", "forbidden"), "YouTube verweigert den Download"),
    (("private video",), "privates Video"),
    (("not available in your country", "blocked it in your country", "geo"), "in Deutschland gesperrt"),
    (("video unavailable", "not available", "has been removed", "terminated"), "nicht mehr verfügbar"),
    (("timed out", "timeout", "connection", "temporary failure"), "Netzwerkfehler"),
]


def download_reason(error) -> str:
    text = clean(error).lower()
    for needles, reason in DOWNLOAD_REASONS:
        if any(n in text for n in needles):
            return reason
    return "anderer Fehler"
