"""MOEX morning digest tool — fetches market data from MOEX ISS public API."""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.tradingview import get_tradingview_ideas, format_tradingview_ideas

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


def _extract_index_value(data: Dict, secid: str) -> Optional[Dict]:
    """Extract last/prev values for a single index from ISS response."""
    sec_cols, sec_rows = _parse_table(data, "securities")
    md_cols, md_rows = _parse_table(data, "marketdata")
    securities = _rows_to_dicts(sec_cols, sec_rows)
    marketdata = _rows_to_dicts(md_cols, md_rows)
    md_by_id = {r.get("SECID"): r for r in marketdata}
    for sec in securities:
        if sec.get("SECID") == secid:
            md = md_by_id.get(secid, {})
            return {
                "last": md.get("CURRENTVALUE") or md.get("LASTVALUE") or sec.get("PREVPRICE") or "—",
                "prev": sec.get("PREVLEGALCLOSEPRICE") or sec.get("PREVPRICE") or 0,
            }
    return None


def get_moex_indices() -> str:
    """Fetch IMOEX and RTSI index levels.

    IMOEX lives on the SNDX board; RTSI lives on its own board and must be
    queried via the board-agnostic endpoint.
    """
    endpoints = [
        ("IMOEX", (
            f"{MOEX_BASE}/engines/stock/markets/index/boards/SNDX/securities.json"
            "?securities=IMOEX&iss.meta=off&iss.only=securities,marketdata"
        )),
        ("RTSI", (
            f"{MOEX_BASE}/engines/stock/markets/index/securities.json"
            "?securities=RTSI&iss.meta=off&iss.only=securities,marketdata"
        )),
    ]

    lines = []
    for secid, url in endpoints:
        data = _fetch_json(url)
        if not data:
            lines.append(f"⚠️ **{secid}**: нет данных")
            continue
        val = _extract_index_value(data, secid)
        if not val:
            lines.append(f"⚠️ **{secid}**: нет данных")
            continue
        last, prev = val["last"], val["prev"]
        try:
            last_f = float(last)
            prev_f = float(prev)
            if prev_f > 0:
                change_pct = (last_f - prev_f) / prev_f * 100
                sign = "+" if change_pct >= 0 else ""
                emoji = "🟢" if change_pct >= 0 else "🔴"
                lines.append(f"{emoji} **{secid}**: {last_f:,.2f} ({sign}{change_pct:.2f}%)")
            else:
                lines.append(f"📊 **{secid}**: {last}")
        except (TypeError, ValueError):
            lines.append(f"📊 **{secid}**: {last}")

    return "\n".join(lines) if lines else "⚠️ Нет данных по индексам"


HISTORY_URL = (
    f"{MOEX_BASE}/history/engines/stock/markets/shares/boards/TQBR/securities.json"
    "?iss.meta=off&limit=100"
)


def _fetch_history_stocks(top_n: int = 50) -> List[Dict]:
    """Fetch previous session data from MOEX ISS history endpoint.

    Returns a list of dicts with keys: sid, name, close, open, value, chg_pct,
    sorted by value descending.
    """
    data = _fetch_json(HISTORY_URL)
    if not data:
        return []

    cols, rows = _parse_table(data, "history")
    result = []
    for row in _rows_to_dicts(cols, rows):
        sid = row.get("SECID", "")
        name = row.get("SHORTNAME") or sid
        try:
            close = float(row.get("CLOSE") or row.get("LEGALCLOSEPRICE") or 0)
            open_ = float(row.get("OPEN") or 0)
            value = float(row.get("VALUE") or 0)
        except (TypeError, ValueError):
            continue
        if value <= 0 or close <= 0:
            continue
        chg_pct = (close - open_) / open_ * 100 if open_ > 0 else 0.0
        result.append({"sid": sid, "name": name, "close": close, "open": open_, "value": value, "chg_pct": chg_pct})

    result.sort(key=lambda x: x["value"], reverse=True)
    return result[:top_n]


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

    # Fall back to history endpoint when market is closed (no live volume)
    if not merged:
        hist = _fetch_history_stocks(top_n)
        if not hist:
            return "⚠️ Нет данных по акциям"
        lines = ["(данные за последнюю сессию)\n**Топ по объёму торгов:**"]
        for s in hist:
            sign = "+" if s["chg_pct"] >= 0 else ""
            emoji = "🟢" if s["chg_pct"] >= 0 else "🔴"
            vol_m = s["value"] / 1_000_000
            lines.append(
                f"{emoji} **{s['sid']}** ({s['name']}): "
                f"{s['close']:,.2f} ({sign}{s['chg_pct']:.2f}%) | Объём: {vol_m:.0f}M₽"
            )
        return "\n".join(lines)

    top = merged[:top_n]

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

    # Fall back to history endpoint when market is closed (no live movers)
    if not movers:
        hist = _fetch_history_stocks(50)
        if not hist:
            return "⚠️ Нет данных по движению акций"
        hist_sorted = sorted(hist, key=lambda x: x["chg_pct"], reverse=True)
        gainers_h = hist_sorted[:top_n]
        losers_h = hist_sorted[-top_n:][::-1]
        lines = ["(данные за последнюю сессию)\n**🚀 Лидеры роста:**"]
        for s in gainers_h:
            lines.append(f"  🟢 {s['sid']} ({s['name']}): {s['close']:,.2f} (+{s['chg_pct']:.2f}%)")
        lines.append("\n**📉 Лидеры падения:**")
        for s in losers_h:
            lines.append(f"  🔴 {s['sid']} ({s['name']}): {s['close']:,.2f} ({s['chg_pct']:.2f}%)")
        return "\n".join(lines)

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


def get_currency_rates() -> str:
    """Fetch USD/RUB and CNY/RUB from MOEX ISS CETS currency market."""
    url = (
        f"{MOEX_BASE}/engines/currency/markets/selt/boards/CETS/securities.json"
        "?securities=USD000UTSTOM,CNYRUB_TOM&iss.meta=off&iss.only=securities,marketdata"
    )
    data = _fetch_json(url)
    if not data:
        return "⚠️ Нет данных по валютам"

    sec_cols, sec_rows = _parse_table(data, "securities")
    md_cols, md_rows = _parse_table(data, "marketdata")
    securities = _rows_to_dicts(sec_cols, sec_rows)
    marketdata = _rows_to_dicts(md_cols, md_rows)

    md_by_id = {r.get("SECID"): r for r in marketdata}

    lines = []
    labels = {
        "USD000UTSTOM": ("💵", "USD/RUB"),
        "CNYRUB_TOM": ("🟡", "CNY/RUB"),
    }
    for sec in securities:
        sid = sec.get("SECID", "")
        if sid not in labels:
            continue
        emoji, label = labels[sid]
        md = md_by_id.get(sid, {})

        last = md.get("LAST") or md.get("CLOSEPRICE") or sec.get("PREVPRICE")
        prev = sec.get("PREVPRICE") or sec.get("PREVWAPRICE")

        try:
            last_f = float(last)
            prev_f = float(prev)
            if prev_f > 0:
                chg = (last_f - prev_f) / prev_f * 100
                sign = "+" if chg >= 0 else ""
                chg_emoji = "🟢" if chg >= 0 else "🔴"
                lines.append(f"{emoji} **{label}**: {last_f:.4f} {chg_emoji}({sign}{chg:.2f}%)")
            else:
                lines.append(f"{emoji} **{label}**: {last_f:.4f}")
        except (TypeError, ValueError):
            lines.append(f"{emoji} **{label}**: {last or '—'}")

    return "\n".join(lines) if lines else "⚠️ Нет данных по валютам"


def _get_market_session_date() -> Optional[str]:
    """Get the date of the most recent MOEX trading session from IMOEX live data.

    Uses the TRADE_SESSION_DATE field from IMOEX marketdata — this is the date
    of the last data point MOEX has, updated in real-time during trading.
    Returns YYYY-MM-DD string or None on error.
    """
    url = (
        f"{MOEX_BASE}/engines/stock/markets/index/boards/SNDX/securities.json"
        "?securities=IMOEX&iss.meta=off&iss.only=marketdata"
    )
    data = _fetch_json(url)
    if not data:
        return None
    md_cols, md_rows = _parse_table(data, "marketdata")
    if not md_rows:
        return None
    row = dict(zip(md_cols, md_rows[0]))
    return row.get("TRADE_SESSION_DATE")  # e.g. "2026-02-27"


def _get_live_trading_status() -> Optional[str]:
    """Get TRADINGSTATUS for SBER on TQBR board.

    Returns the raw TRADINGSTATUS string:
      'T' = security is listed as tradeable (most common)
      'N' = not trading (suspended or non-trading day)
      None = could not determine

    Note: 'T' does NOT mean the market is currently open — it means the
    security is in a tradeable state. Use TRADE_SESSION_DATE for date checks.
    """
    url = (
        f"{MOEX_BASE}/engines/stock/markets/shares/boards/TQBR/securities.json"
        "?securities=SBER&iss.meta=off&iss.only=marketdata"
    )
    data = _fetch_json(url)
    if not data:
        return None
    md_cols, md_rows = _parse_table(data, "marketdata")
    if not md_rows:
        return None
    row = dict(zip(md_cols, md_rows[0]))
    return row.get("TRADINGSTATUS")


def _get_last_trading_day() -> Optional[str]:
    """Query MOEX ISS history to find the most recent completed trading day.

    Uses the /history/.../dates.json endpoint — returns the 'till' field
    which is the last date with published trading data.
    """
    dates_url = (
        f"{MOEX_BASE}/history/engines/stock/markets/shares/boards/TQBR/dates.json"
        "?iss.meta=off"
    )
    data = _fetch_json(dates_url)
    if data:
        _, date_rows = _parse_table(data, "dates")
        if date_rows:
            return date_rows[0][1]  # "till" column = last date with data
    return None


def is_trading_day(date_str: str = None) -> dict:
    """Check if a given date (YYYY-MM-DD) is a MOEX trading day.

    Uses a two-stage detection:
    1. Fast path: weekend check (no API call needed)
    2. Real-time check: compare TRADE_SESSION_DATE from IMOEX live data
       with the requested date — this works correctly during trading hours
       (unlike the history endpoint which only updates end-of-day).
    """
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo  # type: ignore

    from datetime import date as _date

    tz_msk = ZoneInfo("Europe/Moscow")
    if date_str is None:
        date_str = datetime.now(tz_msk).date().isoformat()

    d = _date.fromisoformat(date_str)
    weekday = d.weekday()

    # Fast path: weekends are never trading days on MOEX
    if weekday >= 5:
        day_name = "суббота" if weekday == 5 else "воскресенье"
        last_trading = _get_last_trading_day()
        reason = f"Выходной день ({day_name}) — MOEX не торгует"
        if last_trading:
            reason += f". Последний торговый день: {last_trading}"
        return {
            "date": date_str,
            "is_trading": False,
            "reason": reason,
            "last_trading_day": last_trading,
        }

    # Real-time check: what date does MOEX live data show?
    session_date = _get_market_session_date()

    if session_date is not None:
        if session_date == date_str:
            # MOEX live data is for today → market has traded (or is trading) today
            return {
                "date": date_str,
                "is_trading": True,
                "reason": f"Торговый день — MOEX ведёт данные за {date_str} (TRADE_SESSION_DATE={session_date})",
                "last_trading_day": date_str,
            }
        else:
            # MOEX live data is for a different (earlier) date → today is a holiday
            return {
                "date": date_str,
                "is_trading": False,
                "reason": (
                    f"Не торговый день (праздник) — "
                    f"последние данные MOEX за {session_date}, не за сегодня ({date_str})"
                ),
                "last_trading_day": session_date,
            }

    # Fallback: if live check fails, try history endpoint
    log.warning("is_trading_day: TRADE_SESSION_DATE unavailable, falling back to history check")
    url = (
        f"{MOEX_BASE}/history/engines/stock/markets/shares/boards/TQBR/securities.json"
        f"?date={date_str}&iss.meta=off&limit=1&iss.only=history"
    )
    data = _fetch_json(url)
    if data is None:
        return {
            "date": date_str,
            "is_trading": True,
            "reason": "Не удалось получить данные от MOEX ISS (API недоступен) — предполагаем торговый день",
            "last_trading_day": date_str,
        }

    _, rows = _parse_table(data, "history")
    if rows:
        return {
            "date": date_str,
            "is_trading": True,
            "reason": "Торговый день — данные за эту дату есть на MOEX (history fallback)",
            "last_trading_day": date_str,
        }

    last_trading = _get_last_trading_day()
    reason = f"Не торговый день (выходной или праздник) — данных за {date_str} нет на MOEX (history fallback)"
    if last_trading:
        reason += f". Последний торговый день: {last_trading}"

    return {
        "date": date_str,
        "is_trading": False,
        "reason": reason,
        "last_trading_day": last_trading,
    }


def _get_moex_digest(ctx: ToolContext) -> str:
    """Fetch full MOEX morning digest: indices, currencies, top stocks, movers, ideas.

    Includes holiday/non-trading-day awareness: if today is not a trading day,
    the digest will say so clearly and show data from the last trading session.
    """
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

    if not is_trading:
        last_td = trading_info.get("last_trading_day")
        header = f"# 📊 Дайджест MOEX — {now_formatted}\n"
        holiday_notice = (
            f"\n⛔ **Сегодня не торговый день**\n"
            f"{trading_info['reason']}\n"
        )
        if last_td:
            # Format YYYY-MM-DD → DD.MM.YYYY
            try:
                from datetime import date as _date2
                last_td_fmt = _date2.fromisoformat(last_td).strftime("%d.%m.%Y")
            except Exception:
                last_td_fmt = last_td
            holiday_notice += f"\n📅 Данные за последнюю торговую сессию: **{last_td_fmt}**\n"
        else:
            holiday_notice += "\n📅 Данные за последнюю доступную сессию:\n"

        parts = [
            header,
            holiday_notice,
            "## 📈 Индексы\n" + get_moex_indices(),
            "\n## 💱 Валюты\n" + get_currency_rates(),
            "\n## 🏆 Топ акций по объёму\n" + get_top_stocks(10),
            "\n## 📊 Движение рынка\n" + get_movers(5),
            "\n## 💡 Идеи TradingView\n" + format_tradingview_ideas(get_tradingview_ideas(5)),
        ]
    else:
        parts = [
            f"# 📊 Дайджест MOEX — {now_formatted}\n",
            "## 📈 Индексы\n" + get_moex_indices(),
            "\n## 💱 Валюты\n" + get_currency_rates(),
            "\n## 🏆 Топ акций по объёму\n" + get_top_stocks(10),
            "\n## 📊 Движение рынка\n" + get_movers(5),
            "\n## 💡 Идеи TradingView\n" + format_tradingview_ideas(get_tradingview_ideas(5)),
        ]

    return "\n".join(parts)


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
