"""TradingView ideas scraper — reusable helper module."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Dict, List

import requests as _requests

log = logging.getLogger(__name__)

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


def _render(items: list, top_n: int, market: str, fallback_url: str) -> str:
    result_lines = [f"💡 Топ-{top_n} идей TradingView ({market.upper()})\n"]
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
            f"🔗 {idea.get('chart_url', fallback_url)}"
        )
        result_lines.append("")
    return "\n".join(result_lines).rstrip()


def get_tradingview_ideas(top_n: int = 5, market: str = "moex") -> str:
    """Fetch top trading ideas for a given TradingView market.

    Tries three parsing strategies in order:
      1. Embedded JSON blob  "ideas":{"data": ...} (Next.js hydration)
      2. JSON-LD structured data  <script type="application/ld+json">
      3. __INITIAL_STATE__ / plain "ideas":[...] regex

    Args:
        top_n: Number of ideas to return (default 5)
        market: TradingView market slug (default "moex")
    """
    url = f"https://www.tradingview.com/ideas/{market}/?sort=recent"
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

        return _render(ideas, top_n, market, url)

    except Exception as e:
        log.warning("TradingView parse error: %s", e)
        return f"⚠️ TradingView: не удалось получить идеи ({e})"
