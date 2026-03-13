"""MOEX morning digest tool — formats and sends market data via Telegram."""

from __future__ import annotations

from datetime import datetime
from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.tradingview import get_tradingview_ideas, format_tradingview_ideas
from ouroboros.tools.moex_client import (
    get_moex_indices,
    get_currency_rates,
    get_top_stocks,
    get_movers,
    is_trading_day,
)


def build_moex_digest_text() -> str:
    """Build the full MOEX morning digest text. No ToolContext needed — pure data fetching."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # type: ignore

    tz_msk = ZoneInfo("Europe/Moscow")
    now_msk = datetime.now(tz_msk)
    today_str = now_msk.date().isoformat()
    now_formatted = now_msk.strftime("%d.%m.%Y %H:%M МСК")

    trading_info = is_trading_day(today_str)
    is_trading = trading_info["is_trading"]

    header = f"# 📊 Дайджест MOEX — {now_formatted}\n"
    notice = ""
    if not is_trading:
        last_td = trading_info.get("last_trading_day")
        notice = f"\n⛔ **Сегодня не торговый день**\n{trading_info['reason']}\n"
        if last_td:
            try:
                from datetime import date as _date2
                last_td_fmt = _date2.fromisoformat(last_td).strftime("%d.%m.%Y")
            except Exception:
                last_td_fmt = last_td
            notice += f"\n📅 Данные за последнюю торговую сессию: **{last_td_fmt}**\n"
        else:
            notice += "\n📅 Данные за последнюю доступную сессию:\n"

    parts = [header]
    if notice:
        parts.append(notice)
    parts += [
        "## 📈 Индексы\n" + get_moex_indices(),
        "\n## 💱 Валюты\n" + get_currency_rates(),
        "\n## 🏆 Топ акций по объёму\n" + get_top_stocks(10),
        "\n## 📊 Движение рынка\n" + get_movers(5),
        "\n## 💡 Идеи TradingView\n" + format_tradingview_ideas(get_tradingview_ideas(5)),
    ]

    return "\n".join(parts)


def _get_moex_digest(ctx: ToolContext) -> str:
    """Fetch full MOEX morning digest (LLM tool wrapper)."""
    return build_moex_digest_text()


def send_moex_digest_direct(drive_root: str) -> None:
    """Send MOEX digest directly via Telegram — no LLM needed.

    Used by cron direct handler to avoid agent/worker pipeline.
    Total timeout: 120 seconds.
    """
    import concurrent.futures
    import json as _json
    import logging
    from pathlib import Path as _Path

    log = logging.getLogger(__name__)
    _drive = _Path(drive_root)

    def _log(event_type: str, **kwargs):
        try:
            import datetime as _dt
            record = {"ts": _dt.datetime.now(_dt.timezone.utc).isoformat(), "type": event_type}
            record.update(kwargs)
            log_path = _drive / "logs" / "supervisor.jsonl"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(_json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _run():
        # Read owner_chat_id from state
        state_path = _drive / "state" / "state.json"
        try:
            state = _json.loads(state_path.read_text(encoding="utf-8"))
            owner_chat_id = int(state.get("owner_chat_id") or 0)
        except Exception as e:
            _log("digest_direct_error", phase="read_state", error=str(e))
            return

        if not owner_chat_id:
            _log("digest_direct_error", phase="read_state", error="no owner_chat_id")
            return

        try:
            text = build_moex_digest_text()
        except Exception as e:
            _log("digest_direct_error", phase="build_text", error=str(e))
            text = f"⚠️ Ошибка при формировании дайджеста MOEX: {e}"

        try:
            from supervisor.telegram import send_with_budget
            send_with_budget(owner_chat_id, text)
            _log("digest_direct_sent", chat_id=owner_chat_id, text_len=len(text))
        except Exception as e:
            _log("digest_direct_error", phase="send_telegram", error=str(e))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(_run)
        try:
            future.result(timeout=120)
        except concurrent.futures.TimeoutError:
            log.error("send_moex_digest_direct: timed out after 120s")
            try:
                import json as _j
                from pathlib import Path as _P
                from datetime import datetime as _dt, timezone as _tz
                p = _P(drive_root) / "logs" / "supervisor.jsonl"
                with open(p, "a") as f:
                    f.write(_j.dumps({"ts": _dt.now(_tz.utc).isoformat(), "type": "digest_direct_timeout"}) + "\n")
            except Exception:
                pass
        except Exception as e:
            log.error("send_moex_digest_direct: error: %s", e)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("get_moex_digest", {
            "name": "get_moex_digest",
            "description": (
                "Fetch Russian stock market data from MOEX ISS public API. "
                "Returns a formatted digest with index levels (IMOEX, RTSI), "
                "currency rates (USD/RUB, CNY/RUB), "
                "top stocks by volume, top gainers/losers, "
                "and top TradingView ideas for MOEX. "
                "Includes holiday awareness: if today is not a trading day, "
                "the digest will clearly state that and show last-session data. "
                "Use this to generate the morning market briefing."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        }, _get_moex_digest),
        ToolEntry("check_moex_trading_day", {
            "name": "check_moex_trading_day",
            "description": (
                "Check if a given date is a MOEX trading day. "
                "Returns is_trading (bool), reason, and last_trading_day. "
                "Uses MOEX ISS history API — reliable for past dates and same-day check."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": "ISO date string YYYY-MM-DD. Defaults to today (Moscow time).",
                    }
                },
                "required": [],
            },
        }, lambda ctx: is_trading_day(ctx.params.get("date"))),
    ]
