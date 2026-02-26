"""MOEX morning digest tool — fetches market data from MOEX ISS public API."""

from __future__ import annotations

import json
import logging
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests as _requests

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
    """Fetch top trading ideas for MOEX from TradingView.

    Uses requests with a realistic browser User-Agent and tries three
    parsing strategies in order:
      1. Embedded JSON blob  `"ideas":{"data": ...}` (Next.js hydration)
      2. JSON-LD structured data  `<script type="application/ld+json">`
      3. `window.__INITIAL_STATE__` / plain `"ideas":[...]` regex
    """
    url = "https://www.tradingview.com/ideas/moex/?sort=recent"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": "https://www.tradingview.com/",
    }

    _MONTHS = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

    def _fmt_date(updated_at: str) -> str:
        try:
            dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            return f"{dt.day:02d} {_MONTHS[dt.month]} {dt.year}"
        except Exception:
            return updated_at[:10] if updated_at else "—"

    def _dir_str(direction) -> str:
        if direction == 1 or str(direction).upper() == "LONG":
            return "📈 LONG"
        if direction == -1 or str(direction).upper() == "SHORT":
            return "📉 SHORT"
        return "➡️ NEUTRAL"

    def _render(items: list) -> str:
        result_lines = [f"💡 Топ-{top_n} идей TradingView (MOEX)\n"]
        for idx, idea in enumerate(items[:top_n], 1):
            name = idea.get("name", "—")
            desc = idea.get("description", "")
            if len(desc) > 100:
                desc = desc[:100] + "..."
            ticker_part = f" | {idea['ticker']}" if idea.get("ticker") else ""
            result_lines.append(f"{idx}. {_dir_str(idea.get('direction', 0))}{ticker_part} — {name}")
            if desc:
                result_lines.append(f"   {desc}")
            result_lines.append(
                f"   👤 {idea.get('author', '')} | "
                f"📅 {idea.get('date_str', '—')} | "
                f"🔗 {idea.get('chart_url', url)}"
            )
            result_lines.append("")
        return "\n".join(result_lines).rstrip()

    try:
        resp = _requests.get(url, headers=headers, timeout=20)
        html = resp.text
        ideas: List[Dict] = []

        # --- Strategy 1: embedded Next.js hydration blob "ideas":{"data":...} ---
        marker = '"ideas":{"data":'
        pos = html.find(marker)
        if pos != -1:
            start = pos + len('"ideas":')
            depth = 0
            end = start
            for i, ch in enumerate(html[start:], start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            try:
                blob = json.loads(html[start:end])
                for item in blob.get("data", {}).get("items", [])[:top_n]:
                    symbol = item.get("symbol", {}) if isinstance(item.get("symbol"), dict) else {}
                    ideas.append({
                        "name": item.get("name", "—"),
                        "description": item.get("description", ""),
                        "chart_url": item.get("chart_url", url),
                        "direction": symbol.get("direction", 0),
                        "ticker": symbol.get("short_name", ""),
                        "author": (item.get("user") or {}).get("username", ""),
                        "date_str": _fmt_date(item.get("updated_at", "")),
                    })
            except Exception as exc:
                log.debug("TradingView strategy 1 failed: %s", exc)

        # --- Strategy 2: JSON-LD structured data ---
        if not ideas:
            for jld_text in re.findall(
                r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                html, re.DOTALL
            ):
                try:
                    data = json.loads(jld_text)
                    entries = data if isinstance(data, list) else [data]
                    for entry in entries:
                        headline = entry.get("headline") or entry.get("name")
                        if not headline:
                            continue
                        author_obj = entry.get("author", {})
                        author = author_obj.get("name", "") if isinstance(author_obj, dict) else ""
                        ideas.append({
                            "name": headline,
                            "description": entry.get("description", ""),
                            "chart_url": entry.get("url", url),
                            "direction": 0,
                            "ticker": "",
                            "author": author,
                            "date_str": (entry.get("dateModified") or entry.get("datePublished", ""))[:10],
                        })
                        if len(ideas) >= top_n:
                            break
                except Exception as exc:
                    log.debug("TradingView strategy 2 entry failed: %s", exc)
                if len(ideas) >= top_n:
                    break

        # --- Strategy 3: __INITIAL_STATE__ or bare "ideas":[...] pattern ---
        if not ideas:
            for pattern in [
                r'window\.__INITIAL_STATE__\s*=\s*(\{.+?\});\s*</script>',
                r'"ideas"\s*:\s*(\[.+?\])\s*,\s*"',
            ]:
                m = re.search(pattern, html, re.DOTALL)
                if not m:
                    continue
                try:
                    blob = json.loads(m.group(1))
                    entries = blob if isinstance(blob, list) else []
                    for entry in entries[:top_n]:
                        ideas.append({
                            "name": entry.get("title") or entry.get("name", "—"),
                            "description": "",
                            "chart_url": url,
                            "direction": 0,
                            "ticker": entry.get("symbol", ""),
                            "author": entry.get("username", ""),
                            "date_str": str(entry.get("published_at", ""))[:10],
                        })
                except Exception as exc:
                    log.debug("TradingView strategy 3 pattern failed: %s", exc)
                if ideas:
                    break

        if not ideas:
            raise ValueError("no ideas found via any parsing strategy")

        return _render(ideas)

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
