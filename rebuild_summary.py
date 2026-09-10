#!/usr/bin/env python3
"""
Bygger om data/summary.csv helt från grunden, baserat på ALLA filer i
data/raw/ och den senaste klassificeringslogiken i monitor.py.

Använd den här när logiken i monitor.py har ändrats och du vill att hela
historiken ska vara beräknad på samma, senaste sätt - istället för att
manuellt radera eller välja bort enskilda datum.

Kör lokalt: python rebuild_summary.py
Kör i GitHub: via workflowen "Bygg om sammanfattning" (manuell knapp).
"""

import csv

from monitor import RAW_DIR, SUMMARY_FILE, classify_day

FIELDNAMES = [
    "date", "scheduled", "first_seen", "last_seen",
    "last_state", "outcome", "physically_confirmed",
]


def main():
    raw_files = sorted(RAW_DIR.glob("*.csv"))
    all_rows = []
    for raw_file in raw_files:
        date_str = raw_file.stem  # t.ex. "2026-09-09" från "2026-09-09.csv"
        all_rows.extend(classify_day(date_str))

    with SUMMARY_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"Byggde om summary.csv: {len(all_rows)} rader från {len(raw_files)} dagar.")


if __name__ == "__main__":
    main()
