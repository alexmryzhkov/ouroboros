"""MOEX morning digest tool — fetches market data from MOEX ISS public API."""

from __future__ import annotations

import json
import logging
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

MOEX_BASE = "https://iss.moex.com/iss"
TIMEOUT = 15  # seconds


def _fetch_json(url: str) -> Optional[Dict]:
    """Fetch JSON from MOEX ISS API. Returns None on error."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Ouroboros/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("MOEX API fetch error: %s — %s", url, e)
        return None


def _parse_table(data: Dict, table_name: str) -> Tuple[List[str], List[List]]:
    """Parse ISS response table into (columns, rows)."""
    table = data.get(table_name, {})
    columns = table.get("columns", [])
    rows = table.get("data", [])
    return columns, rows


def _rows_to_dicts(columns: List[str], rows: List[List]) -> List[Dict]:
    return [dict(zip(columns, row)) for row in rows]


def get_moex_indices() -> str:
    """Fetch IMOEX and RTSI index levels."""
    url = (
        f"{MOEX_BASE}/engines/stock/markets/index/boards/SNDX/securities.json"
        "?securities=IMOEX,RTSI&iss.meta=off&iss.only=securities,marketdata"
    )
    data = _fetch_json(url)
    if not data:
        return "⚠️ Не удалось получить данные индексов"

    sec_cols, sec_rows = _parse_table(data, "securities")
    md_cols, md_rows = _parse_table(data, "marketdata")

    securities = _rows_to_dicts(sec_cols, sec_rows)
    marketdata = _rows_to_dicts(md_cols, md_rows)

    # Merge by SECID
    md_by_id = {r.get("SECID"): r for r in marketdata}

    lines = []
    for sec in securities:
        sid = sec.get("SECID", "")
        md = md_by_id.get(sid, {})
        last = md.get("CURRENTVALUE") or md.get("LASTVALUE") or sec.get("PREVPRICE") or "—"
        prev = sec.get("PREVLEGALCLOSEPRICE") or sec.get("PREVPRICE") or 0

        try:
            last_f = float(last)
            prev_f = float(prev)
            if prev_f > 0:
                change_pct = (last_f - prev_f) / prev_f * 100
                sign = "+" if change_pct >= 0 else ""
                emoji = "🟢" if change_pct >= 0 else "🔴"
                lines.append(f"{emoji} **{sid}**: {last_f:,.2f} ({sign}{change_pct:.2f}%)")
            else:
                lines.append(f"📊 **{sid}**: {last}")
        except (TypeError, ValueError):
            lines.append(f"📊 **{sid}**: {last}")

    return "\n".join(lines) if lines else "⚠️ Нет данных по индексам"


def get_top_stocks(top_n: int = 10) -> str:
    """Fetch top stocks by trading volume on TQBR board."""
    url = (
        f"{MOEX_BASE}/engines/stock/markets/shares/boards/TQBR/securities.json"
        "?iss.meta=off&iss.only=securities,marketdata"
    )
    data = _fetch_json(url)
    if not data:
        return "⚠️ Не удалось получить данные по акциям"

    sec_cols, sec_rows = _parse_table(data, "securities")
    md_cols, md_rows = _parse_table(data, "marketdata")

    securities = _rows_to_dicts(sec_cols, sec_rows)
    marketdata = _rows_to_dicts(md_cols, md_rows)

    # Merge by SECID
    sec_by_id = {r.get("SECID"): r for r in securities}
    merged = []
    for md in marketdata:
        sid = md.get("SECID", "")
        sec = sec_by_id.get(sid, {})
        volume = md.get("VALTODAY") or md.get("VALTODAY_USD") or 0
        last = md.get("LAST") or md.get("MARKETPRICE") or 0
        prev = sec.get("PREVPRICE") or md.get("OPEN") or 0
        shortname = sec.get("SHORTNAME") or sid

        try:
            vol_f = float(volume or 0)
            last_f = float(last or 0)
            prev_f = float(prev or 0)
        except (TypeError, ValueError):
            continue

        if vol_f > 0 and last_f > 0:
            merged.append({
                "sid": sid,
                "name": shortname,
                "last": last_f,
                "prev": prev_f,
                "volume": vol_f,
            })

    # Sort by volume descending
    merged.sort(key=lambda x: x["volume"], reverse=True)
    top = merged[:top_n]

    if not top:
        return "⚠️ Нет данных по акциям"

    lines = ["**Топ по объёму торгов:**"]
    for s in top:
        try:
            if s["prev"] > 0:
                chg = (s["last"] - s["prev"]) / s["prev"] * 100
                sign = "+" if chg >= 0 else ""
                emoji = "🟢" if chg >= 0 else "🔴"
                vol_m = s["volume"] / 1_000_000
                lines.append(
                    f"{emoji} **{s['sid']}** ({s['name']}): "
                    f"{s['last']:,.2f} ({sign}{chg:.2f}%) | Объём: {vol_m:.0f}M₽"
                )
            else:
                lines.append(f"📊 **{s['sid']}** ({s['name']}): {s['last']:,.2f}")
        except Exception:
            lines.append(f"📊 **{s['sid']}**: {s['last']}")

    return "\n".join(lines)


def get_movers(top_n: int = 5) -> str:
    """Get top gainers and losers from TQBR."""
    url = (
        f"{MOEX_BASE}/engines/stock/markets/shares/boards/TQBR/securities.json"
        "?iss.meta=off&iss.only=securities,marketdata"
    )
    data = _fetch_json(url)
    if not data:
        return "⚠️ Не удалось получить данные по движению акций"

    sec_cols, sec_rows = _parse_table(data, "securities")
    md_cols, md_rows = _parse_table(data, "marketdata")

    sec_by_id = {r.get("SECID"): r for r in _rows_to_dicts(sec_cols, sec_rows)}
    movers = []

    for md in _rows_to_dicts(md_cols, md_rows):
        sid = md.get("SECID", "")
        sec = sec_by_id.get(sid, {})
        last = md.get("LAST") or 0
        prev = sec.get("PREVPRICE") or 0
        shortname = sec.get("SHORTNAME") or sid

        try:
            last_f, prev_f = float(last), float(prev)
            if prev_f > 0 and last_f > 0:
                chg = (last_f - prev_f) / prev_f * 100
                movers.append({"sid": sid, "name": shortname, "last": last_f, "chg": chg})
        except (TypeError, ValueError):
            pass

    if not movers:
        return "⚠️ Нет данных по движению акций"

    movers.sort(key=lambda x: x["chg"], reverse=True)
    gainers = movers[:top_n]
    losers = movers[-top_n:][::-1]

    lines = ["**🚀 Лидеры роста:**"]
    for s in gainers:
        lines.append(f"  🟢 {s['sid']} ({s['name']}): {s['last']:,.2f} (+{s['chg']:.2f}%)")

    lines.append("\n**📉 Лидеры падения:**")
    for s in losers:
        lines.append(f"  🔴 {s['sid']} ({s['name']}): {s['last']:,.2f} ({s['chg']:.2f}%)")

    return "\n".join(lines)


def get_tradingview_ideas(top_n: int = 5) -> str:
    """Fetch top trading ideas for MOEX from TradingView using embedded JSON."""
    url = "https://www.tradingview.com/ideas/moex/"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru,en;q=0.9",
    }

    _MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")

        marker = '"ideas":{"data":'
        pos = html.find(marker)
        if pos == -1:
            raise ValueError("JSON marker not found in page")

        # Start at the `{"data":` opening brace
        start = pos + len('"ideas":')
        depth = 0
        end = start
        for i, ch in enumerate(html[start:], start):
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        else:
            raise ValueError("Unbalanced JSON braces")

        ideas_blob = json.loads(html[start:end])
        items = ideas_blob.get("data", {}).get("items", [])[:top_n]

        if not items:
            raise ValueError("No items found in JSON")

        result_lines = [f"💡 Топ-{top_n} идей TradingView (MOEX)\n"]
        for idx, item in enumerate(items, 1):
            name = item.get("name", "—")
            description = item.get("description", "")
            if len(description) > 100:
                description = description[:100] + "..."
            chart_url = item.get("chart_url", url)
            updated_at = item.get("updated_at", "")
            symbol = item.get("symbol", {})
            direction = symbol.get("direction", 0)
            ticker = symbol.get("short_name", "")
            author = item.get("user", {}).get("username", "")

            try:
                dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                date_str = f"{dt.day:02d} {_MONTHS[dt.month]} {dt.year}"
            except Exception:
                date_str = updated_at[:10] if updated_at else "—"

            if direction == 1:
                dir_str = "📈 LONG"
            elif direction == -1:
                dir_str = "📉 SHORT"
            else:
                dir_str = "➡️ NEUTRAL"

            ticker_part = f" | {ticker}" if ticker else ""
            result_lines.append(f"{idx}. {dir_str}{ticker_part} — {name}")
            if description:
                result_lines.append(f"   {description}")
            result_lines.append(f"   👤 {author} | 📅 {date_str} | 🔗 {chart_url}")
            result_lines.append("")

        return "\n".join(result_lines).rstrip()

    except Exception as e:
        log.warning("TradingView parse error: %s", e)
        return f"⚠️ TradingView: не удалось получить идеи ({e})"


def _get_moex_digest(ctx: ToolContext) -> str:
    """Fetch full MOEX morning digest: indices, top stocks, movers."""
    now_msk = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    parts = [
        f"# 📊 Дайджест MOEX — {now_msk}\n",
        "## 📈 Индексы\n" + get_moex_indices(),
        "\n## 🏆 Топ акций по объёму\n" + get_top_stocks(10),
        "\n## 📊 Движение рынка\n" + get_movers(5),
        "\n## 💡 Идеи TradingView\n" + get_tradingview_ideas(5),
    ]

    return "\n".join(parts)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("get_moex_digest", {
            "name": "get_moex_digest",
            "description": (
                "Fetch Russian stock market data from MOEX ISS public API. "
                "Returns a formatted digest with index levels (IMOEX, RTSI), "
                "top stocks by volume, and top gainers/losers. "
                "Use this to generate the morning market briefing."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        }, _get_moex_digest),
    ]
