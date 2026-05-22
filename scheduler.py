#!/usr/bin/env python3
"""Local scheduler — runs triage.py every weekday at 8 AM in your local timezone."""

import subprocess
import sys
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from tzlocal import get_localzone

load_dotenv()

SCRIPT = Path(__file__).parent / "triage.py"


def run_triage() -> None:
    print("Starting morning triage…", flush=True)
    result = subprocess.run([sys.executable, str(SCRIPT)], check=False)
    if result.returncode != 0:
        print(f"triage.py exited with code {result.returncode}", flush=True)


def main() -> None:
    tz = get_localzone()
    scheduler = BlockingScheduler(timezone=tz)
    scheduler.add_job(
        run_triage,
        CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=tz),
    )
    print(f"Scheduler started. Triage will run Mon–Fri at 08:00 {tz}.")
    print("Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\nScheduler stopped.")


if __name__ == "__main__":
    main()
