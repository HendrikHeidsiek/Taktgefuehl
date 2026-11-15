from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app import ai, costs, db

from test_ai import FakeClaude

PRICING = """# Pricing

## Model pricing

| Model | Base input tokens | 5m cache writes | 1h cache writes | Cache hits and refreshes | Output tokens |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Claude Opus 5.5 | $4 / MTok | $5 / MTok | $8 / MTok | $0.20 / MTok<sup>2</sup> | $20 / MTok |
| Claude Haiku 5.5 (for prompts up to 100,000 tokens) | $0.10 / MTok | $0.125 / MTok | $0.20 / MTok | $0.01 / MTok | $0.50 / MTok |
| Claude Haiku 5.5 (for prompts over 100,000 tokens) | $0.50 / MTok | $0.625 / MTok | $1 / MTok | $0.05 / MTok | $2.50 / MTok |

## Batch processing

| Model | Batch input | Batch output |
| :--- | :--- | :--- |
| Claude Opus 5.5 | $2 / MTok | $10 / MTok |
"""


def model(mid, name, effort=True, fallback=True, structured=True):
    return SimpleNamespace(id=mid, display_name=name, created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                           deprecated_at=None, allowed_fallback_models=["claude-opus-5"] if fallback else [],
                           capabilities=Caps({"structured_outputs": {"supported": structured},
                                              "effort": {"low": {"supported": effort}}}))


class Caps:
    """Like the SDK's BetaModelCapabilities: an object, not a dict."""

    def __init__(self, data):
        self.data = data

    def model_dump(self):
        return self.data


class Catalog(FakeClaude):
    def __init__(self, answer):
        super().__init__(answer)
        self.beta.models = SimpleNamespace(list=lambda **kw: [
            model("claude-opus-5-5", "Claude Opus 5.5"),
            model("claude-haiku-5-5", "Claude Haiku 5.5", fallback=False),
            model("claude-old", "Claude Old", effort=False, fallback=False),
            model("claude-chat-only", "Claude Chat", structured=False)])


def test_prices_are_read_from_the_first_table():
    prices = costs.parse_prices(PRICING)
    assert prices["Claude Opus 5.5"] == {"input": 4, "cache_write": 5, "cache_read": 0.2, "output": 20}
    assert prices["Claude Haiku 5.5"]["input"] == 0.10  # the short-prompt row
    assert len(prices) == 2


def test_catalog_choice_and_booking(data, monkeypatch):
    monkeypatch.setattr(costs, "_fetch_pricing", lambda: PRICING)
    claude = Catalog(lambda prompt: {"phases": []})
    cat = costs.refresh(claude, log=lambda *a: None)
    assert [m["id"] for m in cat["models"]] == ["claude-opus-5-5", "claude-haiku-5-5", "claude-old"]
    assert costs.model_info("claude-old")["price"] is None

    ai.ask(claude, "x", {}, "Phasen benennen")  # default model: effort and fallback
    first = claude.requests[-1]
    assert first["output_config"]["effort"] == "low" and first["fallbacks"] == "default"
    db.set_state("ai_model", "claude-haiku-5-5")
    ai.ask(claude, "x", {}, "Songs einordnen")
    assert "fallbacks" not in claude.requests[-1] and claude.requests[-1]["model"] == "claude-haiku-5-5"
    db.set_state("ai_model", "claude-old")
    ai.ask(claude, "x", {}, "Songs einordnen")
    assert "effort" not in claude.requests[-1]["output_config"]

    rows = db.query("SELECT model, purpose, cost FROM ai_usage ORDER BY id")
    assert rows[0]["cost"] == pytest.approx((1000 * 4 + 200 * 20) / 1e6)
    assert rows[1]["cost"] == pytest.approx((1000 * 0.1 + 200 * 0.5) / 1e6)
    assert rows[2]["cost"] is None  # no price yet ...

    monkeypatch.setattr(costs, "_fetch_pricing", lambda: PRICING + "")
    monkeypatch.setattr(costs, "parse_prices", lambda md: {"Claude Old": {"input": 1, "cache_write": 1,
                                                                          "cache_read": 0.1, "output": 5}})
    costs.refresh(claude, log=lambda *a: None)
    assert db.one("SELECT cost FROM ai_usage WHERE model = 'claude-old'")["cost"] == pytest.approx(0.002)  # ... now
    assert costs.model_info("claude-opus-5-5")["price"]["input"] == 4  # kept when the page lacks it

    s = costs.summary()
    month = s["months"][0]
    assert month["calls"] == 3 and month["unpriced"] == 0
    assert {p["key"] for p in month["purposes"]} == {"Phasen benennen", "Songs einordnen"}
    assert month["models"][0]["name"] == "Claude Opus 5.5" and s["recent"][0]["model"] == "claude-old"


def test_fallback_attempts_are_booked_each(data):
    it = lambda m, i, o: SimpleNamespace(model=m, type="message", input_tokens=i, output_tokens=o,
                                         cache_creation_input_tokens=0, cache_read_input_tokens=0)
    usage = SimpleNamespace(input_tokens=50, output_tokens=60, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                            iterations=[it("claude-opus-5-5", 40, 0), it("claude-opus-5", 50, 60)])
    costs.book_response(SimpleNamespace(usage=usage, model="claude-opus-5"), "Songs einordnen")
    assert [r["model"] for r in db.query("SELECT model FROM ai_usage ORDER BY id")] == ["claude-opus-5-5", "claude-opus-5"]


def test_unreachable_price_page_keeps_the_models(data, monkeypatch):
    def down():
        raise OSError("offline")
    monkeypatch.setattr(costs, "_fetch_pricing", down)
    cat = costs.refresh(Catalog(lambda p: {}), log=lambda *a: None)
    assert len(cat["models"]) == 3 and "nicht erreichbar" in cat["prices_error"]
