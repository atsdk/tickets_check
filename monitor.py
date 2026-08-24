#!/usr/bin/env python3
"""
London <-> Valencia fare monitor.

Sources:
  1. Ryanair unofficial fare-finder API (services-api.ryanair.com) - STN route.
  2. Google Flights via the `fast-flights` library - easyJet / Vueling / Wizz / BA
     from LGW, LTN, LHR, STN.

Alerts via Telegram (and optionally email) when a qualifying round-trip combo
for 2 people drops below TOTAL_TARGET_GBP, or when a new all-time low appears.

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
from datetime import datetime, date
from email.mime.text import MIMEText

import requests

# ----------------------------------------------------------------------------
# CONFIG - edit here
# ----------------------------------------------------------------------------

PAX = 2

# Outbound: London -> Valencia, arrive 23-26 Dec 2026 (short flight => same-day)
OUTBOUND_DATES = ["2026-12-23", "2026-12-24", "2026-12-25", "2026-12-26"]
# Return: Valencia -> London, 4-7 Jan 2027
RETURN_DATES = ["2027-01-04", "2027-01-05", "2027-01-06", "2027-01-07"]

# Earliest acceptable departure time (local), per departure airport.
# Default applies to anything not listed (incl. VLC on the way back).
EARLIEST_DEP = {"STN": "08:30", "DEFAULT": "10:00"}

# London airports checked on Google Flights (LCY dropped: no cheap VLC service)
GOOGLE_LONDON_AIRPORTS = ["LGW", "LTN", "LHR", "STN"]

DIRECT_ONLY = True

# Alert thresholds
TOTAL_TARGET_GBP = 150.0   # alert if best round-trip total for PAX people <= this
DROP_ALERT_GBP = 5.0       # also alert if best total drops by at least this much
DAILY_SUMMARY_HOUR_UTC = 18  # one summary/day after this hour even without a drop

# Rough conversion if Google returns non-GBP prices (US runner IPs sometimes do)
FX_TO_GBP = {"£": 1.0, "€": 0.85, "$": 0.76, "GBP": 1.0, "EUR": 0.85, "USD": 0.76}

STATE_FILE = "state.json"
CSV_FILE = "prices.csv"

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

def earliest_for(airport: str) -> str:
    return EARLIEST_DEP.get(airport, EARLIEST_DEP["DEFAULT"])


def time_ok(airport: str, hhmm: str) -> bool:
    return hhmm >= earliest_for(airport)


def to_gbp(amount: float, currency: str) -> float:
    return round(amount * FX_TO_GBP.get(currency, 1.0), 2)


def parse_price(text: str):
    """'£38' / '€45' / '$52' / 'GBP 38.99' -> (38.0, '£')"""
    if not text:
        return None, None
    m = re.search(r"([£€$])?\s*([\d,]+(?:\.\d+)?)", str(text))
    if not m:
        return None, None
    cur = m.group(1) or ("£" if "GBP" in str(text) else "€" if "EUR" in str(text) else "$" if "USD" in str(text) else "£")
    return float(m.group(2).replace(",", "")), cur


def parse_ampm_time(text: str):
    """'6:20 AM on Wed, Dec 23' -> '06:20' (24h). Returns None if unparseable."""
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", str(text), re.IGNORECASE)
    if not m:
        m24 = re.search(r"\b(\d{1,2}):(\d{2})\b", str(text))
        if m24:
            return f"{int(m24.group(1)):02d}:{m24.group(2)}"
        return None
    h, mnt, ap = int(m.group(1)), m.group(2), m.group(3).upper()
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mnt}"


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
                "raw_price": f"{value} {cur}",
            })
    except Exception as e:
        print(f"[warn] Ryanair {origin}->{dest} failed: {e}")
    return out


# ----------------------------------------------------------------------------
# Source 2: Google Flights via fast-flights
# ----------------------------------------------------------------------------

def google_fares(origin, dest, day):
    try:
        from fast_flights import FlightData, Passengers, get_flights
    except ImportError:
        print("[warn] fast-flights not installed; skipping Google source")
        return []
    out = []
    try:
        result = get_flights(
            flight_data=[FlightData(date=day, from_airport=origin, to_airport=dest)],
            trip="one-way", seat="economy",
            passengers=Passengers(adults=PAX, children=0,
                                  infants_in_seat=0, infants_on_lap=0),
            fetch_mode="fallback",
        )
        for f in result.flights:
            stops = getattr(f, "stops", 0)
            if DIRECT_ONLY and stops not in (0, "0", "Nonstop", None):
                continue
            hhmm = parse_ampm_time(getattr(f, "departure", ""))
            if hhmm is None or not time_ok(origin, hhmm):
                continue
            value, cur = parse_price(getattr(f, "price", ""))
            if value is None:
                continue
            out.append({
                "source": "google", "airline": getattr(f, "name", "?"),
                "from": origin, "to": dest, "date": day, "dep_time": hhmm,
                "price_pp_gbp": to_gbp(value, cur),  # Google shows per-person
                "raw_price": str(getattr(f, "price", "")),
            })
    except Exception as e:
        print(f"[warn] Google {origin}->{dest} {day} failed: {e}")
    return out


# ----------------------------------------------------------------------------
# Collection
# ----------------------------------------------------------------------------

def collect():
    outbound, inbound = [], []

    # Ryanair: the endpoint returns only the single cheapest fare in a range,
    # so query per day to see every date's price
    for day in OUTBOUND_DATES:
        outbound += ryanair_fares("STN", "VLC", day, day, earliest_for("STN"))
        time.sleep(0.5)
    for day in RETURN_DATES:
        inbound += ryanair_fares("VLC", "STN", day, day, earliest_for("VLC"))
        time.sleep(0.5)

    # Google Flights: per airport x date
    for day in OUTBOUND_DATES:
        for ap in GOOGLE_LONDON_AIRPORTS:
            outbound += google_fares(ap, "VLC", day)
            time.sleep(1.5)
    for day in RETURN_DATES:
        for ap in GOOGLE_LONDON_AIRPORTS:
            inbound += google_fares("VLC", ap, day)
            time.sleep(1.5)

    # Dedupe: keep cheapest per (airline, from, to, date, dep_time)
    def dedupe(fares):
        best = {}
        for f in fares:
            k = (f["airline"], f["from"], f["to"], f["date"], f["dep_time"])
            if k not in best or f["price_pp_gbp"] < best[k]["price_pp_gbp"]:
                best[k] = f
        return sorted(best.values(), key=lambda x: x["price_pp_gbp"])

    return dedupe(outbound), dedupe(inbound)


# ----------------------------------------------------------------------------
# State, logging, alerting
# ----------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"best_total": None, "last_summary": ""}


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


def build_message(outbound, inbound, total, reason):
    lines = [f"✈️ LON↔VLC monitor — {reason}"]
    if total is not None:
        lines.append(f"Best qualifying round trip for {PAX}: £{total:.2f}")
    lines.append("\nBest outbound (arrive 23–26 Dec):")
    lines += ["  " + fmt(f) for f in outbound[:4]] or ["  none found ≥ time limits"]
    lines.append("\nBest return (4–7 Jan):")
    lines += ["  " + fmt(f) for f in inbound[:4]] or ["  none found ≥ time limits"]
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
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    outbound, inbound = collect()
    log_csv(ts, outbound, "outbound")
    log_csv(ts, inbound, "return")

    total = None
    if outbound and inbound:
        total = (outbound[0]["price_pp_gbp"] + inbound[0]["price_pp_gbp"]) * PAX

    state = load_state()
    prev = state.get("best_total")
    today = date.today().isoformat()
    hour = datetime.utcnow().hour

    reason = None
    if total is not None and total <= TOTAL_TARGET_GBP:
        reason = f"🎯 under target £{TOTAL_TARGET_GBP:.0f}!"
    elif total is not None and prev is not None and total <= prev - DROP_ALERT_GBP:
        reason = f"⬇️ price drop (was £{prev:.2f})"
    elif prev is None:
        reason = "first run — monitoring is live"
    elif state.get("last_summary") != today and hour >= DAILY_SUMMARY_HOUR_UTC:
        reason = "daily summary"

    print(build_message(outbound, inbound, total, reason or "no alert"))

    if reason:
        msg = build_message(outbound, inbound, total, reason)
        send_telegram(msg)
        send_email("LON-VLC fare monitor", msg)
        state["last_summary"] = today

    if total is not None and (prev is None or total < prev):
        state["best_total"] = total
    save_state(state)

    if not outbound and not inbound:
        print("[error] all sources returned nothing")
        sys.exit(1)


if __name__ == "__main__":
    main()
