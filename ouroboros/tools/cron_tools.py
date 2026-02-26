"""Cron management tools — LLM-accessible interface to supervisor/cron.py."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)


def _list_cron_jobs(ctx: ToolContext) -> str:
    """List all scheduled cron jobs."""
    from supervisor.cron import list_jobs
    jobs = list_jobs(ctx.drive_root)
    if not jobs:
        return "No cron jobs configured."
    lines = ["**Scheduled jobs:**\n"]
    for j in jobs:
        sched = j.get("schedule", {})
        tz = sched.get("timezone", "UTC")
        hour = sched.get("hour", "?")
        minute = sched.get("minute", 0)
        enabled = "✅" if j.get("enabled", True) else "❌"
        last_run = j.get("last_run_date") or "never"
        lines.append(
            f"{enabled} **{j['id']}** — {j.get('name', '')}\n"
            f"   Schedule: daily {hour:02}:{minute:02d} {tz}\n"
            f"   Last run: {last_run}\n"
            f"   Task: {j.get('task_text', '')[:100]}...\n"
        )
    return "\n".join(lines)


def _add_cron_job(
    ctx: ToolContext,
    job_id: str,
    name: str,
    task_text: str,
    hour: int = 9,
    minute: int = 0,
    timezone: str = "Europe/Moscow",
    enabled: bool = True,
) -> str:
    """Add or update a cron job."""
    from supervisor.cron import add_job
    job = {
        "id": job_id,
        "name": name,
        "schedule": {"type": "daily", "hour": hour, "minute": minute, "timezone": timezone},
        "task_text": task_text,
        "enabled": enabled,
        "last_run_date": "",
    }
    return add_job(ctx.drive_root, job)


def _remove_cron_job(ctx: ToolContext, job_id: str) -> str:
    """Remove a cron job by ID."""
    from supervisor.cron import remove_job
    return remove_job(ctx.drive_root, job_id)


def _enable_cron_job(ctx: ToolContext, job_id: str) -> str:
    """Enable a cron job."""
    from supervisor.cron import set_job_enabled
    return set_job_enabled(ctx.drive_root, job_id, True)


def _disable_cron_job(ctx: ToolContext, job_id: str) -> str:
    """Disable a cron job without removing it."""
    from supervisor.cron import set_job_enabled
    return set_job_enabled(ctx.drive_root, job_id, False)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("list_cron_jobs", {
            "name": "list_cron_jobs",
            "description": "List all scheduled cron jobs (periodic tasks).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }, _list_cron_jobs),
        ToolEntry("add_cron_job", {
            "name": "add_cron_job",
            "description": "Add or update a recurring cron job that fires at a specific time daily.",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Unique job identifier (snake_case)"},
                    "name": {"type": "string", "description": "Human-readable name"},
                    "task_text": {"type": "string", "description": "Task description sent to the agent when job fires"},
                    "hour": {"type": "integer", "description": "Hour to fire (0-23), default 9"},
                    "minute": {"type": "integer", "description": "Minute to fire (0-59), default 0"},
                    "timezone": {"type": "string", "description": "Timezone name, e.g. 'Europe/Moscow'"},
                    "enabled": {"type": "boolean", "description": "Whether job is active"},
                },
                "required": ["job_id", "name", "task_text"],
            },
        }, _add_cron_job),
        ToolEntry("remove_cron_job", {
            "name": "remove_cron_job",
            "description": "Remove a cron job permanently.",
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "Job ID to remove"}},
                "required": ["job_id"],
            },
        }, _remove_cron_job),
        ToolEntry("enable_cron_job", {
            "name": "enable_cron_job",
            "description": "Enable a disabled cron job.",
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "Job ID to enable"}},
                "required": ["job_id"],
            },
        }, _enable_cron_job),
        ToolEntry("disable_cron_job", {
            "name": "disable_cron_job",
            "description": "Disable a cron job without removing it.",
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "Job ID to disable"}},
                "required": ["job_id"],
            },
        }, _disable_cron_job),
    ]
