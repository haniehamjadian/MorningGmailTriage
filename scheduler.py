#!/usr/bin/env python3
"""
Long-running scheduler: runs triage.run() every weekday at 08:00 local time.

Usage:
    python scheduler.py

To run as a background service (systemd example in README), or use system cron:
    0 8 * * 1-5 cd /path/to/MorningGmailTriage && python triage.py >> triage.log 2>&1
"""
import logging
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def job():
    try:
        from triage import run
        run()
    except Exception as exc:
        log.error("Triage failed: %s", exc, exc_info=True)


if __name__ == "__main__":
    scheduler = BlockingScheduler()
    # Weekdays (Mon–Fri), 08:00 local time
    scheduler.add_job(job, CronTrigger(day_of_week="mon-fri", hour=8, minute=0))
    log.info("Scheduler started — triage runs weekdays at 08:00 local time. Ctrl-C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler stopped.")
        sys.exit(0)
