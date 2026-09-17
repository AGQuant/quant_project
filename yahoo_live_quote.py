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

ENDPOINT CORRECTION (cc#2096, 15-Sep-2026) -- the original v7/finance/quote batch call above was
"not live-verified from this session" by design (this sandbox cannot reach Yahoo either); cc#2094
(equity_cmp_poll.py) reused this function and got the first real production evidence: 0 of 52 real
symbols returned, no exception -- the exact failure signature of v7/finance/quote's crumb+cookie
auth requirement (industry-standard since ~2022 for unauthenticated callers), not a network fault.
fetch_live_quotes() below now calls v8/finance/chart/{ticker} instead -- the SAME endpoint
yahoo_daily_update.py/yahoo_symbol_resolver.py already use and have PROVEN reachable in production,
confirmed no auth needed. Trade-off, stated plainly: v8/chart is NOT batchable (one symbol per
request, unlike v7/quote's comma-separated batch) -- fetched CONCURRENTLY instead, at the exact
sem_size=3/sleep_s=0.4 settings yahoo_daily_update.SEMAPHORE_DEFAULT/SLEEP_DEFAULT already run in
production for the nightly EOD backfill, reused rather than guessed at a faster, unverified rate.
For mobile_home2()'s full-universe ADR/breadth call (~208-212 symbols) that means roughly 1-2
minutes per outage-triggered request, not the 3.5+ minutes a naive sequential 1-req/sec throttle
would cost -- slow is an acceptable trade for a rare outage-fallback path; silently returning
nothing forever, the prior behaviour, is not. Price/OHLC are read from the chart response's own
`meta` object (regularMarketPrice/previousClose/regularMarketDayHigh/Low -- long-stable, public
Yahoo chart fields) with a fallback to the latest daily bar's close when meta is absent, so a
partial/unusual response still degrades to "absent" rather than a fabricated value, same principle
as before. Every failure path still degrades to "symbol absent from the result", never raising or
fabricating -- unchanged from the original design, only the transport underneath it changed.

SOURCE TAGGING -- never disguised as a genuine Fyers tick, matching domestic_live()'s own existing
two-tier "source": "live_intraday" / "eod_fallback" convention. This adds a third: "yahoo_live_
fallback". Every value this module returns carries it.

RECOVERY -- automatic, no state to reset. mobile_home2() calls fyers_eq_outage() FRESH on every
request; there is nothing to persist. The instant the fyers_eq leg's newest bar is recent again,
the very next request's check returns False and mobile_home2.py's normal domestic_live()/
market_mood() path runs unchanged -- the Yahoo values were never cached or reused.
"""
import asyncio
import logging
import os
import urllib.parse
from datetime import datetime, timedelta, timezone, time as dt_time

import httpx

from feed_guardian import _leg_ages, STALE_MIN   # cc#1417: REUSE, not a second detector

log = logging.getLogger("scorr.yahoo_live_quote")

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = dt_time(9, 15)
MARKET_CLOSE = dt_time(15, 30)

# cc#2096: v8/chart, not v7/quote -- see the module doc's ENDPOINT CORRECTION section for why.
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
TIMEOUT_SEC = 15          # a per-symbol chart call over real network latency, not the old 4s
                          # batch-call budget -- matches yahoo_daily_update.py's own chart timeout
SEM_SIZE = 3              # reused verbatim from yahoo_daily_update.SEMAPHORE_DEFAULT -- the one
SLEEP_S = 0.4             # concurrency/pace this codebase has actually run against Yahoo in
                          # production (nightly EOD backfill), not a fresh guess for this call
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


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


async def _fetch_one(client, sem, nse_sym, fetched_at):
    """One symbol's chart call, semaphore-limited (cc#2096). Returns (nse_sym, quote_or_None).
    meta carries the live price directly; the last daily bar is the fallback when meta is thin --
    either way, a genuinely unresolvable symbol returns None, never a fabricated number."""
    ticker = _yahoo_symbol(nse_sym)
    url = CHART_URL.format(ticker=urllib.parse.quote(ticker)) + "?interval=1d&range=5d"
    async with sem:
        try:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
            result = (data.get("chart") or {}).get("result") or []
            if not result:
                return nse_sym, None
            res = result[0]
            meta = res.get("meta") or {}
            price = _f(meta.get("regularMarketPrice"))
            prev_close = _f(meta.get("previousClose") or meta.get("chartPreviousClose"))
            day_high = _f(meta.get("regularMarketDayHigh"))
            day_low = _f(meta.get("regularMarketDayLow"))
            day_open = None
            if price is None or day_open is None or day_high is None or day_low is None:
                # meta thin -- fall back to the latest bar (yahoo_daily_update.py's own proven
                # parse shape: indicators.quote[0].{open,high,low,close}[])
                q = (res.get("indicators") or {}).get("quote") or [{}]
                q = q[0]
                closes = q.get("close") or []
                opens = q.get("open") or []
                highs = q.get("high") or []
                lows = q.get("low") or []
                last_close = next((c for c in reversed(closes) if c is not None), None)
                if price is None:
                    price = _f(last_close)
                if day_open is None:
                    day_open = _f(next((o for o in reversed(opens) if o is not None), None))
                if day_high is None:
                    day_high = _f(next((h for h in reversed(highs) if h is not None), None))
                if day_low is None:
                    day_low = _f(next((l for l in reversed(lows) if l is not None), None))
                if prev_close is None and len(closes) >= 2:
                    prev_close = _f(next((c for c in reversed(closes[:-1]) if c is not None), None))
            if price is None:
                return nse_sym, None
            chg_pct = round((price / prev_close - 1) * 100.0, 2) if prev_close else None
            return nse_sym, {"price": price, "prev_close": prev_close, "open": day_open,
                              "high": day_high, "low": day_low, "chg_pct": chg_pct,
                              "asof": fetched_at, "source": SOURCE_TAG}
        except Exception as e:
            log.warning("yahoo_live_quote chart fetch %s failed (dropped, not fabricated): %s",
                        nse_sym, e)
            return nse_sym, None
        finally:
            await asyncio.sleep(SLEEP_S)


async def _fetch_live_quotes_async(symbols, budget_sec=None):
    fetched_at = _ist_now().replace(microsecond=0).isoformat()
    uniq = list(dict.fromkeys(s for s in symbols if s))
    sem = asyncio.Semaphore(SEM_SIZE)
    out = {}
    async with httpx.AsyncClient(
        timeout=TIMEOUT_SEC,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        limits=httpx.Limits(max_connections=SEM_SIZE * 2, max_keepalive_connections=SEM_SIZE),
    ) as client:
        tasks = [asyncio.ensure_future(_fetch_one(client, sem, s, fetched_at)) for s in uniq]
        if budget_sec is None:
            results = await asyncio.gather(*tasks, return_exceptions=False)
            return {sym: q for sym, q in results if q is not None}
        # cc#2198: a HARD overall budget. Whatever answered inside it is returned; the rest is
        # cancelled and simply absent (never zero-filled). The pacing above (SEM_SIZE x SLEEP_S per
        # symbol) means a long list can never fit a short budget -- callers with a deadline pass
        # short lists; the full-universe sweep belongs on a background thread (mobile_home2).
        done, pending = await asyncio.wait(tasks, timeout=budget_sec)
        for t in pending:
            t.cancel()
        if pending:
            log.warning("yahoo_live_quote: %.1fs budget hit -- %d of %d symbols answered, %d dropped "
                        "(not fabricated)", budget_sec, len(done), len(tasks), len(pending))
            await asyncio.gather(*pending, return_exceptions=True)   # let the cancels unwind before the client closes
        for t in done:
            try:
                sym, q = t.result()
            except Exception:
                continue
            if q is not None:
                out[sym] = q
    return out


def fetch_live_quotes(symbols, budget_sec=None):
    """Live quote fetch, one v8/finance/chart call per symbol, concurrency-limited (cc#2096 --
    see the module doc's ENDPOINT CORRECTION for why this is no longer the v7/finance/quote batch
    call). Returns {nse_symbol: {price, chg_pct, prev_close, open, high, low, asof, source}} for
    whatever Yahoo actually returned -- a symbol Yahoo didn't return, or whose request failed
    outright, is simply ABSENT from the result. Never zero-filled, never a stale/cached value
    passed off as fresh. Synchronous on the outside (both call sites are plain `def`s on a worker
    thread -- a FastAPI sync route handler or a scheduler background thread, never the running
    event loop), asyncio.run() is safe to use here."""
    if not symbols:
        return {}
    return asyncio.run(_fetch_live_quotes_async(symbols, budget_sec))
