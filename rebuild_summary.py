#!/usr/bin/env python3
"""
Bygger om summary.csv för ALLA bevakningar (se WATCHES i monitor.py) helt
från grunden, baserat på filerna i data/<nyckel>/raw/ och den senaste
klassificeringslogiken.

Använd den här när logiken i monitor.py har ändrats och du vill att hela
historiken ska vara beräknad på samma, senaste sätt.

Kör lokalt: python rebuild_summary.py
Kör i GitHub: via workflowen "Bygg om sammanfattning" (manuell knapp).
"""

import csv

from monitor import WATCHES, SUMMARY_FIELDNAMES, raw_dir_for, summary_file_for, classify_day


def rebuild_watch(watch: dict):
    raw_files = sorted(raw_dir_for(watch["key"]).glob("*.csv"))
    all_rows = []
    for raw_file in raw_files:
        date_str = raw_file.stem
        all_rows.extend(classify_day(date_str, watch))

    summary_file = summary_file_for(watch["key"])
    summary_file.parent.mkdir(parents=True, exist_ok=True)
    with summary_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"[{watch['key']}] Byggde om summary.csv: {len(all_rows)} rader från {len(raw_files)} dagar.")


def main():
    for watch in WATCHES:
        rebuild_watch(watch)


if __name__ == "__main__":
    main()
