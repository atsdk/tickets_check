# London ↔ Valencia fare monitor

Watches one-way fares London → Valencia (VLC), plus Madrid (MAD) and Castellón
(CDT) as train-onward fallbacks, for the dates in `OUTBOUND_DATES`. No departures
before 09:00 (earlier allowed from Stansted, see `EARLIEST_DEP`), under-seat bag
only. Runs free in the cloud on GitHub Actions every ~2 hours and sends you on
Telegram one ranked list of the cheapest fares (see below) whenever a listed
fare's price drops by more than £5 per person.

**Sources:** Ryanair's unofficial fare-finder JSON API (direct, reliable) +
Google Flights via `fast-flights` (covers easyJet, Vueling, Wizz Air, BA from
LGW/LTN/LHR/STN). No Selenium, no paid APIs.

## Setup (~10 min, once)

### 1. Telegram bot (2 min)
1. In Telegram, message **@BotFather** → `/newbot` → pick any name → copy the **token**.
2. Open a chat with your new bot and send it any message (e.g. "hi").
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and copy
   the number at `"chat":{"id": ...}` — that's your **chat id**.

### 2. GitHub repo
1. Create a **public** repo (public = unlimited free Actions minutes; a private
   repo's 2000 free min/month only supports ~hourly runs — if you go private,
   change the cron in `.github/workflows/monitor.yml` to `"0 * * * *"`).
2. Push this folder's contents to it (`.github/` folder included).
3. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`
4. **Actions** tab → enable workflows → open **fare-monitor** → **Run workflow**
   to test. Within a couple of minutes you should get a "first run — monitoring
   is live" Telegram message with the current best fares.

That's it. It now runs every ~2 hours with your laptop off, until you delete the
repo or disable the workflow.

### Optional: email alerts
Add secrets `SMTP_HOST` (e.g. `smtp.gmail.com`), `SMTP_PORT` (`465`),
`SMTP_USER`, `SMTP_PASS` (a Gmail **app password**, not your real one),
`EMAIL_TO`. Leave unset to skip email.

## When it alerts
- A listed fare is its date's cheapest for that destination and is more than
  **£5** below that date's price on the previous run (edit `DROP_ALERT_GBP`)
- Once daily, on the first run after 07:00 UTC (summary), and on the first run

Every message is one list of the `TOP_N` (7) best fares across all destinations,
each shown as `<departure airport>-<destination airport>`. Fares above
`MAX_PRICE_GBP` (£80, actual price) are hidden. Madrid and Castellón are ranked
as if £20 dearer (`TRANSFER_COST_GBP`, the train to Valencia), but the price
shown is the actual fare. Madrid and Castellón flights must land at least
`MIN_CONNECTION_MIN` (2 h) before the last onward connection to Valencia
(`LAST_ONWARD_DEP`: 21:00 train from Madrid, 21:15 coach from Castellón), so
the latest landing is 19:00 in Madrid and 19:15 in Castellón.

All checked fares append to `prices.csv` in the repo, so you also get a price
history to see trends.

## Honest caveats
- Ryanair fare-finder prices are per-person "from" fares — occasionally only
  1 seat is left at that price. Always verify at checkout.
- Google Flights occasionally blocks datacenter IPs; those runs just skip the
  Google source (Ryanair still works) and catch up next run.
- GitHub cron is best-effort — runs can be 5–20 min late. Fine for this.
- After ~60 days GitHub emails a "workflow will be disabled due to inactivity"
  warning on repos with no pushes — the bot's own price-data commits normally
  prevent this, but click "keep active" if you get the email.
- Google prices may come back in USD/EUR from US runners; the script converts
  with rough fixed rates (`FX_TO_GBP` in `monitor.py`).

## Tuning
Everything lives at the top of `monitor.py`: dates, earliest departure times
per airport, airports list, thresholds, direct-only flag.
