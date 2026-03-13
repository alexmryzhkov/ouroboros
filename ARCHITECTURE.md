# Architecture

This file describes the technical architecture of Ouroboros.
Maintained by the agent. See BIBLE.md section 8.

## Core Modules (`ouroboros/`)

| File | Lines | Role |
|------|-------|------|
| `agent.py` | ~422 | Thin orchestrator: startup, health checks, message dispatch |
| `loop.py` | ~661 | LLM loop orchestration: model selection, budget guard, compaction, fallback |
| `execution.py` | ~348 | Tool execution layer: timeouts, parallel dispatch, stateful browser tools |
| `context.py` | ~789 | Context formatting for LLM: system prompt assembly, health invariants |
| `health.py` | ~298 | Health checks: startup verification, version sync, budget thresholds |
| `llm.py` | ~280 | LLM client wrapper: OpenRouter API, pricing, cost tracking |
| `memory.py` | ~269 | Persistent memory: scratchpad, identity, user context |

## Supervisor (`supervisor/`)

| File | Lines | Role |
|------|-------|------|
| `workers.py` | ~543 | Worker lifecycle, direct-chat handling, auto-resume |
| `queue.py` | ~421 | Task queue, timeout enforcement, evolution scheduling |
| `cron.py` | ~280 | Cron scheduler: direct Python handlers, weekday filtering |
| `state.py` | ~150 | Persistent state management with file locking |
| `events.py` | ~100 | Event bus between supervisor and agent |
| `telegram.py` | ~120 | Telegram bot integration |

## Tools (`ouroboros/tools/`)

Auto-discovered via `get_tools()` from each module. Core tools always loaded;
non-core available on demand via `list_available_tools` / `enable_tools`.

Key tools: `moex_digest.py` + `moex_client.py` (MOEX market data), `tradingview.py`, `search.py`, `browser.py`, `git_tools.py`, `claude_code.py`.

## Data Flow

```
Telegram → supervisor/workers.py → TaskQueue
                                        ↓
                                   agent.py (OuroborosAgent)
                                        ↓
                                   loop.py (run_llm_loop)
                                   ├── LLM (OpenRouter)
                                   └── execution.py (_handle_tool_calls)
                                            └── tools/registry.py
```

## Execution Layer (execution.py)

Extracted from `loop.py` in v7.4.0. Handles:
- `_execute_single_tool`: parse args, run tool, log result
- `_execute_with_timeout`: hard timeout wrapper (per-tool configurable)
- `_StatefulToolExecutor`: thread-sticky executor for Playwright browser tools
- `_handle_tool_calls`: parallel dispatch for read-only tools, sequential otherwise
- Constants: `READ_ONLY_PARALLEL_TOOLS`, `STATEFUL_BROWSER_TOOLS`

## Design Principles

- **Minimalism**: every module fits in one context window (~1000 lines max, Bible §8)
- **Separation of concerns**: loop.py orchestrates, execution.py executes, context.py formats
- **LLM-first**: agent controls model/effort/tools; code only enforces hard limits
- **Self-modifying**: agent reads and rewrites its own code via `claude_code_edit`
