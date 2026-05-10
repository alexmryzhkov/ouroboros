"""
Tool history compaction — compresses old tool call rounds to save tokens.

Extracted from context.py to keep context assembly separate from
history management. Called from loop.py, not from build_llm_messages.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict

from ouroboros.utils import estimate_tokens  # noqa: F401 (imported for potential future use)

log = logging.getLogger(__name__)


def _compact_tool_result(msg: dict, content: str) -> dict:
    """
    Compact a single tool result message.

    Args:
        msg: Original tool result message dict
        content: Content string to compact

    Returns:
        Compacted message dict
    """
    is_error = content.startswith("⚠️")
    # Create a short summary
    if is_error:
        summary = content[:200]  # Keep error details
    else:
        # Keep first line or first 80 chars
        first_line = content.split('\n')[0][:80]
        char_count = len(content)
        summary = f"{first_line}... ({char_count} chars)" if char_count > 80 else content[:200]

    return {**msg, "content": summary}


def _compact_assistant_msg(msg: dict) -> dict:
    """
    Compact assistant message content and tool_call arguments.

    Args:
        msg: Original assistant message dict

    Returns:
        Compacted message dict
    """
    compacted_msg = dict(msg)

    # Trim content (progress notes)
    content = msg.get("content") or ""
    if len(content) > 200:
        content = content[:200] + "..."
    compacted_msg["content"] = content

    # Compact tool_call arguments
    if msg.get("tool_calls"):
        compacted_tool_calls = []
        for tc in msg["tool_calls"]:
            compacted_tc = dict(tc)

            # Always preserve id and function name
            if "function" in compacted_tc:
                func = dict(compacted_tc["function"])
                args_str = func.get("arguments", "")

                if args_str:
                    compacted_tc["function"] = _compact_tool_call_arguments(
                        func["name"], args_str
                    )
                else:
                    compacted_tc["function"] = func

            compacted_tool_calls.append(compacted_tc)

        compacted_msg["tool_calls"] = compacted_tool_calls

    return compacted_msg


def compact_tool_history(messages: list, keep_recent: int = 6) -> list:
    """
    Compress old tool call/result message pairs into compact summaries.

    Keeps the last `keep_recent` tool-call rounds intact (they may be
    referenced by the LLM). Older rounds get their tool results truncated
    to a short summary line, and tool_call arguments are compacted.

    This dramatically reduces prompt tokens in long tool-use conversations
    without losing important context (the tool names and whether they succeeded
    are preserved).
    """
    # Find all indices that are tool-call assistant messages
    # (messages with tool_calls field)
    tool_round_starts = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_round_starts.append(i)

    if len(tool_round_starts) <= keep_recent:
        return messages  # Nothing to compact

    # Rounds to compact: all except the last keep_recent
    rounds_to_compact = set(tool_round_starts[:-keep_recent])

    # Build compacted message list
    result = []
    for i, msg in enumerate(messages):
        # Skip system messages with multipart content (prompt caching format)
        if msg.get("role") == "system" and isinstance(msg.get("content"), list):
            result.append(msg)
            continue

        if msg.get("role") == "tool" and i > 0:
            # Check if the preceding assistant message (with tool_calls)
            # is one we want to compact
            # Find which round this tool result belongs to
            parent_round = None
            for rs in reversed(tool_round_starts):
                if rs < i:
                    parent_round = rs
                    break

            if parent_round is not None and parent_round in rounds_to_compact:
                # Compact this tool result
                content = str(msg.get("content") or "")
                result.append(_compact_tool_result(msg, content))
                continue

        # For compacted assistant messages, also trim the content (progress notes)
        # AND compact tool_call arguments
        if i in rounds_to_compact and msg.get("role") == "assistant":
            result.append(_compact_assistant_msg(msg))
            continue

        result.append(msg)

    return result


def compact_tool_history_llm(messages: list, keep_recent: int = 6) -> list:
    """LLM-driven compaction: summarize old tool results via a light model.

    Falls back to simple truncation (compact_tool_history) on any error.
    Called when the agent explicitly invokes the compact_context tool.
    """
    tool_round_starts = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_round_starts.append(i)

    if len(tool_round_starts) <= keep_recent:
        return messages

    rounds_to_compact = set(tool_round_starts[:-keep_recent])

    old_results = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "tool" or i == 0:
            continue
        parent_round = None
        for rs in reversed(tool_round_starts):
            if rs < i:
                parent_round = rs
                break
        if parent_round is not None and parent_round in rounds_to_compact:
            content = str(msg.get("content") or "")
            if len(content) > 120:
                tool_call_id = msg.get("tool_call_id", "")
                old_results.append({"idx": i, "tool_call_id": tool_call_id, "content": content[:1500]})

    if not old_results:
        return compact_tool_history(messages, keep_recent=keep_recent)

    batch_text = "\n---\n".join(
        f"[{r['tool_call_id']}]\n{r['content']}" for r in old_results[:20]
    )
    prompt = (
        "Summarize each tool result below into 1-2 lines of key facts. "
        "Preserve errors, file paths, and important values. "
        "Output one summary per [id] block, same order.\n\n" + batch_text
    )

    try:
        from ouroboros.llm import LLMClient, DEFAULT_LIGHT_MODEL
        light_model = os.environ.get("OUROBOROS_MODEL_LIGHT") or DEFAULT_LIGHT_MODEL
        client = LLMClient()
        resp_msg, _usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=light_model,
            reasoning_effort="low",
            max_tokens=1024,
        )
        summary_text = resp_msg.get("content") or ""
        if not summary_text.strip():
            raise ValueError("empty summary response")
    except Exception:
        log.warning("LLM compaction failed, falling back to truncation", exc_info=True)
        return compact_tool_history(messages, keep_recent=keep_recent)

    summary_lines = summary_text.strip().split("\n")
    summary_map: Dict[str, str] = {}
    current_id = None
    current_lines: list = []
    for line in summary_lines:
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            if current_id is not None:
                summary_map[current_id] = " ".join(current_lines).strip()
            bracket_end = stripped.index("]")
            current_id = stripped[1:bracket_end]
            rest = stripped[bracket_end + 1:].strip()
            current_lines = [rest] if rest else []
        elif current_id is not None:
            current_lines.append(stripped)
    if current_id is not None:
        summary_map[current_id] = " ".join(current_lines).strip()

    idx_to_summary = {}
    for r in old_results:
        s = summary_map.get(r["tool_call_id"])
        if s:
            idx_to_summary[r["idx"]] = s

    result = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "system" and isinstance(msg.get("content"), list):
            result.append(msg)
            continue
        if i in idx_to_summary:
            result.append({**msg, "content": idx_to_summary[i]})
            continue
        if msg.get("role") == "tool" and i > 0:
            parent_round = None
            for rs in reversed(tool_round_starts):
                if rs < i:
                    parent_round = rs
                    break
            if parent_round is not None and parent_round in rounds_to_compact:
                content = str(msg.get("content") or "")
                result.append(_compact_tool_result(msg, content))
                continue
        if i in rounds_to_compact and msg.get("role") == "assistant":
            result.append(_compact_assistant_msg(msg))
            continue
        result.append(msg)

    return result


def _compact_tool_call_arguments(tool_name: str, args_json: str) -> Dict[str, str]:
    """
    Compact tool call arguments for old rounds.

    For tools with large content payloads, remove the large field and add _truncated marker.
    For other tools, truncate arguments if > 500 chars.

    Args:
        tool_name: Name of the tool
        args_json: JSON string of tool arguments

    Returns:
        Dict with 'name' and 'arguments' (JSON string, possibly compacted)
    """
    # Tools with large content fields that should be stripped
    LARGE_CONTENT_TOOLS = {
        "drive_write": "content",
        "claude_code_edit": "prompt",
        "update_scratchpad": "content",
        "update_user_context": "content",
    }

    try:
        args = json.loads(args_json)

        # Check if this tool has a large content field to remove
        if tool_name in LARGE_CONTENT_TOOLS:
            large_field = LARGE_CONTENT_TOOLS[tool_name]
            if large_field in args and args[large_field]:
                args[large_field] = {"_truncated": True}
                return {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)}

        # For other tools, if args JSON is > 500 chars, truncate
        if len(args_json) > 500:
            truncated = args_json[:200] + "..."
            return {"name": tool_name, "arguments": truncated}

        # Otherwise return unchanged
        return {"name": tool_name, "arguments": args_json}

    except (json.JSONDecodeError, Exception):
        # If we can't parse JSON, leave it unchanged
        # But still truncate if too long
        if len(args_json) > 500:
            return {"name": tool_name, "arguments": args_json[:200] + "..."}
        return {"name": tool_name, "arguments": args_json}
