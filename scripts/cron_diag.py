#!/usr/bin/env python3
"""Cron diagnostics script — prints current state and whether jobs are due."""
import sys, json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, '/app')

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from supervisor.cron import _load_jobs, _is_due, check_and_fire

DRIVE_ROOT = Path('/data')

def main():
    jobs = _load_jobs(DRIVE_ROOT)
    print(f"\n=== Cron Diagnostics @ {datetime.now().isoformat()} ===\n")

    for job in jobs:
        jid = job.get('id')
        sched = job.get('schedule', {})
        tz_name = sched.get('timezone', 'UTC')
        tz = ZoneInfo(tz_name)
        now_tz = datetime.now(tz)

        target_hour = int(sched.get('hour', 9))
        target_minute = int(sched.get('minute', 0))
        window_minutes = int(sched.get('window_minutes', 60))

        target_today = now_tz.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
        delta_seconds = (now_tz - target_today).total_seconds()

        is_due = _is_due(job)

        print(f"Job: {jid}")
        print(f"  Now ({tz_name}): {now_tz.strftime('%H:%M:%S')}")
        print(f"  Target time: {target_hour:02d}:{target_minute:02d}")
        print(f"  Delta seconds: {delta_seconds:.1f} (window: 0 to {window_minutes * 60}s)")
        print(f"  Last run date: {job.get('last_run_date', '')!r}")
        print(f"  Enabled: {job.get('enabled', True)}")
        print(f"  IS DUE: {is_due}")
        print()

    print("=== End ===\n")

if __name__ == '__main__':
    main()
