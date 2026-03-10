"""
Ouroboros agent core — thin orchestrator.

Delegates to: loop.py (LLM tool loop), tools/ (tool schemas/execution),
llm.py (LLM calls), memory.py (scratchpad/identity),
context.py (context building), review.py (code collection/metrics).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import queue
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

from ouroboros.utils import (
    utc_now_iso, read_text, append_jsonl,
    safe_relpath, truncate_for_log,
    get_git_info, sanitize_task_for_event, get_budget_remaining,
)
from ouroboros.llm import LLMClient, add_usage
from ouroboros.tools import ToolRegistry
from ouroboros.tools.registry import ToolContext
from ouroboros.memory import Memory
from ouroboros.context import build_llm_messages
from ouroboros.loop import run_llm_loop
from ouroboros.health import log_worker_boot_once


# ---------------------------------------------------------------------------
# Environment + Paths
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Env:
    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    branch_dev: str = field(default_factory=lambda: os.environ.get("OUROBOROS_BRANCH_PREFIX", "ouroboros"))

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / safe_relpath(rel)).resolve()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class OuroborosAgent:
    """One agent instance per worker process. Mostly stateless; long-term state lives on Drive."""

    def __init__(self, env: Env, event_queue: Any = None):
        self.env = env
        self._pending_events: List[Dict[str, Any]] = []
        self._event_queue: Any = event_queue
        self._current_chat_id: Optional[int] = None
        self._current_task_type: Optional[str] = None

        # Message injection: owner can send messages while agent is busy
        self._incoming_messages: queue.Queue = queue.Queue()
        self._busy = False
        self._last_progress_ts: float = 0.0
        self._task_started_ts: float = 0.0

        # SSOT modules
        self.llm = LLMClient()
        self.tools = ToolRegistry(repo_dir=env.repo_dir, drive_root=env.drive_root)
        self.memory = Memory(drive_root=env.drive_root, repo_dir=env.repo_dir)

        self._log_worker_boot_once()

    def inject_message(self, text: str) -> None:
        """Thread-safe: inject owner message into the active conversation."""
        self._incoming_messages.put(text)

    def _log_worker_boot_once(self) -> None:
        log_worker_boot_once(self.env.repo_dir, self.env.drive_root, self.env.branch_dev)

    # =====================================================================
    # Main entry point
    # =====================================================================

    def _prepare_task_context(self, task: Dict[str, Any]) -> Tuple[ToolContext, List[Dict[str, Any]], Dict[str, Any]]:
        """Set up ToolContext, build messages, return (ctx, messages, cap_info)."""
        drive_logs = self.env.drive_path("logs")
        sanitized_task = sanitize_task_for_event(task, drive_logs)
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_received", "task": sanitized_task})

        # Set tool context for this task
        ctx = ToolContext(
            repo_dir=self.env.repo_dir,
            drive_root=self.env.drive_root,
            branch_dev=self.env.branch_dev,
            pending_events=self._pending_events,
            current_chat_id=self._current_chat_id,
            current_task_type=self._current_task_type,
            emit_progress_fn=self._emit_progress,
            task_depth=int(task.get("depth", 0)),
            is_direct_chat=bool(task.get("_is_direct_chat")),
        )
        self.tools.set_context(ctx)

        # Typing indicator via event queue (no direct Telegram API)
        self._emit_typing_start()

        # --- Build context (delegated to context.py) ---
        messages, cap_info = build_llm_messages(
            env=self.env,
            memory=self.memory,
            task=task,
            review_context_builder=self._build_review_context,
        )

        if cap_info.get("trimmed_sections"):
            try:
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(), "type": "context_soft_cap_trim",
                    "task_id": task.get("id"), **cap_info,
                })
            except Exception:
                log.warning("Failed to log context soft cap trim event", exc_info=True)
                pass

        # Read budget remaining for cost guard (OpenRouter limit_remaining is SSOT)
        budget_remaining = None
        try:
            state_path = self.env.drive_path("state") / "state.json"
            state_data = json.loads(read_text(state_path))
            budget_remaining = get_budget_remaining(state_data)
        except Exception:
            pass

        cap_info["budget_remaining"] = budget_remaining
        return ctx, messages, cap_info

    def handle_task(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        self._busy = True
        start_time = time.time()
        self._task_started_ts = start_time
        self._last_progress_ts = start_time
        self._pending_events = []
        self._current_chat_id = int(task.get("chat_id") or 0) or None
        self._current_task_type = str(task.get("type") or "")

        drive_logs = self.env.drive_path("logs")

        # Guard: skip tasks with empty text to avoid infinite LLM loops
        if not task.get("text", "").strip():
            log.warning("handle_task: skipping task %s — empty text", task.get("id"))
            self._busy = False
            return []

        heartbeat_stop = self._start_task_heartbeat_loop(str(task.get("id") or ""))

        try:
            # --- Prepare task context ---
            ctx, messages, cap_info = self._prepare_task_context(task)
            budget_remaining = cap_info.get("budget_remaining")

            # --- LLM loop (delegated to loop.py) ---
            usage: Dict[str, Any] = {}
            llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}

            # Set initial reasoning effort based on task type
            task_type_str = str(task.get("type") or "").lower()
            if task_type_str in ("evolution", "review"):
                initial_effort = "high"
            else:
                initial_effort = "medium"

            try:
                text, usage, llm_trace = run_llm_loop(
                    messages=messages,
                    tools=self.tools,
                    llm=self.llm,
                    drive_logs=drive_logs,
                    emit_progress=self._emit_progress,
                    incoming_messages=self._incoming_messages,
                    task_type=task_type_str,
                    task_id=str(task.get("id") or ""),
                    budget_remaining_usd=budget_remaining,
                    event_queue=self._event_queue,
                    initial_effort=initial_effort,
                    drive_root=self.env.drive_root,
                )
            except Exception as e:
                tb = traceback.format_exc()
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(), "type": "task_error",
                    "task_id": task.get("id"), "error": repr(e),
                    "traceback": truncate_for_log(tb, 2000),
                })
                text = f"⚠️ Error during processing: {type(e).__name__}: {e}"

            # Empty response guard
            if not isinstance(text, str) or not text.strip():
                text = "⚠️ Model returned an empty response. Try rephrasing your request."

            # Emit events for supervisor
            self._emit_task_results(task, text, usage, llm_trace, start_time, drive_logs)
            return list(self._pending_events)

        finally:
            self._busy = False
            # Clean up browser if it was used during this task
            try:
                from ouroboros.tools.browser import cleanup_browser
                cleanup_browser(self.tools._ctx)
            except Exception:
                log.debug("Failed to cleanup browser", exc_info=True)
                pass
            while not self._incoming_messages.empty():
                try:
                    self._incoming_messages.get_nowait()
                except queue.Empty:
                    break
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            self._current_task_type = None

    # =====================================================================
    # Task result emission
    # =====================================================================

    def _emit_task_results(
        self, task: Dict[str, Any], text: str,
        usage: Dict[str, Any], llm_trace: Dict[str, Any],
        start_time: float, drive_logs: pathlib.Path,
    ) -> None:
        """Emit all end-of-task events to supervisor."""
        # NOTE: per-round llm_usage events are already emitted in loop.py
        # (_emit_llm_usage_event). Do NOT emit an aggregate llm_usage here —
        # that would double-count in update_budget_from_usage.
        # Cost/token summaries are carried by task_metrics and task_done events.

        self._pending_events.append({
            "type": "send_message", "chat_id": task["chat_id"],
            "text": text or "\u200b", "log_text": text or "",
            "format": "markdown",
            "task_id": task.get("id"), "ts": utc_now_iso(),
        })

        duration_sec = round(time.time() - start_time, 3)
        n_tool_calls = len(llm_trace.get("tool_calls", []))
        n_tool_errors = sum(1 for tc in llm_trace.get("tool_calls", [])
                            if isinstance(tc, dict) and tc.get("is_error"))
        try:
            append_jsonl(drive_logs / "events.jsonl", {
                "ts": utc_now_iso(), "type": "task_eval", "ok": True,
                "task_id": task.get("id"), "task_type": task.get("type"),
                "duration_sec": duration_sec,
                "tool_calls": n_tool_calls,
                "tool_errors": n_tool_errors,
                "response_len": len(text),
            })
        except Exception:
            log.warning("Failed to log task eval event", exc_info=True)
            pass

        self._pending_events.append({
            "type": "task_metrics",
            "task_id": task.get("id"), "task_type": task.get("type"),
            "duration_sec": duration_sec,
            "tool_calls": n_tool_calls, "tool_errors": n_tool_errors,
            "cost_usd": round(float(usage.get("cost") or 0), 6),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_rounds": int(usage.get("rounds") or 0),
            "ts": utc_now_iso(),
        })

        self._pending_events.append({
            "type": "task_done",
            "task_id": task.get("id"),
            "task_type": task.get("type"),
            "cost_usd": round(float(usage.get("cost") or 0), 6),
            "total_rounds": int(usage.get("rounds") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "ts": utc_now_iso(),
        })
        append_jsonl(drive_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "task_done",
            "task_id": task.get("id"),
            "task_type": task.get("type"),
            "cost_usd": round(float(usage.get("cost") or 0), 6),
            "total_rounds": int(usage.get("rounds") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
        })

        # Store task result for parent task retrieval
        try:
            results_dir = pathlib.Path(self.env.drive_root) / "task_results"
            results_dir.mkdir(parents=True, exist_ok=True)
            result_data = {
                "task_id": task.get("id"),
                "parent_task_id": task.get("parent_task_id"),
                "status": "completed",
                "result": text[:4000] if text else "",  # Truncate to avoid huge files
                "cost_usd": round(float(usage.get("cost") or 0), 6),
                "total_rounds": int(usage.get("rounds") or 0),
                "ts": utc_now_iso(),
            }
            result_file = results_dir / f"{task.get('id')}.json"
            tmp_file = results_dir / f"{task.get('id')}.json.tmp"
            tmp_file.write_text(json.dumps(result_data, ensure_ascii=False, indent=2))
            os.rename(tmp_file, result_file)
        except Exception as e:
            log.warning("Failed to store task result: %s", e)

    # =====================================================================
    # Review context builder
    # =====================================================================

    def _build_review_context(self) -> str:
        """Collect code snapshot + complexity metrics for review tasks."""
        try:
            from ouroboros.review import collect_sections, compute_complexity_metrics, format_metrics
            sections, stats = collect_sections(self.env.repo_dir, self.env.drive_root)
            metrics = compute_complexity_metrics(sections)

            parts = [
                "## Code Review Context\n",
                format_metrics(metrics),
                f"\nFiles: {stats['files']}, chars: {stats['chars']}\n",
                "\nUse repo_read to inspect specific files. "
                "Use run_shell for tests. Key files below:\n",
            ]

            total_chars = 0
            max_chars = 80_000
            files_added = 0
            for path, content in sections:
                if total_chars >= max_chars:
                    parts.append(f"\n... ({len(sections) - files_added} more files, use repo_read)")
                    break
                preview = content[:2000] if len(content) > 2000 else content
                file_block = f"\n### {path}\n```\n{preview}\n```\n"
                total_chars += len(file_block)
                parts.append(file_block)
                files_added += 1

            return "\n".join(parts)
        except Exception as e:
            return f"## Code Review Context\n\n(Failed to collect: {e})\nUse repo_read and repo_list to inspect code."

    # =====================================================================
    # Event emission helpers
    # =====================================================================

    def _emit_progress(self, text: str) -> None:
        self._last_progress_ts = time.time()
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put({
                "type": "send_message", "chat_id": self._current_chat_id,
                "text": f"💬 {text}", "format": "markdown", "is_progress": True,
                "ts": utc_now_iso(),
            })
        except Exception:
            log.warning("Failed to emit progress event", exc_info=True)
            pass

    def _emit_typing_start(self) -> None:
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put({
                "type": "typing_start", "chat_id": self._current_chat_id,
                "ts": utc_now_iso(),
            })
        except Exception:
            log.warning("Failed to emit typing start event", exc_info=True)
            pass

    def _emit_task_heartbeat(self, task_id: str, phase: str) -> None:
        if self._event_queue is None:
            return
        try:
            self._event_queue.put({
                "type": "task_heartbeat", "task_id": task_id,
                "phase": phase, "ts": utc_now_iso(),
            })
        except Exception:
            log.warning("Failed to emit task heartbeat event", exc_info=True)
            pass

    def _start_task_heartbeat_loop(self, task_id: str) -> Optional[threading.Event]:
        if self._event_queue is None or not task_id.strip():
            return None
        interval = 30
        stop = threading.Event()
        self._emit_task_heartbeat(task_id, "start")

        def _loop() -> None:
            while not stop.wait(interval):
                self._emit_task_heartbeat(task_id, "running")

        threading.Thread(target=_loop, daemon=True).start()
        return stop


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_agent(repo_dir: str, drive_root: str, event_queue: Any = None) -> OuroborosAgent:
    env = Env(repo_dir=pathlib.Path(repo_dir), drive_root=pathlib.Path(drive_root))
    return OuroborosAgent(env, event_queue=event_queue)
