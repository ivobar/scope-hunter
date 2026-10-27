# RASA 8 price tracker

Checks the shops in `shops.toml`, stores prices in `prices.db` (SQLite) and
notifies you about drops, target hits, restocks and broken scrapers.

## Setup
    python -m venv .venv && source .venv/bin/activate   # Python 3.11+
    pip install -r requirements.txt
    python price_tracker.py --dry-run      # first look, nothing saved
    python price_tracker.py                # real run (creates prices.db)
    python price_tracker.py --report       # latest vs lowest, sorted by EUR

If a shop shows FAILED or a wrong price:
    python price_tracker.py --debug skypoint
lists candidate price elements; add `selector = "..."` to that shop in shops.toml.

## Notifications (environment variables; enable any combination)
| Channel  | Variables |
|----------|-----------|
| Discord  | `DISCORD_WEBHOOK_URL` (Server Settings → Integrations → Webhooks → New Webhook → Copy URL) |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| ntfy     | `NTFY_TOPIC` (optional `NTFY_SERVER`, default https://ntfy.sh) |
| Email    | `SMTP_HOST`, `SMTP_PORT` (587), `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`, optional `EMAIL_FROM` |

Test with `python price_tracker.py --test-notify`. Add `--summary` to any run to
also send a price table for every shop (both GitHub Actions runs do this; Discord
gets a narrower version: EUR price, 🤑 on a new all-time low, stock icon).

## Testing
    pip install -r requirements-dev.txt
    pytest                                         # unit tests on saved sample pages
    python price_tracker.py --dry-run --summary    # live check against the real shops

The unit tests also run on every push (`.github/workflows/tests.yml`).

## Scheduling
**Home machine (cron)**, twice a day:

    0 8,20 * * * cd /path/to/tracker && .venv/bin/python price_tracker.py >> tracker.log 2>&1

Put the notification variables in the crontab or a wrapper script.

**GitHub Actions**: push this folder to a repo, add the variables as repository
secrets (Settings → Secrets and variables → Actions, or
`gh secret set DISCORD_WEBHOOK_URL`), and `.github/workflows/price-check.yml` runs
at 05:23 and 17:23 UTC (alerts + price summary each time) and commits
`prices.db` back. Use Actions → RASA 8 price check → Run workflow to start a run
by hand, with optional dry-run and summary switches. Note that runners use US IP addresses, so some shops
may show other currencies or block the request; running from home sees the
prices you see.