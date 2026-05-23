#!/usr/bin/env python3
"""
Runs the morning Gmail triage every weekday at 08:00 local time.

Keep this process running continuously (e.g. via systemd, screen, or Docker).
For cron-based scheduling instead, add this line to your crontab:
    0 8 * * 1-5 /usr/bin/python3 /path/to/triage.py
"""

import time
from datetime import datetime

import schedule
from dotenv import load_dotenv

from triage import run_triage

load_dotenv()


def _run_if_weekday() -> None:
    if datetime.now().weekday() < 5:  # Monday=0 … Friday=4
        run_triage()
    else:
        print(f"[{datetime.now().isoformat()}] Weekend — skipping triage.")


schedule.every().day.at("08:00").do(_run_if_weekday)

if __name__ == "__main__":
    print("Scheduler started. Waiting for 08:00 on weekdays…")
    while True:
        schedule.run_pending()
        time.sleep(30)
