"""Persistent cron scheduler for Ouroboros.

Jobs are stored in /data/state/cron_jobs.json.
Supports daily jobs with timezone-aware scheduling.
The launcher calls check_and_fire() periodically (every 60s) to trigger due jobs.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

log = logging.getLogger(__name__)

_CRON_FILE = "state/cron_jobs.json"
_lock = threading.Lock()

# Type alias for enqueue callback
EnqueueFn = Callable[[str, Optional[int]], None]


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _default_jobs() -> List[Dict]:
    """Return the default set of cron jobs (pre-configured)."""
    return [
        {
            "id": "moex_morning_digest",
            "name": "MOEX Morning Digest",
            "schedule": {"type": "daily", "hour": 10, "minute": 0, "timezone": "Europe/Moscow", "window_minutes": 60},
            "task_text": (
                "Run MOEX morning digest: call get_moex_digest tool to fetch market data, "
                "then search for today's top financial news about Russian market (web_search), "
                "then compose a complete morning briefing with: "
                "1) Market indices (IMOEX, RTSI), "
                "2) Top stocks by volume with % changes, "
                "3) Top gainers and losers, "
                "4) Key news headlines (3-5 items), "
                "5) Any important economic events today (ЦБ РФ, дивиденды, отчёты). "
                "Send the complete digest to the owner via send_owner_message."
            ),
            "enabled": True,
            "last_run_date": "",
        }
    ]


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _load_jobs(drive_root: Path) -> List[Dict]:
    path = drive_root / _CRON_FILE
    if not path.exists():
        jobs = _default_jobs()
        _save_jobs(drive_root, jobs)
        return jobs
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.warning("Failed to load cron jobs, resetting to defaults", exc_info=True)
        jobs = _default_jobs()
        _save_jobs(drive_root, jobs)
        return jobs


def _save_jobs(drive_root: Path, jobs: List[Dict]) -> None:
    path = drive_root / _CRON_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def _is_due(job: Dict) -> bool:
    """Check if a job should fire right now.

    Uses a catch-up window: if the system was offline at the scheduled time,
    the job will still fire within `window_minutes` of the scheduled time
    (default: 60 minutes), as long as it hasn't run today yet.
    """
    if not job.get("enabled", True):
        return False

    sched = job.get("schedule", {})
    sched_type = sched.get("type", "daily")

    if sched_type != "daily":
        log.debug("Unsupported schedule type: %s", sched_type)
        return False

    tz_name = sched.get("timezone", "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        log.warning("Unknown timezone: %s, falling back to UTC", tz_name)
        tz = ZoneInfo("UTC")

    now_tz = datetime.now(tz)
    target_hour = int(sched.get("hour", 9))
    target_minute = int(sched.get("minute", 0))
    window_minutes = int(sched.get("window_minutes", 60))

    # Build target datetime for today in the job's timezone
    target_today = now_tz.replace(
        hour=target_hour, minute=target_minute, second=0, microsecond=0
    )

    # Calculate how many minutes past the target time we currently are
    delta_seconds = (now_tz - target_today).total_seconds()

    # Must be within [0, window_minutes) of the target — i.e. target has passed
    # but catch-up window is still open. Negative delta means target is in the
    # future (too early); delta >= window means we're past the catch-up window.
    if not (0 <= delta_seconds < window_minutes * 60):
        return False

    # Check: hasn't run today yet
    today_str = now_tz.date().isoformat()
    last_run = job.get("last_run_date", "")
    if last_run == today_str:
        return False

    return True


def check_and_fire(drive_root: Path, enqueue_fn: EnqueueFn) -> List[str]:
    """Check all jobs and fire any that are due.

    Args:
        drive_root: Path to /data
        enqueue_fn: Callable(task_text, chat_id) to enqueue a task

    Returns:
        List of job IDs that were fired.
    """
    fired = []
    with _lock:
        jobs = _load_jobs(drive_root)
        modified = False

        for job in jobs:
            if _is_due(job):
                job_id = job.get("id", "unknown")
                task_text = job.get("task_text", "")
                log.info("Cron job firing: %s (%s)", job_id, job.get("name", ""))

                try:
                    enqueue_fn(task_text, None)

                    # Update last_run_date
                    tz_name = job.get("schedule", {}).get("timezone", "UTC")
                    try:
                        tz = ZoneInfo(tz_name)
                    except Exception:
                        tz = ZoneInfo("UTC")
                    job["last_run_date"] = datetime.now(tz).date().isoformat()
                    modified = True
                    fired.append(job_id)
                    log.info("Cron job enqueued: %s", job_id)
                except Exception:
                    log.error("Failed to enqueue cron job %s", job_id, exc_info=True)

        if modified:
            _save_jobs(drive_root, jobs)

    return fired


# ---------------------------------------------------------------------------
# Management API (for LLM tools)
# ---------------------------------------------------------------------------

def list_jobs(drive_root: Path) -> List[Dict]:
    with _lock:
        return _load_jobs(drive_root)


def add_job(drive_root: Path, job: Dict) -> str:
    """Add or replace a cron job (matched by id)."""
    with _lock:
        jobs = _load_jobs(drive_root)
        existing_ids = {j["id"] for j in jobs}
        if job["id"] in existing_ids:
            jobs = [j if j["id"] != job["id"] else job for j in jobs]
            action = "updated"
        else:
            jobs.append(job)
            action = "added"
        _save_jobs(drive_root, jobs)
    return f"Job '{job['id']}' {action}"


def remove_job(drive_root: Path, job_id: str) -> str:
    with _lock:
        jobs = _load_jobs(drive_root)
        before = len(jobs)
        jobs = [j for j in jobs if j.get("id") != job_id]
        if len(jobs) == before:
            return f"Job '{job_id}' not found"
        _save_jobs(drive_root, jobs)
    return f"Job '{job_id}' removed"


def set_job_enabled(drive_root: Path, job_id: str, enabled: bool) -> str:
    with _lock:
        jobs = _load_jobs(drive_root)
        for job in jobs:
            if job.get("id") == job_id:
                job["enabled"] = enabled
                _save_jobs(drive_root, jobs)
                state = "enabled" if enabled else "disabled"
                return f"Job '{job_id}' {state}"
    return f"Job '{job_id}' not found"


# ---------------------------------------------------------------------------
# Background thread
# ---------------------------------------------------------------------------

def start_cron_thread(drive_root: Path, enqueue_fn: EnqueueFn, interval_sec: int = 60) -> threading.Thread:
    """Start a daemon thread that checks cron jobs every interval_sec seconds."""
    def _loop():
        log.info("Cron thread started (interval=%ds)", interval_sec)
        while True:
            try:
                fired = check_and_fire(drive_root, enqueue_fn)
                if fired:
                    log.info("Cron fired jobs: %s", fired)
            except Exception:
                log.error("Cron check_and_fire error", exc_info=True)
            import time
            time.sleep(interval_sec)

    t = threading.Thread(target=_loop, daemon=True, name="cron-scheduler")
    t.start()
    return t
