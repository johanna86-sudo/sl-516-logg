#!/usr/bin/env python3
"""
Loggar avgångar för buss 516 mot Häggvik vid Sergels torg.

Använder SL:s öppna "Transport"-API (inget API-nyckel behövs):
https://www.trafiklab.se/api/our-apis/sl/transport/

Körs en gång per anrop (tänkt att triggas av ett schemalagt GitHub Actions-jobb
var 3:e minut). Skriptet:
  1. Struntar i att göra något alls om klockan i Stockholm inte är inom
     bevakningsfönstret (default 15:45-18:15, vardagar).
  2. Slår upp siteId för hållplatsen (cachas i data/site_cache.json).
  3. Hämtar aktuella avgångar för hållplatsen, filtrerar ut linje 516 mot
     Häggvik.
  4. Sparar en rad per matchande avgång i data/raw/<datum>.csv (en "ögonblicksbild").
  5. Bygger om dagens sammanfattningsrad i data/summary.csv, där varje unik
     schemalagd avgång klassas som:
       - RAN_ON_TIME / RAN_DELAYED_<n>min  -> avgången sågs ända fram (rimligt
         nära sin förväntade tid, staten gick inte till NOTEXPECTED igen)
       - VANISHED  -> avgången syntes i tavlan med ett framtida klockslag,
         men försvann helt innan den klocktiden nåddes, utan att någonsin
         markeras som avgången
       - CANCELLED -> SL:s API markerade den uttryckligen som inställd
       - NEVER_SEEN -> fanns i schemat men syntes aldrig alls i tavlan under
         mätfönstret (mest konservativ variant av "försvinner")
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
# Konfiguration – ändra här om du vill bevaka en annan linje/hållplats/fönster
# ---------------------------------------------------------------------------
STOP_NAME = "Sergels torg"
LINE_DESIGNATION = "516"
DESTINATION_MATCH = "Häggvik"  # substräng, skiftlägesokänslig
WINDOW_START = dtime(15, 15)  # 15 min innan första avgången (15:30)
WINDOW_END = dtime(18, 50)    # mäter en bit efter SCOPE_END, för att hinna
                                # bekräfta utfallet för den sista avgången
SCOPE_END = dtime(18, 30)     # avgångar planerade efter detta räknas inte
                                # med i sammanfattningen - sista bussen går
                                # 18:30 enligt tidtabellen
WEEKDAYS_ONLY = True  # mån-fre

TZ = ZoneInfo("Europe/Stockholm")
BASE_URL = "https://transport.integration.sl.se/v1"

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
SITE_CACHE_FILE = DATA_DIR / "site_cache.json"
SUMMARY_FILE = DATA_DIR / "summary.csv"

RAW_DIR.mkdir(parents=True, exist_ok=True)


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
    """Slår upp siteId för en hållplats, med enkel fil-cache."""
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
        # fallback: substräng-matchning om exakt namn inte hittas
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
    if WEEKDAYS_ONLY and now_local.weekday() >= 5:  # 5=lör, 6=sön
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
        if not departures:
            # Skriv ändå en "hjärtslag"-rad så vi vet att en mätning
            # faktiskt gjordes vid den här tidpunkten, även om inga
            # matchande avgångar syntes just då. Det är avgörande för att
            # senare kunna avgöra om en avgång verkligen försvann eller om
            # vi bara inte hann mäta i tid (se classify_day).
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


def classify_day(date_str: str):
    """Läser dagens raw-fil och klassar varje unik schemalagd avgång.

    Metod: för varje avgång som slutar synas i tavlan INNAN sin egen
    förväntade avgångstid, kollar vi om det faktiskt gjordes en ny mätning
    EFTER den tidpunkten (oavsett hur långt senare - GitHub Actions kör
    inte alltid exakt var 3:e minut). Om avgången fortfarande saknades vid
    den mätningen har vi goda belägg för att den verkligen försvann. Om
    ingen mätning hann göras efter dess avgångstid innan fönstret stängde
    kan vi inte veta säkert, och den märks UNCERTAIN istället för att
    gissa.
    """
    raw_file = RAW_DIR / f"{date_str}.csv"
    if not raw_file.exists():
        return []

    all_rows = []
    with raw_file.open(newline="", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # Alla tidpunkter då en mätning verkligen gjordes (oavsett om något
    # matchade), oavsett hur oregelbundet GitHub kört dem.
    poll_times = sorted({
        datetime.fromisoformat(r["poll_time"]) for r in all_rows
    })

    by_scheduled = {}
    for row in all_rows:
        if row["scheduled"] == "_HEARTBEAT_":
            continue
        by_scheduled.setdefault(row["scheduled"], []).append(row)

    now_local = datetime.now(TZ)
    results = []
    for sched, rows in sorted(by_scheduled.items()):
        try:
            sched_dt = datetime.fromisoformat(sched).replace(tzinfo=TZ)
        except ValueError:
            sched_dt = None

        if sched_dt and sched_dt.time() > SCOPE_END:
            # Ligger utanför det fönster vi faktiskt bevakar (t.ex. en
            # avgång kl 18:30 som bara syntes för att tavlan tittar en bit
            # framåt i tiden) - tas inte med i sammanfattningen.
            continue

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
        elif last_expected_dt and last_seen_dt >= last_expected_dt - timedelta(minutes=2):
            # Sågs ända fram till (eller nästan fram till) sin egen
            # förväntade avgångstid - stark signal att den faktiskt gick.
            delay_min = None
            if sched_dt and last_expected_dt:
                delay_min = round((last_expected_dt - sched_dt).total_seconds() / 60)
            if delay_min is not None and delay_min > 2:
                outcome = f"RAN_DELAYED_{delay_min}min"
            else:
                outcome = "RAN_ON_TIME"
        else:
            # Försvann innan sin egen förväntade tid. Kolla om vi hann
            # mäta igen EFTER den tidpunkten innan fönstret stängde.
            later_polls = [t for t in poll_times if last_expected_dt and t > last_expected_dt]
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
    except RuntimeError as e:
        # Ett konfigurationsfel (t.ex. hållplatsen hittades inte) - värt att
        # synas som ett rött, riktigt fel.
        print(f"Konfigurationsfel: {e}", file=sys.stderr)
        sys.exit(1)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        # SL:s server svarade inte som förväntat även efter återförsök.
        # Detta är ofta tillfälligt - vi loggar det tydligt men låter INTE
        # hela jobbet räknas som misslyckat, eftersom nästa pollning om
        # 3 minuter oftast löser det själv.
        print(f"Kunde inte hämta data från SL just nu, hoppar över denna pollning: {e}")
        return

    raw_file = append_raw_rows(now_local, departures)
    print(f"Loggade {len(departures)} matchande avgångar till {raw_file}")

    rewrite_summary_for_date(now_local.strftime("%Y-%m-%d"))
    print("Uppdaterade data/summary.csv")


if __name__ == "__main__":
    main()
