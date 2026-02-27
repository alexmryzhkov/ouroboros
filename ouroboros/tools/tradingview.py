"""TradingView ideas fetcher — JSON API via ru.tradingview.com."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List

import requests as _requests

log = logging.getLogger(__name__)

_MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Referer": "https://ru.tradingview.com/",
    "X-Requested-With": "XMLHttpRequest",
}


def _fmt_date(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return f"{dt.day:02d} {_MONTHS[dt.month]} {dt.year}"
    except Exception:
        return ts[:10] if ts else "—"


def _dir_str(direction) -> str:
    if direction == 1:
        return "LONG"
    if direction == 2:
        return "SHORT"
    return "NEUTRAL"


def _fetch_ideas(base_url: str, market: str) -> list:
    url = f"{base_url}/ideas/{market}/?sort=recent&format=json"
    resp = _requests.get(url, headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    items = data["data"]["ideas"]["data"]["items"]
    # Sort by created_at descending to ensure newest ideas first
    # (the API's own sort may not be strictly chronological)
    def _parse_created(item: dict):
        ts = item.get("created_at") or ""
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            from datetime import timezone
            return datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=_parse_created, reverse=True)
    return items


def get_tradingview_ideas(top_n: int = 5, market: str = "moex") -> list[dict]:
    """Fetch top trading ideas for a given TradingView market.

    Args:
        top_n: Number of ideas to return (default 5).
        market: TradingView market slug (default "moex").

    Returns:
        List of dicts with keys: direction, ticker, name, description,
        author, date, url.
    """
    items = None
    for base in ("https://ru.tradingview.com", "https://www.tradingview.com"):
        try:
            items = _fetch_ideas(base, market)
            log.debug("TradingView: fetched %d ideas from %s", len(items), base)
            break
        except Exception as exc:
            log.warning("TradingView fetch failed from %s: %s", base, exc)

    if not items:
        return []

    if len(items) < 10:
        log.warning("TradingView: only %d ideas returned — API may have changed", len(items))

    result: List[Dict] = []
    for item in items[:top_n]:
        symbol = item.get("symbol") or {}
        result.append({
            "direction": _dir_str(symbol.get("direction", 0)),
            "ticker": symbol.get("short_name", ""),
            "name": item.get("name", "—"),
            "description": item.get("description", ""),
            "author": (item.get("user") or {}).get("username", ""),
            "date": _fmt_date(item.get("created_at", "")),
            "url": item.get("chart_url", ""),
        })
    return result


# ---------------------------------------------------------------------------
# Public formatter (importable without agent registry)
# ---------------------------------------------------------------------------

def format_tradingview_ideas(ideas: list[dict], top_n: int = 5, market: str = "moex") -> str:
    """Format a list of TradingView ideas dicts into a human-readable string."""
    if not ideas:
        return "⚠️ TradingView: не удалось получить идеи"
    lines = [f"💡 Топ-{top_n} идей TradingView ({market.upper()})\n"]
    for idx, idea in enumerate(ideas, 1):
        desc = idea.get("description", "")
        if len(desc) > 100:
            desc = desc[:100] + "..."
        dir_emoji = {"LONG": "📈", "SHORT": "📉"}.get(idea.get("direction", ""), "➡️")
        ticker_part = f" | {idea['ticker']}" if idea.get("ticker") else ""
        lines.append(f"{idx}. {dir_emoji} {idea.get('direction', 'NEUTRAL')}{ticker_part} — {idea.get('name', '—')}")
        if desc:
            lines.append(f"   {desc}")
        lines.append(f"   👤 {idea.get('author', '')} | 📅 {idea.get('date', '')} | 🔗 {idea.get('url', '')}")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# Agent tool registry
# ---------------------------------------------------------------------------

try:
    from ouroboros.tools.registry import ToolEntry

    def _tool_wrapper(top_n: int = 5, market: str = "moex") -> str:
        ideas = get_tradingview_ideas(top_n=top_n, market=market)
        return format_tradingview_ideas(ideas, top_n=top_n, market=market)

    def get_tools():
        return [
            ToolEntry("get_tradingview_ideas", {
                "name": "get_tradingview_ideas",
                "description": (
                    "Fetch top trading ideas from TradingView for a given market. "
                    "Returns formatted list with direction, ticker, author, date and chart URL. "
                    "Default market is 'moex' (Moscow Exchange)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "top_n": {"type": "integer", "description": "Number of ideas to return (default 5)"},
                        "market": {"type": "string", "description": "Market slug, e.g. 'moex', 'stocks' (default 'moex')"},
                    },
                    "required": [],
                },
            }, _tool_wrapper),
        ]

except ImportError:
    pass
