"""
Ouroboros — Tool execution engine.

Handles single tool dispatch, timeout enforcement, parallel execution,
and result processing. Extracted from loop.py to keep the LLM loop thin.
"""

from __future__ import annotations

import json
import logging
import pathlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from ouroboros.tools.registry import ToolRegistry
from ouroboros.utils import (
    utc_now_iso, append_jsonl, truncate_for_log,
    sanitize_tool_args_for_log, sanitize_tool_result_for_log,
)

log = logging.getLogger(__name__)

READ_ONLY_PARALLEL_TOOLS = frozenset({
    "repo_read", "repo_list",
    "drive_read", "drive_list",
    "web_search", "codebase_digest", "chat_history",
})

# Stateful browser tools require thread-affinity (Playwright sync uses greenlet)
STATEFUL_BROWSER_TOOLS = frozenset({"browse_page", "browser_action"})


def _truncate_tool_result(result: Any) -> str:
    """
    Hard-cap tool result string to 15000 characters.
    If truncated, append a note with the original length.
    """
    result_str = str(result)
    if len(result_str) <= 15000:
        return result_str
    original_len = len(result_str)
    return result_str[:15000] + f"\n... (truncated from {original_len} chars)"


def _execute_single_tool(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    task_id: str = "",
) -> Dict[str, Any]:
    """
    Execute a single tool call and return all needed info.

    Returns dict with: tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS

    # Parse arguments
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
    except (json.JSONDecodeError, ValueError) as e:
        result = f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{fn_name}': {e}"
        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": result,
            "is_error": True,
            "args_for_log": {},
            "is_code_tool": is_code_tool,
        }

    args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})

    # Execute tool
    tool_ok = True
    try:
        result = tools.execute(fn_name, args)
    except Exception as e:
        tool_ok = False
        result = f"⚠️ TOOL_ERROR ({fn_name}): {type(e).__name__}: {e}"
        append_jsonl(drive_logs / "events.jsonl", {
            "ts": utc_now_iso(), "type": "tool_error", "task_id": task_id,
            "tool": fn_name, "args": args_for_log, "error": repr(e),
        })

    # Log tool execution (sanitize secrets from result before persisting)
    append_jsonl(drive_logs / "tools.jsonl", {
        "ts": utc_now_iso(), "tool": fn_name, "task_id": task_id,
        "args": args_for_log,
        "result_preview": sanitize_tool_result_for_log(truncate_for_log(result, 2000)),
    })

    is_error = (not tool_ok) or str(result).startswith("⚠️")

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": is_error,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
    }


class _StatefulToolExecutor:
    """
    Thread-sticky executor for stateful tools (browser, etc).

    Playwright sync API uses greenlet internally which has strict thread-affinity:
    once a greenlet starts in a thread, all subsequent calls must happen in the same thread.
    This executor ensures browse_page/browser_action always run in the same thread.

    On timeout: we shutdown the executor and create a fresh one to reset state.
    """
    def __init__(self):
        self._executor: Optional[ThreadPoolExecutor] = None

    def submit(self, fn, *args, **kwargs):
        """Submit work to the sticky thread. Creates executor on first call."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stateful_tool")
        return self._executor.submit(fn, *args, **kwargs)

    def reset(self):
        """Shutdown current executor and create a fresh one. Used after timeout/error."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def shutdown(self, wait=True, cancel_futures=False):
        """Final cleanup."""
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            self._executor = None


def _make_timeout_result(
    fn_name: str,
    tool_call_id: str,
    is_code_tool: bool,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
    reset_msg: str = "",
) -> Dict[str, Any]:
    """
    Create a timeout error result dictionary and log the timeout event.

    Args:
        reset_msg: Optional additional message (e.g., "Browser state has been reset. ")

    Returns: Dict with tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
    """
    args_for_log = {}
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
        args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})
    except Exception:
        pass

    result = (
        f"⚠️ TOOL_TIMEOUT ({fn_name}): exceeded {timeout_sec}s limit. "
        f"The tool is still running in background but control is returned to you. "
        f"{reset_msg}Try a different approach or inform the owner{' about the issue' if not reset_msg else ''}."
    )

    append_jsonl(drive_logs / "events.jsonl", {
        "ts": utc_now_iso(), "type": "tool_timeout",
        "tool": fn_name, "args": args_for_log,
        "timeout_sec": timeout_sec,
    })
    append_jsonl(drive_logs / "tools.jsonl", {
        "ts": utc_now_iso(), "tool": fn_name,
        "args": args_for_log, "result_preview": result,
    })

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": True,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
    }


def _execute_with_timeout(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
    stateful_executor: Optional[_StatefulToolExecutor] = None,
) -> Dict[str, Any]:
    """
    Execute a tool call with a hard timeout.

    On timeout: returns TOOL_TIMEOUT error so the LLM regains control.
    For stateful tools (browser): resets the sticky executor to recover state.
    For regular tools: the hung worker thread leaks as daemon — watchdog handles recovery.
    """
    fn_name = tc["function"]["name"]
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS
    use_stateful = stateful_executor and fn_name in STATEFUL_BROWSER_TOOLS

    # Two distinct paths: stateful (thread-sticky) vs regular (per-call)
    if use_stateful:
        # Stateful executor: submit + wait, reset on timeout
        future = stateful_executor.submit(_execute_single_tool, tools, tc, drive_logs, task_id)
        try:
            return future.result(timeout=timeout_sec)
        except TimeoutError:
            stateful_executor.reset()
            reset_msg = "Browser state has been reset. "
            return _make_timeout_result(
                fn_name, tool_call_id, is_code_tool, tc, drive_logs,
                timeout_sec, task_id, reset_msg
            )
    else:
        # Regular executor: explicit lifecycle to avoid shutdown(wait=True) deadlock
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_execute_single_tool, tools, tc, drive_logs, task_id)
            try:
                return future.result(timeout=timeout_sec)
            except TimeoutError:
                return _make_timeout_result(
                    fn_name, tool_call_id, is_code_tool, tc, drive_logs,
                    timeout_sec, task_id, reset_msg=""
                )
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


def _safe_args(v: Any) -> Any:
    """Ensure args are JSON-serializable for trace logging."""
    try:
        return json.loads(json.dumps(v, ensure_ascii=False, default=str))
    except Exception:
        log.debug("Failed to serialize args for trace logging", exc_info=True)
        return {"_repr": repr(v)}


def _process_tool_results(
    results: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
) -> int:
    """
    Process tool execution results and append to messages/trace.

    Args:
        results: List of tool execution result dicts
        messages: Message list to append tool results to
        llm_trace: Trace dict to append tool call info to
        emit_progress: Callback for progress updates

    Returns:
        Number of errors encountered
    """
    error_count = 0

    for exec_result in results:
        fn_name = exec_result["fn_name"]
        is_error = exec_result["is_error"]

        if is_error:
            error_count += 1

        # Truncate tool result before appending to messages
        truncated_result = _truncate_tool_result(exec_result["result"])

        # Append tool result message
        messages.append({
            "role": "tool",
            "tool_call_id": exec_result["tool_call_id"],
            "content": truncated_result
        })

        # Append to LLM trace
        llm_trace["tool_calls"].append({
            "tool": fn_name,
            "args": _safe_args(exec_result["args_for_log"]),
            "result": truncate_for_log(exec_result["result"], 700),
            "is_error": is_error,
        })

    return error_count


def _handle_tool_calls(
    tool_calls: List[Dict[str, Any]],
    tools: ToolRegistry,
    drive_logs: pathlib.Path,
    task_id: str,
    stateful_executor: _StatefulToolExecutor,
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
) -> int:
    """
    Execute tool calls and append results to messages.

    Returns: Number of errors encountered
    """
    # Parallelize only for a strict read-only whitelist; all calls wrapped with timeout.
    can_parallel = (
        len(tool_calls) > 1 and
        all(
            tc.get("function", {}).get("name") in READ_ONLY_PARALLEL_TOOLS
            for tc in tool_calls
        )
    )

    if not can_parallel:
        results = [
            _execute_with_timeout(tools, tc, drive_logs,
                                  tools.get_timeout(tc["function"]["name"]), task_id,
                                  stateful_executor)
            for tc in tool_calls
        ]
    else:
        max_workers = min(len(tool_calls), 8)
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            future_to_index = {
                executor.submit(
                    _execute_with_timeout, tools, tc, drive_logs,
                    tools.get_timeout(tc["function"]["name"]), task_id,
                    stateful_executor,
                ): idx
                for idx, tc in enumerate(tool_calls)
            }
            results = [None] * len(tool_calls)
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                results[idx] = future.result()
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    # Process results in original order
    return _process_tool_results(results, messages, llm_trace, emit_progress)
