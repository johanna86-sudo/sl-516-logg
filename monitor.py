#!/usr/bin/env python3
"""
Loggar avgångar för buss 516, i två riktningar:
  - "afternoon": Sergels torg -> Häggvik, 15:15-18:30
  - "morning":   Silverdals kapell -> stan, 06:00-09:00

Använder SL:s öppna "Transport"-API (inget API-nyckel behövs):
https://www.trafiklab.se/api/our-apis/sl/transport/

Körs som en "session": GitHub Actions startar skriptet någon gång kring ett
bevakningsfönster, och skriptet pollar sedan självt (var 60:e sekund, var
30:e när en avgång är nära) tills fönstret stängt, och pushar data var 10:e
minut. Det gör att det räcker att GitHub startar jobbet en enda gång per
fönster. Skriptet:
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
import random
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone, time as dtime
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
        elif last_expected_dt and last_seen_dt >= last_expected_dt - timedelta(minutes=20):
            delay_min = None
            if sched_dt and last_expected_dt:
                delay_min = round((last_expected_dt - sched_dt).total_seconds() / 60)

            # Sågs den nära sin avgångstid MED fysisk bekräftelse
            # (ATSTOP/DEPARTED/PASSED)? Det är den enda riktigt säkra
            # RAN-signalen. Utan den vet vi inte om den faktiskt kom, eller
            # om den bara "hängde kvar" på tavlan fram till Nu och sedan
            # försvann tyst - precis det mönster du beskrivit.
            if physically_confirmed:
                outcome = "RAN_CONFIRMED" if not delay_min or delay_min <= 2 else f"RAN_CONFIRMED_DELAYED_{delay_min}min"
            elif delay_min is not None and delay_min > 2:
                outcome = f"RAN_LIKELY_DELAYED_{delay_min}min"
            else:
                outcome = "RAN_LIKELY"
        else:
            later_polls = [t for t in confirmed_poll_times if last_expected_dt and t > last_expected_dt]
            if len(later_polls) >= 2:
                outcome = "VANISHED_CONFIRMED"
            elif len(later_polls) == 1:
                outcome = "VANISHED_LIKELY"
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




# ---------------------------------------------------------------------------
# Sessionsläge: EN körning pollar själv genom hela fönstret.
#
# Tidigare triggades ett nytt jobb var 5:e minut av GitHub. GitHub startar
# dock inte alla schemalagda körningar (många hoppas över), vilket gav stora
# luckor i datan. Nu räcker det att GitHub startar jobbet EN gång någonstans
# kring fönstret - därefter sköter skriptet pollningen själv.
# ---------------------------------------------------------------------------
POLL_INTERVAL_SECONDS = 60        # normal mätfrekvens
IMMINENT_INTERVAL_SECONDS = 30    # tätare när en avgång är nära
IMMINENT_LOOKAHEAD_MINUTES = 6    # "nära" = väntas inom så här lång tid
PRESTART_WAIT_MINUTES = 120       # om jobbet startar så här tidigt före ett
                                   # fönster väntar det in fönstret. GitHub
                                   # startar ofta jobben timmar för sent, så
                                   # marginalen behöver vara stor.
COMMIT_EVERY_SECONDS = 600        # spara/pusha data var 10:e minut
MAX_RUNTIME_SECONDS = 345 * 60    # säkerhetsgräns: 2 h väntan + 3 h 35 min
                                   # fönster ryms, under GitHubs 6 h-gräns


def has_imminent_departure(now_local: datetime, departures: list) -> bool:
    """Sant om någon avgång väntas inom IMMINENT_LOOKAHEAD_MINUTES - det är
    då "försvinner tyst vid Nu"-mönstret faktiskt inträffar, så vi vill mäta
    tätare just runt den stunden."""
    for dep in departures:
        expected_raw = dep.get("expected") or dep.get("scheduled")
        if not expected_raw:
            continue
        try:
            expected_dt = datetime.fromisoformat(expected_raw)
            if expected_dt.tzinfo is None:
                expected_dt = expected_dt.replace(tzinfo=TZ)
        except ValueError:
            continue
        minutes_left = (expected_dt - now_local).total_seconds() / 60
        if -1 <= minutes_left <= IMMINENT_LOOKAHEAD_MINUTES:
            return True
    return False


def git_sync(message_prefix: str = "Logg"):
    """Committar och pushar data/ - bara när skriptet körs i GitHub Actions.
    Fel här får aldrig stoppa mätningen; det slutliga commit-steget i
    workflowen är reserv."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    try:
        subprocess.run(["git", "config", "user.name", "sl-516-monitor-bot"], check=True)
        subprocess.run(["git", "config", "user.email", "actions@users.noreply.github.com"], check=True)
        subprocess.run(["git", "add", "data/"], check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
            return
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        subprocess.run(["git", "commit", "-m", f"{message_prefix}: {stamp}"], check=True)
        for attempt in range(1, 6):
            if subprocess.run(["git", "push"]).returncode == 0:
                print("Pushade data till GitHub.")
                return
            print(f"Push misslyckades (försök {attempt}/5), hämtar ändringar och försöker igen...")
            subprocess.run(["git", "pull", "--rebase", "--autostash"])
            time.sleep(random.randint(1, 5))
        print("Gav upp att pusha den här gången - nästa försök sker om en stund.")
    except Exception as e:  # noqa: BLE001 - får aldrig stoppa mätningen
        print(f"git_sync misslyckades: {e}")


def run_session(now_fn=None, sleep_fn=None, fetch_fn=None, sync_fn=None):
    """Pollar alla bevakningar i ett svep tills alla fönster har stängt.
    Parametrarna finns för att kunna testa med falsk klocka/falska svar."""
    now_fn = now_fn or (lambda: datetime.now(TZ))
    sleep_fn = sleep_fn or time.sleep
    fetch_fn = fetch_fn or fetch_matching_departures
    sync_fn = sync_fn or git_sync

    start = now_fn()
    if WEEKDAYS_ONLY and start.weekday() >= 5:
        print(f"Helg ({start.isoformat()}) - gjorde inget.")
        return

    def window_soon_or_active(now):
        for w in WATCHES:
            if in_monitoring_window(now, w):
                return True
            ws = datetime.combine(now.date(), w["window_start"], tzinfo=TZ)
            if now < ws <= now + timedelta(minutes=PRESTART_WAIT_MINUTES):
                return True
        return False

    if not window_soon_or_active(start):
        print(f"Utanför alla bevakningsfönster ({start.isoformat()}) - gjorde inget.")
        return

    site_ids = {}
    touched = set()   # (datum, bevakningsnyckel)
    watch_by_key = {w["key"]: w for w in WATCHES}
    last_sync = start
    polls = 0

    def finalize():
        for date_str, key in sorted(touched):
            rewrite_summary_for_date(date_str, watch_by_key[key])
        sync_fn()

    while True:
        now = now_fn()
        if (now - start).total_seconds() > MAX_RUNTIME_SECONDS:
            print("Nådde maxtid - avslutar.")
            break

        active = [w for w in WATCHES if in_monitoring_window(now, w)]
        if not active:
            if window_soon_or_active(now):
                sleep_fn(30)   # väntar in nästa fönster
                continue
            break              # alla fönster är klara

        imminent = False
        for watch in active:
            key = watch["key"]
            try:
                if key not in site_ids:
                    site_ids[key] = get_site_id(watch["stop_name"])
                departures = fetch_fn(site_ids[key], watch)
            except RuntimeError as e:
                print(f"[{key}] Konfigurationsfel: {e}", file=sys.stderr)
                finalize()
                sys.exit(1)
            except (OSError, ValueError) as e:   # nätverk, HTTP, trasig JSON
                print(f"[{key}] Kunde inte hämta data från SL just nu, hoppar över: {e}")
                continue

            append_raw_rows(now, departures, key)
            touched.add((now.strftime("%Y-%m-%d"), key))
            polls += 1
            if has_imminent_departure(now, departures):
                imminent = True

        if (now - last_sync).total_seconds() >= COMMIT_EVERY_SECONDS:
            finalize()
            last_sync = now

        sleep_fn(IMMINENT_INTERVAL_SECONDS if imminent else POLL_INTERVAL_SECONDS)

    finalize()
    print(f"Klart: {polls} mätningar gjorda under den här körningen.")


def main():
    run_session()


if __name__ == "__main__":
    main()
