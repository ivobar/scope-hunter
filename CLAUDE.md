# scope-hunter

Price tracker for the **Celestron RASA 8 OTA** (Celestron #91073, OTA only, not the
imaging kit or mount bundles). Goal: buy it at a discount by getting alerts on price
drops across European and UK shops. Started in a claude.ai chat; this file carries
that context over.

## How it works
- `price_tracker.py`: fetches each URL in `shops.toml`, extracts the price
  (JSON-LD Product/Offer → meta/microdata → optional per-shop CSS `selector`),
  converts to EUR with ECB daily rates, stores history in `prices.db` (SQLite).
- Alerts (one combined message per run): price drop ≥ `min_drop_pct` (flags new
  all-time lows), price ≤ `target_eur`, back in stock, 3 consecutive failures.
- Notifiers via env vars: Discord webhook, Telegram, ntfy, SMTP email (see README.md).
- CLI: `--dry-run`, `--report`, `--debug <shop name substring>`, `--test-notify`,
  `--summary` (price table for all shops).
- `.github/workflows/price-check.yml`: twice-daily run, commits `prices.db` back.
- Python 3.11+ (tomllib). Deps: requests, beautifulsoup4.

## Status
- Unit tests: `pytest` (tests/, saved sample pages); also run in CI (tests.yml).
- First live run (2026-10-26, home IP): 9 of 10 shops extract correctly via
  JSON-LD/meta, no selectors needed. Astroshop.eu is disabled in shops.toml
  (Cloudflare bot challenge, HTTP 403). Rother Valley URL confirmed as plain OTA.
- Notifications: Discord webhook (`DISCORD_WEBHOOK_URL`) chosen; GitHub Actions
  runs at 06:00 UTC with `--summary` (price table) and 18:00 UTC alerts only.

## Next steps
1. Owner adds the `DISCORD_WEBHOOK_URL` repo secret, then runs the workflow by hand
   (dry_run=true) to check what the US-based Actions runners get from each shop.
2. Set `target_eur` in `shops.toml` once the owner picks a target price.

## Notes
- UK prices include UK VAT; importing to the EU adds customs and local VAT, so
  UK listings only pay off with a large discount.
- Be polite: random 2–5 s delay between shops, at most a couple of runs per day.
