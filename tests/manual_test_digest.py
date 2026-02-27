#!/usr/bin/env python3
"""Manual integration test for the MOEX morning digest pipeline.

Run directly (no pytest required):
    python tests/manual_test_digest.py

Or via pytest:
    python -m pytest tests/manual_test_digest.py -v -s

Tests each component independently, then the full digest end-to-end.
Prints actual output so you can visually verify the data looks right.
Uses a live network connection — this is intentional for a manual test.

Notes:
- During non-trading hours (18:50–10:00 MSK) stock volume data will be absent.
  Those checks are soft-pass automatically.
- TradingView scraping may occasionally fail due to anti-bot measures.

Exit codes:
    0 — all tests passed
    1 — one or more tests failed
"""

from __future__ import annotations

import pathlib
import sys

import pytest

# Allow running as: python tests/manual_test_digest.py from any cwd
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import textwrap
import traceback
from typing import Callable, List, Tuple

# ── Colour helpers ────────────────────────────────────────────────────────────

_NO_COLOUR = not sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if _NO_COLOUR:
        return text
    return f"\033[{code}m{text}\033[0m"


def GREEN(t: str) -> str:  return _c("32", t)
def RED(t: str) -> str:    return _c("31", t)
def YELLOW(t: str) -> str: return _c("33", t)
def BOLD(t: str) -> str:   return _c("1",  t)
def DIM(t: str) -> str:    return _c("2",  t)


# ── Test harness ──────────────────────────────────────────────────────────────

_results: List[Tuple[str, bool, str]] = []


def run_test(name: str, fn: Callable[[], str | None]) -> bool:
    """Run a single test, capture output and exceptions."""
    print(f"\n{BOLD('─' * 60)}")
    print(f"{BOLD('TEST')}: {name}")
    print(BOLD("─" * 60))
    try:
        output = fn()
        if output:
            indented = textwrap.indent(str(output), "  ")
            print(indented)
        print(GREEN("✅ PASSED"))
        _results.append((name, True, ""))
        return True
    except AssertionError as e:
        print(RED(f"❌ FAILED: {e}"))
        _results.append((name, False, str(e)))
        return False
    except Exception as e:
        tb = traceback.format_exc()
        print(RED(f"💥 ERROR: {e}"))
        print(DIM(tb))
        _results.append((name, False, f"{type(e).__name__}: {e}"))
        return False


def print_summary() -> int:
    """Print final summary. Returns exit code."""
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\n{BOLD('═' * 60)}")
    print(BOLD("SUMMARY"))
    print(BOLD("═" * 60))
    for name, ok, err in _results:
        status = GREEN("✅") if ok else RED("❌")
        print(f"  {status}  {name}")
        if err:
            print(f"      {DIM(err)}")
    print(BOLD("─" * 60))
    colour = GREEN if failed == 0 else RED
    print(colour(f"  {passed}/{len(_results)} tests passed"))
    if failed:
        print(RED(f"  {failed} test(s) FAILED"))
    return 0 if failed == 0 else 1


# ── Individual check functions ────────────────────────────────────────────────

def check_fetch_json() -> str:
    """Low-level _fetch_json helper works and returns a dict."""
    from ouroboros.tools.moex_digest import _fetch_json, MOEX_BASE

    url = (
        f"{MOEX_BASE}/engines/stock/markets/index/boards/SNDX/securities.json"
        "?securities=IMOEX&iss.meta=off"
    )
    data = _fetch_json(url)
    assert data is not None, "HTTP fetch returned None — check network"
    assert isinstance(data, dict), f"Expected dict, got {type(data)}"
    assert len(data) > 0, "Returned dict is empty"
    return f"Response keys: {list(data.keys())[:5]}"


def check_parse_table() -> str:
    """_parse_table extracts columns and rows from ISS response."""
    from ouroboros.tools.moex_digest import _parse_table

    fake_data = {
        "mytable": {
            "columns": ["A", "B", "C"],
            "data": [[1, 2, 3], [4, 5, 6]],
        }
    }
    cols, rows = _parse_table(fake_data, "mytable")
    assert cols == ["A", "B", "C"], f"Unexpected columns: {cols}"
    assert rows == [[1, 2, 3], [4, 5, 6]], f"Unexpected rows: {rows}"
    return f"columns={cols}, rows={rows}"


def check_moex_indices() -> str:
    """IMOEX and RTSI values are fetched and formatted."""
    from ouroboros.tools.moex_digest import get_moex_indices

    result = get_moex_indices()
    assert isinstance(result, str), "Result must be a string"
    assert len(result) > 0, "Result must not be empty"
    assert "IMOEX" in result or "RTSI" in result, \
        f"Neither IMOEX nor RTSI found in output:\n{result}"
    assert result != "⚠️ Нет данных по индексам", \
        "Got error placeholder — check MOEX API connectivity"
    return result


def check_currency_rates() -> str:
    """USD/RUB and CNY/RUB rates are fetched and formatted."""
    from ouroboros.tools.moex_digest import get_currency_rates

    result = get_currency_rates()
    assert isinstance(result, str), "Result must be a string"
    assert len(result) > 0, "Result must not be empty"
    assert "USD" in result or "CNY" in result, \
        f"No currency found in output:\n{result}"
    assert result != "⚠️ Нет данных по валютам", \
        "Got error placeholder — check MOEX API connectivity"
    return result


def check_top_stocks() -> str:
    """Top stocks by volume are fetched (top-5).

    Soft pass when market is closed (no volume data available).
    """
    from ouroboros.tools.moex_digest import get_top_stocks

    result = get_top_stocks(top_n=5)
    assert isinstance(result, str), "Result must be a string"
    assert len(result) > 0, "Result must not be empty"

    # Hard fail: HTTP/parse error
    assert "Не удалось получить данные" not in result, \
        f"Hard fetch error — check MOEX API connectivity:\n{result}"

    # Soft pass: market closed, no volume today
    if "⚠️ Нет данных по акциям" in result:
        print(YELLOW("  ⚠️ No volume data — market likely closed (soft pass)"))
        return result

    # Normal: check for known tickers
    known = {"SBER", "LKOH", "GAZP", "VTBR", "GMKN", "YDEX", "OZON", "T"}
    found = [t for t in known if t in result]
    assert len(found) >= 1, \
        f"No known blue-chip tickers found in top-5. Output:\n{result}"
    return result


def check_movers() -> str:
    """Top gainers and losers are fetched.

    Soft pass when market is closed (no LAST prices available).
    """
    from ouroboros.tools.moex_digest import get_movers

    result = get_movers(top_n=3)
    assert isinstance(result, str), "Result must be a string"
    assert len(result) > 0, "Result must not be empty"

    # Hard fail: HTTP/parse error
    assert "Не удалось получить данные по движению акций" not in result, \
        f"Hard fetch error:\n{result}"

    # Soft pass: no movers data (market closed)
    if "⚠️ Нет данных по движению акций" in result:
        print(YELLOW("  ⚠️ No mover data — market likely closed (soft pass)"))
        return result

    assert "рост" in result.lower() or "падени" in result.lower() or "%" in result, \
        f"No percentage change data in movers output:\n{result}"
    return result


def check_tradingview_ideas() -> str:
    """TradingView MOEX ideas are scraped successfully."""
    from ouroboros.tools.tradingview import get_tradingview_ideas

    result = get_tradingview_ideas(top_n=3, market="moex")
    assert isinstance(result, str), "Result must be a string"
    assert len(result) > 0, "Result must not be empty"
    assert "не удалось получить идеи" not in result.lower(), \
        f"TradingView scraping failed:\n{result}"
    assert (
        "tradingview.com" in result
        or "👤" in result
        or "📈" in result
        or "📉" in result
    ), f"No recognisable idea content found:\n{result}"
    return result


def check_tradingview_ideas_nasdaq() -> str:
    """TradingView can fetch ideas for a non-MOEX market (nasdaq)."""
    from ouroboros.tools.tradingview import get_tradingview_ideas

    result = get_tradingview_ideas(top_n=2, market="nasdaq")
    assert isinstance(result, str), "Result must be a string"
    assert len(result) > 0, "Result must not be empty"
    print(DIM("  (soft check — content varies by market)"))
    return result


def check_full_digest() -> str:
    """End-to-end: the complete MOEX digest is assembled without errors."""
    from ouroboros.tools.moex_digest import _get_moex_digest

    result = _get_moex_digest(ctx=None)  # type: ignore[arg-type]
    assert isinstance(result, str), "Result must be a string"
    assert len(result) > 100, f"Digest too short ({len(result)} chars) — likely failed"
    for section in ("Индексы", "Валют", "Топ", "Движение", "Идеи"):
        assert section in result, f"Section '{section}' missing from full digest"
    print(DIM(f"\n  Digest length: {len(result)} chars"))
    return result


def check_no_error_placeholders() -> str:
    """Full digest should not contain ⚠️ error placeholders.

    Prints a warning for each placeholder found.
    Fails only if a hard API-connectivity error is present.
    """
    from ouroboros.tools.moex_digest import _get_moex_digest

    result = _get_moex_digest(ctx=None)  # type: ignore[arg-type]
    error_lines = [line for line in result.splitlines() if "⚠️" in line]

    # Hard errors: HTTP failures or parse failures
    hard_errors = [
        l for l in error_lines
        if "не удалось" in l.lower() or "нет данных по индексам" in l.lower()
        or "нет данных по валютам" in l.lower()
    ]

    if error_lines:
        for line in error_lines:
            print(YELLOW(f"  ⚠️ {line.strip()}"))

    if hard_errors:
        raise AssertionError(
            f"Hard API errors found in digest:\n"
            + "\n".join(f"  - {l.strip()}" for l in hard_errors)
        )

    if error_lines:
        return f"Soft warnings only ({len(error_lines)} placeholder(s)) — likely market closed"
    return "No error placeholders ✓"


# ── pytest-compatible wrappers ────────────────────────────────────────────────

@pytest.mark.network
def test_fetch_json():
    check_fetch_json()

@pytest.mark.network
def test_parse_table():
    check_parse_table()

@pytest.mark.network
def test_moex_indices():
    check_moex_indices()

@pytest.mark.network
def test_currency_rates():
    check_currency_rates()

@pytest.mark.network
def test_top_stocks():
    check_top_stocks()

@pytest.mark.network
def test_movers():
    check_movers()

@pytest.mark.network
def test_tradingview_ideas():
    check_tradingview_ideas()

@pytest.mark.network
def test_tradingview_nasdaq():
    check_tradingview_ideas_nasdaq()

@pytest.mark.network
def test_full_digest():
    check_full_digest()

@pytest.mark.network
def test_no_error_placeholders():
    check_no_error_placeholders()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(BOLD("\n🧪 MOEX Digest — Manual Integration Tests"))
    print(DIM("Tests use live network. Run during market hours for best results.\n"))

    tests = [
        ("Low-level: _fetch_json",         check_fetch_json),
        ("Low-level: _parse_table",         check_parse_table),
        ("Component: MOEX indices",         check_moex_indices),
        ("Component: Currency rates",       check_currency_rates),
        ("Component: Top stocks (top-5)",   check_top_stocks),
        ("Component: Movers (top-3)",       check_movers),
        ("Component: TradingView MOEX",     check_tradingview_ideas),
        ("Component: TradingView NASDAQ",   check_tradingview_ideas_nasdaq),
        ("Integration: Full digest",        check_full_digest),
        ("Quality: No error placeholders",  check_no_error_placeholders),
    ]

    for name, fn in tests:
        run_test(name, fn)

    sys.exit(print_summary())
