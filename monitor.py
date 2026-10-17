#!/usr/bin/env python3
"""
London -> Valencia fare monitor (outbound only).

Sources:
  1. Ryanair unofficial fare-finder API (services-api.ryanair.com) - STN route.
  2. Google Flights via `fast-flights` v3 - easyJet / Vueling / Wizz / BA
     from LGW, LTN, LHR, STN.

Sends a Telegram (and optionally email) message with one list: the TOP_N
cheapest fares at or under MAX_PRICE_GBP across all destinations, ranked with
TRANSFER_COST_GBP added for Madrid/Castellón (shown prices are actual fares).
Sent whenever a listed fare is a date's price that dropped by more than
DROP_ALERT_GBP per person since the previous run, plus a daily summary.

State (state.json) and price history (prices.csv) are committed back to the
repo by the GitHub Actions workflow.
"""

import csv
import json
import os
import re
import smtplib
import sys
import time
from datetime import datetime, date, timezone
from email.mime.text import MIMEText

import requests

try:
    from fast_flights import FlightQuery, Passengers, create_query, get_flights
    FAST_FLIGHTS_OK = True
except Exception as _e:  # noqa: N816
    print(f"[warn] fast-flights unavailable ({type(_e).__name__}: {_e}); "
          "Google source disabled")
    FAST_FLIGHTS_OK = False

# ----------------------------------------------------------------------------
# CONFIG - edit here
# ----------------------------------------------------------------------------

PAX = 2

# Outbound: London -> each destination, merged into one ranked list.
# MAD and CDT (Castellón) are fallbacks: train on to Valencia is sometimes cheap.
DESTINATIONS = ["VLC", "MAD", "CDT"]
OUTBOUND_DATES = ["2026-12-23", "2026-12-24", "2026-12-25", "2026-12-26", "2026-12-27", "2026-12-28"]

# Hide fares whose actual per-person price is above this
MAX_PRICE_GBP = 80.0
# Train cost to Valencia, added for ranking only (shown prices stay actual)
TRANSFER_COST_GBP = {"MAD": 20.0, "CDT": 20.0}
# How many fares the message lists
TOP_N = 7

# Earliest acceptable departure time (local), per departure airport.
# Default applies to anything not listed.
EARLIEST_DEP = {"STN": "06:50", "DEFAULT": "09:00"}

# Last onward connection to Valencia (local time) per fallback destination.
# Flights landing later than this minus MIN_CONNECTION_MIN, or after midnight,
# are dropped.
#   MAD: last Madrid Chamartín -> Valencia high-speed train ~21:00 (Renfe
#        AVE/Avlo; OUIGO 21:15 may not run on holidays).
#   CDT: airport bus waits for the flight; last Castellón -> Valencia coach 21:15.
LAST_ONWARD_DEP = {"MAD": "21:00", "CDT": "21:15"}
MIN_CONNECTION_MIN = 120

# London airports checked on Google Flights (LCY dropped: no cheap VLC service)
GOOGLE_LONDON_AIRPORTS = ["LGW", "LTN", "LHR", "STN"]

DIRECT_ONLY = True

# Alert when any date's cheapest per-person fare falls by MORE than this
# compared with the previous run
DROP_ALERT_GBP = 5.0
# GitHub throttles cron heavily, so runs can be hours apart: send the daily
# summary on the FIRST run after this UTC hour (if none sent yet today).
DAILY_SUMMARY_AFTER_UTC = 7

# Rough conversion if Google returns non-GBP prices despite currency=GBP
FX_TO_GBP = {"£": 1.0, "€": 0.85, "$": 0.76, "GBP": 1.0, "EUR": 0.85, "USD": 0.76}

STATE_FILE = "state.json"
CSV_FILE = "prices.csv"

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def utcnow():
    return datetime.now(timezone.utc)


def earliest_for(airport: str) -> str:
    # Zero-pad ("9:00" -> "09:00"): time_ok compares HH:MM strings
    h, m = EARLIEST_DEP.get(airport, EARLIEST_DEP["DEFAULT"]).split(":")
    return f"{int(h):02d}:{int(m):02d}"


def time_ok(airport: str, hhmm: str) -> bool:
    return hhmm >= earliest_for(airport)


def latest_arrival(dest):
    """Latest acceptable landing time (HH:MM) at dest, or None if unconstrained."""
    last = LAST_ONWARD_DEP.get(dest)
    if last is None:
        return None
    h, m = map(int, last.split(":"))
    t = h * 60 + m - MIN_CONNECTION_MIN
    return f"{t // 60:02d}:{t % 60:02d}"


def arrival_ok(dest, dep_date, arr_date, arr_hhmm):
    """True if a flight landing at arr_date arr_hhmm still makes the connection."""
    latest = latest_arrival(dest)
    return latest is None or (arr_date == dep_date and arr_hhmm <= latest)


def to_gbp(amount: float, currency: str) -> float:
    return round(amount * FX_TO_GBP.get(currency, 1.0), 2)


# ----------------------------------------------------------------------------
# Source 1: Ryanair fare finder (unofficial, no key)
# ----------------------------------------------------------------------------

def ryanair_fares(origin, dest, day, earliest):
    """Cheapest Ryanair fare on `day` departing at/after `earliest` and landing
    in time for the onward connection (see LAST_ONWARD_DEP)."""
    url = "https://services-api.ryanair.com/farfnd/v4/oneWayFares"
    latest_dep = "23:59"
    try:
        # The endpoint returns only the cheapest fare and cannot filter by
        # arrival time: if that fare lands too late, ask again for flights
        # departing before it
        for _ in range(10):
            params = {
                "departureAirportIataCode": origin,
                "arrivalAirportIataCode": dest,
                "outboundDepartureDateFrom": day,
                "outboundDepartureDateTo": day,
                "outboundDepartureTimeFrom": earliest,
                "outboundDepartureTimeTo": latest_dep,
                "currency": "GBP",
                "market": "en-gb",
            }
            r = requests.get(url, params=params, timeout=30,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            out, too_late = [], []
            for fare in r.json().get("fares", []):
                ob = fare.get("outbound", {})
                price = (ob.get("price") or {})
                value, cur = price.get("value"), price.get("currencyCode", "GBP")
                dep = ob.get("departureDate", "")  # e.g. 2026-12-23T08:30:00
                arr = ob.get("arrivalDate", "")
                if value is None or "T" not in dep or "T" not in arr:
                    continue
                d, hhmm = dep[:10], dep[11:16]
                if not time_ok(origin, hhmm):
                    continue
                if not arrival_ok(dest, d, arr[:10], arr[11:16]):
                    too_late.append(hhmm)
                    continue
                out.append({
                    "source": "ryanair-api", "airline": "Ryanair",
                    "from": origin, "to": dest, "date": d, "dep_time": hhmm,
                    "price_pp_gbp": to_gbp(value, cur),
                })
            if out or not too_late:
                return out
            # Retry with departures up to one minute before the late flight
            h, m = map(int, min(too_late).split(":"))
            t = h * 60 + m - 1
            if t < 0:
                return []
            latest_dep = f"{t // 60:02d}:{t % 60:02d}"
            time.sleep(0.5)
    except Exception as e:
        print(f"[warn] Ryanair {origin}->{dest} {day} failed: {e}")
    return []


# ----------------------------------------------------------------------------
# Source 2: Google Flights via fast-flights v3
# ----------------------------------------------------------------------------

def google_fares(origin, dest, day):
    if not FAST_FLIGHTS_OK:
        return []
    out = []
    try:
        q = create_query(
            flights=[FlightQuery(
                date=day, from_airport=origin, to_airport=dest,
                max_stops=0 if DIRECT_ONLY else None,
                earliest_departure_hour=int(earliest_for(origin).split(":")[0]),
            )],
            trip="one-way",
            passengers=Passengers(adults=PAX),
            currency="GBP",
        )
        for fl in get_flights(q):
            try:
                seg = fl.flights[0]
                h, m = seg.departure.time
                hhmm = f"{h:02d}:{m:02d}"
                if not time_ok(origin, hhmm):
                    continue
                if latest_arrival(dest) is not None:
                    arr = fl.flights[-1].arrival
                    y, mo, d = arr.date
                    ah, am = arr.time
                    if not arrival_ok(dest, day, f"{y:04d}-{mo:02d}-{d:02d}",
                                      f"{ah:02d}:{am:02d}"):
                        continue
                price = float(fl.price)  # int, in GBP (currency=GBP)
                if price <= 0:
                    continue
                out.append({
                    "source": "google", "airline": ", ".join(fl.airlines) or "?",
                    "from": origin, "to": dest, "date": day, "dep_time": hhmm,
                    "price_pp_gbp": round(price, 2),  # Google shows per-person
                })
            except Exception:
                continue
    except Exception as e:
        print(f"[warn] Google {origin}->{dest} {day} failed: "
              f"{type(e).__name__}: {e}")
    return out


# ----------------------------------------------------------------------------
# Collection
# ----------------------------------------------------------------------------

def collect(dest):
    outbound = []

    # Ryanair: the endpoint returns only the single cheapest fare in a range,
    # so query per day to see every date's price
    for day in OUTBOUND_DATES:
        outbound += ryanair_fares("STN", dest, day, earliest_for("STN"))
        time.sleep(0.5)

    # Google Flights: per airport x date
    for day in OUTBOUND_DATES:
        for ap in GOOGLE_LONDON_AIRPORTS:
            outbound += google_fares(ap, dest, day)
            time.sleep(1.5)

    # Dedupe: keep cheapest per (airline, from, to, date, dep_time)
    best = {}
    for f in outbound:
        k = (f["airline"], f["from"], f["to"], f["date"], f["dep_time"])
        if k not in best or f["price_pp_gbp"] < best[k]["price_pp_gbp"]:
            best[k] = f
    return sorted(best.values(), key=lambda x: x["price_pp_gbp"])


def cheapest_per_day(fares):
    """{date: cheapest fare that date} for dates with at least one fare."""
    best = {}
    for f in fares:
        if f["date"] not in best or f["price_pp_gbp"] < best[f["date"]]["price_pp_gbp"]:
            best[f["date"]] = f
    return best


# ----------------------------------------------------------------------------
# State, logging, alerting
# ----------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"last_prices_by_dest": None, "last_summary": ""}


def save_state(state):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


def log_csv(ts, fares, leg):
    new = not os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["ts", "leg", "source", "airline", "from", "to",
                        "date", "dep_time", "price_pp_gbp"])
        for f in fares:
            w.writerow([ts, leg, f["source"], f["airline"], f["from"], f["to"],
                        f["date"], f["dep_time"], f["price_pp_gbp"]])


def rank_price(f):
    """Price used for ranking: actual fare plus train transfer to Valencia."""
    return f["price_pp_gbp"] + TRANSFER_COST_GBP.get(f["to"], 0.0)


def top_fares(fares):
    """TOP_N fares at or under MAX_PRICE_GBP, cheapest rank_price first."""
    ok = [f for f in fares if f["price_pp_gbp"] <= MAX_PRICE_GBP]
    return sorted(ok, key=lambda f: (rank_price(f), f["date"]))[:TOP_N]


def fmt(f):
    return (f"{f['date']} {f['dep_time']} {f['from']}-{f['to']} "
            f"{f['airline']}: £{f['price_pp_gbp']:.2f}pp ({f['source']})")


def build_message(rows, reason):
    """rows: [(fare, previous price if that date's price dropped, else None)]."""
    extra = ", ".join(f"{d} +£{c:.0f}" for d, c in TRANSFER_COST_GBP.items())
    lines = [f"✈️ LON→{'/'.join(DESTINATIONS)} monitor — {reason}",
             f"\nTop {TOP_N}, {OUTBOUND_DATES[0]} … {OUTBOUND_DATES[-1]}, "
             f"fares ≤ £{MAX_PRICE_GBP:.0f} (ranked with {extra} for the train; "
             "prices shown are actual):"]
    for i, (f, old) in enumerate(rows, 1):
        line = f"  {i}. {fmt(f)}"
        if old is not None:
            line += f" ⬇️ was £{old:.2f}"
        lines.append(line)
    if not rows:
        lines.append(f"  none ≤ £{MAX_PRICE_GBP:.0f} within time limits")
    lines.append("\nPrices are per-person 'from' fares, under-seat bag only — "
                 "verify at checkout.")
    return "\n".join(lines)


def send_telegram(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[info] Telegram secrets not set; skipping")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=30)
        r.raise_for_status()
        print("[info] Telegram alert sent")
    except Exception as e:
        print(f"[warn] Telegram send failed: {e}")


def send_email(subject, text):
    host = os.environ.get("SMTP_HOST")
    if not host:
        return
    try:
        msg = MIMEText(text)
        msg["Subject"] = subject
        msg["From"] = os.environ["SMTP_USER"]
        msg["To"] = os.environ.get("EMAIL_TO", os.environ["SMTP_USER"])
        with smtplib.SMTP_SSL(host, int(os.environ.get("SMTP_PORT", "465"))) as s:
            s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
            s.send_message(msg)
        print("[info] Email sent")
    except Exception as e:
        print(f"[warn] Email send failed: {e}")


def main():
    ts = utcnow().strftime("%Y-%m-%d %H:%M")
    state = load_state()
    # {dest: {date: price_pp_gbp}} from previous run
    prev = state.get("last_prices_by_dest")
    today = utcnow().date().isoformat()
    hour = utcnow().hour

    outbound, per_day, drops = [], {}, {}
    for dest in DESTINATIONS:
        fares = collect(dest)
        outbound += fares
        per_day[dest] = cheapest_per_day(fares)
        # Dates whose cheapest fare fell by more than DROP_ALERT_GBP: {date: old price}
        old_prices = (prev or {}).get(dest, {})
        drops[dest] = {}
        for day, f in per_day[dest].items():
            old = old_prices.get(day)
            if old is not None and old - f["price_pp_gbp"] > DROP_ALERT_GBP:
                drops[dest][day] = old
    log_csv(ts, outbound, "outbound")

    # Mark a listed fare as dropped only if it is that date's cheapest fare
    # for its destination, so the "was" price refers to the same thing
    rows = []
    for f in top_fares(outbound):
        old = drops[f["to"]].get(f["date"])
        rows.append((f, old if per_day[f["to"]].get(f["date"]) is f else None))

    # Alert only on drops visible in the list
    n_drops = sum(old is not None for _, old in rows)
    reason = None
    if n_drops:
        reason = f"⬇️ price drop on {n_drops} listed fare(s)"
    elif prev is None:
        reason = "monitoring is live"
    elif state.get("last_summary") != today and hour >= DAILY_SUMMARY_AFTER_UTC:
        reason = "daily summary"

    print(build_message(rows, reason or "no alert"))

    if reason:
        msg = build_message(rows, reason)
        send_telegram(msg)
        send_email("LON fare monitor", msg)
        state["last_summary"] = today

    # Keep the old price for dates with no fare this run, so a source outage
    # followed by recovery does not look like a drop
    state["last_prices_by_dest"] = {
        dest: {**(prev or {}).get(dest, {}),
               **{d: f["price_pp_gbp"] for d, f in per_day[dest].items()}}
        for dest in DESTINATIONS
    }
    state.pop("last_prices", None)  # old VLC-only format
    save_state(state)

    if not outbound:
        print("[error] all sources returned nothing")
        sys.exit(1)


if __name__ == "__main__":
    main()
