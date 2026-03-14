"""
ouroboros/health.py — System health checks and startup verification.

Extracted from agent.py to keep agent as a thin orchestrator.
All functions are standalone (no agent instance required).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import subprocess
import time
import threading
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

from ouroboros.utils import (
    utc_now_iso, read_text, append_jsonl, get_git_info, get_budget_remaining,
)

# ---------------------------------------------------------------------------
# Module-level guard for one-time worker boot logging
# ---------------------------------------------------------------------------
_worker_boot_logged = False
_worker_boot_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_uncommitted_changes(repo_dir: pathlib.Path, branch_dev: str) -> Tuple[dict, int]:
    """Check for uncommitted changes and attempt auto-rescue commit & push."""
    # Remove stale index.lock (race condition when multiple workers start)
    lock_path = repo_dir / ".git" / "index.lock"
    if lock_path.exists():
        try:
            lock_age = time.time() - lock_path.stat().st_mtime
            if lock_age > 30:  # stale if older than 30s
                lock_path.unlink(missing_ok=True)
                log.warning(f"Removed stale .git/index.lock (age={lock_age:.0f}s)")
            else:
                # Another process is actively using git — skip
                return {"status": "ok", "note": "index.lock held by another process"}, 0
        except Exception:
            pass
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=10, check=True
        )
        dirty_files = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
        if dirty_files:
            # Auto-rescue: commit and push
            auto_committed = False
            try:
                # Stage all changes (tracked + untracked init files)
                subprocess.run(["git", "add", "-A"], cwd=str(repo_dir), timeout=10, check=True)
                subprocess.run(
                    ["git", "commit", "-m", "auto-rescue: uncommitted changes detected on startup"],
                    cwd=str(repo_dir), timeout=30, check=True
                )
                # Validate branch name
                if not re.match(r'^[a-zA-Z0-9_/-]+$', branch_dev):
                    raise ValueError(f"Invalid branch name: {branch_dev}")
                # Pull with rebase before push
                subprocess.run(
                    ["git", "pull", "--rebase", "origin", branch_dev],
                    cwd=str(repo_dir), timeout=60, check=True
                )
                # Push
                try:
                    subprocess.run(
                        ["git", "push", "origin", branch_dev],
                        cwd=str(repo_dir), timeout=60, check=True
                    )
                    auto_committed = True
                    log.warning(f"Auto-rescued {len(dirty_files)} uncommitted files on startup")
                except subprocess.CalledProcessError:
                    # If push fails, undo the commit
                    subprocess.run(
                        ["git", "reset", "HEAD~1"],
                        cwd=str(repo_dir), timeout=10, check=True
                    )
                    raise
            except Exception as e:
                log.warning(f"Failed to auto-rescue uncommitted changes: {e}", exc_info=True)
            return {
                "status": "warning", "files": dirty_files[:20],
                "auto_committed": auto_committed,
            }, 1
        else:
            return {"status": "ok"}, 0
    except Exception as e:
        return {"status": "error", "error": str(e)}, 0


def check_version_sync(repo_dir: pathlib.Path) -> Tuple[dict, int]:
    """Check VERSION file sync with git tags and README.md."""
    try:
        from ouroboros.utils import safe_relpath

        def repo_path(rel: str) -> pathlib.Path:
            return (repo_dir / safe_relpath(rel)).resolve()

        version_file = read_text(repo_path("VERSION")).strip()
        issue_count = 0
        result_data: Dict[str, Any] = {"version_file": version_file}

        # Check pyproject.toml version
        pyproject_path = repo_path("pyproject.toml")
        pyproject_content = read_text(pyproject_path)
        match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pyproject_content, re.MULTILINE)
        if match:
            pyproject_version = match.group(1)
            result_data["pyproject_version"] = pyproject_version
            if version_file != pyproject_version:
                result_data["status"] = "warning"
                issue_count += 1

        # Check README.md version (Bible P15: VERSION == README version)
        try:
            readme_content = read_text(repo_path("README.md"))
            readme_match = re.search(r'\*\*Version:\*\*\s*(\d+\.\d+\.\d+)', readme_content)
            if readme_match:
                readme_version = readme_match.group(1)
                result_data["readme_version"] = readme_version
                if version_file != readme_version:
                    result_data["status"] = "warning"
                    issue_count += 1
        except Exception:
            log.debug("Failed to check README.md version", exc_info=True)

        # Check git tags
        result = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=str(repo_dir),
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            result_data["status"] = "warning"
            result_data["message"] = "no_tags"
            return result_data, issue_count
        else:
            latest_tag = result.stdout.strip().lstrip('v')
            result_data["latest_tag"] = latest_tag
            if version_file != latest_tag:
                result_data["status"] = "warning"
                issue_count += 1

        if issue_count == 0:
            result_data["status"] = "ok"

        return result_data, issue_count
    except Exception as e:
        return {"status": "error", "error": str(e)}, 0


def check_budget(drive_root: pathlib.Path) -> Tuple[dict, int]:
    """Check budget remaining with warning thresholds (OpenRouter SSOT)."""
    try:
        state_path = drive_root / "state" / "state.json"
        state_data = json.loads(read_text(state_path))

        remaining = get_budget_remaining(state_data)
        if remaining is None:
            return {"status": "unconfigured"}, 0
        or_limit = state_data.get("openrouter_limit")
        total = float(or_limit) if or_limit is not None else remaining
        spent = total - remaining

        if remaining < 10:
            status = "emergency"
            issues = 1
        elif remaining < 50:
            status = "critical"
            issues = 1
        elif remaining < 100:
            status = "warning"
            issues = 0
        else:
            status = "ok"
            issues = 0

        return {
            "status": status,
            "remaining_usd": round(remaining, 2),
            "total_usd": round(total, 2),
            "spent_usd": round(spent, 2),
        }, issues
    except Exception as e:
        return {"status": "error", "error": str(e)}, 0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def verify_system_state(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    branch_dev: str,
    git_sha: str,
) -> None:
    """Bible Principle 1: verify system state on every startup.

    Checks:
    - Uncommitted changes (auto-rescue commit & push)
    - VERSION file sync with git tags
    - Budget remaining (warning thresholds)
    """
    checks: Dict[str, Any] = {}
    issues = 0
    drive_logs = drive_root / "logs"

    # 1. Uncommitted changes
    checks["uncommitted_changes"], issue_count = check_uncommitted_changes(repo_dir, branch_dev)
    issues += issue_count

    # 2. VERSION vs git tag
    checks["version_sync"], issue_count = check_version_sync(repo_dir)
    issues += issue_count

    # 3. Budget check
    checks["budget"], issue_count = check_budget(drive_root)
    issues += issue_count

    # Log verification result
    event = {
        "ts": utc_now_iso(),
        "type": "startup_verification",
        "checks": checks,
        "issues_count": issues,
        "git_sha": git_sha,
    }
    append_jsonl(drive_logs / "events.jsonl", event)

    if issues > 0:
        log.warning(f"Startup verification found {issues} issue(s): {checks}")


def verify_restart(drive_root: pathlib.Path, git_sha: str) -> None:
    """Best-effort restart verification — checks pending_restart_verify.json."""
    try:
        pending_path = drive_root / "state" / "pending_restart_verify.json"
        claim_path = pending_path.with_name(
            f"pending_restart_verify.claimed.{os.getpid()}.json"
        )
        try:
            os.rename(str(pending_path), str(claim_path))
        except (FileNotFoundError, Exception):
            return
        try:
            claim_data = json.loads(read_text(claim_path))
            expected_sha = str(claim_data.get("expected_sha", "")).strip()
            ok = bool(expected_sha and expected_sha == git_sha)
            append_jsonl(drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(), "type": "restart_verify",
                "pid": os.getpid(), "ok": ok,
                "expected_sha": expected_sha, "observed_sha": git_sha,
            })
        except Exception:
            log.debug("Failed to log restart verify event", exc_info=True)
        try:
            claim_path.unlink()
        except Exception:
            log.debug("Failed to delete restart verify claim file", exc_info=True)
    except Exception:
        log.debug("Restart verification failed", exc_info=True)


def get_runtime_health_status(env: Any) -> str:
    """Compute runtime health invariants for LLM context.

    Returns a multi-line string with one status line per check.
    Surfaces anomalies as informational text. The LLM (not code) decides
    what action to take based on what it reads here.
    """
    checks = []

    # 1. Version sync: VERSION file vs pyproject.toml
    try:
        ver_file = read_text(env.repo_path("VERSION")).strip()
        pyproject_text = read_text(env.repo_path("pyproject.toml"))
        pyproject_ver = ""
        for line in pyproject_text.splitlines():
            if line.strip().startswith("version"):
                pyproject_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
        if ver_file and pyproject_ver and ver_file != pyproject_ver:
            checks.append(f"CRITICAL: VERSION DESYNC — VERSION={ver_file}, pyproject.toml={pyproject_ver}")
        elif ver_file:
            checks.append(f"OK: version sync ({ver_file})")
    except Exception:
        pass

    # 2. Budget remaining (OpenRouter ground truth)
    try:
        state_text = read_text(env.drive_path("state/state.json"))
        state_data = json.loads(state_text)
        remaining = get_budget_remaining(state_data)
        if remaining is not None:
            if remaining < 10:
                checks.append(f"CRITICAL: LOW BUDGET — remaining=${remaining:.2f}")
            elif remaining < 50:
                checks.append(f"WARNING: LOW BUDGET — remaining=${remaining:.2f}")
            else:
                checks.append(f"OK: budget remaining=${remaining:.2f}")
        else:
            checks.append("OK: budget (not yet fetched)")
    except Exception:
        pass

    # 3. Per-task cost anomalies
    try:
        from supervisor.state import per_task_cost_summary
        costly = [t for t in per_task_cost_summary(5) if t["cost"] > 5.0]
        for t in costly:
            checks.append(
                f"WARNING: HIGH-COST TASK — task_id={t['task_id']} "
                f"cost=${t['cost']:.2f} rounds={t['rounds']}"
            )
        if not costly:
            checks.append("OK: no high-cost tasks (>$5)")
    except Exception:
        pass

    # 4. Stale identity.md
    try:
        identity_path = pathlib.Path(str(env.drive_path("memory/identity.md")))
        if identity_path.exists():
            age_hours = (time.time() - identity_path.stat().st_mtime) / 3600
            if age_hours > 8:
                checks.append(f"WARNING: STALE IDENTITY — identity.md last updated {age_hours:.0f}h ago")
            else:
                checks.append("OK: identity.md recent")
    except Exception:
        pass

    # 5. Duplicate processing detection
    try:
        msg_hash_to_tasks: dict = {}
        tail_bytes = 256_000

        def _scan_file_for_injected(path, type_field="type", type_value="owner_message_injected"):
            path = pathlib.Path(str(path))
            if not path.exists():
                return
            file_size = path.stat().st_size
            with path.open("r", encoding="utf-8") as f:
                if file_size > tail_bytes:
                    f.seek(file_size - tail_bytes)
                    f.readline()
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        if ev.get(type_field) != type_value:
                            continue
                        text = ev.get("text", "")
                        if not text and "event_repr" in ev:
                            text = ev.get("event_repr", "")[:200]
                        if not text:
                            continue
                        text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
                        tid = ev.get("task_id") or "unknown"
                        if text_hash not in msg_hash_to_tasks:
                            msg_hash_to_tasks[text_hash] = set()
                        msg_hash_to_tasks[text_hash].add(tid)
                    except (json.JSONDecodeError, ValueError):
                        continue

        _scan_file_for_injected(env.drive_path("logs/events.jsonl"))
        _scan_file_for_injected(
            env.drive_path("logs/supervisor.jsonl"),
            type_field="event_type",
            type_value="owner_message_injected",
        )

        duplicates = {h: tids for h, tids in msg_hash_to_tasks.items() if len(tids) > 1}
        if duplicates:
            checks.append(f"CRITICAL: DUPLICATE PROCESSING — {len(duplicates)} message(s) processed by multiple tasks")
        else:
            checks.append("OK: no duplicate message processing detected")
    except Exception:
        pass

    return "\n".join(f"- {c}" for c in checks)


def log_worker_boot_once(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    branch_dev: str,
) -> None:
    """Log the worker boot event once per process (module-level guard)."""
    global _worker_boot_logged
    try:
        with _worker_boot_lock:
            if _worker_boot_logged:
                return
            _worker_boot_logged = True
        git_branch, git_sha = get_git_info(repo_dir)
        append_jsonl(drive_root / "logs" / "events.jsonl", {
            "ts": utc_now_iso(), "type": "worker_boot",
            "pid": os.getpid(), "git_branch": git_branch, "git_sha": git_sha,
        })
        verify_restart(drive_root, git_sha)
        verify_system_state(repo_dir, drive_root, branch_dev, git_sha)
    except Exception:
        log.warning("Worker boot logging failed", exc_info=True)
