#!/usr/bin/env python3
"""
Loggar avgångar för buss 516 mot Häggvik vid Sergels torg.

Använder SL:s öppna "Transport"-API (inget API-nyckel behövs):
https://www.trafiklab.se/api/our-apis/sl/transport/
"""

import csv
import json
import os
import sys
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import urllib.request
import urllib.error

STOP_NAME = "Sergels torg"
LINE_DESIGNATION = "516"
DESTINATION_MATCH = "Häggvik"
WINDOW_START = dtime(15, 45)
WINDOW_END = dtime(18, 15)
WEEKDAYS_ONLY = True

TZ = ZoneInfo("Europe/Stockholm")
BASE_URL = "https://transport.integration.sl.se/v1"

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
SITE_CACHE_FILE = DATA_DIR / "site_cache.json"
SUMMARY_FILE = DATA_DIR / "summary.csv"

RAW_DIR.mkdir(parents=True, exist_ok=True)


def http_get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "sl-516-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_site_id(stop_name: str) -> int:
    if SITE_CACHE_FILE.exists():
        cache = json.loads(SITE_CACHE_FILE.read_text())
        if stop_name in cache:
            return cache[stop_name]
    else:
        cache = {}

    sites = http_get_json(f"{BASE_URL}/sites?expand=true")
    match = None
    for site in sites:
        if site.get("name", "").strip().lower() == stop_name.strip().lower():
            match = site
            break
    if match is None:
        for site in sites:
            if stop_name.strip().lower() in site.get("name", "").strip().lower():
                match = site
                break
    if match is None:
        raise RuntimeError(f"Hittade ingen hållplats som matchar '{stop_name}'")

    cache[stop_name] = match["id"]
    SITE_CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))
    return match["id"]


def in_monitoring_window(now_local: datetime) -> bool:
    if WEEKDAYS_ONLY and now_local.weekday() >= 5:
        return False
    return WINDOW_START <= now_local.time() <= WINDOW_END


def fetch_matching_departures(site_id: int):
    payload = http_get_json(f"{BASE_URL}/sites/{site_id}/departures")
    out = []
    for dep in payload.get("departures", []):
        line = dep.get("line", {}) or {}
        designation = str(line.get("designation") or line.get("name") or "")
        destination = str(dep.get("destination") or "")
        if designation.strip() == LINE_DESIGNATION and DESTINATION_MATCH.lower() in destination.lower():
            out.append(dep)
    return out


def append_raw_rows(poll_time_local: datetime, departures: list):
    date_str = poll_time_local.strftime("%Y-%m-%d")
    raw_file = RAW_DIR / f"{date_str}.csv"
    is_new = not raw_file.exists()
    with raw_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "poll_time", "scheduled", "expected", "state",
                "destination", "journey_state", "journey_id",
            ])
        for dep in departures:
            journey = dep.get("journey", {}) or {}
            writer.writerow([
                poll_time_local.isoformat(timespec="seconds"),
                dep.get("scheduled", ""),
                dep.get("expected", ""),
                dep.get("state", ""),
                dep.get("destination", ""),
                journey.get("state", ""),
                journey.get("id", ""),
            ])
    return raw_file


def classify_day(date_str: str):
    raw_file = RAW_DIR / f"{date_str}.csv"
    if not raw_file.exists():
        return []

    by_scheduled = {}
    with raw_file.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sched = row["scheduled"]
            by_scheduled.setdefault(sched, []).append(row)

    now_local = datetime.now(TZ)
    results = []
    for sched, rows in sorted(by_scheduled.items()):
        try:
            sched_dt = datetime.fromisoformat(sched).replace(tzinfo=TZ)
        except ValueError:
            sched_dt = None

        states_seen = [r["state"] for r in rows]
        last_row = rows[-1]
        last_seen_dt = datetime.fromisoformat(last_row["poll_time"])

        last_expected_raw = last_row["expected"] or sched
        try:
            last_expected_dt = datetime.fromisoformat(last_expected_raw).replace(tzinfo=TZ)
        except ValueError:
            last_expected_dt = sched_dt

        if "CANCELLED" in states_seen:
            outcome = "CANCELLED"
        elif sched_dt and sched_dt > now_local:
            outcome = "PENDING"
        else:
            gap_min = None
            if last_expected_dt:
                gap_min = (last_expected_dt - last_seen_dt).total_seconds() / 60

            if gap_min is not None and gap_min > 5:
                outcome = "VANISHED"
            else:
                delay_min = None
                if sched_dt and last_expected_dt:
                    delay_min = round((last_expected_dt - sched_dt).total_seconds() / 60)
                if delay_min is not None and delay_min > 2:
                    outcome = f"RAN_DELAYED_{delay_min}min"
                else:
                    outcome = "RAN_ON_TIME"

        results.append({
            "date": date_str,
            "scheduled": sched,
            "first_seen": rows[0]["poll_time"],
            "last_seen": last_row["poll_time"],
            "last_state": last_row["state"],
            "outcome": outcome,
        })
    return results


def rewrite_summary_for_date(date_str: str):
    new_rows = classify_day(date_str)
    if not new_rows:
        return

    existing = []
    if SUMMARY_FILE.exists():
        with SUMMARY_FILE.open(newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))

    existing = [r for r in existing if r["date"] != date_str]
    existing.extend(new_rows)
    existing.sort(key=lambda r: (r["date"], r["scheduled"]))

    with SUMMARY_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "scheduled", "first_seen", "last_seen", "last_state", "outcome"])
        writer.writeheader()
        writer.writerows(existing)


def main():
    now_local = datetime.now(TZ)

    if not in_monitoring_window(now_local):
        print(f"Utanför bevakningsfönstret ({now_local.isoformat()}) – gör inget.")
        return

    try:
        site_id = get_site_id(STOP_NAME)
        departures = fetch_matching_departures(site_id)
    except (urllib.error.URLError, RuntimeError) as e:
        print(f"Fel vid hämtning: {e}", file=sys.stderr)
        sys.exit(1)

    raw_file = append_raw_rows(now_local, departures)
    print(f"Loggade {len(departures)} matchande avgångar till {raw_file}")

    rewrite_summary_for_date(now_local.strftime("%Y-%m-%d"))
    print("Uppdaterade data/summary.csv")


if __name__ == "__main__":
    main()
