#!/usr/bin/env python3
"""
inspect_moex_status.py — Diagnostic tool to understand MOEX board/market status
via the ISS API. Checks SNDX board (index) and TQBR board (shares) for:
  - TRADINGSTATUS per-security in marketdata
  - Board-level traded status
  - History dates endpoint (last trading day)
  - Live IMOEX value + TRADINGSTATUS

Run with: python inspect_moex_status.py
"""

import json
import sys
import urllib.request
import urllib.error
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

MOEX_BASE = "https://iss.moex.com/iss"
TIMEOUT = 15
MSK = ZoneInfo("Europe/Moscow")


def fetch(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "inspect_moex/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"  ❌ fetch error: {e}")
        return None


def parse_table(data: dict, name: str) -> tuple[list, list]:
    t = data.get(name, {})
    return t.get("columns", []), t.get("data", [])


def rows_to_dicts(cols, rows):
    return [dict(zip(cols, r)) for r in rows]


def section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print('='*60)


def main():
    now_msk = datetime.now(MSK)
    print(f"\n🕐 Current time (MSK): {now_msk.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"🕐 Current time (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}")
    today = now_msk.date().isoformat()
    weekday_names = ["Mon","Tue","Wed","Thu","Fri","SAT","SUN"]
    print(f"📅 Today: {today} ({weekday_names[now_msk.weekday()]})")

    # ------------------------------------------------------------------ #
    # 1. SNDX board — IMOEX marketdata with TRADINGSTATUS
    # ------------------------------------------------------------------ #
    section("1. SNDX board (IMOEX) — TRADINGSTATUS in marketdata")
    url = (f"{MOEX_BASE}/engines/stock/markets/index/boards/SNDX/securities.json"
           "?securities=IMOEX&iss.meta=off&iss.only=securities,marketdata")
    print(f"  URL: {url}")
    data = fetch(url)
    if data:
        md_cols, md_rows = parse_table(data, "marketdata")
        md = rows_to_dicts(md_cols, md_rows)
        sec_cols, sec_rows = parse_table(data, "securities")
        sec = rows_to_dicts(sec_cols, sec_rows)
        print(f"  securities columns: {sec_cols}")
        print(f"  marketdata columns: {md_cols}")
        if md:
            row = md[0]
            print(f"\n  IMOEX marketdata row (selected fields):")
            for k in ["SECID","TRADINGSTATUS","CURRENTVALUE","LASTVALUE","UPDATETIME","SEQNUM"]:
                print(f"    {k}: {row.get(k)}")
            print(f"\n  Full TRADINGSTATUS: {row.get('TRADINGSTATUS')!r}")
            print(f"  Interpretation:")
            ts = row.get("TRADINGSTATUS")
            if ts == "O":
                print("    ✅ 'O' = Open / Trading")
            elif ts == "N":
                print("    ⛔ 'N' = Not trading / Closed")
            elif ts == "S":
                print("    🔄 'S' = Session (pre-market or auction)")
            elif ts is None or ts == "":
                print("    ❓ Empty / None — no status field")
            else:
                print(f"    ❓ Unknown status: {ts!r}")
        else:
            print("  ⚠️  marketdata table is EMPTY")
        if sec:
            row = sec[0]
            print(f"\n  IMOEX securities row (selected fields):")
            for k in ["SECID","PREVPRICE","PREVLEGALCLOSEPRICE","STATUS"]:
                print(f"    {k}: {row.get(k)}")

    # ------------------------------------------------------------------ #
    # 2. TQBR board — SBER as representative share, TRADINGSTATUS
    # ------------------------------------------------------------------ #
    section("2. TQBR board (SBER) — TRADINGSTATUS in marketdata")
    url2 = (f"{MOEX_BASE}/engines/stock/markets/shares/boards/TQBR/securities.json"
            "?securities=SBER&iss.meta=off&iss.only=securities,marketdata")
    print(f"  URL: {url2}")
    data2 = fetch(url2)
    if data2:
        md_cols2, md_rows2 = parse_table(data2, "marketdata")
        md2 = rows_to_dicts(md_cols2, md_rows2)
        print(f"  marketdata columns: {md_cols2}")
        if md2:
            row2 = md2[0]
            print(f"\n  SBER marketdata row (selected fields):")
            for k in ["SECID","TRADINGSTATUS","LAST","OPEN","VALTODAY","TIME","UPDATETIME"]:
                print(f"    {k}: {row2.get(k)}")
            ts2 = row2.get("TRADINGSTATUS")
            print(f"\n  Full TRADINGSTATUS: {ts2!r}")
            if ts2 == "O":
                print("    ✅ 'O' = Open / Trading")
            elif ts2 == "N":
                print("    ⛔ 'N' = Not trading / Closed")
            elif ts2 == "S":
                print("    🔄 'S' = Session")
            else:
                print(f"    ❓ Unknown: {ts2!r}")
        else:
            print("  ⚠️  marketdata table is EMPTY")

    # ------------------------------------------------------------------ #
    # 3. All possible TRADINGSTATUS values from bulk TQBR query
    # ------------------------------------------------------------------ #
    section("3. TQBR bulk — all unique TRADINGSTATUS values present right now")
    url3 = (f"{MOEX_BASE}/engines/stock/markets/shares/boards/TQBR/securities.json"
            "?iss.meta=off&iss.only=marketdata")
    print(f"  URL: {url3}")
    data3 = fetch(url3)
    if data3:
        md_cols3, md_rows3 = parse_table(data3, "marketdata")
        md3 = rows_to_dicts(md_cols3, md_rows3)
        statuses = {}
        for r in md3:
            ts = r.get("TRADINGSTATUS") or "(empty/None)"
            statuses[ts] = statuses.get(ts, 0) + 1
        print(f"  Total securities: {len(md3)}")
        print(f"  TRADINGSTATUS distribution:")
        for st, count in sorted(statuses.items()):
            print(f"    {st!r}: {count} securities")

    # ------------------------------------------------------------------ #
    # 4. History dates endpoint (what is the last known trading day)
    # ------------------------------------------------------------------ #
    section("4. History dates — last known trading day")
    url4 = (f"{MOEX_BASE}/history/engines/stock/markets/shares/boards/TQBR/dates.json"
            "?iss.meta=off")
    print(f"  URL: {url4}")
    data4 = fetch(url4)
    if data4:
        _, date_rows = parse_table(data4, "dates")
        if date_rows:
            print(f"  dates row: {date_rows[0]}")
            print(f"  from: {date_rows[0][0]}")
            print(f"  till (last trading day): {date_rows[0][1]}")
        else:
            print("  ⚠️  dates table empty")

    # ------------------------------------------------------------------ #
    # 5. Today's history check (does today appear in history yet?)
    # ------------------------------------------------------------------ #
    section(f"5. History check for TODAY ({today})")
    url5 = (f"{MOEX_BASE}/history/engines/stock/markets/shares/boards/TQBR/securities.json"
            f"?date={today}&iss.meta=off&limit=1&iss.only=history")
    print(f"  URL: {url5}")
    data5 = fetch(url5)
    if data5:
        _, hist_rows = parse_table(data5, "history")
        print(f"  Row count for today: {len(hist_rows)}")
        if hist_rows:
            hist_cols, _ = parse_table(data5, "history")
            row = dict(zip(hist_cols, hist_rows[0]))
            print(f"  First row TRADEDATE={row.get('TRADEDATE')}, CLOSE={row.get('CLOSE')}")
            print("  ✅ Today IS in history (market has traded today and data is available)")
        else:
            print("  ⛔ Today NOT in history yet (market hasn't traded yet today OR data not yet published)")

    # ------------------------------------------------------------------ #
    # 6. SNDX board metadata — is_traded flag
    # ------------------------------------------------------------------ #
    section("6. SNDX board metadata")
    url6 = f"{MOEX_BASE}/engines/stock/markets/index/boards/SNDX.json?iss.meta=off"
    print(f"  URL: {url6}")
    data6 = fetch(url6)
    if data6:
        for table_name in data6.keys():
            cols, rows = parse_table(data6, table_name)
            if rows:
                print(f"\n  Table '{table_name}': columns={cols}")
                print(f"  First row: {rows[0]}")

    # ------------------------------------------------------------------ #
    # Summary: recommended logic
    # ------------------------------------------------------------------ #
    section("SUMMARY — Recommended market status detection logic")
    print("""
  METHOD: Check TRADINGSTATUS on IMOEX in SNDX board marketdata

  URL: /engines/stock/markets/index/boards/SNDX/securities.json
       ?securities=IMOEX&iss.meta=off&iss.only=marketdata

  Field: marketdata[0]["TRADINGSTATUS"]

  Values:
    "O"  → Market is OPEN (active trading session)
    "N"  → Market is CLOSED (before open, after close, or holiday)
    "S"  → Session state (auction, pre-market clearing)
    None → Unknown / weekend / system down

  This works in real-time — no dependence on history endpoint.

  For "last trading day" (to show historical data on holidays):
    Use /history/engines/stock/markets/shares/boards/TQBR/dates.json
    The "till" field = last date with completed trading data.

  COMBINED LOGIC:
    1. Check TRADINGSTATUS on IMOEX → is market open RIGHT NOW?
    2. If closed → fetch last_trading_day from history/dates
    3. Use last_trading_day to pull historical data
    """)

    print("\n✅ Inspection complete.\n")


if __name__ == "__main__":
    main()
