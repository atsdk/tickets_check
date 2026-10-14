#!/usr/bin/env python3
"""
London -> Valencia fare monitor (outbound only).

Sources:
  1. Ryanair unofficial fare-finder API (services-api.ryanair.com) - STN route.
  2. Google Flights via `fast-flights` v3 - easyJet / Vueling / Wizz / BA
     from LGW, LTN, LHR, STN.

Sends a Telegram (and optionally email) message with the cheapest qualifying
fare for each outbound date whenever any date's price drops by more than
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

# Outbound: London -> Valencia
OUTBOUND_DATES = ["2026-12-23", "2026-12-24", "2026-12-25", "2026-12-26", "2026-12-27", "2026-12-28"]

# Earliest acceptable departure time (local), per departure airport.
# Default applies to anything not listed.
EARLIEST_DEP = {"STN": "07:30", "DEFAULT": "09:00"}

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


def to_gbp(amount: float, currency: str) -> float:
    return round(amount * FX_TO_GBP.get(currency, 1.0), 2)


# ----------------------------------------------------------------------------
# Source 1: Ryanair fare finder (unofficial, no key)
# ----------------------------------------------------------------------------

def ryanair_fares(origin, dest, date_from, date_to, earliest):
    url = "https://services-api.ryanair.com/farfnd/v4/oneWayFares"
    params = {
        "departureAirportIataCode": origin,
        "arrivalAirportIataCode": dest,
        "outboundDepartureDateFrom": date_from,
        "outboundDepartureDateTo": date_to,
        "outboundDepartureTimeFrom": earliest,
        "outboundDepartureTimeTo": "23:59",
        "currency": "GBP",
        "market": "en-gb",
    }
    out = []
    try:
        r = requests.get(url, params=params, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        for fare in r.json().get("fares", []):
            ob = fare.get("outbound", {})
            price = (ob.get("price") or {})
            value, cur = price.get("value"), price.get("currencyCode", "GBP")
            dep = ob.get("departureDate", "")  # e.g. 2026-12-23T08:30:00
            if value is None or "T" not in dep:
                continue
            d, hhmm = dep.split("T")[0], dep.split("T")[1][:5]
            if not time_ok(origin, hhmm):
                continue
            out.append({
                "source": "ryanair-api", "airline": "Ryanair",
                "from": origin, "to": dest, "date": d, "dep_time": hhmm,
                "price_pp_gbp": to_gbp(value, cur),
            })
    except Exception as e:
        print(f"[warn] Ryanair {origin}->{dest} failed: {e}")
    return out


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

def collect():
    outbound = []

    # Ryanair: the endpoint returns only the single cheapest fare in a range,
    # so query per day to see every date's price
    for day in OUTBOUND_DATES:
        outbound += ryanair_fares("STN", "VLC", day, day, earliest_for("STN"))
        time.sleep(0.5)

    # Google Flights: per airport x date
    for day in OUTBOUND_DATES:
        for ap in GOOGLE_LONDON_AIRPORTS:
            outbound += google_fares(ap, "VLC", day)
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
    return {"last_prices": None, "last_summary": ""}


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


def fmt(f):
    return (f"{f['date']} {f['dep_time']} {f['from']}->{f['to']} "
            f"{f['airline']}: £{f['price_pp_gbp']:.2f}pp ({f['source']})")


def build_message(per_day, drops, reason):
    lines = [f"✈️ LON→VLC monitor — {reason}",
             f"\nCheapest per day ({OUTBOUND_DATES[0]} … {OUTBOUND_DATES[-1]}):"]
    for day in OUTBOUND_DATES:
        f = per_day.get(day)
        line = "  " + (fmt(f) if f else f"{day} none found within time limits")
        if day in drops:
            line += f" ⬇️ was £{drops[day]:.2f}"
        lines.append(line)
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
    outbound = collect()
    log_csv(ts, outbound, "outbound")
    per_day = cheapest_per_day(outbound)

    state = load_state()
    prev = state.get("last_prices")  # {date: price_pp_gbp} from previous run
    today = utcnow().date().isoformat()
    hour = utcnow().hour

    # Dates whose cheapest fare fell by more than DROP_ALERT_GBP: {date: old price}
    drops = {}
    for day, f in per_day.items():
        old = (prev or {}).get(day)
        if old is not None and old - f["price_pp_gbp"] > DROP_ALERT_GBP:
            drops[day] = old

    reason = None
    if drops:
        reason = f"⬇️ price drop on {len(drops)} day(s)"
    elif prev is None:
        reason = "monitoring is live — cheapest fare per day"
    elif state.get("last_summary") != today and hour >= DAILY_SUMMARY_AFTER_UTC:
        reason = "daily summary"

    print(build_message(per_day, drops, reason or "no alert"))

    if reason:
        msg = build_message(per_day, drops, reason)
        send_telegram(msg)
        send_email("LON-VLC fare monitor", msg)
        state["last_summary"] = today

    # Keep the old price for dates with no fare this run, so a source outage
    # followed by recovery does not look like a drop
    state["last_prices"] = {**(prev or {}),
                            **{d: f["price_pp_gbp"] for d, f in per_day.items()}}
    state.pop("best_total", None)  # round-trip total no longer tracked
    save_state(state)

    if not outbound:
        print("[error] all sources returned nothing")
        sys.exit(1)


if __name__ == "__main__":
    main()
