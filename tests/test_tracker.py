import sqlite3
from pathlib import Path

import pytest

import price_tracker as pt
from price_tracker import Result

FIXTURES = Path(__file__).parent / "fixtures"


def page(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- parsing

@pytest.mark.parametrize("raw, expected", [
    ("2 830,00 zł", 2830.0),
    ("€2.830,00", 2830.0),
    ("£2,399.00", 2399.0),
    ("2830.00", 2830.0),
    ("2830,5", 2830.5),
    ("2,830", 2830.0),
    ("2.830", 2830.0),
    ("1.234.567", 1234567.0),
    (2830, 2830.0),
    (0, None),
    ("", None),
    ("call us", None),
    (None, None),
])
def test_parse_price(raw, expected):
    assert pt.parse_price(raw) == expected


@pytest.mark.parametrize("text, code", [
    ("11 999 zł", "PLN"), ("2.830,00 €", "EUR"), ("£2,399", "GBP"), ("2830", None),
])
def test_currency_from_text(text, code):
    assert pt.currency_from_text(text) == code


@pytest.mark.parametrize("value, expected", [
    ("https://schema.org/InStock", True),
    ("in stock", True),
    ("LimitedAvailability", True),
    ("https://schema.org/OutOfStock", False),
    ("", None),
    (None, None),
])
def test_stock_from_value(value, expected):
    assert pt.stock_from_value(value) is expected


# --------------------------------------------------------------------------- extraction

def test_extract_jsonld_offer():
    r = pt.extract(page("jsonld_offer.html"))
    assert (r.price, r.currency, r.in_stock, r.method) == (2830.0, "EUR", True, "json-ld")


def test_extract_jsonld_graph_with_price_specification():
    r = pt.extract(page("jsonld_graph_pricespec.html"))
    assert (r.price, r.currency, r.in_stock, r.method) == (11999.0, "PLN", False, "json-ld")


def test_extract_meta_tags():
    r = pt.extract(page("meta_only.html"))
    assert (r.price, r.currency, r.in_stock, r.method) == (2399.0, "GBP", True, "meta")


def test_extract_selector():
    r = pt.extract(page("selector_only.html"), "#our-price")
    assert (r.price, r.currency, r.method) == (2449.0, "GBP", "selector")


def test_selector_borrows_stock_from_structured_data():
    r = pt.extract(page("jsonld_offer.html"), "span.price")
    assert (r.price, r.currency, r.in_stock, r.method) == (2830.0, "EUR", True, "selector")


def test_bad_selector_falls_back_to_auto_detection():
    r = pt.extract(page("jsonld_offer.html"), "#does-not-exist")
    assert r.method == "json-ld" and r.price == 2830.0


def test_no_price_found():
    r = pt.extract(page("no_price.html"))
    assert r.price is None and r.error == "no price found on page"


def test_to_eur():
    rates = {"EUR": 1.0, "PLN": 4.3, "GBP": 0.85}
    assert pt.to_eur(4300, "PLN", rates) == 1000.0
    assert pt.to_eur(850, "gbp", rates) == 1000.0
    assert pt.to_eur(100, "CHF", rates) is None
    assert pt.to_eur(None, "EUR", rates) is None


# --------------------------------------------------------------------------- alerts

URL = "https://shop.example/rasa8"
SETTINGS = {"min_drop_pct": 0.5}


def prev(price=2830.0, currency="EUR", price_eur=2830.0, in_stock=1):
    return {"price": price, "currency": currency, "price_eur": price_eur, "in_stock": in_stock}


def test_price_drop_alert_and_new_low():
    msgs = pt.evaluate("Shop", URL, Result(2700.0, "EUR", True, price_eur=2700.0),
                       prev(), 2800.0, SETTINGS)
    assert len(msgs) == 1
    assert "📉" in msgs[0] and "-4.6%" in msgs[0] and "new all-time low" in msgs[0]


def test_price_drop_not_a_new_low():
    msgs = pt.evaluate("Shop", URL, Result(2700.0, "EUR", price_eur=2700.0),
                       prev(), 2500.0, SETTINGS)
    assert len(msgs) == 1 and "all-time low" not in msgs[0]


def test_small_drop_ignored():
    assert pt.evaluate("Shop", URL, Result(2825.0, "EUR", price_eur=2825.0),
                       prev(), 2800.0, SETTINGS) == []


def test_currency_change_is_not_a_drop():
    assert pt.evaluate("Shop", URL, Result(2400.0, "GBP", price_eur=2820.0),
                       prev(), 2830.0, SETTINGS) == []


def test_target_alert_only_when_crossing():
    settings = {**SETTINGS, "target_eur": 2500}
    res = Result(2490.0, "EUR", price_eur=2490.0)
    crossed = pt.evaluate("Shop", URL, res, prev(), 2830.0, settings)
    assert any("🎯" in m for m in crossed)
    already_below = pt.evaluate("Shop", URL, res, prev(2495.0, price_eur=2495.0), 2495.0, settings)
    assert not any("🎯" in m for m in already_below)


def test_back_in_stock():
    msgs = pt.evaluate("Shop", URL, Result(2830.0, "EUR", True, price_eur=2830.0),
                       prev(in_stock=0), 2830.0, SETTINGS)
    assert len(msgs) == 1 and "📦" in msgs[0]


def test_failed_fetch_gives_no_alerts():
    assert pt.evaluate("Shop", URL, Result(error="timeout"), prev(), 2830.0, SETTINGS) == []


# --------------------------------------------------------------------------- storage

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(pt.SCHEMA)
    yield c
    c.close()


def test_storage_helpers(conn):
    pt.save(conn, "Shop", URL, Result(2900.0, "EUR", True, "json-ld", price_eur=2900.0))
    pt.save(conn, "Shop", URL, Result(2800.0, "EUR", False, "json-ld", price_eur=2800.0))
    assert pt.lowest(conn, "Shop") == 2800.0
    last = pt.last_ok(conn, "Shop")
    assert (last["price"], last["in_stock"]) == (2800.0, 0)
    assert pt.trailing_errors(conn, "Shop") == 0

    for _ in range(3):
        pt.save(conn, "Shop", URL, Result(error="HTTP 403"))
    assert pt.trailing_errors(conn, "Shop") == 3
    assert pt.last_ok(conn, "Shop")["price"] == 2800.0  # failures don't hide the last price
    assert pt.lowest(conn, "Other") is None


# --------------------------------------------------------------------------- notifications

def test_chunks_keeps_short_text_whole():
    assert pt._chunks("a\n\nb") == ["a\n\nb"]


def test_chunks_splits_on_blank_lines_under_limit():
    alerts = [f"alert {i} " + "x" * 80 for i in range(50)]
    parts = pt._chunks("\n\n".join(alerts), limit=500)
    assert all(len(p) <= 500 for p in parts)
    assert "\n\n".join(parts) == "\n\n".join(alerts)  # nothing lost or reordered


def test_chunks_hard_splits_oversized_block():
    parts = pt._chunks("y" * 1200, limit=500)
    assert [len(p) for p in parts] == [500, 500, 200]


def test_summary_table():
    rows = [
        ("Expensive", Result(2600.0, "GBP", True, price_eur=3050.0), 2500.0),
        ("Cheap", Result(2700.0, "EUR", None, price_eur=2700.0), 2650.0),
        ("Broken", Result(error="HTTP 403 Forbidden"), None),
    ]
    text = pt.summary_table(rows)
    lines = text.splitlines()
    assert lines[0] == "📋 Price summary" and lines[1] == "```" and lines[-1] == "```"
    body = lines[3:-1]
    assert body[0].startswith("Cheap") and "€2,700" in body[0] and "2,650.00 EUR" in body[0]
    assert body[1].startswith("Expensive") and body[1].endswith("yes")
    assert body[2].startswith("Broken") and "FAILED: HTTP 403 Forbidden" in body[2]


def test_summary_table_lowest_includes_current_price():
    text = pt.summary_table([("Shop", Result(2500.0, "EUR", True, price_eur=2500.0), 2600.0)])
    assert "2,500.00 EUR" in text.splitlines()[3].split("€2,500")[1]


def test_discord_summary():
    rows = [
        ("Teleskop-Express", Result(2449.0, "EUR", None, price_eur=2449.0), 2449.0),
        ("Astrofy", Result(2050.0, "EUR", True, price_eur=2050.0), 2099.0),     # new low
        ("Harrison", Result(2599.0, "GBP", False, price_eur=3101.0), 2599.0),
        ("Skypoint", Result(error="403 Client Error: Forbidden for url: https://x"), 2099.0),
        ("Odd", Result(error="no price found on page"), None),
    ]
    lines = pt.discord_summary(rows).splitlines()
    assert lines[0] == "📋 Prices" and lines[1] == "```" and lines[-1] == "```"
    body = lines[2:-1]
    w = len("Teleskop-Express")  # names are padded to the longest one
    assert body[0] == f"{'Astrofy':<{w}} €2,050 🤑 ✅"
    assert body[1] == f"{'Teleskop-Express':<{w}} €2,449    ❔"
    assert body[2] == f"{'Harrison':<{w}} €3,101    ❌"
    assert body[3] == f"{'Skypoint':<{w}} ⚠️ HTTP 403"
    assert body[4] == f"{'Odd':<{w}} ⚠️ failed"
    assert "GBP" not in "\n".join(lines) and "Lowest" not in "\n".join(lines)


def test_discord_summary_no_emoji_without_history_or_on_equal_price():
    rows = [("First run", Result(2000.0, "EUR", True, price_eur=2000.0), None),
            ("Same low", Result(2099.0, "EUR", True, price_eur=2099.0), 2099.0)]
    assert "🤑" not in pt.discord_summary(rows)


def test_new_low_judged_in_shop_currency_not_eur():
    # same PLN price, EUR value moved with the exchange rate: not a new low
    rows = [("PLN shop", Result(9999.0, "PLN", True, price_eur=2200.0), 9999.0)]
    assert "🤑" not in pt.discord_summary(rows)


class FakeResponse:
    def raise_for_status(self):
        pass


def test_notify_posts_to_discord(monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "NTFY_TOPIC", "SMTP_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    calls = []
    monkeypatch.setattr(pt.requests, "post",
                        lambda url, **kw: calls.append((url, kw)) or FakeResponse())

    pt.notify("📉 Shop: 2,830.00 EUR → 2,700.00 EUR")

    assert len(calls) == 1
    url, kw = calls[0]
    assert url == "https://discord.example/webhook"
    assert kw["json"]["content"].startswith("**RASA 8 price alert**")
    assert "2,700.00 EUR" in kw["json"]["content"]
    assert kw["json"]["flags"] == 4


def test_notify_sends_discord_its_own_text(monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "SMTP_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setenv("NTFY_TOPIC", "rasa8-test")
    calls = []
    monkeypatch.setattr(pt.requests, "post",
                        lambda url, **kw: calls.append((url, kw)) or FakeResponse())

    pt.notify("full table", discord_text="narrow table")

    sent = {url: kw for url, kw in calls}
    assert "narrow table" in sent["https://discord.example/webhook"]["json"]["content"]
    assert sent["https://ntfy.sh/rasa8-test"]["data"] == b"full table"


def test_notify_dry_run_sends_nothing(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(pt.requests, "post",
                        lambda *a, **kw: pytest.fail("dry run must not send"))
    pt.notify("hello", dry_run=True)
