"""MOEX morning digest tool — fetches market data from MOEX ISS public API."""

from __future__ import annotations

import json
import logging
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


def _get_moex_digest(ctx: ToolContext) -> str:
    """Fetch full MOEX morning digest: indices, top stocks, movers."""
    now_msk = datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M UTC")

    parts = [
        f"# 📊 Дайджест MOEX — {now_msk}\n",
        "## 📈 Индексы\n" + get_moex_indices(),
        "\n## 🏆 Топ акций по объёму\n" + get_top_stocks(10),
        "\n## 📊 Движение рынка\n" + get_movers(5),
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
