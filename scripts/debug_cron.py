#!/usr/bin/env python3
"""
Cron timezone diagnosis script for Ouroboros.

Prints a full diagnostic report of the cron scheduler state:
- Current time in all relevant timezones
- Each job's schedule, last_run_date, delta from target, window check
- Whether _is_due() would return True right now
- Any detected issues (missing window_minutes, stale last_run_date, etc.)

Usage:
    python scripts/debug_cron.py
    python scripts/debug_cron.py --simulate "2026-02-27 10:05:00"  # simulate a specific time
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore


DRIVE_ROOT = Path("/data")
CRON_FILE = DRIVE_ROOT / "state" / "cron_jobs.json"


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def load_jobs() -> list[dict]:
    if not CRON_FILE.exists():
        print(f"[ERROR] Cron file not found: {CRON_FILE}")
        return []
    return json.loads(CRON_FILE.read_text(encoding="utf-8"))


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z%z")


def diagnose_job(job: dict, simulated_now: datetime | None = None) -> None:
    print("─" * 60)
    job_id = job.get("id", "unknown")
    job_name = job.get("name", "")
    enabled = job.get("enabled", True)
    last_run_date = job.get("last_run_date", "")
    sched = job.get("schedule", {})

    print(f"  Job ID    : {job_id}")
    print(f"  Name      : {job_name}")
    print(f"  Enabled   : {enabled}")
    print(f"  Last run  : {last_run_date!r}  {'⚠️  (never run)' if not last_run_date else ''}")

    if not enabled:
        print("  ⛔  Job is disabled — skipping further checks")
        return

    sched_type = sched.get("type", "daily")
    tz_name = sched.get("timezone", "UTC")
    target_hour = int(sched.get("hour", 9))
    target_minute = int(sched.get("minute", 0))
    window_minutes = sched.get("window_minutes")

    print(f"\n  Schedule:")
    print(f"    type           : {sched_type}")
    print(f"    timezone       : {tz_name}")
    print(f"    time           : {target_hour:02d}:{target_minute:02d}")
    if window_minutes is None:
        print(f"    window_minutes : ⚠️  NOT SET in JSON — will default to 60 in code")
        window_minutes = 60
    else:
        print(f"    window_minutes : {window_minutes}")

    if sched_type != "daily":
        print(f"  ⚠️  Unsupported schedule type '{sched_type}', _is_due() will return False")
        return

    # Resolve timezone
    try:
        tz = ZoneInfo(tz_name)
        print(f"    ZoneInfo       : OK ({tz})")
    except Exception as e:
        print(f"    ZoneInfo       : ❌ FAILED — {e} — falling back to UTC")
        tz = ZoneInfo("UTC")

    # Current time
    if simulated_now is not None:
        now_utc = simulated_now.astimezone(timezone.utc)
    else:
        now_utc = datetime.now(timezone.utc)

    now_tz = now_utc.astimezone(tz)
    today_str = now_tz.date().isoformat()

    print(f"\n  Time context:")
    print(f"    UTC now        : {fmt_dt(now_utc)}")
    print(f"    Local now ({tz_name[:16]:<16}): {fmt_dt(now_tz)}")
    print(f"    Today (local)  : {today_str}")

    # Build target_today
    target_today = now_tz.replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0
    )
    delta_seconds = (now_tz - target_today).total_seconds()
    delta_minutes = delta_seconds / 60

    print(f"\n  Delta from target ({target_hour:02d}:{target_minute:02d} {tz_name}):")
    print(f"    target_today   : {fmt_dt(target_today)}")
    print(f"    delta_seconds  : {delta_seconds:.1f}s  ({delta_minutes:.1f} min)")

    # Window check
    window_ok = 0 <= delta_seconds < window_minutes * 60
    if delta_seconds < 0:
        time_until = -delta_minutes
        print(f"    window check   : ❌ TOO EARLY — target is {time_until:.1f} min in the FUTURE")
    elif delta_seconds >= window_minutes * 60:
        past_window = delta_minutes - window_minutes
        print(f"    window check   : ❌ WINDOW EXPIRED — {past_window:.1f} min past the {window_minutes}min window")
    else:
        print(f"    window check   : ✅ IN WINDOW — {delta_minutes:.1f} / {window_minutes} min elapsed")

    # Last-run check
    last_run_ok = last_run_date != today_str
    if not last_run_ok:
        print(f"    last_run check : ❌ ALREADY RAN today ({last_run_date})")
    else:
        print(f"    last_run check : ✅ NOT YET RUN today (last={last_run_date!r}, today={today_str})")

    # Final verdict
    would_fire = window_ok and last_run_ok
    verdict = "✅ WOULD FIRE" if would_fire else "⛔ WOULD NOT FIRE"
    print(f"\n  _is_due() result : {verdict}")

    # Summarise issues
    issues = []
    if "window_minutes" not in sched:
        issues.append("window_minutes missing from cron_jobs.json (using code default=60)")
    if not would_fire and enabled:
        if not window_ok:
            issues.append("outside the catch-up window")
        if not last_run_ok:
            issues.append("already ran today")
    if issues:
        print(f"\n  ⚠️  Issues detected:")
        for issue in issues:
            print(f"     • {issue}")
    else:
        print(f"\n  No issues detected.")


def main():
    parser = argparse.ArgumentParser(description="Diagnose Ouroboros cron timezone logic")
    parser.add_argument(
        "--simulate",
        metavar="DATETIME",
        help='Simulate a specific UTC datetime, e.g. "2026-02-27 10:05:00"',
    )
    args = parser.parse_args()

    simulated_now = None
    if args.simulate:
        try:
            simulated_now = datetime.fromisoformat(args.simulate).replace(tzinfo=timezone.utc)
            print(f"[SIMULATION MODE] Using time: {fmt_dt(simulated_now)}\n")
        except ValueError as e:
            print(f"[ERROR] Bad --simulate value: {e}")
            sys.exit(1)

    print("=" * 60)
    print("  OUROBOROS CRON DIAGNOSIS")
    print("=" * 60)

    # Real time info
    now_utc = simulated_now or datetime.now(timezone.utc)
    print(f"\n  Real UTC now: {fmt_dt(datetime.now(timezone.utc))}")
    print(f"  Moscow time : {fmt_dt(datetime.now(timezone.utc).astimezone(ZoneInfo('Europe/Moscow')))}")

    jobs = load_jobs()
    if not jobs:
        print("\n  No jobs found.")
        return

    print(f"\n  Jobs loaded: {len(jobs)}")
    for job in jobs:
        diagnose_job(job, simulated_now=simulated_now)

    print("\n" + "=" * 60)
    print("  Diagnosis complete.")
    print("=" * 60)

    # Quick fix hint
    any_missing_window = any(
        "window_minutes" not in job.get("schedule", {})
        for job in jobs
        if job.get("enabled", True)
    )
    if any_missing_window:
        print("""
  💡 FIX HINT: Add window_minutes to cron_jobs.json schedules:
     "schedule": { ..., "window_minutes": 60 }
  This makes the JSON self-documenting and avoids relying on code defaults.
""")


if __name__ == "__main__":
    main()
