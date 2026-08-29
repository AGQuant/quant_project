"""yahoo_live_quote.py — cc#1417 LIVE Yahoo fallback for domestic indices + ADR breadth,
market-hours Fyers outages only.
==============================================================================================
SCOPE, EXACTLY (card cc#1417): Card 1's NIFTY/BankNifty index rows and the Home ADR/breadth
figure -- the two surfaces confirmed feasible with Yahoo cash data. NOT Card 4 futures/options
CMP, NOT V8's own signal-generation inputs (cc#1418 handles those with an honest alert instead,
since Yahoo carries zero NSE derivatives coverage -- confirmed by reading yahoo_symbol_resolver.py
directly, every ticker pattern it ever builds is <SYM>.NS/.BO cash-only).

DETECTION -- REUSES feed_guardian.py, DOES NOT BUILD A SECOND MECHANISM (explicit instruction).
feed_guardian already tracks LEGS=("fyers_eq","fyers_fut") and fires its own red_flags at
STALE_MIN=10 minutes past the newest bar on that leg, market hours only. Card 1's index tile and
ADR/breadth both ultimately read intraday_prices on the CASH side (domestic_live() cash OHLC,
v8_signal_writer._write_adr_intraday()'s per-symbol latest-cash-bar scan) -- i.e. exactly the
fyers_eq leg. fyers_eq_outage() below re-runs feed_guardian's own per-leg-age query and applies
its own STALE_MIN threshold -- same signal, same number, not a fork of it.

THE REAL UNIVERSE SIZE, MEASURED, NOT ASSUMED -- the card's own "up to ~1800 names" premise does
NOT hold. Queried live: distinct cash symbols with an intraday_prices row, 8 trading days running
19-Aug through 28-Aug-2026: 206, 212, 212, 212, 212, 212, 211, 211 -- stable at ~208-212, matching
futures_universe(is_active=TRUE)=208 almost exactly. The Home breadth figure has ALWAYS been the
F&O/index universe, not a broader NSE500-scale set. This changes the batch-feasibility math the
card asked to be investigated: at ~210 symbols, TWO batched requests (BATCH_SIZE=150) cover the
entire universe -- not the 30+ minutes a 1-symbol-per-second throttle (yahoo_symbol_resolver.py's
own THROTTLE=1.0, built for nightly chart-endpoint backfill) would need for ~1800 names.

BATCH ENDPOINT -- NOT LIVE-VERIFIED FROM THIS SESSION, STATED HONESTLY. This session's sandboxed
network denies query1.finance.yahoo.com (org policy on the egress proxy's CONNECT tunnel) -- the
exact live response shape/limits of Yahoo's v7/finance/quote batch contract could not be
curl-tested from here. What follows is the long-stable, publicly documented behaviour of that
endpoint (the same one yfinance and many other open-source tools rely on: a comma-separated
`symbols=` query param returns many quotes in ONE call), DISTINCT from the chart/history endpoint
yahoo_daily_update.py/yahoo_symbol_resolver.py already use and already confirmed reachable in
production. Every failure path below degrades to "symbol absent from the result" rather than
raising or fabricating a value, specifically because this exact call was not verified live here --
confirm the first real batch call once deployed (Railway's egress is not sandboxed the way this
session's is) and note the true latency observed.

SOURCE TAGGING -- never disguised as a genuine Fyers tick, matching domestic_live()'s own existing
two-tier "source": "live_intraday" / "eod_fallback" convention. This adds a third: "yahoo_live_
fallback". Every value this module returns carries it.

RECOVERY -- automatic, no state to reset. mobile_home2() calls fyers_eq_outage() FRESH on every
request; there is nothing to persist. The instant the fyers_eq leg's newest bar is recent again,
the very next request's check returns False and mobile_home2.py's normal domestic_live()/
market_mood() path runs unchanged -- the Yahoo values were never cached or reused.
"""
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone, time as dt_time

from feed_guardian import _leg_ages, STALE_MIN   # cc#1417: REUSE, not a second detector

log = logging.getLogger("scorr.yahoo_live_quote")

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)

QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
BATCH_SIZE = 150          # ~210-symbol universe -> 2 requests; keeps the symbols= query string
                          # comfortably under typical URL-length limits even as the universe grows
TIMEOUT_SEC = 4           # a live-page fallback must fail fast, never hang a request behind it
SOURCE_TAG = "yahoo_live_fallback"

_IDX_YAHOO = {"NIFTY50": "^NSEI", "BANKNIFTY": "^NSEBANK"}


def _ist_now():
    return datetime.now(IST).replace(tzinfo=None)


def fyers_eq_outage(cur, now=None):
    """(is_outage: bool, age_minutes: float|None). True only during market hours, weekdays, when
    the fyers_eq leg's newest bar is older than feed_guardian's own STALE_MIN (10 min) -- the
    SAME threshold and the SAME per-leg age query feed_guardian.guardian_summary() red-flags on.
    Off-hours or on a non-trading day this is always False -- an idle feed is not an outage, it's
    the market being shut, and this fallback must never fire outside real market hours."""
    if now is None:
        now = _ist_now()
    if now.weekday() >= 5 or not (MARKET_OPEN <= now.time() <= MARKET_CLOSE):
        return False, None
    ages = _leg_ages(cur, now)
    age = ages.get("fyers_eq")
    return (age is not None and age > STALE_MIN), age


def _yahoo_symbol(nse_symbol):
    return _IDX_YAHOO.get(nse_symbol, f"{nse_symbol}.NS")


def fetch_live_quotes(symbols):
    """Batched live quote fetch. Returns {nse_symbol: {price, chg_pct, prev_close, open, high,
    low, asof, source}} for whatever Yahoo actually returned -- a symbol Yahoo didn't return, or
    a batch whose request failed outright, is simply ABSENT from the result. Never zero-filled,
    never a stale/cached value passed off as fresh -- see the module doc for why this exact call
    is unverified from this session and what that means for the failure handling below."""
    out = {}
    uniq = list(dict.fromkeys(symbols))
    for i in range(0, len(uniq), BATCH_SIZE):
        batch = uniq[i:i + BATCH_SIZE]
        y_syms = [_yahoo_symbol(s) for s in batch]
        rev = {y: s for y, s in zip(y_syms, batch)}
        url = QUOTE_URL + "?" + urllib.parse.urlencode({"symbols": ",".join(y_syms)})
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            fetched_at = _ist_now().replace(microsecond=0).isoformat()
            for r in (((data.get("quoteResponse") or {}).get("result")) or []):
                nse_sym = rev.get(r.get("symbol"))
                price = r.get("regularMarketPrice")
                if not nse_sym or price is None:
                    continue
                out[nse_sym] = {
                    "price": float(price),
                    "prev_close": float(r["regularMarketPreviousClose"]) if r.get("regularMarketPreviousClose") is not None else None,
                    "open": float(r["regularMarketOpen"]) if r.get("regularMarketOpen") is not None else None,
                    "high": float(r["regularMarketDayHigh"]) if r.get("regularMarketDayHigh") is not None else None,
                    "low": float(r["regularMarketDayLow"]) if r.get("regularMarketDayLow") is not None else None,
                    "chg_pct": float(r["regularMarketChangePercent"]) if r.get("regularMarketChangePercent") is not None else None,
                    "asof": fetched_at,
                    "source": SOURCE_TAG,
                }
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as e:
            log.warning("yahoo_live_quote batch %d-%d failed (%s symbols dropped, not fabricated): %s",
                        i, i + len(batch), len(batch), e)
            continue   # this batch's symbols simply stay absent -- never a fabricated value
    return out
