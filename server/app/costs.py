"""Which Claude models the server may use, what they cost, and a ledger of what the AI calls cost.

The model list comes from the Models API (only models this key can use, with their capabilities); the
prices from Anthropic's pricing page, since the API has none. Both are fetched every few days. Each
request is booked with its tokens and, when the price is known, its cost in US dollars. Prices are
taken at booking time; a request booked without a price gets one as soon as the price list knows it.
"""
import re
import time
import urllib.request

from . import db

PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
REFRESH_EVERY = 3 * 86400
FALLBACK_BETA = "server-side-fallback-2026-06-01"  # makes /v1/models list allowed_fallback_models


def catalog() -> dict:
    """{"at", "models": [...], "prices_at", "prices_error"}; empty until the first refresh."""
    return db.get_state("ai_catalog") or {"at": None, "models": [], "prices_at": None, "prices_error": None}


def model_info(model_id: str) -> dict | None:
    return next((m for m in catalog()["models"] if m["id"] == model_id), None)


def _money(cell: str) -> float | None:
    m = re.search(r"\$([\d.,]+)\s*/\s*MTok", cell)
    return float(m.group(1).replace(",", "")) if m else None


def parse_prices(markdown: str) -> dict[str, dict]:
    """The first table under "## Model pricing" -> {"Claude Opus 5.5": {input, cache_write, cache_read,
    output}} in dollars per million tokens. For models with several rows (e.g. by prompt length) the first
    row counts: the requests here are short."""
    section = markdown.split("## Model pricing", 1)[-1]
    prices: dict[str, dict] = {}
    for line in section.splitlines():
        if not line.startswith("|"):
            if prices:
                break  # end of the first table
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6 or _money(cells[1]) is None:
            continue
        name = re.sub(r"\s*\(.*$", "", re.sub(r"<[^>]+>", "", cells[0])).strip()
        prices.setdefault(name, {"input": _money(cells[1]), "cache_write": _money(cells[2]),
                                 "cache_read": _money(cells[4]), "output": _money(cells[5])})
    return prices


def _fetch_pricing() -> str:
    req = urllib.request.Request(PRICING_URL, headers={"User-Agent": "Taktgefuehl/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode()


def refresh(claude, log=print) -> dict:
    """Fetches the models this key can use and the current prices, and prices bookings that had none."""
    old = catalog()
    models = []
    for m in claude.beta.models.list(betas=[FALLBACK_BETA], limit=100):
        caps = m.capabilities or {}
        if hasattr(caps, "model_dump"):  # the SDK returns a typed object
            caps = caps.model_dump()
        if not caps.get("structured_outputs", {}).get("supported"):
            continue  # the AI calls here need JSON output
        models.append({
            "id": m.id, "name": m.display_name, "created_at": int(m.created_at.timestamp()),
            "deprecated": m.deprecated_at is not None,
            "effort_low": bool(caps.get("effort", {}).get("low", {}).get("supported")),
            "fallback": bool(m.allowed_fallback_models),
        })
    prices, error, prices_at = {}, None, old["prices_at"]
    try:
        prices = parse_prices(_fetch_pricing())
        prices_at = int(time.time())
        if not prices:
            error = "Preisseite hatte keine lesbare Tabelle"
    except Exception as e:  # keep the old prices then
        error = f"Preisseite nicht erreichbar: {e}"
    old_prices = {m["id"]: m.get("price") for m in old["models"]}
    for m in models:
        m["price"] = prices.get(m["name"]) or old_prices.get(m["id"])
    out = {"at": int(time.time()), "models": sorted(models, key=lambda m: -m["created_at"]),
           "prices_at": prices_at, "prices_error": error}
    db.set_state("ai_catalog", out)
    _price_open_bookings(out)
    log(f"{len(models)} Modelle, {sum(1 for m in models if m['price'])} mit Preis"
        + (f" ({error})" if error else ""))
    return out


def refresh_if_stale(claude, log=print) -> None:
    at = catalog()["at"]
    if claude and (not at or time.time() - at > REFRESH_EVERY):
        try:
            refresh(claude, log)
        except Exception as e:  # the AI work itself must not fail over the price list
            log(f"Modell- und Preisliste nicht aktualisiert: {e}", "warn")


def cost(price: dict | None, input_tokens: int, output_tokens: int, cache_write: int, cache_read: int) -> float | None:
    if not price or price.get("input") is None or price.get("output") is None:
        return None
    return (input_tokens * price["input"] + output_tokens * price["output"]
            + cache_write * (price.get("cache_write") or price["input"])
            + cache_read * (price.get("cache_read") or 0)) / 1e6


def book(model: str, purpose: str, input_tokens: int, output_tokens: int, cache_write: int = 0,
         cache_read: int = 0) -> None:
    info = model_info(model)
    db.write("INSERT INTO ai_usage(at, model, purpose, input_tokens, output_tokens, cache_write, cache_read, cost) "
             "VALUES(?,?,?,?,?,?,?,?)",
             (int(time.time()), model, purpose, input_tokens, output_tokens, cache_write, cache_read,
              cost(info and info.get("price"), input_tokens, output_tokens, cache_write, cache_read)))


def book_response(response, purpose: str) -> None:
    """Books every attempt of a response: with a server-side fallback, usage.iterations lists each model
    that ran (the top-level usage only covers the last one)."""
    usage = response.usage
    attempts = [it for it in (getattr(usage, "iterations", None) or []) if getattr(it, "model", None)]
    for it in attempts or [usage]:
        book(getattr(it, "model", None) or response.model, purpose, it.input_tokens or 0, it.output_tokens or 0,
             it.cache_creation_input_tokens or 0, it.cache_read_input_tokens or 0)


def _price_open_bookings(cat: dict) -> None:
    prices = {m["id"]: m["price"] for m in cat["models"] if m.get("price")}
    rows = db.query("SELECT * FROM ai_usage WHERE cost IS NULL")
    db.write_many([("UPDATE ai_usage SET cost = ? WHERE id = ?",
                    (cost(prices[r["model"]], r["input_tokens"], r["output_tokens"], r["cache_write"],
                          r["cache_read"]), r["id"]))
                   for r in rows if r["model"] in prices])


def summary(now: int | None = None) -> dict:
    """Costs by month (with models and purposes) and the last requests, for the app."""
    now = now or int(time.time())
    names = {m["id"]: m["name"] for m in catalog()["models"]}
    months = []
    for r in db.query("SELECT strftime('%Y-%m', at, 'unixepoch', 'localtime') AS month, SUM(cost) AS cost, "
                      "COUNT(*) AS calls, SUM(cost IS NULL) AS unpriced, SUM(input_tokens) AS input_tokens, "
                      "SUM(output_tokens) AS output_tokens FROM ai_usage GROUP BY month ORDER BY month DESC LIMIT 12"):
        def by(column: str) -> list[dict]:
            return [{"key": x["k"], "name": names.get(x["k"], x["k"]) if column == "model" else x["k"],
                     "cost": x["cost"] or 0, "calls": x["calls"]}
                    for x in db.query(f"SELECT {column} AS k, SUM(cost) AS cost, COUNT(*) AS calls FROM ai_usage "
                                      "WHERE strftime('%Y-%m', at, 'unixepoch', 'localtime') = ? "
                                      "GROUP BY k ORDER BY cost DESC, calls DESC", (r["month"],))]
        months.append({"month": r["month"], "cost": r["cost"] or 0, "calls": r["calls"], "unpriced": r["unpriced"],
                       "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"],
                       "models": by("model"), "purposes": by("purpose")})
    last30 = db.one("SELECT SUM(cost) AS cost, COUNT(*) AS calls FROM ai_usage WHERE at >= ?", (now - 30 * 86400,))
    total = db.one("SELECT SUM(cost) AS cost, COUNT(*) AS calls FROM ai_usage")
    recent = [{"at": r["at"], "model": r["model"], "name": names.get(r["model"], r["model"]), "purpose": r["purpose"],
               "input_tokens": r["input_tokens"], "output_tokens": r["output_tokens"], "cost": r["cost"]}
              for r in db.query("SELECT * FROM ai_usage ORDER BY at DESC, id DESC LIMIT 30")]
    return {"currency": "USD", "total": total["cost"] or 0, "calls": total["calls"],
            "last_30_days": last30["cost"] or 0, "months": months, "recent": recent}
