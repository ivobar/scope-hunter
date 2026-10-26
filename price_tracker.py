#!/usr/bin/env python3
"""
RASA 8 price tracker.

Checks product pages listed in shops.toml, extracts the price (JSON-LD product
data first, then meta/microdata tags, or a per-shop CSS selector override),
stores the history in SQLite, converts to EUR with ECB reference rates, and
sends one combined notification when something interesting happens:

  * a price drop (optionally flagged as a new all-time low)
  * a price at or below your EUR target
  * a shop coming back in stock
  * a shop failing 3 runs in a row (page layout probably changed)

Usage:
  python price_tracker.py                 # normal run (cron / GitHub Actions)
  python price_tracker.py --dry-run       # fetch and print only, no save, no notify
  python price_tracker.py --report        # table of latest + lowest prices
  python price_tracker.py --debug skyp    # inspect one shop (name substring)
  python price_tracker.py --test-notify   # check your notification setup
  python price_tracker.py --summary       # normal run + price table for all shops

Notification channels are enabled by environment variables (see README.md).
Requires Python 3.11+ (uses tomllib).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import smtplib
import sqlite3
import sys
import time
import tomllib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("rasa8")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"

CURRENCY_HINTS = [  # order matters: longer/more specific first
    ("PLN", "PLN"), ("zł", "PLN"), ("EUR", "EUR"), ("€", "EUR"),
    ("GBP", "GBP"), ("£", "GBP"), ("USD", "USD"), ("$", "USD"),
]
PRICE_TEXT_RE = re.compile(
    r"(€|£|zł|PLN|EUR|GBP)\s*\d[\d\s.,]*|\d[\d\s.,]*\s*(€|£|zł|PLN|EUR|GBP)"
)
IN_STOCK_MARKERS = ("instock", "limitedavailability", "onlineonly", "instoreonly")


@dataclass
class Result:
    price: float | None = None
    currency: str | None = None
    in_stock: bool | None = None
    method: str | None = None
    error: str | None = None
    price_eur: float | None = None


# --------------------------------------------------------------------------- parsing

def parse_price(value) -> float | None:
    """Turn '2 830,00 zł', '€2.830,00', '£2,399.00', '2830.00' or 2830 into a float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = re.sub(r"[^\d.,]", "", str(value)).strip(".,")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):          # 2.830,00  (EU)
            s = s.replace(".", "").replace(",", ".")
        else:                                    # 2,830.00  (UK/US)
            s = s.replace(",", "")
    elif "," in s:
        if re.search(r",\d{1,2}$", s):           # 2830,00
            s = s.replace(",", ".")
        else:                                    # 2,830
            s = s.replace(",", "")
    elif s.count(".") > 1 or re.search(r"\.\d{3}$", s):  # 2.830 / 1.234.567
        s = s.replace(".", "")
    try:
        p = float(s)
    except ValueError:
        return None
    return p if p > 0 else None


def currency_from_text(text: str | None) -> str | None:
    if not text:
        return None
    for hint, code in CURRENCY_HINTS:
        if hint in text:
            return code
    return None


def stock_from_value(value) -> bool | None:
    if value is None:
        return None
    v = re.sub(r"[\s_\-]", "", str(value).lower())
    if not v:
        return None
    return any(m in v for m in IN_STOCK_MARKERS)


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _as_list(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def from_jsonld(soup: BeautifulSoup) -> Result | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text(), strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(data):
            types = _as_list(node.get("@type"))
            if "Product" not in types:
                continue
            for offer in _as_list(node.get("offers")):
                if not isinstance(offer, dict):
                    continue
                price = offer.get("price") or offer.get("lowPrice")
                currency = offer.get("priceCurrency")
                if price is None:
                    for spec in _as_list(offer.get("priceSpecification")):
                        if isinstance(spec, dict) and spec.get("price") is not None:
                            price = spec["price"]
                            currency = currency or spec.get("priceCurrency")
                            break
                p = parse_price(price)
                if p:
                    return Result(p, currency, stock_from_value(offer.get("availability")), "json-ld")
    return None


def from_meta(soup: BeautifulSoup) -> Result | None:
    for attrs in ({"property": "product:price:amount"},
                  {"property": "og:price:amount"},
                  {"itemprop": "price"}):
        el = soup.find(attrs=attrs)
        if not el:
            continue
        raw = el.get("content") or el.get_text(" ", strip=True)
        p = parse_price(raw)
        if not p:
            continue
        cur_el = (soup.find(attrs={"property": "product:price:currency"})
                  or soup.find(attrs={"property": "og:price:currency"})
                  or soup.find(attrs={"itemprop": "priceCurrency"}))
        currency = (cur_el.get("content") if cur_el else None) or currency_from_text(raw)
        stock_el = (soup.find(attrs={"itemprop": "availability"})
                    or soup.find(attrs={"property": "product:availability"})
                    or soup.find(attrs={"property": "og:availability"}))
        stock = None
        if stock_el:
            stock = stock_from_value(stock_el.get("content") or stock_el.get("href")
                                     or stock_el.get_text(strip=True))
        return Result(p, currency, stock, "meta")
    return None


def from_selector(soup: BeautifulSoup, selector: str) -> Result | None:
    el = soup.select_one(selector)
    if not el:
        return None
    raw = el.get("content") or el.get_text(" ", strip=True)
    p = parse_price(raw)
    return Result(p, currency_from_text(raw), None, "selector") if p else None


def extract(html: str, selector: str | None = None) -> Result:
    soup = BeautifulSoup(html, "html.parser")
    auto = from_jsonld(soup) or from_meta(soup)
    if selector:
        manual = from_selector(soup, selector)
        if manual:
            if auto:  # borrow currency/stock info from structured data
                manual.currency = manual.currency or auto.currency
                manual.in_stock = auto.in_stock
            return manual
        log.warning("  selector %r matched nothing usable, falling back to auto-detection", selector)
    return auto or Result(error="no price found on page")


# --------------------------------------------------------------------------- network

def fetch(session: requests.Session, url: str) -> str:
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            last_exc = e
            if attempt == 0:
                time.sleep(5)
    raise last_exc  # type: ignore[misc]


def ecb_rates(session: requests.Session) -> dict[str, float]:
    """EUR-based reference rates: 1 EUR = rate[currency]."""
    rates = {"EUR": 1.0}
    try:
        r = session.get(ECB_URL, timeout=20)
        r.raise_for_status()
        for el in ET.fromstring(r.content).iter():
            if el.get("currency") and el.get("rate"):
                rates[el.get("currency")] = float(el.get("rate"))
    except Exception as e:  # noqa: BLE001
        log.warning("Could not fetch ECB rates (%s); only EUR prices get a EUR value", e)
    return rates


def to_eur(price: float | None, currency: str | None, rates: dict[str, float]) -> float | None:
    if price is None or not currency:
        return None
    rate = rates.get(currency.upper())
    return round(price / rate, 2) if rate else None


# --------------------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    shop      TEXT NOT NULL,
    url       TEXT,
    price     REAL,
    currency  TEXT,
    price_eur REAL,
    in_stock  INTEGER,
    method    TEXT,
    error     TEXT
)"""


def last_ok(conn, shop):
    return conn.execute(
        "SELECT price, currency, price_eur, in_stock FROM prices "
        "WHERE shop = ? AND price IS NOT NULL ORDER BY id DESC LIMIT 1", (shop,)
    ).fetchone()


def lowest(conn, shop):
    row = conn.execute(
        "SELECT MIN(price) FROM prices WHERE shop = ? AND price IS NOT NULL", (shop,)
    ).fetchone()
    return row[0] if row else None


def trailing_errors(conn, shop) -> int:
    n = 0
    for (err,) in conn.execute(
        "SELECT error FROM prices WHERE shop = ? ORDER BY id DESC LIMIT 10", (shop,)
    ):
        if err is None:
            break
        n += 1
    return n


def save(conn, shop, url, res: Result):
    conn.execute(
        "INSERT INTO prices (ts, shop, url, price, currency, price_eur, in_stock, method, error) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), shop, url,
         res.price, res.currency, res.price_eur,
         None if res.in_stock is None else int(res.in_stock), res.method, res.error),
    )
    conn.commit()


# --------------------------------------------------------------------------- alerts

def fmt(price, currency) -> str:
    return f"{price:,.2f} {currency or ''}".strip() if price is not None else "—"


def evaluate(name, url, res: Result, prev, prev_low, settings) -> list[str]:
    if res.price is None:
        return []
    msgs = []
    min_pct = float(settings.get("min_drop_pct", 0.5))

    if prev and prev["price"] and prev["currency"] == res.currency:
        old = prev["price"]
        if res.price < old * (1 - min_pct / 100):
            pct = (old - res.price) / old * 100
            low = " (new all-time low)" if prev_low is None or res.price < prev_low else ""
            msgs.append(f"📉 {name}: {fmt(old, res.currency)} → {fmt(res.price, res.currency)} "
                        f"(-{pct:.1f}%){low}\n{url}")

    target = settings.get("target_eur")
    if target and res.price_eur is not None and res.price_eur <= target:
        prev_eur = prev["price_eur"] if prev else None
        if prev_eur is None or prev_eur > target:
            msgs.append(f"🎯 {name}: ≈€{res.price_eur:,.0f} is at or below your target of "
                        f"€{target:,.0f}\n{url}")

    if prev and prev["in_stock"] == 0 and res.in_stock:
        msgs.append(f"📦 {name} is back in stock at {fmt(res.price, res.currency)}\n{url}")
    return msgs


def summary_table(rows: list[tuple[str, Result, float | None]]) -> str:
    """Price table for (shop name, result, lowest seen) rows, cheapest in EUR first."""
    rows = sorted(rows, key=lambda r: (r[1].price_eur is None, r[1].price_eur or 0))
    lines = [f"{'Shop':<22} {'Latest':>14} {'≈ EUR':>7} {'Lowest':>14}  Stock"]
    for name, res, low in rows:
        if res.price is None:
            lines.append(f"{name:<22} FAILED: {(res.error or '?')[:60]}")
            continue
        stock = {True: "yes", False: "no", None: "?"}[res.in_stock]
        eur = f"€{res.price_eur:,.0f}" if res.price_eur is not None else "—"
        lines.append(f"{name:<22} {fmt(res.price, res.currency):>14} {eur:>7} "
                     f"{fmt(low, res.currency):>14}  {stock}")
    return "📋 Daily summary\n```\n" + "\n".join(lines) + "\n```"


# --------------------------------------------------------------------------- notifications

def _chunks(text: str, limit: int = 1900) -> list[str]:
    """Split text into pieces under Discord's 2000-char limit, preferably on blank lines."""
    out, cur = [], ""
    for part in text.split("\n\n"):
        while len(part) > limit:  # a single oversized block: hard split
            if cur:
                out.append(cur)
                cur = ""
            out.append(part[:limit])
            part = part[limit:]
        if cur and len(cur) + 2 + len(part) > limit:
            out.append(cur)
            cur = part
        else:
            cur = f"{cur}\n\n{part}" if cur else part
    if cur:
        out.append(cur)
    return out


def notify(text: str, dry_run: bool = False) -> None:
    print("\n" + "=" * 60 + "\n" + text + "\n" + "=" * 60)
    if dry_run:
        return
    title = "RASA 8 price alert"

    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": f"{title}\n\n{text}",
                                "disable_web_page_preview": True},
                          timeout=20).raise_for_status()
        except requests.RequestException as e:
            log.error("Telegram failed: %s", e)

    webhook = os.getenv("DISCORD_WEBHOOK_URL")
    if webhook:
        try:
            for part in _chunks(f"**{title}**\n\n{text}"):
                requests.post(webhook, json={"content": part, "username": "RASA 8 tracker",
                                             "flags": 4},  # 4 = no link previews
                              timeout=20).raise_for_status()
        except requests.RequestException as e:
            log.error("Discord failed: %s", e)

    topic = os.getenv("NTFY_TOPIC")
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        try:
            requests.post(f"{server}/{topic}", data=text.encode("utf-8"),
                          headers={"Title": title, "Tags": "telescope"},
                          timeout=20).raise_for_status()
        except requests.RequestException as e:
            log.error("ntfy failed: %s", e)

    host, to = os.getenv("SMTP_HOST"), os.getenv("EMAIL_TO")
    if host and to:
        try:
            msg = EmailMessage()
            msg["Subject"] = title
            msg["From"] = os.getenv("EMAIL_FROM") or os.getenv("SMTP_USER") or to
            msg["To"] = to
            msg.set_content(text)
            with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=30) as s:
                s.starttls()
                if os.getenv("SMTP_USER"):
                    s.login(os.environ["SMTP_USER"], os.getenv("SMTP_PASSWORD", ""))
                s.send_message(msg)
        except Exception as e:  # noqa: BLE001
            log.error("Email failed: %s", e)


# --------------------------------------------------------------------------- commands

def run(conn, session, settings, shops, dry_run, summary=False):
    rates = ecb_rates(session)
    alerts: list[str] = []
    rows: list[tuple[str, Result, float | None]] = []
    for i, shop in enumerate(shops):
        if i:
            time.sleep(random.uniform(2, 5))  # be polite
        name, url = shop["name"], shop["url"]
        try:
            res = extract(fetch(session, url), shop.get("selector"))
        except Exception as e:  # noqa: BLE001
            res = Result(error=str(e)[:200])
        if res.price and not res.currency:
            res.currency = shop.get("currency")
        res.price_eur = to_eur(res.price, res.currency, rates)

        if res.price:
            stock = {True: "in stock", False: "out of stock", None: "stock ?"}[res.in_stock]
            eur = f"≈€{res.price_eur:,.0f}" if res.price_eur is not None else ""
            log.info("%-22s %16s %9s  %-12s [%s]", name, fmt(res.price, res.currency),
                     eur, stock, res.method)
        else:
            log.info("%-22s FAILED: %s", name, res.error)

        prev, prev_low = last_ok(conn, name), lowest(conn, name)
        alerts += evaluate(name, url, res, prev, prev_low, settings)
        lows = [p for p in (prev_low, res.price) if p is not None]
        rows.append((name, res, min(lows) if lows else None))

        if not dry_run:
            save(conn, name, url, res)
            if trailing_errors(conn, name) == 3:
                alerts.append(f"⚠️ {name}: price lookup failed 3 runs in a row ({res.error}). "
                              f"The page may have changed; try --debug.\n{url}")
    if not alerts:
        log.info("\nNo alerts this run.")
    if summary:
        alerts.append(summary_table(rows))
    if alerts:
        notify("\n\n".join(alerts), dry_run)


def report(conn):
    rows = conn.execute("""
        SELECT p.shop, p.price, p.currency, p.price_eur, p.in_stock, p.ts,
               (SELECT MIN(q.price) FROM prices q
                 WHERE q.shop = p.shop AND q.price IS NOT NULL) AS low
          FROM prices p
         WHERE p.id IN (SELECT MAX(id) FROM prices WHERE price IS NOT NULL GROUP BY shop)
         ORDER BY p.price_eur IS NULL, p.price_eur""").fetchall()
    if not rows:
        print("No data yet. Run the tracker first.")
        return
    print(f"{'Shop':<22} {'Latest':>16} {'≈ EUR':>9} {'Lowest seen':>16}  {'Stock':<12} Checked")
    for r in rows:
        stock = {1: "in stock", 0: "out of stock"}.get(r["in_stock"], "?")
        eur = f"€{r['price_eur']:,.0f}" if r["price_eur"] is not None else "—"
        print(f"{r['shop']:<22} {fmt(r['price'], r['currency']):>16} {eur:>9} "
              f"{fmt(r['low'], r['currency']):>16}  {stock:<12} {r['ts'][:16]}")


def debug(session, shops, needle):
    matches = [s for s in shops if needle.lower() in s["name"].lower()]
    if not matches:
        sys.exit(f"No shop name contains {needle!r}")
    for shop in matches:
        print(f"\n### {shop['name']}\n{shop['url']}")
        try:
            html = fetch(session, shop["url"])
        except Exception as e:  # noqa: BLE001
            print(f"  fetch failed: {e}")
            continue
        soup = BeautifulSoup(html, "html.parser")
        print(f"  page size: {len(html):,} bytes, title: {soup.title.string.strip() if soup.title and soup.title.string else '?'}")
        print(f"  json-ld : {from_jsonld(soup)}")
        print(f"  meta    : {from_meta(soup)}")
        if shop.get("selector"):
            print(f"  selector: {from_selector(soup, shop['selector'])}")
        print("  price-looking text on the page (candidate selectors):")
        seen = 0
        for s in soup.find_all(string=PRICE_TEXT_RE):
            parent = s.parent
            if parent.name in ("script", "style", "noscript"):
                continue
            classes = "".join(f".{c}" for c in parent.get("class", []))
            ident = f"#{parent['id']}" if parent.get("id") else ""
            print(f"    {parent.name}{ident}{classes:<40}  {s.strip()[:60]!r}")
            seen += 1
            if seen >= 15:
                break


def main():
    ap = argparse.ArgumentParser(description="Track RASA 8 prices across shops.")
    ap.add_argument("-c", "--config", default="shops.toml")
    ap.add_argument("--db", default="prices.db")
    ap.add_argument("--dry-run", action="store_true", help="fetch and print only; no save, no notify")
    ap.add_argument("--report", action="store_true", help="show latest and lowest prices")
    ap.add_argument("--debug", metavar="SHOP", help="inspect shops whose name contains SHOP")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification")
    ap.add_argument("--summary", action="store_true",
                    help="also send a price table for all shops, even without alerts")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    with open(args.config, "rb") as f:
        cfg = tomllib.load(f)
    settings, shops = cfg.get("settings", {}), cfg.get("shop", [])

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute(SCHEMA)

    session = requests.Session()
    session.headers.update(HEADERS)

    if args.report:
        report(conn)
    elif args.test_notify:
        notify("✅ Test notification from the RASA 8 price tracker.")
    elif args.debug:
        debug(session, shops, args.debug)
    else:
        run(conn, session, settings, shops, args.dry_run, args.summary)


if __name__ == "__main__":
    main()
