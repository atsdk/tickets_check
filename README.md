# London ↔ Valencia fare monitor

Watches fares for 2 people, London → Valencia (23–26 Dec 2026) and back (4–7 Jan 2027),
no departures before 10:00 (08:30 allowed from Stansted), under-seat bag only.
Runs free in the cloud on GitHub Actions every ~30 min and pings you on Telegram
when the best round-trip total for 2 drops below £150 or hits a new low.

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

That's it. It now runs every ~30 min with your laptop off, until you delete the
repo or disable the workflow.

### Optional: email alerts
Add secrets `SMTP_HOST` (e.g. `smtp.gmail.com`), `SMTP_PORT` (`465`),
`SMTP_USER`, `SMTP_PASS` (a Gmail **app password**, not your real one),
`EMAIL_TO`. Leave unset to skip email.

## When it alerts
- Best qualifying round trip for 2 ≤ **£150** (edit `TOTAL_TARGET_GBP` in `monitor.py`)
- Price drops ≥ £5 below the previous best
- Once daily after 18:00 UTC (summary), and on the first run

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
