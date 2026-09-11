#!/usr/bin/env python3
"""
Loggar avgångar för buss 516, i två riktningar:
  - "afternoon": Sergels torg -> Häggvik, 15:15-18:30
  - "morning":   Silverdals kapell -> stan, 06:00-09:00

Använder SL:s öppna "Transport"-API (inget API-nyckel behövs):
https://www.trafiklab.se/api/our-apis/sl/transport/

Körs en gång per anrop (triggas av ett schemalagt GitHub Actions-jobb).
Skriptet:
  1. Går igenom varje bevakning i WATCHES. Struntar i en bevakning om
     klockan i Stockholm inte är inom just dess fönster just nu.
  2. Slår upp siteId för hållplatsen (cachas i data/site_cache.json,
     delad mellan bevakningarna).
  3. Hämtar aktuella avgångar för hållplatsen, filtrerar ut linje 516 (och
     ev. destination).
  4. Sparar en rad per matchande avgång i data/<nyckel>/raw/<datum>.csv.
  5. Bygger om dagens sammanfattningsrad i data/<nyckel>/summary.csv, där
     varje unik schemalagd avgång klassas som:
       - RAN_ON_TIME / RAN_DELAYED_<n>min  -> avgången sågs ända fram mot
         sin egen förväntade avgångstid
       - VANISHED  -> avgången försvann innan sin förväntade tid, OCH vi
         bekräftade via en senare mätning att den fortfarande saknades
       - UNCERTAIN_WINDOW_ENDED -> avgången försvann innan sin förväntade
         tid, men fönstret stängde innan vi hann bekräfta om den kom sent
         eller aldrig kom
       - CANCELLED -> SL:s API markerade den uttryckligen som inställd
     Varje rad har också physically_confirmed (True/False) - om SL någon
     gång rapporterade fordonet som ATSTOP/DEPARTED/PASSED för just den
     avgången. Indikation, inte garanti (SL dokumenterar inte fältet).
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import urllib.request
import urllib.error

# ---------------------------------------------------------------------------
# Konfiguration - en post per bevakning. Lägg till fler här vid behov.
# ---------------------------------------------------------------------------
WATCHES = [
    {
        "key": "afternoon",
        "stop_name": "Sergels torg",
        "line_designation": "516",
        "destination_match": "Häggvik",  # substräng, skiftlägesokänslig
        "window_start": dtime(15, 15),   # 15 min innan första avgången (15:30)
        "window_end": dtime(18, 50),     # marginal efter scope_end för att
                                          # hinna bekräfta sista avgången
        "scope_end": dtime(18, 30),      # sista avgång vi räknar med
    },
    {
        "key": "morning",
        "stop_name": "Silverdals kapell",
        "line_designation": "516",
        "destination_match": "Sergels torg",
        "window_start": dtime(5, 45),    # 15 min innan första avgången (06:00)
        "window_end": dtime(9, 15),      # marginal efter scope_end
        "scope_end": dtime(9, 0),        # sista avgång vi räknar med
    },
]
WEEKDAYS_ONLY = True  # mån-fre

TZ = ZoneInfo("Europe/Stockholm")
BASE_URL = "https://transport.integration.sl.se/v1"

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
SITE_CACHE_FILE = DATA_DIR / "site_cache.json"  # delad mellan bevakningar


def raw_dir_for(watch_key: str) -> Path:
    d = DATA_DIR / watch_key / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


def summary_file_for(watch_key: str) -> Path:
    return DATA_DIR / watch_key / "summary.csv"


def http_get_json(url: str, attempts: int = 3, backoff_seconds: float = 5.0):
    """Hämtar JSON från en URL, med några återförsök om SL:s server svarar
    med ett tillfälligt fel (t.ex. HTTP 500)."""
    last_error = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(url, headers={"User-Agent": "sl-516-monitor/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last_error = e
            print(f"Försök {attempt}/{attempts} misslyckades ({e}), försöker igen om {backoff_seconds}s...")
            if attempt < attempts:
                time.sleep(backoff_seconds)
    raise last_error


def get_site_id(stop_name: str) -> int:
    """Slår upp siteId för en hållplats, med enkel fil-cache (delad för
    alla bevakningar)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
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


def in_monitoring_window(now_local: datetime, watch: dict) -> bool:
    if WEEKDAYS_ONLY and now_local.weekday() >= 5:  # 5=lör, 6=sön
        return False
    return watch["window_start"] <= now_local.time() <= watch["window_end"]


def fetch_matching_departures(site_id: int, watch: dict):
    payload = http_get_json(f"{BASE_URL}/sites/{site_id}/departures")
    out = []
    for dep in payload.get("departures", []):
        line = dep.get("line", {}) or {}
        designation = str(line.get("designation") or line.get("name") or "")
        if designation.strip() != watch["line_designation"]:
            continue
        dest_match = watch["destination_match"]
        if dest_match:
            destination = str(dep.get("destination") or "")
            if dest_match.lower() not in destination.lower():
                continue
        out.append(dep)
    return out


def append_raw_rows(poll_time_local: datetime, departures: list, watch_key: str):
    date_str = poll_time_local.strftime("%Y-%m-%d")
    raw_file = raw_dir_for(watch_key) / f"{date_str}.csv"
    is_new = not raw_file.exists()
    with raw_file.open("a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow([
                "poll_time", "scheduled", "expected", "state",
                "destination", "journey_state", "journey_id",
            ])
        if not departures:
            # Skriv ändå en "hjärtslag"-rad så vi vet att en mätning
            # faktiskt gjordes vid den här tidpunkten, även om inga
            # matchande avgångar syntes just då. Avgörande för att kunna
            # avgöra om en avgång verkligen försvann (se classify_day).
            writer.writerow([
                poll_time_local.isoformat(timespec="seconds"),
                "_HEARTBEAT_", "", "", "", "", "",
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


def classify_day(date_str: str, watch: dict):
    """Läser dagens raw-fil för en bevakning och klassar varje unik
    schemalagd avgång. Se modulens docstring för metodbeskrivning."""
    raw_file = raw_dir_for(watch["key"]) / f"{date_str}.csv"
    if not raw_file.exists():
        return []

    with raw_file.open(newline="", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # Bara pollningar som faktiskt returnerade minst en avgång räknas som
    # tillförlitligt "vi kollade och den var borta"-bevis. En helt tom
    # mätning (bara en _HEARTBEAT_-rad, noll avgångar alls för linjen) är
    # ofta ett tillfälligt hack hos SL:s API snarare än att alla avgångar
    # verkligen försvann samtidigt - den räknas därför INTE som bevis för
    # VANISHED, bara som att en mätning gjordes (syns ändå i raw-filen).
    confirmed_poll_times = sorted({
        datetime.fromisoformat(r["poll_time"]) for r in all_rows
        if r["scheduled"] != "_HEARTBEAT_"
    })

    by_scheduled = {}
    for row in all_rows:
        if row["scheduled"] == "_HEARTBEAT_":
            continue
        by_scheduled.setdefault(row["scheduled"], []).append(row)

    now_local = datetime.now(TZ)
    scope_end = watch["scope_end"]
    results = []
    for sched, rows in sorted(by_scheduled.items()):
        try:
            sched_dt = datetime.fromisoformat(sched).replace(tzinfo=TZ)
        except ValueError:
            sched_dt = None

        if sched_dt and sched_dt.time() > scope_end:
            continue

        states_seen = [r["state"] for r in rows]
        physically_confirmed = any(
            s in ("ATSTOP", "DEPARTED", "PASSED") for s in states_seen
        )
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
        elif last_expected_dt and last_seen_dt >= last_expected_dt - timedelta(minutes=2):
            delay_min = None
            if sched_dt and last_expected_dt:
                delay_min = round((last_expected_dt - sched_dt).total_seconds() / 60)
            if delay_min is not None and delay_min > 2:
                outcome = f"RAN_DELAYED_{delay_min}min"
            else:
                outcome = "RAN_ON_TIME"
        else:
            later_polls = [t for t in confirmed_poll_times if last_expected_dt and t > last_expected_dt]
            if later_polls:
                outcome = "VANISHED"
            else:
                outcome = "UNCERTAIN_WINDOW_ENDED"

        results.append({
            "date": date_str,
            "scheduled": sched,
            "first_seen": rows[0]["poll_time"],
            "last_seen": last_row["poll_time"],
            "last_state": last_row["state"],
            "outcome": outcome,
            "physically_confirmed": physically_confirmed,
        })
    return results


SUMMARY_FIELDNAMES = [
    "date", "scheduled", "first_seen", "last_seen",
    "last_state", "outcome", "physically_confirmed",
]


def rewrite_summary_for_date(date_str: str, watch: dict):
    new_rows = classify_day(date_str, watch)
    if not new_rows:
        return

    summary_file = summary_file_for(watch["key"])
    existing = []
    if summary_file.exists():
        with summary_file.open(newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))

    existing = [r for r in existing if r["date"] != date_str]
    existing.extend(new_rows)
    existing.sort(key=lambda r: (r["date"], r["scheduled"]))

    summary_file.parent.mkdir(parents=True, exist_ok=True)
    with summary_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDNAMES)
        writer.writeheader()
        writer.writerows(existing)


def run_watch(now_local: datetime, watch: dict):
    key = watch["key"]
    if not in_monitoring_window(now_local, watch):
        return

    try:
        site_id = get_site_id(watch["stop_name"])
        departures = fetch_matching_departures(site_id, watch)
    except RuntimeError as e:
        print(f"[{key}] Konfigurationsfel: {e}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"[{key}] Kunde inte hämta data från SL just nu, hoppar över denna pollning: {e}")
        return

    raw_file = append_raw_rows(now_local, departures, key)
    print(f"[{key}] Loggade {len(departures)} matchande avgångar till {raw_file}")

    rewrite_summary_for_date(now_local.strftime("%Y-%m-%d"), watch)
    print(f"[{key}] Uppdaterade {summary_file_for(key)}")


def main():
    now_local = datetime.now(TZ)
    any_active = False
    for watch in WATCHES:
        if in_monitoring_window(now_local, watch):
            any_active = True
        run_watch(now_local, watch)

    if not any_active:
        print(f"Utanför alla bevakningsfönster ({now_local.isoformat()}) - gjorde inget.")


if __name__ == "__main__":
    main()
