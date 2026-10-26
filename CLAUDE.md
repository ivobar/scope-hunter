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
- Notifiers via env vars: Telegram, ntfy, SMTP email (see README.md).
- CLI: `--dry-run`, `--report`, `--debug <shop name substring>`, `--test-notify`.
- `.github/workflows/price-check.yml`: twice-daily run, commits `prices.db` back.
- Python 3.11+ (tomllib). Deps: requests, beautifulsoup4.

## Status
- Parsing, extraction, alert and failure logic were tested only against
  simulated HTML; the code has NOT yet been run against the real shops.
- 10 shops configured: Teleskopy.pl, Delta Optical (PLN); Astrofy, Skypoint,
  Astroshop.eu, Teleskop-Express, Pierro Astro (EUR); First Light Optics,
  Rother Valley Optics, Harrison Telescopes (GBP).

## Next steps
1. Run `python price_tracker.py --dry-run` and fix any shop that fails or shows a
   wrong price (use `--debug`, add `selector`; consider a Playwright fallback for
   shops that need JS or block plain requests).
2. Verify the Rother Valley URL ("ota-kit") is the plain OTA, not the imaging kit.
3. Choose notification channel and scheduling (home cron vs GitHub Actions; Actions
   runners use US IPs, so shops may show other currencies or block them).
   If using home cron, add `prices.db` to `.gitignore`.
4. Set `target_eur` in `shops.toml` once the owner picks a target price.

## Notes
- UK prices include UK VAT; importing to the EU adds customs and local VAT, so
  UK listings only pay off with a large discount.
- Be polite: random 2–5 s delay between shops, at most a couple of runs per day.
