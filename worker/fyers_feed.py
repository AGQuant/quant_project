"""
Fyers Live Feed - Scorr V8
============================
Standalone Railway WORKER (not in FastAPI). The live intraday source.

Architecture (v6 — 5-MIN SYSTEM, equity + futures + options on single WS):
  1. BACKFILL  - on boot, one-time 7-day history for ALL equity symbols (skip-if-fresh, async).
  2. LIVE WS   - single persistent WebSocket, up to 5000 symbols (Fyers v3 limit).
                 * 211 equity  (NSE:SYMBOL-EQ)   → source='fyers_eq', timeframe='5m'
                 * 209 futures (NSE:SYMBOLMNTHFUT) → source='fyers_fut', timeframe='5m'
                 * ~1040 options (top-50 mcap + NIFTY + BANKNIFTY, ATM±10 CE+PE)
                   → stored in option_chain table, 5-min bars
  3. OI POLL   - futures OI via DEPTH REST every OI_POLL_MINS
                 (quotes API has NO OI — Fyers KB confirmed; depth is the only source).
  4. HEAL GAP  - daily at 18:00 IST: checks equity symbols, fills missing bars.
  5. CMP FLUSH - every 30s during market hours → cmp_prices (IST timestamp).
  6. ATM ROLL  - every 15 min during market hours: recheck ATM per option symbol,
                 re-subscribe if drifted ±2 strikes.
  7. MONTHLY ROLL - on expiry day (last Tuesday): rebuild futures + option symbol lists.
  8. PURGE     - rolling (cc#297): intraday_prices source='fyers_eq' at EQUITY_RETENTION_DAYS
                 (365d, long sim/BT7 history); fyers_fut/legacy AND futures_basis at
                 INTRADAY_FUT_RETENTION_DAYS (7d); option_chain at OPTION_RETENTION_DAYS (7d).
                 Rows older deleted daily. Equity and futures_basis no longer share a constant.

v6.1 (10-Jun-2026):
  * CRITICAL DEADLOCK FIX: flush_all() holds agg.lock while _flush → _compute_basis
    tried to re-acquire it for the last_oi fallback. threading.Lock is NOT
    re-entrant → housekeeping thread froze on the first futures bar flush and
    every WS tick then blocked on the same lock (feed frozen 13:54 IST).
    Fix: agg.lock is now an RLock AND _compute_basis reads last_oi without
    locking (CPython dict .get is GIL-atomic).
  * OPTION SYMBOL MASTER: ladders from Fyers NSE_FO master (actually-listed strikes).
  * INDEX/ETF LTP: NIFTY500, GOLDBEES, SILVERBEES in the 30s quotes poll.
  * OI POLL DEBUG: start/first-response logging + dict/list response handling.

v6.2 (15-Jun-2026):
  * RETENTION SPLIT: intraday_prices + futures_basis extended 7d → 30d to bank
    real 5-min history for the intraday filter optimizer. option_chain stays 7d
    (heaviest churn, not used by the sim) via OPTION_RETENTION_DAYS. purge_old_bars
    now uses two cutoffs.
  * cc#297 (08-Jul-2026): retention DECOUPLED. fyers_eq extended 30d → 365d via its own
    EQUITY_RETENTION_DAYS; futures_basis moved back to the 7d INTRADAY_FUT_RETENTION_DAYS
    window (it had been dragged to 30d by the shared constant). fyers_fut/option_chain/
    global_intraday unchanged at 7d. Equity and futures_basis no longer share a constant.

5-MIN SYSTEM (canonical spec session_log id=167):
  All rolling intraday feeds store at 5-min granularity. NOT a flash/1-min system.
  1-min is deprecated as default (future on-demand only — flip BAR_MINUTES).

TOKEN MODEL (Fyers v3, SEBI framework from 01-Apr-2026):
  Refresh-token flow is DISABLED. ONE 2FA login per TRADING DAY.
  access_token valid the whole trading day, survives restarts.
  Stored in Railway table fyers_tokens (id=1).

  Boot logic (get_valid_token):
    1. --auth-code given  -> bootstrap (mint + store today's token).
    2. else stored access_token created TODAY AND verified live -> reuse it.
    3. else -> AUTO-LOGIN via TOTP (headless) -> store + return. Zero-touch.

USAGE:
  Normal (zero-touch): python fyers_feed.py
  Manual override:     python fyers_feed.py --auth-code <code>
"""

# ops: worker bounce 12-Jul-2026 (weekend, market closed) to resume the stalled cc#389/#390 Phase A
# 5m warehouse backfill. Re-bounce #2: post-#416 restart, phase_a_run='pending' so the boot-claim
# (_claim_phase_a_worker) fires immediately and resumes the 365d warehouse from checkpoint
# (BAJAJHLDNG, 26/212 done) instead of waiting for the hourly idle check. No logic change.

import argparse, bisect, calendar, hashlib, os, sys, json, time, logging, threading, re
from datetime import datetime, timedelta, time as dt_time, date
# cc#416: this file now lives in worker/. When run as the worker entry (`python worker/fyers_feed.py`)
# sys.path[0] is worker/, so the repo-root modules it still uses (nse_holidays, fyers_backfill) are not
# importable without adding the repo root. Idempotent + harmless when imported by the app (root already
# on path). Worker-internal siblings (fyers_autologin, fyers_hist_backfill) resolve via worker/.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)
import pytz, psycopg2, psycopg2.errors, requests
from nse_holidays import is_trading_day   # cc#188: market-hours gate for subscribe_verify
# cc#1056: THIRD app-shared root module. The source sets live in exactly one place so the app
# and the worker cannot disagree about what counts as a futures bar. Adding it here also adds
# it to railway.worker.json watchPatterns — a shared module the worker imports but does not
# watch is a module that can change under a running worker without redeploying it.
from price_sources import NOT_FUT_SQL, FUT_SOURCES

FYERS_CLIENT_ID = os.environ.get('FYERS_CLIENT_ID', '1A4STS8ZGD-100')
FYERS_SECRET    = os.environ.get('FYERS_SECRET',    '')
FYERS_PIN       = os.environ.get('FYERS_PIN',       '')
DATABASE_URL    = os.environ.get('DATABASE_URL')

AUTHCODE_URL      = 'https://api-t1.fyers.in/api/v3/validate-authcode'
QUOTES_URL        = 'https://api-t1.fyers.in/data/quotes'
DEPTH_URL         = 'https://api-t1.fyers.in/data/depth'
OPTION_MASTER_URL = 'https://public.fyers.in/sym_details/NSE_FO.csv'
IST               = pytz.timezone('Asia/Kolkata')

# cc#297: retention constants DECOUPLED. fyers_eq (equity) gets its OWN 365d constant; every other
# intraday store — fyers_fut, residual legacy bars, AND futures_basis — uses the 7d futures window.
# The old shared RETENTION_DAYS (=30, drove BOTH fyers_eq and futures_basis) is removed so an equity
# bump can never silently drag futures_basis along again.
EQUITY_RETENTION_DAYS = 730   # intraday_prices source='fyers_eq' ONLY (30→365 08-Jul; →730 cc#381 11-Jul: 2yr rolling for replay/sim depth)
HIST_RETENTION_DAYS   = 730   # cc#381: source='fyers_hist' backtest warehouse — 2yr rolling (matches equity; was purge-exempt in cc#377)
INTRADAY_FUT_RETENTION_DAYS = 7   # cc#227: fyers_fut + residual legacy fyers/yahoo intraday bars — AND futures_basis (cc#297)
# ── cc#809 LIVE FEED EXPANSION ────────────────────────────────────────────────────────────────────
# The full CSV/screener equity universe streams on the SAME single Fyers WS as the F&O set. Its bars
# are tagged with their OWN source so retention, health counts and the watchdog can all tell the two
# apart. This is deliberate: reusing 'fyers_eq' would silently give ~1,600 extra symbols the 730-day
# F&O retention (the cc#381 2-yr re-optimisation window) and would inflate the watchdog's eq count so
# a total collapse of the F&O leg could hide behind healthy extended-leg numbers.
EXT_SOURCE            = 'fyers_ext'   # intraday_prices.source for the extended (non-F&O) equity leg
EXT_RETENTION_DAYS    = 30            # founder-approved: 30d rolling (~+370 MB steady state)
EXT_STAGE_FLAG        = 'feed_ext_stage'   # app_config: 'off' | '<int>' | 'all' — staged rollout dial
EXT_STAGE_DEFAULT     = 'off'         # cc#809 ships DARK. Nothing subscribes until the flag is set.
EXT_VERIFY_WAIT_SEC   = 180           # post-subscribe settle before the extended leg is graded
EXT_MIN_TICK_FRACTION = 0.25          # <25% of the extended leg writing bars => treat the stage as failed
# cc#1002 (founder 11-Aug): staged RAMP of the extended equity leg toward the full ~1,800 universe,
# with an engineered RETREAT to the last-good stage instead of all the way to 'off'. Stages are
# ext-leg sizes; total equity ~= 209 F&O + this. Advancing a stage is ONE app_config UPDATE of
# feed_ext_stage (no code push, no mid-market reboot); the retreat below is what makes raising it
# safe against the incident-8 signature (20-Jul: a batch too large -> broker force-close -> 94-min
# freeze). The retreat only fires when the CURRENT stage is ABOVE the last-good floor (an active
# ramp) — at or below the proven baseline it keeps the cc#809 'off' behaviour, so there is no loop.
EXT_RAMP_STAGES   = (500, 790, 1190, 1590)   # ~707 / ~1000 / ~1400 / ~1800 total equity (ext-leg sizes)
EXT_LASTGOOD_FLAG = 'feed_ext_stage_last_good'   # app_config int: largest stage that held a full clean session
# Tier 3 of the cc#809 retention scheme. Index 5-min bars are written by update_index_ltp() through
# agg.on_tick(..., source='fyers_eq') — they are NOT a separate source, and re-tagging them would
# break every consumer that reads index candles (the /api/intraday and v10 paths both filter on
# source IN ('fyers_eq','fyers_hist')). So the 5-year tier is carved out BY SYMBOL instead, which is
# a purely additive change: these symbols currently roll at 730d, so widening to 1825d can only
# retain more, never delete anything the old rule kept.
INDEX_RETENTION_DAYS  = 1825          # NIFTY50/BANKNIFTY/INDIAVIX/NIFTY500/GOLDBEES/SILVERBEES
MARKET_OPEN    = dt_time(9, 15)
# cc#843 FOUNDER-LOCKED 03-Aug: DO NOT CONNECT PRE-OPEN AT ALL.
# Root cause (Claude-web diagnosis 03-Aug, ops_log 14428-14477): a Fyers WS established PRE-OPEN
# comes up SUBSCRIBE-DEAD. Equity registered at connect time streams, but derivatives never activate
# and every later subscribe on that connection is ACKED (sub/200/Subscribed) yet silently NOT
# PROCESSED. The smoking gun is the ABSENCE of an error: the sick 09:21 connection took a batch
# containing 12 provably-invalid symbols and returned ZERO -300 frames, while the fresh 09:24
# connection given the identical payload rejected all 12 immediately and reached fut 208/208 +
# ext 480/488 by 09:27. Acks are meaningless on a sick connection.
# So we no longer establish a connection we know comes up poisoned. 09:05 boot does token remint +
# REST self-test only; the FIRST AND ONLY WS connect happens at 09:16.
# WHY 09:16 AND NOT 09:20: the 09:15 bar feeds V14 ORB (09:15-09:30 observe) and the mood-gate open
# read. Connecting at 09:16 captures 09:16-09:20 ticks, i.e. a partial-but-real 09:15 bar with the
# correct close; 09:20 would lose that bar entirely for 1,800+ symbols. ACCEPTED COST, founder-
# informed: equity gives up the 09:15:00-09:16:00 ticks it used to get from the pre-open connection.
# Correctness of the futures + extended legs from the open is worth more than 60s of equity ticks.
WS_FIRST_CONNECT = dt_time(9, 16)

# ── cc#855 SEBI CLOSING AUCTION SESSION (CAS), live 03-Aug-2026 ────────────────────────────────
# SEBI circular HO/47/11/11(3)2025-MRD-POD2/I/2765/2026 (16-Jan-2026). The single
# MARKET_CLOSE = 15:30 that used to gate every write is now WRONG IN BOTH DIRECTIONS: it accepted
# dead-tape equity bars between 15:15 and 15:30, and it truncated real futures bars between 15:30
# and 15:40. There is no longer one market close — there are three, by segment.
#
# WHO IS AFFECTED. CAS applies to CATEGORY I (F&O-eligible) cash stocks ONLY. That is exactly the
# `fyers_eq` leg, which is subscribed from futures_universe.is_active. The `fyers_ext` leg is the
# EXTENDED NON-F&O equity universe (cc#809) and is NOT in scope of the circular — those stocks keep
# continuous trading to 15:30 and have no auction. Conflating the two legs would have tagged ~480
# non-F&O symbols as auction bars they never had.
EQ_CONTINUOUS_END = dt_time(15, 15)   # Category I cash: continuous trading ENDS
EQ_AUCTION_END    = dt_time(15, 35)   # Category I cash: auction matched, closing price finalised
EQ_NONFNO_CLOSE   = dt_time(15, 30)   # non-F&O cash (fyers_ext): UNCHANGED, no auction
FUT_CLOSE         = dt_time(15, 40)   # equity derivatives (futures + options); VWAP window 15:10-15:40

# The operational envelope: "is the trading day still running at all". Used by the boot-gap check,
# the backfill hold, reconnect logic and the health loop — none of which reason about segments, all
# of which must now stay open until the LAST segment stops. This is deliberately NOT a rename of
# MARKET_CLOSE: it answers a different question ("is the session live") than the write guards
# ("may THIS segment legitimately produce THIS bucket"), and it must never be used to gate a write.
SESSION_END = FUT_CLOSE

# Auction-window tags on intraday_prices.source. The column already exists, so no DDL is needed
# (MAINTENANCE_LOCK_RULE id=3041 would have blocked an ALTER here anyway).
AUCTION_WINDOW_SOURCE = 'fyers_eq_auction'   # 15:15-15:30 — order collection, NOT continuous trade
AUCTION_CLOSE_SOURCE  = 'auction'            # 15:30-15:35 — the official closing-auction print

# Option bars land in option_chain, not intraday_prices, so they never pass through
# bar_source_tag() — their flush guards on FUT_CLOSE directly. Listed here anyway so the
# set reads as 'what is a derivative', which is the question segment_close() asks.
_DERIVATIVE_SOURCES = {'fyers_fut', 'fyers_fut_rest', 'fyers_opt'}


def segment_close(source: str) -> dt_time:
    """Latest bucket time `source` may legitimately produce. Anything at or after it is phantom."""
    if source in _DERIVATIVE_SOURCES:
        return FUT_CLOSE
    if source == EXT_SOURCE:
        return EQ_NONFNO_CLOSE
    return EQ_AUCTION_END          # fyers_eq / fyers_hist — Category I cash


def bar_source_tag(source, bt_time):
    """cc#855: decide whether a bar is keepable AND under what source tag it must be stored.

    Returns the source to PERSIST, or None if the bar must be rejected as off-session.

    The auction is NOT price action. Between 15:15 and 15:30 a Category I stock has no continuous
    market — orders are only being collected — and the 15:30 print is a single matched auction
    price that routinely moves 1.5-2% on 50x normal volume (04-Aug: APOLLOHOSP 8884 -> 9050 on
    2.59L shares; 11 of the top 12 moves that day were UP, i.e. a market-wide imbalance, not news).
    Those bars are CAPTURED because the auction close is the official close and EOD reconciliation
    needs it, but they are TAGGED so no momentum metric can ever mistake them for a 5-min bar.
    """
    if bt_time < MARKET_OPEN or bt_time >= segment_close(source):
        return None
    if source in _DERIVATIVE_SOURCES or source == EXT_SOURCE:
        return source              # derivatives + non-F&O cash trade continuously; nothing to tag
    if bt_time >= EQ_NONFNO_CLOSE:     # 15:30-15:35 → the official auction close print
        return AUCTION_CLOSE_SOURCE
    if bt_time >= EQ_CONTINUOUS_END:   # 15:15-15:30 → auction order-collection window
        return AUCTION_WINDOW_SOURCE
    return source

INDEX_LTP_SYMBOLS = {
    'NIFTY50':    'NSE:NIFTY50-INDEX',
    'BANKNIFTY':  'NSE:NIFTYBANK-INDEX',
    'INDIAVIX':   'NSE:INDIAVIX-INDEX',
    'NIFTY500':   'NSE:NIFTY500-INDEX',
    'GOLDBEES':   'NSE:GOLDBEES-EQ',
    'SILVERBEES': 'NSE:SILVERBEES-EQ',
}

SKIP_SYMBOLS    = {'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX',
                    'NIFTY50'}  # cc#489 step_6: distinct row from 'NIFTY' in futures_universe —
                    # was leaking into get_universe() and producing two invalid Fyers
                    # subscriptions (NSE:NIFTY50-EQ has no equity listing; NSE:NIFTY5026JULFUT
                    # is not a real contract — the futures root is 'NIFTY', already covered by
                    # INDEX_FUTURES_UNIVERSE below).
SPECIAL_SYMBOLS = {'M&M': 'NSE:M&M-EQ'}

# ── Option chain config ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
OPTION_RETENTION_DAYS = 7      # option_chain stays lean (heaviest churn, not used by sim)
ATM_CHECK_MINS        = 15     # re-check ATM every 15 min
ATM_DRIFT_STRIKES     = 2      # re-subscribe if ATM drifts by this many strikes
N_STRIKES             = 30     # cc#320: INDEX options ATM±30 (61 strikes, was ±10/21) — fixes the
                               # PCR window-drift bias where strikes exited the tracked band as spot
                               # moved and silently dropped their OI from the PCR sums. Index only
                               # (NIFTY/BANKNIFTY); STOCK options stay STOCK_N_STRIKES=3. 7d retention
                               # (purge_old_bars) already covers the wider band. Capacity: index
                               # 84->244 contracts, feed total ~4170->~4330 (< ~5000 WS budget).
# cc#189 (founder redesign 04-Jul): options subscribe ONLY when live prices are
# fresh. No boot/REST hydration — a cold-boot/pre-market restart just waits for
# the market + a fresh cmp_prices tick set, then computes ATM from LIVE prices.
OPT_FRESH_MIN_FRAC    = 0.80          # >=80% of option underlyings must have a fresh tick
OPT_FRESH_WINDOW_MIN  = 10           # "fresh" = cmp_prices tick within the last N minutes
OPT_SUB_DEADLINE      = dt_time(9, 30)  # still unsubscribed by this IST time -> CRITICAL alert
OPT_STOCK_SUB_MIN_TIME = dt_time(9, 25)  # cc#241: HARD floor — no stock-option subscribe/write
                                          # before 09:25 IST (10 min for the open to settle so ATM
                                          # anchors on a real print). Index options are NOT gated.
OPT_STOCK_OVERFLOW_FRAC = 0.95           # cc#241: <95% stock underlyings subscribed -> overflow alert
BAR_MINUTES           = 5      # 5-min system: all rolling intraday bars at 5-min granularity
OI_POLL_MINS          = 5      # poll futures OI via DEPTH REST every N min (quotes has NO OI)
STOCK_OI_POLL_MIN_TIME = dt_time(9, 30)  # cc#482 fix_5: stock ATM OI poll held off till 09:30 (skips
                                          # the noisiest opening 15 min; index OI poll unaffected, still 09:15)
CMP_FLUSH_MINS        = 5      # flush cmp_prices every N min (was 30s; throttled 14-Jun-2026)
OI_CALL_SPACING_SEC   = 0.35   # ~170 req/min — under Fyers 200/min data limit

# ── feed heartbeat / health / watchdog (cc_task #84 + #85) ────────────────────
# The WS stream for the 212 stock futures crashed at the 09:15 open on 25-Jun and
# did not auto-reconnect until ~11:25 — a 2h15m data gap that fed stale prices to
# V8 paper, trade-check and the dashboard. These guard the live stream.
HEARTBEAT_STALE_MINS    = 10   # window for "wrote a live bar recently"
HEALTH_LOG_MINS         = 5    # cc#489: also the watchdog's single check interval
WATCHDOG_MIN_SYMBOLS    = 100  # cc#489 WATCHDOG_SIMPLIFICATION: per-source floor (out of ~210 each)
TOTAL_FUTURES           = 212  # denominator for the N/212 health log
CMP_STALE_GUARD_SECS    = 90   # cc_task #112: only (re)write a cmp_prices row when its tick is
                               # newer than the last flush — no fresh tick => no timestamp update
CMP_SANITY_MAX_DEV      = 0.02 # cc#367: reject a cmp write that deviates >2% from the symbol's most
                               # recent completed equity 5m bar (same session) — a polluted spot tick.
STARTUP_GRACE_MINS      = 10   # suppress the watchdog this long after 09:15 (bars need time to form)
WS_SUB_BATCH            = 200  # cc_task #88: subscribe in 200-symbol batches (Fyers silently drops bulk subs at open)
WS_SUB_BATCH_SLEEP_SEC  = 2    # seconds between subscription batches

NIFTY_STEP   = 50
BNIFTY_STEP  = 100
STOCK_STEPS  = {               # FALLBACK only (master-driven ladder is primary)
    'RELIANCE': 20, 'TCS': 50, 'HDFCBANK': 10, 'INFY': 10, 'ICICIBANK': 10,
    'HDFC': 10, 'SBIN': 5, 'BHARTIARTL': 5, 'KOTAKBANK': 20, 'LT': 20,
    'AXISBANK': 5, 'WIPRO': 5, 'MARUTI': 100, 'BAJFINANCE': 50, 'TITAN': 20,
}

INDEX_OPTION_UNDERLYINGS = {
    'NIFTY':     {'fyers_index': 'NSE:NIFTY50-INDEX',   'step': NIFTY_STEP,  'cmp_sym': 'NIFTY50'},
    'BANKNIFTY': {'fyers_index': 'NSE:NIFTYBANK-INDEX', 'step': BNIFTY_STEP, 'cmp_sym': 'BANKNIFTY'},
}

OPTION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS option_chain (
    id          SERIAL PRIMARY KEY,
    symbol      TEXT    NOT NULL,
    underlying  TEXT    NOT NULL,
    strike      NUMERIC NOT NULL,
    option_type TEXT    NOT NULL,
    expiry      DATE    NOT NULL,
    ltp         NUMERIC,
    oi          BIGINT,
    volume      BIGINT,
    bid         NUMERIC,
    ask         NUMERIC,
    ts          TIMESTAMP NOT NULL,
    UNIQUE (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_option_chain_ts         ON option_chain(ts DESC);
CREATE INDEX IF NOT EXISTS idx_option_chain_underlying ON option_chain(underlying, ts DESC);
"""

FUTURES_BASIS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS futures_basis (
    id            SERIAL PRIMARY KEY,
    symbol        TEXT    NOT NULL,
    ts            TIMESTAMP NOT NULL,
    spot_close    NUMERIC,
    futures_close NUMERIC,
    basis         NUMERIC,
    basis_pct     NUMERIC,
    oi            BIGINT,
    oi_prev       BIGINT,
    oi_chg        BIGINT,
    UNIQUE(symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_futures_basis_symbol_ts ON futures_basis(symbol, ts DESC);
"""

# cc#809: the extended leg is built from ~1,600 UNVALIDATED screener nse_codes, so some of them are
# certain not to be live NSE:XXX-EQ instruments (delisted, renamed, SME/other series). Fyers answers
# those with a WS -300 invalid_symbols frame. The F&O legs persist such a drop via
# futures_universe.is_active, but an extended symbol has no row there — without its own store it
# would be re-subscribed on every single boot and re-trigger the same -300 forever. This table is
# that store, and get_extended_universe() subtracts it.
EXT_BLACKLIST_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feed_ext_blacklist (
    symbol     TEXT PRIMARY KEY,
    reason     TEXT,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('fyers_feed')



# ── helpers ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
# cc#876 — THE ROOT CAUSE OF THE 05/06-AUG 22-HOUR OUTAGE, fixed at the connect.
#
# This used to be a bare psycopg2.connect(DATABASE_URL): no connect_timeout, no TCP keepalives,
# no statement_timeout. A silently dropped socket (Railway proxy idle-drop, NAT timeout, a DB
# restart) leaves the kernel with no idea the peer is gone, so the next execute() sends bytes into
# a black hole and waits for a reply that never comes. It does not raise. It does not return.
#
# That single property defeated EVERY guard in this file, because all of them are built on
# `except`: _mark_db_error only flags psycopg error classes, consecutive_db_failures >= 3 only
# counts raised errors before its os._exit(1), and _housekeeping_supervised wraps the loop in
# `except Exception`. None of them can see a call that never returns. On 05-Aug the housekeeping
# loop blocked here at ~15:28 and the process sat alive-but-silent for 22 hours.
#
# The three settings below convert that hang into an ordinary OperationalError, which the existing
# machinery already knows how to handle: reconnect once, count the failure, exit loudly at three.
#   keepalives_*   — the important one. The kernel probes a dead peer and errors the socket in
#                    roughly 60s instead of waiting forever.
#   connect_timeout — a connect to a black hole fails fast instead of blocking the caller.
#   statement_timeout — a backstop for a query that is served but never completes. 120s is far
#                    above anything this file runs (bar upserts, small health reads), so it can
#                    only ever fire on something already pathological.
DB_CONNECT_TIMEOUT_S    = 10
DB_STATEMENT_TIMEOUT_MS = 120000


def get_db():
    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=DB_CONNECT_TIMEOUT_S,
        keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
        options=f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS}",
    )
# cc#876: module-level so BOTH on_connect (defined early) and _force_reconnect (defined much
# later, inside the same enclosing function) bind the same object regardless of definition order.
RECONNECT_DEADLINE_S = 120
_RECONNECT_CONFIRMED = threading.Event()

# cc#1017: EXTENDED-leg post-reconnect recovery state. 14-Aug incident: after a Fyers-side reconnect the
# batch re-subscribe was ACKNOWLEDGED but only HONOURED for the legacy legs — the extended 909 never
# resumed ticking, and verification (eq/fut only, fixed floors) PASSED while 43% of the universe was
# dead for 3.5h. This counter bounds how many rebuilds the ext-recovery probe spends before it stops
# thrashing the healthy core and escalates loudly (FYERS_INTEGRATION_LEARNINGS: loud failure over quiet
# degradation). Reset to 0 the moment the ext leg is seen ticking again.
_EXT_RECOVERY = {"attempts": 0}
EXT_RECOVERY_MAX_REBUILDS = 2   # after this many rebuilds fail to restore the ext leg -> retreat + CRITICAL


def app_id_hash(): return hashlib.sha256(f'{FYERS_CLIENT_ID}:{FYERS_SECRET}'.encode()).hexdigest()


def last_tuesday(y, m):
    """Last Tuesday of month y/m — NSE expiry since Sep 2025."""
    last_day = calendar.monthrange(y, m)[1]
    d = date(y, m, last_day)
    while d.weekday() != 1:   # 1 = Tuesday
        d = d.replace(day=d.day - 1)
    return d


def current_expiry() -> date:
    """Current active monthly expiry (last Tuesday). Rolls to next month after expiry.

    cc#489 step_5: date.today() uses the container's system clock (Railway = UTC),
    not IST — during IST 00:00-05:29 (UTC 18:30-23:59 the prior day) this returns
    the WRONG calendar date, which near a month boundary could resolve the wrong
    monthly contract. Same naive-IST-vs-UTC bug class as the cmp_prices fixes above."""
    today = datetime.now(IST).replace(tzinfo=None).date()
    exp = last_tuesday(today.year, today.month)
    if today > exp:
        if today.month == 12:
            exp = last_tuesday(today.year + 1, 1)
        else:
            exp = last_tuesday(today.year, today.month + 1)
    return exp


def futures_fyers_symbol(nse_code: str, expiry: date = None) -> str:
    """Build Fyers futures symbol e.g. NSE:SBIN26JUNFUT"""
    if expiry is None:
        expiry = current_expiry()
    return f"NSE:{nse_code}{expiry.strftime('%y')}{expiry.strftime('%b').upper()}FUT"


def option_fyers_symbol(underlying: str, strike, opt_type: str, expiry: date = None) -> str:
    """Build Fyers option symbol e.g. NSE:NIFTY26JUN24000CE"""
    if expiry is None:
        expiry = current_expiry()
    strike_str = str(int(strike)) if float(strike) == int(strike) else str(strike)
    return f"NSE:{underlying}{expiry.strftime('%y')}{expiry.strftime('%b').upper()}{strike_str}{opt_type}"


def atm_strike(cmp: float, step: int) -> int:
    return int(round(cmp / step) * step)


def auto_step(cmp: float) -> int:
    """Derive option strike step from CMP when not in STOCK_STEPS (fallback only)."""
    if cmp < 100:   return 5
    if cmp < 500:   return 10
    if cmp < 1000:  return 20
    if cmp < 3000:  return 50
    if cmp < 10000: return 100
    return 200


# ── option symbol master (Fyers NSE_FO CSV — actually-listed contracts) ───────────

class OptionMaster:
    """
    Loads the Fyers public NSE_FO symbol master and exposes:
      * valid_symbols  — set of every listed NSE F&O ticker
      * atm_window()   — the actual listed strikes around CMP for an underlying/expiry
    Built at boot + reloaded on monthly roll. If download/parse fails,
    loaded=False and callers fall back to step-guessing (pre-v6 behaviour).
    CSV columns (no header, community-documented):
      8=expiry epoch, 9=symbol ticker, 13=underlying, 15=strike, 16=option type.
    """
    def __init__(self):
        self.valid_symbols = set()
        self.strikes       = {}     # (underlying, expiry_date, opt_type) -> sorted [strike]
        self.loaded        = False

    def load(self):
        self.valid_symbols, self.strikes, self.loaded = set(), {}, False
        try:
            r = requests.get(OPTION_MASTER_URL, timeout=30)
            r.raise_for_status()
            rows = 0
            for line in r.text.splitlines():
                parts = line.split(',')
                if len(parts) < 17:
                    continue
                ticker = parts[9].strip()
                if not ticker.startswith('NSE:'):
                    continue
                self.valid_symbols.add(ticker)
                otype = parts[16].strip().upper()
                if otype in ('CE', 'PE'):
                    try:
                        strike = float(parts[15])
                        und    = parts[13].strip().upper()
                        exp    = datetime.fromtimestamp(int(float(parts[8])), IST).date()
                        self.strikes.setdefault((und, exp, otype), []).append(strike)
                    except Exception:
                        continue
                rows += 1
            for k in self.strikes:
                self.strikes[k] = sorted(set(self.strikes[k]))
            self.loaded = rows > 1000   # sanity: a real master has thousands of rows
            log.info(f"Option master: {rows} contracts, {len(self.strikes)} strike chains, loaded={self.loaded}")
        except Exception as e:
            log.warning(f"Option master load FAILED ({e}) — falling back to step-guessing")
            self.loaded = False

    def atm_window(self, underlying, expiry, opt_type, cmp, n=N_STRIKES):
        """Up to 2n+1 ACTUAL listed strikes centered on CMP. None if chain unknown."""
        chain = self.strikes.get((underlying.upper(), expiry, opt_type))
        if not chain:
            return None
        i  = bisect.bisect_left(chain, cmp)
        lo = max(0, i - n)
        hi = min(len(chain), i + n + 1)
        return chain[lo:hi]

    def is_valid(self, ticker):
        return (not self.loaded) or (ticker in self.valid_symbols)


# ── DB / token ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def load_tokens(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT access_token, refresh_token, access_created, refresh_created "
                    "FROM fyers_tokens WHERE id=1")
        return cur.fetchone()

def save_tokens(conn, access=None, refresh=None, new_refresh=False):
    now = datetime.now(IST).replace(tzinfo=None)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM fyers_tokens WHERE id=1")
        if cur.fetchone():
            if new_refresh:
                cur.execute("""UPDATE fyers_tokens SET access_token=%s, refresh_token=%s,
                               access_created=%s, refresh_created=%s, updated_at=NOW() WHERE id=1""",
                            (access, refresh, now, now))
            else:
                cur.execute("UPDATE fyers_tokens SET access_token=%s, access_created=%s, updated_at=NOW() WHERE id=1",
                            (access, now))
        else:
            cur.execute("""INSERT INTO fyers_tokens (id,access_token,refresh_token,access_created,refresh_created,updated_at)
                           VALUES (1,%s,%s,%s,%s,NOW())""", (access, refresh, now, now))
    conn.commit()

def bootstrap_from_authcode(conn, auth_code):
    r = requests.post(AUTHCODE_URL, json={'grant_type':'authorization_code',
        'appIdHash':app_id_hash(),'code':auth_code}, timeout=10)
    d = r.json()
    if d.get('code') != 200: raise Exception(f"Auth-code exchange failed: {d}")
    save_tokens(conn, access=d['access_token'], refresh=d.get('refresh_token'), new_refresh=True)
    log.info("Bootstrap OK - access token stored (valid for today)")
    return d['access_token']

def _token_is_live(token):
    try:
        r = requests.get(QUOTES_URL,
                         params={'symbols': 'NSE:NIFTY50-INDEX'},
                         headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'},
                         timeout=8)
        return r.json().get('s') == 'ok'
    except Exception as e:
        log.warning(f"Token liveness check failed: {e}")
        return False

def get_valid_token(conn, auth_code=None):
    if auth_code:
        try:
            return bootstrap_from_authcode(conn, auth_code)
        except Exception as e:
            log.warning(f"Auth-code bootstrap failed ({e}); falling through")

    row = load_tokens(conn)
    if row and row[0] and row[2]:
        access_token, access_created = row[0], row[2]
        today = datetime.now(IST).replace(tzinfo=None).date()
        if access_created.date() == today:
            log.info("Stored same-day token found — verifying with Fyers...")
            if _token_is_live(access_token):
                log.info("Token verified live — reusing (restart-safe)")
                return access_token
            log.warning("Stored same-day token REJECTED — re-authing")
        else:
            log.warning(f"Stored token from {access_created.date()} — re-authing")

    # cc#489 fix_1 (round 2): try_relogin() swap-in, re-landed. fyers_autologin.auto_login()
    # is now fixed (cc#489) to always use its OWN short-lived DB connection — it never
    # touches this conn — so this call site can never kill the worker's global conn.
    import fyers_autologin
    log.info("Running TOTP auto-login (headless)...")
    res = fyers_autologin.try_relogin(conn)
    if res.get('skipped'):
        log.warning("Auto-login SKIPPED (90s account-block cooldown) — sleeping 90s then retrying once...")
        time.sleep(90)
        res = fyers_autologin.try_relogin(conn)
    if res.get('ok'):
        log.info("TOTP auto-login SUCCESS — fresh token stored")
        return res['token']
    raise SystemExit(
        f"\nAUTO-LOGIN FAILED ({res.get('error')}).\n"
        "Check env vars: FYERS_TOTP_SECRET, FYERS_PIN, FYERS_SECRET, FYERS_FY_ID.\n"
        "Manual fallback:\n"
        f"  1. https://api-t1.fyers.in/api/v3/generate-authcode?client_id={FYERS_CLIENT_ID}"
        "&redirect_uri=http%3A%2F%2F127.0.0.1&response_type=code&state=None\n"
        "  2. python fyers_feed.py --auth-code <code>\n")


# ── universe ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _ist_now_str():
    return datetime.now(IST).replace(tzinfo=None).isoformat()


def _ops_log(conn, category, title, details):
    """cc#339: one ops_log row (category=alert/info). Best-effort — never raises into the boot
    path. cc#497 fix_2a: falls back to a FRESH short-lived connection if the passed conn's write
    fails, so an alert is never silently lost just because that particular conn happens to be
    dead (the 17-Jul zombie: every conn-based alert path went quiet the same morning)."""
    payload = json.dumps(details)
    try:
        with conn.cursor() as c:
            c.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                         VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)""",
                      (category, title, payload))
        conn.commit()
        return
    except Exception as e:
        log.warning(f"_ops_log({title}) failed: {e} — retrying on a fresh connection")
    hc = None
    try:
        hc = get_db()
        with hc.cursor() as c:
            c.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                         VALUES (CURRENT_DATE, NOW(), %s, %s, %s::jsonb)""",
                      (category, title, payload))
        hc.commit()
    except Exception as e2:
        log.warning(f"_ops_log({title}) fresh-conn fallback also failed: {e2}")
    finally:
        if hc is not None:
            try:
                hc.close()
            except Exception:
                pass


# ── cc#605: restart-surviving day flags (app_config) + futures-delivery probe ─────────────────────
def _flag_get(conn, key):
    """cc#605: read a persisted worker day-flag from app_config. None on miss/error (a failed read
    just lets the guarded action proceed — safe)."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key=%s", (key,))
            r = cur.fetchone()
        return r[0] if r else None
    except Exception as e:
        log.warning(f"_flag_get({key}): {e}")
        return None


def _flag_set(conn, key, val):
    """cc#605: persist a worker day-flag to app_config so it survives a hard restart. Fresh-conn
    fallback (like _ops_log) — persisting the mint-once / fut-restart-once flag is the anti-loop
    guarantee, so it must not be silently lost on a stale shared conn."""
    for target in ("shared", "fresh"):
        hc = get_db() if target == "fresh" else conn
        try:
            with hc.cursor() as cur:
                cur.execute("INSERT INTO app_config (key,value,updated_at) VALUES (%s,%s,NOW()) "
                            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()",
                            (key, val))
            hc.commit()
            return True
        except Exception as e:
            log.warning(f"_flag_set({key}) via {target}: {e}")
        finally:
            if target == "fresh":
                try:
                    hc.close()
                except Exception:
                    pass
    return False


# ── cc#660 FEED_GUARDIAN_V1: worker heartbeat + guardian resubscribe-command channel ──────────────
# The app-side feed_guardian is the brain (detect + decide); the worker is the hands. The worker
# (a) touches worker_heartbeat every ~5 min ANY hour it runs, so the app's off-hours liveness tick
# turns a weekend worker death into a <=45-min Telegram instead of a 13h blind spot (Sat 25-Jul);
# (b) polls app_config['feed_guardian_cmd'] and executes a forced RESUBSCRIBE (or a RESTART when the
# guardian escalates) at most ONCE per monotonic nonce, writing the outcome back to
# app_config['feed_guardian_cmd_ack'] + ops_log. All best-effort — never raises into the loop.
HEARTBEAT_WRITE_MIN = 5    # write cadence; well under the app's 45-min silence alarm for margin
_GUARDIAN_CMD_KEY   = "feed_guardian_cmd"
_GUARDIAN_ACK_KEY   = "feed_guardian_cmd_ack"


def _write_heartbeat(conn, boot_ts):
    """cc#660: upsert the single worker_heartbeat row (id=1). Fresh-conn fallback like _flag_set so
    a stale shared conn never silently stops the heartbeat (which would trip a false worker-silent
    alarm).

    cc#1022 HEARTBEAT_IST_CONTRACT_V1 — THE CONTRACT, and it binds every reader:
    BOTH columns are naive IST. last_beat is the DB clock converted to Asia/Kolkata; boot_ts is the
    worker's own IST clock, passed in tz-stripped. Any age computation MUST compare against
    (NOW() AT TIME ZONE 'Asia/Kolkata'), never bare NOW().

    Before this card last_beat was written with bare NOW() — the Railway session is UTC, so one row
    held two different bases: last_beat UTC-naive, boot_ts IST-naive, 5h30m apart. Nothing was
    broken, because feed_guardian compensated by computing age in the same UTC basis (the cc#844
    contract), but the compensation was the hazard: the next reader to do the obvious thing and
    subtract from IST would have read every beat as 5h30m stale and fired a false WORKER SILENT.
    Founder directive is IST everywhere (RAILWAY_MEMORY_RULES id=156), so the mixed basis is gone
    rather than documented. WRITER FIRST, READERS SECOND — see the deploy-order gate on cc#1022."""
    for target in ("shared", "fresh"):
        hc = get_db() if target == "fresh" else conn
        try:
            with hc.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS worker_heartbeat (
                    id INTEGER PRIMARY KEY DEFAULT 1,
                    last_beat TIMESTAMP, boot_ts TIMESTAMP, note TEXT,
                    CONSTRAINT worker_heartbeat_singleton CHECK (id = 1))""")
                # cc#1022: IST, both on insert and on update. boot_ts already arrives IST.
                cur.execute("""INSERT INTO worker_heartbeat (id, last_beat, boot_ts, note)
                               VALUES (1, (NOW() AT TIME ZONE 'Asia/Kolkata'), %s, %s)
                               ON CONFLICT (id) DO UPDATE
                                 SET last_beat=(NOW() AT TIME ZONE 'Asia/Kolkata'),
                                     boot_ts=EXCLUDED.boot_ts, note=EXCLUDED.note""",
                            (boot_ts, "fyers_feed"))
            hc.commit()
            return True
        except Exception as e:
            log.warning(f"_write_heartbeat via {target}: {e}")
        finally:
            if target == "fresh":
                try:
                    hc.close()
                except Exception:
                    pass
    return False


def _fut_bars_today():
    """cc#605: count today's fyers_fut 5m bars. Returns -1 on DB error so a bad read NEVER triggers a
    false restart (the caller acts only on an exact 0)."""
    hc = None
    try:
        hc = get_db()
        midnight = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        with hc.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM intraday_prices WHERE timeframe='5m' "
                        "AND source='fyers_fut' AND ts >= %s", (midnight,))
            return cur.fetchone()[0]
    except Exception as e:
        log.warning(f"_fut_bars_today: {e}")
        return -1
    finally:
        if hc is not None:
            try:
                hc.close()
            except Exception:
                pass


def _rest_quote_ok(token):
    """cc#339 fix_2: ONE REST quote self-test (NSE:SBIN-EQ). Returns (ok, detail). An EMPTY body is
    the exact dead-token signature that produced the 09-Jul 'Expecting value: line 1 column 1' spam,
    so it is treated as a hard failure (not a parse exception)."""
    try:
        r = requests.get(QUOTES_URL, params={'symbols': 'NSE:SBIN-EQ'},
                         headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'}, timeout=8)
        body = (r.text or '').strip()
        if not body:
            return False, 'EMPTY_BODY'
        return (r.json().get('s') == 'ok'), body[:180]
    except Exception as e:
        return False, f'exc:{e}'


def _rest_quote_ok_settle(token, tries=4, delay=3):
    """cc#564: a freshly-minted Fyers access_token frequently returns an EMPTY body on the
    data-REST quote endpoint for a few seconds before it propagates server-side. The boot self-test
    ran the instant after the mint, saw the empty body, and declared the token DEAD -> os._exit(1) ->
    Railway restart-loop that never recovered (20-Jul incident). Retry the SBIN self-test a few times
    with a short backoff before treating empty/exception as a genuinely dead token."""
    detail = 'EMPTY_BODY'
    for i in range(max(1, tries)):
        ok, detail = _rest_quote_ok(token)
        if ok:
            return True, detail
        if i < tries - 1:
            time.sleep(delay)
    return False, detail


def _boot_auth_selfcheck(conn, token):
    """cc#339 fix_2: BEFORE subscribing, prove the token can actually fetch a REST quote. On failure:
    CRITICAL log + ops_log(feed_token_dead) alert, retry auto-login ONCE, and if still dead exit(1)
    so Railway's restart-loop + the alert make it LOUD instead of silent warning spam. Returns the
    (possibly refreshed) valid token."""
    ok, detail = _rest_quote_ok_settle(token)   # cc#564: tolerate fresh-token propagation delay
    if ok:
        log.info("BOOT AUTH OK — REST quote self-test passed (NSE:SBIN-EQ)")
        _ops_log(conn, 'info', 'feed_boot_ok',
                 {'selftest': 'NSE:SBIN-EQ', 'result': 'ok', 'ist': _ist_now_str()})
        return token
    log.critical(f"TOKEN DEAD — REST returning empty/invalid bodies on boot self-test (detail={detail}). "
                 "Retrying auto-login ONCE...")
    _ops_log(conn, 'alert', 'feed_token_dead',
             {'selftest': 'NSE:SBIN-EQ', 'stage': 'boot', 'detail': str(detail)[:180], 'ist': _ist_now_str()})
    # cc#489 fix_1 (round 2, call site 2): try_relogin() swap-in, re-landed.
    import fyers_autologin
    res = fyers_autologin.try_relogin(conn)
    if res.get('skipped'):
        log.warning("Auto-login retry SKIPPED (90s account-block cooldown) — sleeping 90s then retrying once...")
        time.sleep(90)
        res = fyers_autologin.try_relogin(conn)
    if res.get('ok'):
        token = res['token']
        log.info("Auto-login retry SUCCESS — re-testing REST quote...")
    else:
        e = res.get('error')
        log.critical(f"Auto-login retry FAILED ({e}) — os._exit(1) for a loud Railway restart")
        _ops_log(conn, 'alert', 'feed_token_dead',
                 {'stage': 'relogin_exception', 'detail': str(e)[:180], 'ist': _ist_now_str()})
        os._exit(1)
    ok2, detail2 = _rest_quote_ok_settle(token)   # cc#564: fresh token — allow propagation settle
    if ok2:
        log.info("BOOT AUTH OK after re-login — REST quote self-test passed")
        _ops_log(conn, 'info', 'feed_boot_ok',
                 {'selftest': 'NSE:SBIN-EQ', 'result': 'ok_after_relogin', 'ist': _ist_now_str()})
        # cc#564: explicit, DATA-observable proof a fresh token was minted AND verified live.
        _ops_log(conn, 'info', 'token_reminted_live',
                 {'stage': 'boot_selfcheck', 'selftest': 'NSE:SBIN-EQ:ok', 'ist': _ist_now_str()})
        return token
    log.critical("TOKEN STILL DEAD after re-login — os._exit(1) so the failure is LOUD (Railway "
                 "restart-loop + feed_token_dead alert) rather than 2900 silent 'Expecting value' warns")
    _ops_log(conn, 'alert', 'feed_token_dead',
             {'selftest': 'NSE:SBIN-EQ', 'stage': 'post_relogin', 'detail': str(detail2)[:180], 'ist': _ist_now_str()})
    os._exit(1)


def _boot_gap_report(conn):
    """cc#339 fix_3: gap-aware boot. Record the last 5m bar ts in DB + gap minutes so a mid-market
    reboot always self-documents its outage window (ops_log feed_boot_gap). Non-fatal."""
    try:
        with conn.cursor() as c:
            c.execute("""SELECT MAX(ts) FROM intraday_prices
                         WHERE ts::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date AND timeframe='5m'""")
            last = c.fetchone()[0]
        now_ist = datetime.now(IST).replace(tzinfo=None)
        # cc#855: SESSION_END (15:40), not the old 15:30 — a futures bar at 15:35 is not a gap.
        mkt = is_trading_day(now_ist.date()) and MARKET_OPEN <= now_ist.time() <= SESSION_END
        if last is None:
            log.info("BOOT GAP: no 5m bars yet today (cold / pre-market boot)")
            _ops_log(conn, 'info', 'feed_boot_gap',
                     {'last_bar': None, 'gap_min': None, 'market_open': mkt, 'ist': now_ist.isoformat()})
            return
        gap_min = round((now_ist - last).total_seconds() / 60.0, 1)
        level = 'alert' if (mkt and gap_min > 12) else 'info'   # >12min mid-market = a real outage
        log.info(f"BOOT GAP: last 5m bar {last} — gap {gap_min} min (market_open={mkt})")
        _ops_log(conn, level, 'feed_boot_gap',
                 {'last_bar': str(last), 'gap_min': gap_min, 'market_open': mkt, 'ist': now_ist.isoformat()})
    except Exception as e:
        log.warning(f"_boot_gap_report failed (non-fatal): {e}")


def get_universe(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM futures_universe WHERE is_active = TRUE")
        futures = {r[0] for r in cur.fetchall()}
    return sorted(futures - SKIP_SYMBOLS)


# cc#162: NIFTY/BANKNIFTY index futures — index futures were never subscribed
# on the live feed (SKIP_SYMBOLS excludes them from get_universe(), which feeds
# BOTH the equity leg -- correctly, no -EQ instrument exists for an index --
# AND the futures leg -- incorrectly, silently dropping real futures contracts
# that should be subscribed). This is a SEPARATE list, added ONLY to the
# futures leg, never the equity leg. Scope is intentionally just these two
# (task cc#162) -- SKIP_SYMBOLS also lists FINNIFTY/MIDCPNIFTY/SENSEX/BANKEX
# but none of those are actually present in futures_universe.
INDEX_FUTURES_UNIVERSE = ('NIFTY', 'BANKNIFTY')

def get_index_futures_universe(conn):
    """Read live from futures_universe (same is_active pattern as get_universe)
    rather than hardcoding a blind subscribe, so an ops-side deactivation of
    either symbol is honored automatically, same as it already is for the 209
    stock futures."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol FROM futures_universe WHERE is_active = TRUE "
            "AND symbol = ANY(%s)", (list(INDEX_FUTURES_UNIVERSE),))
        return sorted(r[0] for r in cur.fetchall())


def _ext_stage_limit(conn):
    """cc#809: read the staged-rollout dial from app_config. Returns the max number of EXTENDED
    symbols to subscribe: 0 = disabled, a positive int = that many (mcap-rank order), or a very
    large number for 'all'. Unset/garbage => EXT_STAGE_DEFAULT ('off'), i.e. this whole feature
    ships DARK and stays dark until it is switched on deliberately. Staging is a DB flag rather
    than a code constant on purpose: advancing stage 1 -> stage 2 must not require another worker
    deploy, because worker deploys are restricted to outside market hours (cc#416) and a mid-market
    reboot is a coin-flip on re-auth."""
    raw = None
    try:
        raw = _flag_get(conn, EXT_STAGE_FLAG)
    except Exception as e:
        log.warning(f"_ext_stage_limit: flag read failed ({e}) — defaulting to '{EXT_STAGE_DEFAULT}'")
    val = str(raw if raw is not None else EXT_STAGE_DEFAULT).strip().lower()
    if val in ('off', '0', 'none', 'false', ''):
        return 0
    if val in ('all', 'full', '-1'):
        return 10 ** 9
    try:
        return max(0, int(val))
    except Exception:
        log.warning(f"_ext_stage_limit: unrecognised {EXT_STAGE_FLAG}={raw!r} — treating as 'off'")
        return 0


def _ext_last_good(conn):
    """cc#1002: the largest extended-leg stage that has held a full clean in-session grade. The
    auto-retreat falls back to THIS instead of all the way to 'off' — a destabilised ramp drops to
    the last size that worked, not to zero. 0 => no known-good floor yet (retreat then goes fully
    off, the cc#809 behaviour)."""
    try:
        raw = _flag_get(conn, EXT_LASTGOOD_FLAG)
        return max(0, int(str(raw).strip())) if raw not in (None, '') else 0
    except Exception as e:
        log.warning(f"_ext_last_good: {e}")
        return 0


def _set_ext_last_good(conn, stage):
    """cc#1002: record `stage` as the new last-good floor. Only ADVANCES (never lowered here) — a
    stage becomes 'known good' once it holds a clean grade, and a later retreat never rewrites it."""
    try:
        if stage and 0 < stage < 10 ** 9 and stage > _ext_last_good(conn):
            _flag_set(conn, EXT_LASTGOOD_FLAG, str(int(stage)))
            log.info(f"cc#1002: extended leg stage {stage} graded clean — advanced last-good floor")
    except Exception as e:
        log.warning(f"_set_ext_last_good({stage}): {e}")


def get_extended_universe(conn, fno_symbols, limit):
    """cc#809: the EXTENDED equity leg — every symbol in the daily screener CSV universe
    (screener_raw.nse_code) that is NOT already on the F&O equity leg and is not an index.

    Ordered by input_raw.mcap_rank ASC so `limit` always slices the LARGEST names first — that is
    what makes the staged rollout meaningful (stage 1 = +500 largest). Symbols with no mcap_rank
    sort last, alphabetically, so the ordering is total and stable rather than whatever the planner
    happens to return; without that a re-run of "stage 1" could subscribe a different 500.

    Returns [] on any error — the extended leg is strictly additive and must never be able to stop
    the F&O feed from booting."""
    if limit <= 0:
        return []
    try:
        excl = sorted(set(fno_symbols) | SKIP_SYMBOLS | set(INDEX_LTP_SYMBOLS.keys()))
        with conn.cursor() as cur:
            cur.execute(EXT_BLACKLIST_SCHEMA_SQL)   # first boot may run this before ensure_schemas
            cur.execute("""
                SELECT sr.nse_code
                FROM screener_raw sr
                LEFT JOIN input_raw ir ON ir.nse_code = sr.nse_code
                WHERE sr.nse_code IS NOT NULL AND sr.nse_code <> ''
                  AND sr.nse_code <> ALL(%s)
                  AND NOT EXISTS (SELECT 1 FROM feed_ext_blacklist b WHERE b.symbol = sr.nse_code)
                ORDER BY ir.mcap_rank ASC NULLS LAST, sr.nse_code ASC
                LIMIT %s
            """, (excl, limit))
            out = [r[0] for r in cur.fetchall()]
        conn.commit()
        return out
    except Exception as e:
        log.warning(f"get_extended_universe: {e} — extended leg disabled for this boot")
        return []


def _canary_symbols(conn, nse_codes, n):
    """cc#497 fix_1_TIMING_FINAL_FOUNDER_17JUL: top-liquidity NSE codes (mcap-rank order) for
    the two-stage subscribe's canary batch — subscribe a small, high-signal batch first and
    verify it actually ticks before piling the full universe onto a session that might already
    be dead. Falls back to the first n of the (already-sorted) universe if input_raw.mcap_rank
    is unavailable/incomplete — never blocks the canary stage on a missing join."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT fu.symbol FROM futures_universe fu
                JOIN input_raw ir ON ir.nse_code = fu.symbol
                WHERE fu.is_active = TRUE AND fu.symbol = ANY(%s) AND ir.mcap_rank IS NOT NULL
                ORDER BY ir.mcap_rank ASC LIMIT %s
            """, (nse_codes, n))
            ranked = [r[0] for r in cur.fetchall()]
        if len(ranked) >= min(n, len(nse_codes)):
            return ranked
    except Exception as e:
        log.warning(f"_canary_symbols: mcap-rank lookup failed ({e}) — falling back to first {n} of universe")
    return nse_codes[:n]


def get_top50_option_underlyings(conn):
    """Top 50 futures stocks by mcap rank from input_raw."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT fu.symbol
                FROM futures_universe fu
                JOIN input_raw ir ON ir.nse_code = fu.symbol
                WHERE fu.is_active = TRUE
                  AND ir.mcap_rank <= 50
                ORDER BY fu.symbol
            """)
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.warning(f"get_top50_option_underlyings: {e}")
        return []


STOCK_N_STRIKES = 3   # cc#155: stock options subscribe ATM±3 (index stays N_STRIKES=±10)


def get_all_option_underlyings(conn):
    """cc#155 (STOCK_OPTIONS_CHAIN_SPEC_V1, session_log 1173): ALL active futures-universe
    stock underlyings for stock options, excluding indices/SKIP_SYMBOLS (+NIFTY50). Ordered
    by mcap rank so a pilot/limit takes the most-liquid names first. Distinct from
    get_top50_option_underlyings (hard-capped at 50)."""
    try:
        excl = list(SKIP_SYMBOLS | {'NIFTY50'})
        with conn.cursor() as cur:
            cur.execute("""
                SELECT fu.symbol
                FROM futures_universe fu
                LEFT JOIN input_raw ir ON ir.nse_code = fu.symbol
                WHERE fu.is_active = TRUE
                  AND fu.symbol <> ALL(%s)
                ORDER BY COALESCE(ir.mcap_rank, 999999), fu.symbol
            """, (excl,))
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.warning(f"get_all_option_underlyings: {e}")
        return []


def _stock_options_config(conn):
    """cc#155: runtime gate for stock options, read from app_config so scope can be scaled or
    killed LIVE (no redeploy) — the phased rollout + kill-switch the spec's HARD 8/10 design
    requires. Defaults DISABLED => index-only feed unchanged (14-Jun lock preserved).
      stock_options_enabled = 'true'|'false'  (default false)
      stock_options_limit   = int underlyings  (default 20 pilot; <=0 = all 209)
      stock_options_n       = ATM± strikes     (default 3)"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key, value FROM app_config WHERE key = ANY(%s)",
                        (['stock_options_enabled', 'stock_options_limit', 'stock_options_n'],))
            cfg = {k: v for k, v in cur.fetchall()}
        enabled = str(cfg.get('stock_options_enabled', 'false')).strip().lower() == 'true'
        limit = int(cfg.get('stock_options_limit') or 20)
        n = int(cfg.get('stock_options_n') or STOCK_N_STRIKES)
        return enabled, limit, n
    except Exception as e:
        log.warning(f"_stock_options_config: {e} — defaulting to index-only")
        return False, 20, STOCK_N_STRIKES


def _cmp_fresh_fraction(opt_mgr, kind=None):
    """cc#189: fraction of option underlyings whose cmp_prices row was updated
    within the last OPT_FRESH_WINDOW_MIN minutes. Drives the 'subscribe options
    ONLY when live prices are fresh' gate. cc#241: kind filters to index-only /
    stock-only underlyings so the index gate is never blocked by stock cmp
    freshness (and vice-versa).

    cc#489 step_5: the old `updated_at >= NOW() - INTERVAL` claimed to be
    "timezone-agnostic" because both sides are "the DB clock" — false whenever
    the session timezone isn't IST (Railway Postgres defaults to UTC). updated_at
    is stored naive-IST; Postgres casts that naive value as if it WERE the
    session tz, making it look ~5.5h more recent than it really is, so a row
    hours stale could still pass this gate. Compute the cutoff in Python using
    the same naive-IST convention used everywhere else in this file instead.

    cc#497 fix_2c: now opens its OWN fresh short-lived connection instead of taking the shared
    housekeeping conn as a parameter — a dead shared conn silently zeroed this gate all morning
    on 17-Jul (fresh=0.0 forever -> options never subscribed -> no CRITICAL alert either, since
    the alert path shared the same dead conn)."""
    if not opt_mgr._underlyings:
        opt_mgr._build_underlyings()
    syms = [u['cmp_sym'] for u in opt_mgr._underlyings if (kind is None or u.get('kind') == kind)]
    if not syms:
        return 0.0
    hc = None
    try:
        cutoff = datetime.now(IST).replace(tzinfo=None) - timedelta(minutes=OPT_FRESH_WINDOW_MIN)
        hc = get_db()
        with hc.cursor() as cur:
            cur.execute(
                "SELECT COUNT(DISTINCT symbol) FROM cmp_prices "
                "WHERE symbol = ANY(%s) AND updated_at >= %s",
                (syms, cutoff))
            fresh = cur.fetchone()[0] or 0
    except Exception as e:
        log.warning(f"_cmp_fresh_fraction: {e}")
        return 0.0
    finally:
        if hc is not None:
            try:
                hc.close()
            except Exception:
                pass
    return fresh / len(syms)


def fyers_eq_symbol(sym): return SPECIAL_SYMBOLS.get(sym, f'NSE:{sym}-EQ')

def from_fyers_symbol(fsym):
    if fsym == 'NSE:M&M-EQ': return 'M&M'
    if 'FUT' in fsym:
        inner = fsym.replace('NSE:', '')
        # cc#148: was ^([A-Z&]+)\d{2}[A-Z]{3}FUT$ — only [A-Z&], so digit/hyphen
        # tickers (360ONE, BAJAJ-AUTO, NAM-INDIA) failed to match and the RAW
        # contract name (e.g. "360ONE26JULFUT") leaked into intraday_prices.
        # Non-greedy base group now allows digits/hyphens in the ticker itself.
        m = re.match(r'^([A-Z0-9&-]+?)(\d{2}[A-Z]{3})FUT$', inner)
        if m: return m.group(1)
        return inner
    return fsym.replace('NSE:', '').replace('-EQ', '')


# ── option symbol manager ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class OptionSymbolManager:
    """
    Manages option symbol subscriptions.
    Tracks current ATM per underlying, rebuilds on drift or monthly roll.
    v6: strike ladders come from the Fyers symbol master (actually-listed strikes);
    step-guessing remains only as a fallback when the master fails to load.
    """
    def __init__(self, conn, token=None, master: 'OptionMaster' = None):
        self.conn         = conn
        self.token        = token
        self.master       = master
        self.lock         = threading.Lock()
        self.expiry       = current_expiry()
        self.atm_map      = {}   # underlying -> current ATM strike
        self.sym_map      = {}   # fyers_option_symbol -> (underlying, strike, opt_type, expiry)
        self._underlyings = []

    def _build_underlyings(self):
        # cc#155 (STOCK_OPTIONS_CHAIN_SPEC_V1, session_log 1173): index underlyings are ALWAYS
        # present at ATM±N_STRIKES (±10). Stock underlyings (ATM±stock_n, default ±3) are
        # ADDITIVE and config-gated via app_config (_stock_options_config). Default OFF keeps
        # the 14-Jun INDEX-ONLY lock intact; an operator enables + scales the pilot LIVE (no
        # redeploy) and can kill it instantly. Additive-only: stock options never displace the
        # index/eq/fut subscription (the 06-Jul regression that zeroed index ATM±10).
        out = []
        for name, meta in INDEX_OPTION_UNDERLYINGS.items():
            out.append({'name': name, 'step': meta['step'], 'cmp_sym': meta['cmp_sym'],
                        'n': N_STRIKES, 'kind': 'index'})
        enabled, limit, stock_n = _stock_options_config(self.conn)
        n_stock = 0
        if enabled:
            index_names = set(INDEX_OPTION_UNDERLYINGS)
            stocks = [s for s in get_all_option_underlyings(self.conn) if s not in index_names]
            if limit and limit > 0:
                stocks = stocks[:limit]
            for s in stocks:
                out.append({'name': s, 'step': None, 'cmp_sym': s, 'n': stock_n, 'kind': 'stock'})
            n_stock = len(stocks)
        self._underlyings = out
        log.info(f"OptionSymbolManager: {len(out)} underlyings "
                 f"({len(INDEX_OPTION_UNDERLYINGS)} index"
                 + (f" + {n_stock} stock ATM±{stock_n}, limit={limit}" if enabled
                    else "-only; stock options disabled") + ")")

    def _get_cmp(self, cmp_sym, allow_rest=False):
        # cc#189: the AUTOMATIC subscribe path uses LIVE cmp_prices only (allow_rest
        # defaults False) — the housekeeping gate only calls build_initial once
        # cmp_prices is fresh, so ATM strikes come from live prices. The Fyers REST
        # quotes fallback is RETAINED (founder 04-Jul: keep REST for on-demand
        # fallback) and used only when a caller explicitly passes allow_rest=True.
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT cmp FROM cmp_prices WHERE symbol = %s", (cmp_sym,))
                r = cur.fetchone()
                if r and r[0]:
                    return float(r[0])
        except Exception:
            pass
        if not allow_rest or not self.token:
            return None
        # on-demand REST fallback: pull live CMP straight from the Fyers quotes API
        try:
            meta = INDEX_OPTION_UNDERLYINGS.get(cmp_sym)
            fsym = meta['fyers_index'] if meta else fyers_eq_symbol(cmp_sym)
            resp = requests.get(QUOTES_URL, params={'symbols': fsym},
                                headers={'Authorization': f'{FYERS_CLIENT_ID}:{self.token}'},
                                timeout=5)
            d = resp.json()
            if d.get('s') == 'ok':
                for item in d.get('d', []):
                    lp = item.get('v', {}).get('lp')
                    if lp:
                        # cc#229 (id166 permanent fix): seed cmp_prices with the REST CMP so a
                        # cold-boot EMPTY table gets populated -> the option/ATM build never
                        # silently skips and any-time restart is safe.
                        try:
                            with self.conn.cursor() as _c:
                                _c.execute(
                                    "INSERT INTO cmp_prices (symbol, cmp, updated_at, source) "
                                    "VALUES (%s,%s,NOW(),'fyers_rest') "
                                    "ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, "
                                    "updated_at=EXCLUDED.updated_at, source='fyers_rest'",
                                    (cmp_sym, float(lp)))
                            self.conn.commit()
                        except Exception as _se:
                            try: self.conn.rollback()
                            except Exception: pass
                            log.warning(f"_get_cmp seed cmp_prices {cmp_sym}: {_se}")
                        return float(lp)
        except Exception as e:
            log.warning(f"_get_cmp Fyers REST fallback {cmp_sym}: {e}")
        return None

    def _ladder(self, u, cmp):
        """
        Returns list of (strike, opt_type) for ATM±N.
        Primary: actual listed strikes from the symbol master.
        Fallback: step-based generation (pre-v6).
        """
        pairs = []
        n = u.get('n', N_STRIKES)   # cc#155: per-underlying window (index ±10, stock ±3)
        if self.master and self.master.loaded:
            ce = self.master.atm_window(u['name'], self.expiry, 'CE', cmp, n=n)
            pe = self.master.atm_window(u['name'], self.expiry, 'PE', cmp, n=n)
            if ce or pe:
                for s in (ce or []): pairs.append((s, 'CE'))
                for s in (pe or []): pairs.append((s, 'PE'))
                return pairs
            log.warning(f"_ladder: no master chain for {u['name']} {self.expiry} — step fallback")
        step = u['step'] or auto_step(cmp)
        atm  = atm_strike(cmp, step)
        for i in range(-n, n + 1):
            strike = atm + i * step
            if strike <= 0: continue
            pairs.append((strike, 'CE'))
            pairs.append((strike, 'PE'))
        return pairs

    def build_initial(self, allow_rest=False, kind=None):
        """Returns list of Fyers option symbols to subscribe. cc#189: the automatic
        live-price gate calls this with allow_rest=False (cmp_prices only); an
        on-demand caller may pass allow_rest=True to use the Fyers REST CMP
        fallback (retained per founder 04-Jul). cc#241: kind selects which underlyings
        to build — 'index' (early gate), 'stock' (09:25 gate, MERGED additively into
        sym_map so the index chains are never wiped), or None (full rebuild / monthly roll)."""
        # cc#229 (id166 permanent fix): on a cold boot cmp_prices can be EMPTY (worker
        # restarted pre-open or after downtime). Empty cmp_prices -> zero underlyings resolve
        # a CMP -> zero option subscriptions (the known zombie). Detect empty and force the
        # Fyers REST CMP path so prices are fetched + seeded and subscriptions never skip.
        try:
            with self.conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM cmp_prices")
                if (cur.fetchone()[0] or 0) == 0 and self.token:
                    allow_rest = True
                    log.warning("build_initial: cmp_prices EMPTY (cold boot) — forcing Fyers REST "
                                "CMP fetch to seed prices before subscribe (cc#229/id166)")
        except Exception:
            pass
        self._build_underlyings()
        self.expiry = current_expiry()
        symbols = []
        unders = [u for u in self._underlyings if (kind is None or u.get('kind') == kind)]
        with self.lock:
            if kind is None:                       # full rebuild: wipe + rebuild everything
                self.sym_map = {}
                self.atm_map = {}
                self.built_per_underlying = {}
            elif not hasattr(self, 'built_per_underlying'):
                self.built_per_underlying = {}     # cc#241: kind build MERGES (never wipes index)
            for u in unders:
                cmp = self._get_cmp(u['cmp_sym'], allow_rest=allow_rest)
                if not cmp:
                    self.built_per_underlying[u['name']] = 0
                    log.warning(f"No CMP for {u['cmp_sym']} — skipping options")
                    continue
                step = u['step'] or auto_step(cmp)
                self.atm_map[u['name']] = atm_strike(cmp, step)
                before = len(symbols)
                for strike, otype in self._ladder(u, cmp):
                    fsym = option_fyers_symbol(u['name'], strike, otype, self.expiry)
                    if self.master and not self.master.is_valid(fsym):
                        continue
                    self.sym_map[fsym] = (u['name'], strike, otype, self.expiry)
                    symbols.append(fsym)
                self.built_per_underlying[u['name']] = len(symbols) - before
        log.info(f"OptionSymbolManager: built {len(symbols)} {kind or 'all'} option symbols "
                 f"({'master' if self.master and self.master.loaded else 'step-fallback'})")
        return symbols

    def subscribe_health(self, kind=None):
        """cc#189: (underlyings_total, underlyings_ok, missing_names, contracts) from the last
        build — drives the subscribed-vs-expected alert (an underlying with 0 contracts = a
        miss). cc#241: kind filters to index-only / stock-only (the stock overflow alert reads
        kind='stock')."""
        per = getattr(self, 'built_per_underlying', {})
        names = [u['name'] for u in self._underlyings if (kind is None or u.get('kind') == kind)]
        if not names and kind is None:
            names = list(per.keys())
        total = len(names)
        ok = sum(1 for n in names if per.get(n, 0) > 0)
        missing = sorted(n for n in names if per.get(n, 0) == 0)
        contracts = sum(per.get(n, 0) for n in names)
        return total, ok, missing, contracts

    def index_option_syms(self, syms):
        """cc#155: index-only subset of the given option syms — for OI depth polling. Stock
        options are WS-ONLY, NO REST OI (spec 1173): ~2912 stock depth calls = ~27min, which
        would blow the 5-min bar cadence. Index OI poll (~136 syms) stays unchanged."""
        idx = set(INDEX_OPTION_UNDERLYINGS)
        return [s for s in syms if (self.sym_map.get(s) or ('',))[0] in idx]

    def stock_option_syms(self, syms):
        """cc#375: stock-only subset of the given (subscribed) option syms — for a SEPARATE OI depth
        poll so stock option_chain rows carry OI (the WS strips it; without this poll oi stays NULL
        and the cockpit ATM OI d/d is always '--'). Complements index_option_syms. Bounded by the
        subscribed set (app_config stock_options_limit, pilot default 20 underlyings ~= a few hundred
        syms), which fits the 5-min bar; at full 209-stock scale the caller's separate lock lets a
        long cycle skip gracefully rather than delay the index poll."""
        idx = set(INDEX_OPTION_UNDERLYINGS)
        return [s for s in syms if s in self.sym_map and (self.sym_map.get(s) or ('',))[0] not in idx]

    def stock_atm_option_syms(self, syms):
        """cc#482: ATM CE+PE ONLY per stock underlying, for the 5-min OI DEPTH POLL only — the
        WS tick subscription (build_initial, still ATM+-stock_n) is UNCHANGED. Cuts stock OI
        poll load ~86% (full chain -> ~2 strikes/stock) after the 13-Jul open-burst empty-body
        incident. ATM is recomputed FRESH from live CMP on every call (not the 15-min-cached
        atm_map) since intraday ATM drift matters at 5-min poll granularity."""
        idx = set(INDEX_OPTION_UNDERLYINGS)
        stock_syms = [s for s in syms if s in self.sym_map and (self.sym_map.get(s) or ('',))[0] not in idx]
        by_under = {}
        for s in stock_syms:
            und = self.sym_map[s][0]
            by_under.setdefault(und, []).append(s)
        out = []
        for u in self._underlyings:
            if u.get('kind') != 'stock' or u['name'] not in by_under:
                continue
            cmp = self._get_cmp(u['cmp_sym'])
            if not cmp:
                continue
            step = u['step'] or auto_step(cmp)
            atm = atm_strike(cmp, step)
            for s in by_under[u['name']]:
                _, strike, otype, _ = self.sym_map[s]
                if strike == atm:
                    out.append(s)
        return out

    def check_atm_drift(self):
        """Returns (add_syms, remove_syms) if any ATM has drifted >= ATM_DRIFT_STRIKES."""
        add, remove = [], []
        with self.lock:
            for u in self._underlyings:
                cmp = self._get_cmp(u['cmp_sym'])
                if not cmp: continue
                step    = u['step'] or auto_step(cmp)
                new_atm = atm_strike(cmp, step)
                old_atm = self.atm_map.get(u['name'])
                if old_atm is None: continue
                drift = abs(new_atm - old_atm) // step
                if drift < ATM_DRIFT_STRIKES: continue
                log.info(f"ATM drift {u['name']}: {old_atm} → {new_atm} ({drift} strikes)")
                old_syms = [s for s, v in self.sym_map.items() if v[0] == u['name']]
                for s in old_syms:
                    del self.sym_map[s]; remove.append(s)
                self.atm_map[u['name']] = new_atm
                for strike, otype in self._ladder(u, cmp):
                    fsym = option_fyers_symbol(u['name'], strike, otype, self.expiry)
                    if self.master and not self.master.is_valid(fsym):
                        continue
                    self.sym_map[fsym] = (u['name'], strike, otype, self.expiry)
                    add.append(fsym)
        return add, remove

    def check_monthly_roll(self):
        """Returns True if expiry rolled to next month."""
        new_expiry = current_expiry()
        if new_expiry != self.expiry:
            log.info(f"Monthly roll: {self.expiry} → {new_expiry}")
            self.expiry = new_expiry
            if self.master:
                self.master.load()   # refresh listed contracts for the new series
            return True
        return False

    def lookup(self, fsym):
        with self.lock:
            return self.sym_map.get(fsym)


# ── bar aggregator ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class BarAggregator:
    def __init__(self, conn):
        self.conn     = conn
        self.bars     = {}
        self.last_ltp = {}
        # cc_task #112: per-symbol time of the LAST GENUINE tick + time of the last
        # cmp flush. flush_cmp uses these so a symbol with no fresh tick is never
        # re-stamped — stops the stale-write masking that made a dead feed look healthy.
        self.last_ltp_ts        = {}   # symbol -> datetime of most recent real tick
        self._last_cmp_flush_ts = None # datetime of the last successful cmp flush
        self.last_oi  = {}   # symbol -> latest OI from depth REST poll (futures)
        # cc#807 follow-up: (sym, source) -> {'bkt', 'base', 'last'} for turning the cumulative day-volume
        # counter into per-bar volume. In-memory only: a restart re-bases the current bar from the
        # first tick it sees, which under-reports that ONE bar rather than emitting a spike the
        # size of the whole session — the failure this replaces.
        self._vol_state = {}
        # RLock (re-entrant): flush_all holds it while _flush → _compute_basis
        # runs; a plain Lock here deadlocked the whole feed (v6.1 fix).
        self.lock     = threading.RLock()
        self._db_reconnect_attempted = False  # cc#489 step_4: DB-write resilience

    def _bucket(self, ts):
        # 5-min bucket: round down to nearest 5-min boundary
        return ts.replace(minute=ts.minute - ts.minute % BAR_MINUTES, second=0, microsecond=0)

    def _per_bar_volume(self, key, cum, bkt):
        """cc#807 follow-up: convert Fyers' CUMULATIVE day volume into this bar's OWN volume.

        on_message reads msg['vol_traded_today'] — a running total for the session, not a per-bar
        figure — and this class stored it verbatim, so intraday_prices.volume held a cumulative
        counter for every WS-fed bar. The fyers_hist backfill writes genuine per-bar volume into the
        SAME column, so the table mixed two incompatible representations, and a worker restart
        mid-session switched a symbol from one to the other partway through the day (observed
        31-Jul-2026: RELIANCE per-bar to 11:20, cumulative from 11:25; raw sum 206.7M against a true
        day volume of 8.6M).

        Anything that sums or volume-weights this column was wrong: VWAP, VPOC, any rvol or
        volume-ratio consumer. cc#807 added a client-side detector for the chart card; this fixes the
        data itself, which is the only fix that reaches every other consumer.

        Method: remember the cumulative reading at the moment each bucket opened; this bar's volume
        is (current cumulative - that base). A DECREASE means the counter reset — a new session, or
        the feed re-anchoring after a reconnect — so we re-base to the current reading rather than
        emit a negative or an absurd spike. Returns None when no sane value can be derived, and the
        caller then leaves the bar's volume untouched rather than writing a guess."""
        if cum is None:
            return None
        try:
            cum = float(cum)
        except (TypeError, ValueError):
            return None
        if cum < 0:
            return None
        st = self._vol_state.get(key)
        if st is None or st['bkt'] != bkt:
            # New bucket: its base is the last cumulative we saw. On the very first tick for a
            # symbol we have no base, so the bar starts at 0 and fills in as the session proceeds —
            # the honest reading, since we genuinely do not know what traded before we connected.
            base = st['last'] if (st is not None and cum >= st['last']) else 0.0
            st = {'bkt': bkt, 'base': base, 'last': cum}
            self._vol_state[key] = st
        if cum < st['last']:
            # Counter went backwards -> reset/re-anchor. Re-base here; do not emit a negative.
            st['base'] = 0.0
        st['last'] = cum
        return max(0.0, cum - st['base'])

    def on_tick(self, sym, ltp, vol, ts=None, source='fyers_eq', oi=None):
        ts  = ts or datetime.now(IST).replace(tzinfo=None)
        bkt = self._bucket(ts)
        key = (sym, source)
        with self.lock:
            # cc#367: last_ltp/last_ltp_ts feed cmp_prices (SPOT snapshot) via flush_cmp ONLY.
            # The bar dict is keyed by (sym, source) so eq & fut bars stay separate, but last_ltp
            # was keyed by sym alone — a futures tick (source='fyers_fut') would overwrite the
            # spot LTP with a basis-premium/discount price (3-4% off), and flush_cmp then wrote
            # that fut price into cmp_prices as if it were spot. Only spot ticks may set last_ltp.
            # cc#1056: widened from the single 'fyers_fut' literal to the FUT_SOURCES registry.
            # The guard named one futures source at a time when only one existed; any later
            # futures leg routed through on_tick would have walked straight past it and written
            # a basis-off price into cmp_prices as spot. Membership in the registry, not a name.
            if source not in FUT_SOURCES:
                self.last_ltp[sym]    = ltp
                self.last_ltp_ts[sym] = ts   # cc_task #112: mark when this genuine tick arrived
            # cc#807 follow-up: `vol` arrives as the session's CUMULATIVE total (vol_traded_today). Convert it
            # to this bar's own volume BEFORE it is stored. Must run while holding the lock and
            # before the bucket-rollover branch below, because _per_bar_volume re-bases on the
            # bucket it is given.
            bar_vol = self._per_bar_volume(key, vol, bkt)
            bar = self.bars.get(key)
            if bar is None or bar['ts'] != bkt:
                if bar is not None:
                    self._flush(key, bar)
                self.bars[key] = {'ts': bkt, 'o': ltp, 'h': ltp, 'l': ltp,
                                  'c': ltp, 'v': bar_vol or 0, 'oi': oi, 'source': source}
            else:
                bar['h'] = max(bar['h'], ltp)
                bar['l'] = min(bar['l'], ltp)
                bar['c'] = ltp
                # bar_vol is monotonic within a bucket, so the latest reading is the bar's total.
                # `is not None` rather than truthiness: a genuine 0 (no trades yet this bar) must
                # overwrite a stale carry-over, and the old `if vol:` silently skipped exactly that.
                if bar_vol is not None: bar['v'] = bar_vol
                if oi is not None: bar['oi'] = oi

    def _flush(self, key, bar):
        sym, source = key
        try:
            # cc#193: NEVER persist an off-hours bar. Fyers streams phantom ticks
            # on non-trading days and outside session hours (garbage levels — e.g.
            # Sat 04-Jul BANKNIFTY 64,043 while the real Friday close was 58,255).
            # cc#855: the cutoff is now SEGMENT-AWARE and also decides the auction tag —
            # bar_source_tag() returns None for a phantom bar, else the source to store under.
            bt = bar['ts']
            if not is_trading_day(bt.date()):
                return
            tagged = bar_source_tag(source, bt.time())
            if tagged is None:
                return
            source = tagged
        except Exception:
            pass
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO intraday_prices (symbol,ts,open,high,low,close,volume,timeframe,source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'5m',%s)
                    ON CONFLICT (symbol,ts,timeframe,source) DO UPDATE SET
                        open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                        close=EXCLUDED.close,volume=EXCLUDED.volume
                """, (sym, bar['ts'], bar['o'], bar['h'], bar['l'], bar['c'],
                      int(bar['v']), source))
            self.conn.commit()
            self._db_reconnect_attempted = False   # a clean write proves the conn is healthy
            if source == 'fyers_fut':
                self._compute_basis(sym, bar['ts'], bar['c'], bar.get('oi'))
        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            # cc#489 step_4: a dead DB conn must never survive more than one flush
            # cycle silently (16-Jul incident: this exact error was swallowed by a
            # bare "except Exception: log.warning" forever). Reconnect ONCE; if the
            # reconnect itself fails, or the NEXT flush hits this branch again
            # (meaning the reconnected conn also died), exit(1) for a clean restart.
            if self._db_reconnect_attempted:
                log.critical(f"flush {sym} ({source}): DB conn still dead after reconnect "
                             f"({e}) — os._exit(1) for a clean Railway restart")
                os._exit(1)
            log.error(f"flush {sym} ({source}): DB conn error ({e}) — reconnecting once...")
            try:
                self.conn.close()
            except Exception:
                pass
            try:
                # cc#876: get_db(), not a bare connect — this reconnect path used to rebuild
                # the connection WITHOUT timeouts or keepalives, so a socket that died once
                # could be replaced by one that hangs the next time. The safe wrapper already
                # existed; these two call sites simply bypassed it.
                self.conn = get_db()
                self._db_reconnect_attempted = True
            except Exception as e2:
                log.critical(f"flush {sym} ({source}): DB reconnect FAILED ({e2}) — "
                             "os._exit(1) for a clean Railway restart")
                os._exit(1)
        except Exception as e:
            log.warning(f"flush {sym} ({source}): {e}")

    def _compute_basis(self, sym, ts, fut_close, oi=None):
        """On futures bar flush: store basis + OI + OI change vs prior bar."""
        try:
            # Fallback: bar carried no OI (tick pre-dated first poll) — read the
            # latest polled value. NO LOCK here: caller may already hold agg.lock
            # (flush_all path) and CPython dict .get is GIL-atomic anyway.
            if oi is None:
                oi = self.last_oi.get(sym)
            # cc#162: NIFTY the futures-contract root symbol differs from
            # NIFTY50, the canonical spot index symbol used everywhere else in
            # this codebase (market_mood, v8, cmp_prices, raw_prices all key
            # off NIFTY50). Without this alias every spot lookup below misses
            # and basis stays permanently NULL. BANKNIFTY needs no alias — its
            # futures root already matches the spot key used system-wide.
            # futures_basis.symbol itself still stores `sym` (the contract
            # identity), only the SPOT lookups use the alias.
            spot_sym = 'NIFTY50' if sym == 'NIFTY' else sym
            with self.conn.cursor() as cur:
                # Spot = nearest non-futures intraday bar for this symbol at/before ts.
                # (exact ts + source='fyers_eq' missed: eq feed is sparse & ts-misaligned;
                #  bulk spot data is source='fyers'. Match nearest, exclude fyers_fut self.)
                # cc#1056: the exclusion now covers BOTH futures sources. It was written before
                # cc#770 added the REST fallback leg, so a fyers_fut_rest bar could satisfy the
                # "spot" lookup — and basis = fut - spot would then be roughly zero instead of
                # the real basis. Excluding one futures source while another exists is a filter
                # that reads as safe and is not.
                cur.execute("""
                    SELECT close FROM intraday_prices
                    WHERE symbol=%s AND ts::date=%s::date AND ts<=%s AND """ + NOT_FUT_SQL + """
                    ORDER BY ts DESC LIMIT 1
                """, (spot_sym, ts, ts))
                row       = cur.fetchone()
                spot      = float(row[0]) if row else None
                # Fallback: the equity bar for this 5-min bucket may not be flushed
                # yet (eq/fut flush on the same boundary but not simultaneously), so
                # the lookup above can miss → spot None → NULL basis. Fall back to
                # the live CMP (refreshed every CMP_FLUSH_MINS), then prior EOD close.
                if spot is None:
                    cur.execute("SELECT cmp FROM cmp_prices WHERE symbol=%s", (spot_sym,))
                    r2 = cur.fetchone()
                    spot = float(r2[0]) if r2 and r2[0] is not None else None
                if spot is None:
                    cur.execute("SELECT close FROM raw_prices WHERE symbol=%s ORDER BY price_date DESC LIMIT 1", (spot_sym,))
                    r3 = cur.fetchone()
                    spot = float(r3[0]) if r3 and r3[0] is not None else None
                basis     = round(fut_close - spot, 4) if spot is not None else None
                basis_pct = round((fut_close - spot) / spot * 100, 4) if spot else None
                # prior bar OI for this symbol (most recent non-null before this ts)
                oi_prev = None
                if oi is not None:
                    cur.execute("""
                        SELECT oi FROM futures_basis
                        WHERE symbol=%s AND oi IS NOT NULL AND ts < %s
                        ORDER BY ts DESC LIMIT 1
                    """, (sym, ts))
                    pr = cur.fetchone()
                    oi_prev = int(pr[0]) if pr and pr[0] is not None else None
                oi_chg = (int(oi) - oi_prev) if (oi is not None and oi_prev is not None) else None
                cur.execute("""
                    INSERT INTO futures_basis (symbol, ts, spot_close, futures_close, basis, basis_pct, oi, oi_prev, oi_chg)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, ts) DO UPDATE SET
                        spot_close=EXCLUDED.spot_close, futures_close=EXCLUDED.futures_close,
                        basis=EXCLUDED.basis, basis_pct=EXCLUDED.basis_pct,
                        oi=EXCLUDED.oi, oi_prev=EXCLUDED.oi_prev, oi_chg=EXCLUDED.oi_chg
                """, (sym, ts, spot, fut_close, basis, basis_pct,
                      int(oi) if oi is not None else None, oi_prev, oi_chg))
            self.conn.commit()
        except Exception as e:
            log.warning(f"_compute_basis {sym}: {e}")

    def flush_all(self):
        """cc#809: ONE batched upsert per pass instead of one INSERT+COMMIT per symbol.

        This ran every housekeeping pass (~30s) doing a round-trip per open bar. At 209 F&O symbols
        that was ~7 writes/sec — tolerable. At the full ~1,800-symbol universe it becomes ~60
        commits/sec sustained, all day, which is the single thing most likely to make this expansion
        fall over. One executemany + one commit per pass instead.

        Behaviour preserved exactly: same off-hours rejection per bar (cc#193), same upsert, same
        basis computation for futures bars (still per-symbol, still AFTER the write lands, and still
        only for source='fyers_fut' — that leg is ~209 symbols, not the one that needed batching).
        On a DB connection error it falls back to the original per-bar path, which owns the
        reconnect-once-then-os._exit(1) ladder (cc#489 step_4) — that ladder is the reason a dead
        conn cannot silently persist, so the batch path must never swallow it."""
        with self.lock:
            items = list(self.bars.items())
        rows, fut_bars = [], []
        for key, bar in items:
            sym, source = key
            try:
                bt = bar['ts']
                if not is_trading_day(bt.date()):
                    continue   # cc#193: never persist an off-hours/phantom bar
                # cc#855: segment-aware cutoff + auction tagging in one call. A rejected bar is
                # phantom; a tagged one is real but must never read as continuous price action.
                tagged = bar_source_tag(source, bt.time())
                if tagged is None:
                    continue
                source = tagged
            except Exception:
                pass
            rows.append((sym, bar['ts'], bar['o'], bar['h'], bar['l'], bar['c'],
                         int(bar['v']), source))
            if source == 'fyers_fut':
                fut_bars.append((sym, bar['ts'], bar['c'], bar.get('oi')))
        if not rows:
            return
        try:
            with self.conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO intraday_prices (symbol,ts,open,high,low,close,volume,timeframe,source)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,'5m',%s)
                    ON CONFLICT (symbol,ts,timeframe,source) DO UPDATE SET
                        open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,
                        close=EXCLUDED.close,volume=EXCLUDED.volume
                """, rows)
            self.conn.commit()
            self._db_reconnect_attempted = False
        except Exception as e:
            # Fall back to the per-bar path: it carries the cc#489 reconnect/exit ladder, and a
            # single poison row cannot take the whole batch down with it.
            log.warning(f"flush_all batch failed ({e}) — falling back to per-bar writes")
            with self.lock:
                for key, bar in items:
                    self._flush(key, bar)
            return
        for sym, ts, close, oi in fut_bars:
            try:
                self._compute_basis(sym, ts, close, oi)
            except Exception as e:
                log.warning(f"flush_all basis {sym}: {e}")

    def flush_cmp(self):
        """cc_task #112 — STOP STALE-WRITE MASKING (most critical fix).
        Only (re)write a cmp_prices row for a symbol that received a GENUINE tick
        since the last flush. The updated_at stamped is the real tick time, never
        a blanket now(). A symbol with no fresh tick is left untouched so its
        updated_at ages truthfully. If the WS is dead, ZERO rows are written and
        cmp_prices freshness goes stale on its own — so health checks finally see
        the truth instead of a feed that lies while frozen."""
        _ist = datetime.now(IST).replace(tzinfo=None)
        prev = self._last_cmp_flush_ts
        # tolerate the very first flush (prev=None) with a short look-back window so a
        # symbol that ticked just before boot is still written once.
        cutoff = prev if prev is not None else (_ist - timedelta(seconds=CMP_STALE_GUARD_SECS))
        with self.lock:
            rows = [(s, p, self.last_ltp_ts.get(s))
                    for s, p in self.last_ltp.items()
                    if p and self.last_ltp_ts.get(s) is not None
                    and self.last_ltp_ts[s] > cutoff]
        if not rows:
            log.warning("CMP flush SKIPPED — 0 fresh ticks since last flush "
                        "(WS feed likely dead; NOT stamping stale prices)")
            return
        # cc#367: SANITY GUARD (defense-in-depth behind the spot-only last_ltp fix above). A genuine
        # spot tick must sit within CMP_SANITY_MAX_DEV of the symbol's most recent completed equity
        # 5m bar in the SAME session; a larger gap = a polluted tick (fut LTP that slipped through,
        # corrupt post-close tick, symbol mis-map). Reject those writes and record them to ops_log so
        # the eq snapshot can't be corrupted. Best-effort — on any error we write `rows` unchanged.
        try:
            _syms = [r[0] for r in rows]
            _ref = {}
            with self.conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT ON (symbol) symbol, close FROM intraday_prices
                    WHERE source IN ('fyers_eq', 'fyers_ext') AND timeframe='5m'   -- cc#809
                      AND ts::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
                      AND symbol = ANY(%s)
                    ORDER BY symbol, ts DESC
                """, (_syms,))
                for _s, _c in cur.fetchall():
                    if _c: _ref[_s] = float(_c)
            _kept, _rejected = [], []
            for r in rows:
                base = _ref.get(r[0])
                if base and base > 0 and abs(float(r[1]) / base - 1.0) > CMP_SANITY_MAX_DEV:
                    _rejected.append((r[0], round(float(r[1]), 2), round(base, 2),
                                      round((float(r[1]) / base - 1.0) * 100, 2)))
                else:
                    _kept.append(r)
            if _rejected:
                _ops_log(self.conn, 'alert', 'cmp_guard_reject',
                         {"n": len(_rejected), "threshold_pct": CMP_SANITY_MAX_DEV * 100,
                          "rejected": [{"symbol": s, "cmp": p, "eq_bar_close": b, "dev_pct": d}
                                       for s, p, b, d in _rejected[:40]]})
                log.warning(f"CMP guard: rejected {len(_rejected)} polluted tick(s) "
                            f">{CMP_SANITY_MAX_DEV*100:.0f}% off eq bar: "
                            + ", ".join(f"{s}({d:+.1f}%)" for s, _, _, d in _rejected[:8]))
            rows = _kept
        except Exception as e:
            log.warning(f"flush_cmp sanity guard skipped (writing rows unchanged): {e}")
        if not rows:
            return
        try:
            with self.conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO cmp_prices (symbol, cmp, updated_at, source)
                    VALUES (%s, %s, %s, 'fyers')
                    ON CONFLICT (symbol) DO UPDATE SET
                        cmp=EXCLUDED.cmp, updated_at=EXCLUDED.updated_at, source='fyers'
                """, rows)
            self.conn.commit()
            self._last_cmp_flush_ts = _ist
            log.info(f"CMP flushed: {len(rows)} fresh symbols")
        except Exception as e:
            log.warning(f"flush_cmp: {e}")


# ── option bar store ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

class OptionBarStore:
    """Stores 5-min option ticks into option_chain."""
    def __init__(self, conn, opt_mgr: OptionSymbolManager):
        self.conn    = conn
        self.opt_mgr = opt_mgr
        self.bars    = {}
        self.lock    = threading.RLock()
        self.last_oi = {}   # fyers_option_symbol -> latest OI from DEPTH poll (WS strips OI)
        self._db_reconnect_attempted = False  # cc#489 step_4: DB-write resilience

    def _bucket(self, ts):
        # 5-min bucket
        return ts.replace(minute=ts.minute - ts.minute % BAR_MINUTES, second=0, microsecond=0)

    def on_tick(self, fsym, ltp, oi=None, vol=None, bid=None, ask=None, ts=None):
        ts  = ts or datetime.now(IST).replace(tzinfo=None)
        bkt = self._bucket(ts)
        key = (fsym, bkt)
        with self.lock:
            existing = self.bars.get(key)
            if existing is None or existing['bkt'] != bkt:
                if existing is not None:
                    self._flush(fsym, existing)
                self.bars[key] = {'bkt': bkt, 'ltp': ltp, 'oi': oi,
                                  'vol': vol, 'bid': bid, 'ask': ask}
            else:
                existing['ltp'] = ltp
                if oi  is not None: existing['oi']  = oi
                if vol is not None: existing['vol'] = vol
                if bid is not None: existing['bid'] = bid
                if ask is not None: existing['ask'] = ask

    def _flush(self, fsym, bar):
        # cc#193: same off-hours guard as the equity/futures aggregator — never
        # persist an option bar outside a real trading session (phantom weekend
        # ticks are garbage).
        try:
            bt = bar['bkt']
            # cc#855: options are EQUITY DERIVATIVES — they trade to 15:40 like futures, not to
            # the old 15:30. These rows go to option_chain, so there is no source tag to apply.
            if (not is_trading_day(bt.date())) or bt.time() < MARKET_OPEN or bt.time() >= FUT_CLOSE:
                return
        except Exception:
            pass
        meta = self.opt_mgr.lookup(fsym)
        if not meta: return
        underlying, strike, otype, expiry = meta
        # WS strips OI (Fyers SDK pops it) -> fall back to the DEPTH-poll value.
        oi = bar['oi'] if bar.get('oi') is not None else self.last_oi.get(fsym)
        # cc#591 fix_1: NEVER write a NULL option OI. A strike freshly subscribed after an ATM-roll
        # (or one the DEPTH-poll cycle hasn't reached yet — BANKNIFTY ATM±30 is a wide band) has no
        # WS-OI and no last_oi -> NULL, and NULL PE rows sum to 0 -> put_oi_total=0 -> the PCR
        # mood-gate (id=1916) corrupts/nulls (the 20-Jul 13:30 put-leg drop). Carry the symbol's last
        # known OI from option_chain (stale-carry, queried only on the rare None) and seed last_oi so
        # later bars reuse it without re-querying.
        if oi is None:
            try:
                with self.conn.cursor() as _c:
                    _c.execute("""SELECT oi FROM option_chain WHERE symbol=%s AND oi IS NOT NULL
                                  ORDER BY ts DESC LIMIT 1""", (fsym,))
                    _r = _c.fetchone()
                if _r and _r[0] is not None:
                    oi = int(_r[0]); self.last_oi[fsym] = oi
            except Exception:
                pass
        try:
            with self.conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO option_chain
                        (symbol, underlying, strike, option_type, expiry, ltp, oi, volume, bid, ask, ts)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (symbol, ts) DO UPDATE SET
                        ltp=EXCLUDED.ltp, oi=EXCLUDED.oi, volume=EXCLUDED.volume,
                        bid=EXCLUDED.bid, ask=EXCLUDED.ask
                """, (fsym, underlying, strike, otype, expiry,
                      bar['ltp'], oi, bar['vol'], bar['bid'], bar['ask'], bar['bkt']))
            self.conn.commit()
            self._db_reconnect_attempted = False
        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            # cc#489 step_4: same DB-write resilience as BarAggregator._flush.
            if self._db_reconnect_attempted:
                log.critical(f"option_bar flush {fsym}: DB conn still dead after reconnect "
                             f"({e}) — os._exit(1) for a clean Railway restart")
                os._exit(1)
            log.error(f"option_bar flush {fsym}: DB conn error ({e}) — reconnecting once...")
            try:
                self.conn.close()
            except Exception:
                pass
            try:
                # cc#876: get_db(), not a bare connect — this reconnect path used to rebuild
                # the connection WITHOUT timeouts or keepalives, so a socket that died once
                # could be replaced by one that hangs the next time. The safe wrapper already
                # existed; these two call sites simply bypassed it.
                self.conn = get_db()
                self._db_reconnect_attempted = True
            except Exception as e2:
                log.critical(f"option_bar flush {fsym}: DB reconnect FAILED ({e2}) — "
                             "os._exit(1) for a clean Railway restart")
                os._exit(1)
        except Exception as e:
            log.warning(f"option_bar flush {fsym}: {e}")

    def flush_all(self):
        with self.lock:
            for (fsym, _), bar in list(self.bars.items()):
                self._flush(fsym, bar)


# ── index LTP ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def update_index_ltp(conn, token, agg=None):
    try:
        r = requests.get(QUOTES_URL, params={'symbols': ','.join(INDEX_LTP_SYMBOLS.values())},
                         headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'}, timeout=5)
        d = r.json()
        if d.get('s') != 'ok': return
        _ist = datetime.now(IST).replace(tzinfo=None)
        rows = []
        for item in d.get('d', []):
            lp = item['v'].get('lp', 0)
            if not lp: continue
            for name, fsym in INDEX_LTP_SYMBOLS.items():
                if fsym == item['n']:
                    rows.append((name, lp, _ist))
                    if agg is not None:
                        agg.on_tick(name, float(lp), 0, source='fyers_eq')
        if rows:
            with conn.cursor() as cur:
                cur.executemany("""INSERT INTO cmp_prices (symbol,cmp,updated_at,source) VALUES (%s,%s,%s,'fyers')
                    ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, updated_at=EXCLUDED.updated_at, source='fyers'""", rows)
            conn.commit()
    except (psycopg2.InterfaceError, psycopg2.OperationalError, psycopg2.errors.InFailedSqlTransaction):
        # cc#497 fix_2b: a dead shared conn must propagate to the caller (housekeeping's
        # _mark_db_error) instead of being swallowed here forever — this call runs every
        # in-market loop tick, so it was one of the paths silently disabled by the 17-Jul conn
        # death alongside the watchdog/options-gate/alerting paths.
        raise
    except Exception as e:
        log.warning(f"Index LTP: {e}")


# ── purge ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def purge_old_bars(conn):
    now          = datetime.now(IST).replace(tzinfo=None)
    # cc#297: fyers_eq → 365d (long sim/BT7 history); futures_basis → 7d (the INTRADAY_FUT window,
    # NOT the equity constant — decoupled so a future equity bump can't silently drag it along).
    eq_cutoff    = now - timedelta(days=EQUITY_RETENTION_DAYS)             # intraday fyers_eq (730d, 2yr sim history)
    hist_cutoff  = now - timedelta(days=HIST_RETENTION_DAYS)              # cc#381: fyers_hist warehouse (730d, 2yr rolling)
    ext_cutoff   = now - timedelta(days=EXT_RETENTION_DAYS)               # cc#809: fyers_ext extended equity leg (30d)
    idx_cutoff   = now - timedelta(days=INDEX_RETENTION_DAYS)             # cc#809: index feeds (5yr, carved out by symbol)
    basis_cutoff = now - timedelta(days=INTRADAY_FUT_RETENTION_DAYS)       # futures_basis (7d, matches fyers_fut)
    fut_cutoff   = now - timedelta(days=INTRADAY_FUT_RETENTION_DAYS)       # intraday fyers_fut + legacy (7d)
    opt_cutoff   = now - timedelta(days=OPTION_RETENTION_DAYS)            # option_chain (7d, leaner)
    try:
        with conn.cursor() as cur:
            # cc#227: SOURCE-AWARE intraday retention. fyers_eq (canonical equity, cc#228) keeps
            # 365d (cc#297) for BT7/sim history; fyers_fut keeps 7d; residual legacy fyers/yahoo keep
            # 7d (shrinking once the cc#228 relabel/dedupe lands). IS DISTINCT FROM handles any NULL.
            # cc#377/381: source='fyers_hist' (backtest warehouse) rolls on its OWN 2yr window (730d,
            # HIST_RETENTION_DAYS) — was purge-exempt in cc#377; cc#381 gives it a cutoff so it rolls
            # instead of growing forever. Still excluded from the 7d "other" rule below.
            # cc#809 tier 1 (F&O equity, 730d) now EXCLUDES the index symbols, which move to tier 3.
            _idx_syms = sorted(INDEX_LTP_SYMBOLS.keys())
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s AND timeframe='5m' "
                        "AND source='fyers_eq' AND symbol <> ALL(%s)", (eq_cutoff, _idx_syms))
            eq_del = cur.rowcount
            # cc#809 tier 3: index feeds keep 5 years. Same source, carved out by symbol.
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s AND timeframe='5m' "
                        "AND source='fyers_eq' AND symbol = ANY(%s)", (idx_cutoff, _idx_syms))
            idx_del = cur.rowcount
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s AND timeframe='5m' "
                        "AND source='fyers_hist'", (hist_cutoff,))
            hist_del = cur.rowcount
            # cc#809: THIRD tier. The extended (non-F&O) equity leg rolls on 30 days — that is the
            # whole basis of the founder-approved ~+370 MB budget. It MUST also be excluded from the
            # catch-all below, or the 7-day futures rule would silently shred it down to a week.
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s AND timeframe='5m' "
                        "AND source=%s", (ext_cutoff, EXT_SOURCE))
            ext_del = cur.rowcount
            cur.execute("DELETE FROM intraday_prices WHERE ts < %s AND timeframe='5m' "
                        "AND source IS DISTINCT FROM 'fyers_eq' AND source IS DISTINCT FROM 'fyers_hist' "
                        "AND source IS DISTINCT FROM %s", (fut_cutoff, EXT_SOURCE))
            other_del = cur.rowcount
            cur.execute("DELETE FROM option_chain WHERE ts < %s", (opt_cutoff,))
            opt_del = cur.rowcount
            cur.execute("DELETE FROM futures_basis WHERE ts < %s", (basis_cutoff,))
            basis_del = cur.rowcount
        conn.commit()
        log.info(f"Purged intraday: fyers_eq={eq_del} (>{EQUITY_RETENTION_DAYS}d), "
                 f"index={idx_del} (>{INDEX_RETENTION_DAYS}d), "
                 f"fyers_hist={hist_del} (>{HIST_RETENTION_DAYS}d), "
                 f"{EXT_SOURCE}={ext_del} (>{EXT_RETENTION_DAYS}d), "
                 f"fut/legacy={other_del} (>{INTRADAY_FUT_RETENTION_DAYS}d); "
                 f"option_chain={opt_del} (>{OPTION_RETENTION_DAYS}d), "
                 f"futures_basis={basis_del} (>{INTRADAY_FUT_RETENTION_DAYS}d)")
    except Exception as e:
        log.warning(f"purge_old_bars: {e}")


def ensure_schemas(conn):
    with conn.cursor() as cur:
        cur.execute(OPTION_SCHEMA_SQL)
        cur.execute(FUTURES_BASIS_SCHEMA_SQL)
        cur.execute(EXT_BLACKLIST_SCHEMA_SQL)   # cc#809
        cur.execute("ALTER TABLE futures_basis ADD COLUMN IF NOT EXISTS oi BIGINT, "
                    "ADD COLUMN IF NOT EXISTS oi_prev BIGINT, ADD COLUMN IF NOT EXISTS oi_chg BIGINT")
        cur.execute("ALTER TABLE cmp_prices ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'fyers'")
    conn.commit()
    log.info("Schemas ready (option_chain, futures_basis, feed_ext_blacklist)")


# ── futures OI poll (DEPTH REST — quotes API has NO OI) ───────────────────────────────────────────────────────────────────────────────

_OI_POLL_LOCK = threading.Lock()

def poll_futures_oi(token, fut_syms, agg):
    """
    Fyers quotes API has NO OI (KB-confirmed) — depth API is the only source.
    1 symbol/call, rate-limited ~170 req/min. Runs in a background thread.
    Latest OI → agg.last_oi → attached on next futures bar flush → futures_basis.
    Debug: logs the raw response of the FIRST symbol each cycle for diagnosis.
    """
    if not _OI_POLL_LOCK.acquire(blocking=False):
        log.info("OI poll skipped — previous cycle still running")
        return
    # cc#883 item 2: when the detector has ruled REST degraded (empty bodies while the WS is
    # demonstrably alive), the pollers stand down for a while instead of continuing to knock.
    # Skipping a cycle costs OI freshness on the next futures bar; continuing to hammer a
    # throttling endpoint costs the account (cc#770 found the same shape from the other end).
    if _rest_is_degraded():
        _OI_POLL_LOCK.release()
        log.warning("OI poll SKIPPED — REST marked degraded (cc#883 backoff); WS is unaffected")
        return
    try:
        log.info(f"OI poll starting: {len(fut_syms)} futures via depth API")
        headers = {'Authorization': f'{FYERS_CLIENT_ID}:{token}'}
        got, first, empties = 0, True, 0
        # cc#770 THROTTLE FIX: the old loop `continue`d past its per-call sleep on any non-ok/empty body,
        # so it HAMMERED the depth API precisely while being rate-limited (~50% empty bodies, 862 JSON
        # errors since 30-Jul). Every iteration now sleeps: base spacing on success, EXPONENTIAL backoff +
        # jitter on an empty/throttled body (consecutive empties raise the pace, capped), decaying to base
        # on the next success. Stops the hammering that may itself be degrading the account's entitlement.
        base_spacing = OI_CALL_SPACING_SEC
        max_backoff  = max(base_spacing * 16, 2.0)
        consec_empty = 0
        for fsym in fut_syms:
            throttled = False
            try:
                r = requests.get(DEPTH_URL,
                                 params={'symbol': fsym, 'ohlcv_flag': 1},
                                 headers=headers, timeout=8)
                if first:
                    log.info(f"OI poll debug {fsym}: HTTP {r.status_code} body={r.text[:300]}")
                    first = False
                # cc#473: feed the dead-token detector — a char-0/401 response here is the exact
                # expired-token signature that ran silent all 13-Jul morning.
                if _dead_signal(r):
                    _note_api(True); throttled = True
                elif r.status_code == 429 or not (r.text or '').strip():
                    throttled = True   # cc#770: empty body / rate-limit — the throttle signature
                else:
                    _note_api(False)
                    d = r.json()
                    if d.get('s') != 'ok':
                        throttled = True
                    else:
                        data_d = d.get('d')
                        node = {}
                        if isinstance(data_d, dict):
                            node = data_d.get(fsym) or (next(iter(data_d.values())) if data_d else {})
                        elif isinstance(data_d, list) and data_d and isinstance(data_d[0], dict):
                            node = data_d[0].get('v', data_d[0])
                        oi = node.get('oi') if isinstance(node, dict) else None
                        if oi is not None:
                            nse = from_fyers_symbol(fsym)
                            agg.last_oi[nse] = int(oi)   # GIL-atomic dict write; no lock needed
                            got += 1
            except Exception as e:
                log.warning(f"poll_futures_oi {fsym}: {e}")
                throttled = True
            if throttled:
                empties += 1
                consec_empty += 1
                spacing = min(max_backoff, base_spacing * (2 ** min(consec_empty, 4)))
            else:
                consec_empty = 0
                spacing = base_spacing
            time.sleep(spacing + (got % 5) * 0.03)   # jitter
        rate = round(got / len(fut_syms) * 100, 1) if fut_syms else 0
        log.info(f"OI poll (depth API): {got}/{len(fut_syms)} futures OI updated ({rate}% ok, {empties} throttled/empty)")
        if empties and fut_syms and (empties / len(fut_syms)) > 0.3:
            log.warning(f"OI depth poll THROTTLED: {empties}/{len(fut_syms)} empty ({rate}% ok) — "
                        "exponential backoff engaged (cc#770)")
    finally:
        _OI_POLL_LOCK.release()


_OPT_OI_POLL_LOCK = threading.Lock()
_STOCK_OPT_OI_POLL_LOCK = threading.Lock()   # cc#375: separate lock so a slow stock OI cycle never blocks the index poll


# ── cc#473: in-process DEAD-TOKEN detector ────────────────────────────────────────
# 13-Jul incident: worker ran all morning on an expired token — every REST poll came
# back char-0 EMPTY, but there was no in-process detector, so it needed a manual
# Railway restart at 10:04. Fix: count CONSECUTIVE dead-token REST responses (empty
# body or HTTP 401) across the OI poll + a 30s canonical probe; at DEAD_TOKEN_THRESHOLD
# consecutive, raise a dead flag the housekeeping loop consumes to self-heal (inline
# breaker-safe relogin -> clean reboot that REUSES the fresh same-day token). ANY
# non-empty/structured response resets the counter (token is alive).
DEAD_TOKEN_THRESHOLD = 10
_auth_lock  = threading.Lock()
_auth_state = {'consec': 0, 'dead_flag': False}

# ── cc#883 item 2: WS-LIVENESS CROSS-CHECK ────────────────────────────────────────────────────
# 07-Aug, ~09:55: the REST legs (index LTP + OI depth) started returning empty while the WS was
# ticking perfectly — 212 equity and 208 futures symbols, bars landing every 5 minutes. The
# detector above counted ten empties and declared the TOKEN dead. It was not: a token that is
# dead for REST is dead for the WS too, and the WS was demonstrably alive. The self-heal then
# relogged in with a stale PIN every 90 seconds and Fyers blocked the account at 10:07.
#
# So the rule is now: REST-empty AND WS-alive is a REST problem, never a token problem. It logs
# feed_rest_degraded, backs the REST pollers off, and does NOT touch authentication. Only
# REST-dead AND WS-dead (or an explicit 401) is allowed to reach the relogin path.
#
# The evidence is the WS itself rather than a DB read: on_message stamps a monotonic clock per
# leg on every accepted tick, so "is the socket delivering data" is answered by the socket.
WS_ALIVE_MAX_AGE_S   = 360          # 6 minutes — one missed 5-min bar plus slack, per the guardian
REST_DEGRADED_SEC    = 900          # how long a degraded verdict backs the REST pollers off
_ws_last_tick        = {'fyers_eq': 0.0, 'fyers_fut': 0.0}
_rest_degraded_until = 0.0


def _note_ws_tick(source):
    """Stamped on every accepted WS tick. Deliberately lock-free: a float store is atomic under
    the GIL, this runs on the hot message path, and a stamp that is one tick stale changes no
    decision that a 6-minute window makes."""
    if source in _ws_last_tick:
        _ws_last_tick[source] = time.monotonic()


def _ws_alive(max_age_s=WS_ALIVE_MAX_AGE_S):
    """(alive, detail). Alive when EITHER leg has ticked inside the window — the card's rule.
    One leg can legitimately go quiet (futures after 15:30, a thin equity session); both going
    quiet at once while REST is also empty is the only shape that means the token."""
    now = time.monotonic()
    ages = {k: (None if v <= 0 else round(now - v, 1)) for k, v in _ws_last_tick.items()}
    fresh = [k for k, a in ages.items() if a is not None and a <= max_age_s]
    return (bool(fresh), {'ages_sec': ages, 'fresh_legs': fresh, 'window_sec': max_age_s})


def _mark_rest_degraded():
    """Back the REST pollers off without touching auth."""
    global _rest_degraded_until
    _rest_degraded_until = time.monotonic() + REST_DEGRADED_SEC


def _rest_is_degraded():
    return time.monotonic() < _rest_degraded_until


def _dead_signal(r):
    """True iff a REST response is the dead/expired-token signature: an HTTP 401 or a
    char-0 EMPTY body (the exact 09-Jul/13-Jul signature). A NON-empty body — even an
    error/rate-limit JSON — is NOT a dead-token signal (token still authenticates)."""
    try:
        if r is None:
            return False
        if getattr(r, 'status_code', None) == 401:
            return True
        return not (r.text or '').strip()
    except Exception:
        return False


def _note_api(dead: bool):
    """Feed the consecutive dead-token counter. dead=True increments (and trips the
    flag at the threshold); dead=False resets it (a live response clears the streak)."""
    with _auth_lock:
        if dead:
            _auth_state['consec'] += 1
            if _auth_state['consec'] >= DEAD_TOKEN_THRESHOLD:
                _auth_state['dead_flag'] = True
        else:
            _auth_state['consec'] = 0


def _consume_dead_flag():
    """Return True once when the dead-token threshold has been crossed, and reset."""
    with _auth_lock:
        if _auth_state['dead_flag']:
            _auth_state['dead_flag'] = False
            _auth_state['consec'] = 0
            return True
        return False

def poll_options_oi(token, opt_syms, opt_store, lock=None, label="index"):
    """
    Option OI via DEPTH REST. The WS feed strips OI (Fyers SDK pops the 'OI' field),
    so depth is the only live source — same pattern as poll_futures_oi.
    Index cycle (~136 NIFTY+BANKNIFTY ATM+/-10 syms ~= 48s) fits inside the 5-min bar.
    cc#375: also called for the SUBSCRIBED stock options (bounded by stock_options_limit)
    on a SEPARATE lock/thread, so their option_chain rows carry OI instead of NULL.
    Latest OI -> opt_store.last_oi[fsym] -> attached on next option bar flush.

    cc#482 fix_3: 13-Jul incident — an empty-body/failed depth response was silently
    swallowed (Python logger only, never persisted), so the caller went on writing
    stale carried-over OI with no visible trace. Empty/failed responses are now
    counted and, if any occurred this cycle, logged as an ops_log WARNING
    (category=data_audit) — the platform's telemetry table (MEMORY_TAXONOMY_V1
    routes telemetry off session_log to ops_log; functionally the same "not
    swallowed" requirement).
    """
    lock = lock or _OPT_OI_POLL_LOCK
    if not lock.acquire(blocking=False):
        log.info(f"Option OI poll ({label}) skipped — previous cycle still running")
        return
    try:
        log.info(f"Option OI poll ({label}) starting: {len(opt_syms)} options via depth API")
        headers = {'Authorization': f'{FYERS_CLIENT_ID}:{token}'}
        got = 0
        empty_fails = []
        # cc#843 fix_3b: the cc#770 exponential backoff was applied to the FUTURES depth poll only.
        # This loop kept its flat per-call sleep and so carried on hammering the depth API while it
        # was being rate-limited — 778 empty-body warnings in 52 minutes on 03-Aug, ~50% of the 244
        # index-option contracts. Same treatment as poll_futures_oi: every iteration sleeps, an
        # empty/throttled body raises the pace exponentially (capped) and a success decays it back.
        # Hammering a throttled endpoint is not neutral; it is plausibly part of what degrades the
        # account's entitlement in the first place.
        base_spacing = OI_CALL_SPACING_SEC
        max_backoff  = max(base_spacing * 16, 2.0)
        consec_empty = 0
        for fsym in opt_syms:
            throttled = False
            try:
                r = requests.get(DEPTH_URL,
                                 params={'symbol': fsym, 'ohlcv_flag': 1},
                                 headers=headers, timeout=8)
                body = (r.text or '').strip()
                if not body or r.status_code in (401, 429):
                    empty_fails.append(fsym)
                    throttled = True
                else:
                    d = r.json()
                    if d.get('s') != 'ok':
                        empty_fails.append(fsym)
                        throttled = True
                    else:
                        data_d = d.get('d')
                        node = {}
                        if isinstance(data_d, dict):
                            node = data_d.get(fsym) or (next(iter(data_d.values())) if data_d else {})
                        elif isinstance(data_d, list) and data_d and isinstance(data_d[0], dict):
                            node = data_d[0].get('v', data_d[0])
                        oi = node.get('oi') if isinstance(node, dict) else None
                        if oi is not None:
                            opt_store.last_oi[fsym] = int(oi)   # GIL-atomic dict write; no lock needed
                            got += 1
            except Exception as e:
                empty_fails.append(fsym)
                throttled = True
                log.warning(f"poll_options_oi ({label}) {fsym}: {e}")
            if throttled:
                consec_empty += 1
                spacing = min(max_backoff, base_spacing * (2 ** min(consec_empty, 4)))
            else:
                consec_empty = 0
                spacing = base_spacing
            time.sleep(spacing + (got % 5) * 0.03)   # jitter
        _rate = round(got / len(opt_syms) * 100, 1) if opt_syms else 0
        log.info(f"Option OI poll ({label}, depth API): {got}/{len(opt_syms)} option OI updated "
                 f"({_rate}% ok, {len(empty_fails)} throttled/empty)")
        if empty_fails and opt_syms and (len(empty_fails) / len(opt_syms)) > 0.3:
            log.warning(f"Option OI poll ({label}) THROTTLED: {len(empty_fails)}/{len(opt_syms)} "
                        f"empty ({_rate}% ok) — exponential backoff engaged (cc#843 fix_3b)")
        try:
            # cc#495 change_4: was failure-only — the daily feed log needs a real
            # success RATE (got/total), not just a count of empty responses.
            hc = get_db()
            _ops_log(hc, 'info', 'oi_poll_summary',
                      {'label': label, 'got': got, 'total': len(opt_syms),
                       'rate': round(got / len(opt_syms), 3) if opt_syms else None, 'ist': _ist_now_str()})
            if empty_fails:
                _ops_log(hc, 'data_audit', 'oi_poll_empty_response',
                          {'label': label, 'failed': len(empty_fails), 'total': len(opt_syms),
                           'sample': empty_fails[:15], 'ist': _ist_now_str()})
            hc.close()
        except Exception as _oe:
            log.warning(f"poll_options_oi ({label}) failed to log summary: {_oe}")
    finally:
        lock.release()


def _subscribe_union(*groups):
    """cc#884 item 3: THE one place the full subscribe set is assembled.

    The reconnect path and the scheduled sequence used to each build their own list. When a
    reconnect landed inside the scheduled window on 06-Aug they both subscribed, and post-verify
    counted eq 212 against a 206-symbol universe (ops_log 17059). Two builders is how two callers
    end up disagreeing about what "everything" means; one builder, order-preserving and deduped,
    is how they cannot."""
    out, seen = [], set()
    for g in groups:
        for s in (g or []):
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
    return out


def _batched_subscribe(ws, symbols, action='sub', label=''):
    """cc#151: single batched code path for subscribe/unsubscribe (WS_SUB_BATCH chunks
    + sleep + per-batch log). cc_task #88 batching was applied to on_connect only — the
    monthly-roll path still fired one bulk call (fut+options combined) and Fyers
    silently dropped symbols under that load (1-Jul roll: only 3/212 futures survived).
    This is now the ONLY subscribe/unsubscribe path, used by both on_connect and roll."""
    if not symbols:
        return
    # cc#884 item 3 — IDEMPOTENT BY CONSTRUCTION. On 09-Aug-06 09:16:02 a reconnect fired inside
    # the scheduled sequence's window and both paths subscribed; post-verify then counted eq
    # 212 against a 206-symbol universe (ops_log 17059) — the same symbol twice, counted twice.
    # Deduplicating here, at the ONE choke point every subscribe passes through, means "same
    # symbol twice" collapses to one entry no matter which two callers race. Order is preserved
    # so the canary/full batching order (cc#843) is unchanged.
    seen = set()
    deduped = []
    for s in symbols:
        if s in seen:
            continue
        seen.add(s)
        deduped.append(s)
    if len(deduped) != len(symbols):
        log.warning(f"_batched_subscribe({label or action}): {len(symbols) - len(deduped)} duplicate "
                    f"symbol(s) collapsed — {len(symbols)} requested, {len(deduped)} sent")
    symbols = deduped
    verb = 'Subscribing' if action == 'sub' else 'Unsubscribing'
    tag = f" ({label})" if label else ""
    log.info(f"{verb} {len(symbols)} symbols{tag} in batches of {WS_SUB_BATCH}")
    for i in range(0, len(symbols), WS_SUB_BATCH):
        batch = symbols[i:i + WS_SUB_BATCH]
        if action == 'sub':
            ws.subscribe(symbols=batch, data_type="SymbolUpdate")
        else:
            ws.unsubscribe(symbols=batch)
        log.info(f"{verb} batch {i // WS_SUB_BATCH + 1}: {len(batch)} symbols "
                 f"({min(i + WS_SUB_BATCH, len(symbols))}/{len(symbols)})")
        time.sleep(WS_SUB_BATCH_SLEEP_SEC)


# ── cold-boot CMP seed (cc#352, id166 family) ───────────────────────────────────
CMP_BOOT_STALE_MIN = 20   # cc#352: seed cmp_prices from REST if the freshest row is older than this
QUOTE_BATCH        = 50   # Fyers quotes API cap per request

def _seed_cmp_from_rest(conn, token, equity_symbols):
    """cc#352 (id166 family): on a cold/stale boot, seed cmp_prices SPOT from the Fyers REST
    quotes API for the full equity universe BEFORE the WS subscribes — so the worker is never
    left price-less on a pre-market / crash / overnight boot and the options live-price gate has
    prices immediately (empty cmp_prices was the root of the connected-but-deaf zombie).

    cmp_prices holds SPOT (equity/index) price keyed by the plain NSE symbol — matching every
    existing consumer (_get_cmp, the ATM gate) — so we seed the EQUITY leg (NSE:SYM-EQ), never
    futures/options prices (which would corrupt the spot cache). Additive + fully guarded: 3x
    retry with backoff per batch; on total failure logs ops_log feed_boot_initial_failed and
    falls back to prior behavior. This function NEVER raises — it can only add prices, never
    change the subscribe logic, so it cannot break the feed."""
    try:
        with conn.cursor() as cur:
            # cc#489 step_5: was `EXTRACT(EPOCH FROM (NOW() - MAX(updated_at)))/60` — NOW()
            # is tz-aware (UTC-based session), but updated_at is stored naive-IST (the
            # codebase convention: datetime.now(IST).replace(tzinfo=None)), so Postgres
            # compared a UTC instant against a value ~5.5h off from what it actually meant
            # -> negative/wrong ages ("cmp_prices fresh (222 rows, -279m old)" in the 16-Jul
            # boot log). Compute age in Python using the same naive-IST convention instead.
            cur.execute("SELECT COUNT(*), MAX(updated_at) FROM cmp_prices")
            n, max_updated_at = cur.fetchone()
        n = n or 0
        age_min = ((datetime.now(IST).replace(tzinfo=None) - max_updated_at).total_seconds() / 60
                   if max_updated_at else None)
        if n > 0 and age_min is not None and age_min <= CMP_BOOT_STALE_MIN:
            log.info(f"boot cmp seed: cmp_prices fresh ({n} rows, {age_min:.0f}m old) — skip REST seed")
            return
        log.warning(f"boot cmp seed: cmp_prices empty/stale (rows={n}, age_min={age_min}) — "
                    f"seeding SPOT from Fyers REST before subscribe (cc#352/id166)")
    except Exception as e:
        log.warning(f"boot cmp seed precheck failed (skipping seed): {e}")
        return

    fsyms = [fyers_eq_symbol(s) for s in equity_symbols]   # NSE:SYM-EQ (M&M handled by SPECIAL_SYMBOLS)
    seeded, failed_batches = 0, 0
    for i in range(0, len(fsyms), QUOTE_BATCH):
        batch = fsyms[i:i + QUOTE_BATCH]
        rows = []
        for attempt in range(3):
            try:
                r = requests.get(QUOTES_URL, params={'symbols': ','.join(batch)},
                                 headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'}, timeout=10)
                d = r.json()
                if d.get('s') != 'ok':
                    raise RuntimeError(f"quotes s={d.get('s')} {str(d)[:120]}")
                ist = datetime.now(IST).replace(tzinfo=None)
                for item in d.get('d', []):
                    lp   = (item.get('v') or {}).get('lp')
                    fsym = item.get('n')
                    if not lp or not fsym:
                        continue
                    rows.append((from_fyers_symbol(fsym), float(lp), ist))
                break
            except Exception as e:
                if attempt == 2:
                    failed_batches += 1
                    log.warning(f"boot cmp seed batch {i // QUOTE_BATCH} failed after 3x: {e}")
                else:
                    time.sleep(2 ** attempt)
        if rows:
            try:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO cmp_prices (symbol,cmp,updated_at,source) VALUES (%s,%s,%s,'fyers_boot') "
                        "ON CONFLICT (symbol) DO UPDATE SET cmp=EXCLUDED.cmp, "
                        "updated_at=EXCLUDED.updated_at, source='fyers_boot'", rows)
                conn.commit()
                seeded += len(rows)
            except Exception as e:
                log.warning(f"boot cmp seed write batch {i // QUOTE_BATCH}: {e}")
    if seeded == 0:
        try:
            _ops_log(conn, 'alert', 'feed_boot_initial_failed',
                     {'reason': 'REST cmp seed returned 0 rows', 'failed_batches': failed_batches,
                      'universe': len(fsyms), 'ist': _ist_now_str()})
        except Exception:
            pass
        log.error("boot cmp seed: 0 rows seeded — feed_boot_initial_failed (falling back to prior behavior)")
    else:
        log.info(f"boot cmp seed: seeded {seeded}/{len(fsyms)} equity SPOT from REST "
                 f"({failed_batches} batch failures) (cc#352)")


# ── main run ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

# ── cc#390: Phase A 12-mo warehouse INSIDE the worker (main app lacks FYERS_TOTP env; worker has it) ──
# The whole loop runs on its OWN daemon thread with its OWN db connection + token, so it can NEVER
# block or corrupt the WS loop's connection. Market-hours safe (pauses to 15:35 if it crosses a live
# session). Reuses cc#389's fetch_hist_5m + progress/log helpers (import only — no duplicated rules).
def _phase_a_market_open():
    now = datetime.now(IST)
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= SESSION_END   # cc#855


def _claim_phase_a_worker():
    """Atomic FOR UPDATE claim of app_config phase_a_run='pending' -> 'claimed_worker'."""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM app_config WHERE key='phase_a_run' AND value='pending' FOR UPDATE")
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE app_config SET value='claimed_worker', updated_at=NOW() WHERE key='phase_a_run'")
        conn.commit()
        return bool(r)
    except Exception as e:
        log.error(f"phase_a worker claim failed: {e}")
        return False
    finally:
        conn.close()


def _worker_run_phase_a():
    """Full trailing-365d 5m EQ warehouse for every active futures symbol, on the worker's token.
    Resumable (app_config phase_a_progress); failures logged + skipped; ops_log every 20; completion
    session_log PHASE_A_WAREHOUSE_COMPLETE. Pauses between symbols if a live session starts."""
    import fyers_hist_backfill as fhb
    conn = get_db()
    started = time.time()
    try:
        token = get_valid_token(conn)                       # worker owns the Fyers secrets
        today = datetime.now(IST).replace(tzinfo=None).date()
        frm = today - timedelta(days=365)
        with conn.cursor() as cur:
            cur.execute("SELECT UPPER(symbol) FROM futures_universe WHERE is_active=TRUE ORDER BY symbol")
            symbols = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT value FROM app_config WHERE key=%s", (fhb._PHASE_A_PROGRESS,))
            r = cur.fetchone()
        progress = (r[0] or "") if r else ""
        pending = [s for s in symbols if s > progress] if progress else list(symbols)
        fhb._oplog(conn, "data_infra", "PHASE_A_START",
                   {"where": "worker", "universe": len(symbols), "to_do": len(pending),
                    "from": str(frm), "to": str(today), "resuming_after": progress or None})
        done = bars = fails = 0
        failures = []
        for sym in pending:
            # market-hours guard — never fetch during a live session; resume-safe if it crosses over
            while _phase_a_market_open():
                nowt = datetime.now(IST)
                run_at = nowt.replace(hour=15, minute=35, second=0, microsecond=0)
                wait_s = max(60, (run_at - nowt).total_seconds())
                log.info(f"cc#390 Phase A paused for market hours — recheck in {min(wait_s,900)/60:.0f} min")
                time.sleep(min(wait_s, 900))
            try:
                res = fhb.fetch_hist_5m(sym, frm, today, conn=conn, token=token)
                bars += res["bars"]
            except Exception as e:
                fails += 1
                failures.append({"symbol": sym, "error": str(e)[:200]})
                log.error(f"cc#390 Phase A {sym} failed: {e}")
            done += 1
            try:
                fhb._set_config(conn, fhb._PHASE_A_PROGRESS, sym)
            except Exception as e:
                log.warning(f"phase_a checkpoint {sym}: {e}")
            if done % 20 == 0:
                fhb._oplog(conn, "data_infra", "PHASE_A_PROGRESS",
                           {"where": "worker", "done": done, "of": len(pending), "bars": bars,
                            "failures": fails, "elapsed_min": round((time.time() - started) / 60, 1),
                            "last": sym})
            time.sleep(5)                                    # inter-symbol pacing (chunks already 5s-paced)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COUNT(DISTINCT symbol), MIN(ts)::date FROM intraday_prices WHERE source=%s",
                        (fhb.HIST_SOURCE,))
            c = cur.fetchone()
        summary = {"where": "worker", "symbols_processed": done, "universe": len(symbols),
                   "bars_written_this_run": bars, "failures": fails, "failures_list": failures,
                   "elapsed_min": round((time.time() - started) / 60, 1),
                   "warehouse_total_bars": int(c[0] or 0), "warehouse_symbols": int(c[1] or 0),
                   "warehouse_oldest": str(c[2]) if c[2] else None, "source": fhb.HIST_SOURCE}
        try:
            with conn.cursor() as cur:
                cur.execute("""INSERT INTO session_log (session_date, session_ts, category, title, details)
                               VALUES (CURRENT_DATE, NOW(), 'data_audit', 'PHASE_A_WAREHOUSE_COMPLETE', %s::jsonb)""",
                            (json.dumps(summary, default=str),))
            conn.commit()
        except Exception as e:
            log.warning(f"PHASE_A completion log: {e}")
        try:
            fhb._set_config(conn, fhb._PHASE_A_FLAG, "done")
        except Exception:
            pass
        log.info(f"cc#390 worker Phase A COMPLETE: {summary}")
    except Exception as e:
        log.error(f"cc#390 worker Phase A fatal: {e}")
    finally:
        conn.close()


def _phase_a_worker_daemon():
    """Boot + hourly idle check: when off-market, atomically claim phase_a_run and run the warehouse
    once, then keep idle-checking hourly (so a missed boot claim still fires later).
    cc#390 follow-up: main-app auto-claim removed, so the worker is the sole owner of this flag."""
    while True:
        try:
            if not _phase_a_market_open() and _claim_phase_a_worker():
                log.info("cc#390: phase_a_run claimed on worker — starting 365d warehouse (background)")
                _worker_run_phase_a()
        except Exception as e:
            log.error(f"cc#390 phase_a daemon: {e}")
        time.sleep(3600)


def run(auth_code=None):
    import fyers_backfill
    from fyers_apiv3.FyersWebsocket import data_ws

    # cc#497 fix_1_TIMING_FINAL_FOUNDER_17JUL: boot_time anchors the pre-open-vs-midmarket boot
    # decision (root_cause_1_ws_premarket_zombie + midmarket_boot_rule) — captured once, before
    # anything else, since it must reflect when the WORKER started, not when the WS happens to
    # (re)connect. _sub_state tracks whether today's initial subscribe sequence has completed,
    # shared (by mutation, no nonlocal needed) between on_connect/housekeeping/the sequence
    # runner below.
    boot_time  = datetime.now(IST)
    _sub_state = {'day': None, 'done': False, 'holder': None, 'last_ok': 0.0}
    # cc#843: the subscribe sequence is now anchored on WHEN THE CONNECTION WAS ESTABLISHED, not on
    # when the process booted. Under the old design those were the same moment; under the 09:16 gate
    # they are not, and boot_time would have mis-routed a 09:05 boot down the pre-open path.
    _conn_state = {'established_at': None, 'connect_called_at': None}
    # cc#759 fix1: ONE process-level mutex around every subscribe sequence. The 29-Jul death was two
    # sequences (guardian-all + scheduled-0920) subscribing the SAME WS concurrently at 09:20 -> every
    # symbol subscribed twice -> broker dropped the futures leg 210->18. A second caller now NO-OPs.
    _subscribe_lock = threading.Lock()
    SUBSCRIBE_IDEMPOTENCY_SEC = 180   # a non-recovery re-trigger within this window of a success is a no-op

    conn    = get_db()
    token   = get_valid_token(conn, auth_code)
    token   = _boot_auth_selfcheck(conn, token)   # cc#339 fix_2: prove REST auth BEFORE subscribing
    symbols = get_universe(conn)

    # cc#162: futures leg = stocks + confirmed-active index futures (NIFTY/
    # BANKNIFTY). Equity leg stays `symbols`-only -- no -EQ instrument exists
    # for an index, so it must never be added there.
    index_fut_codes = get_index_futures_universe(conn)
    fut_codes       = symbols + index_fut_codes

    # cc#809: the EXTENDED equity leg (full screener CSV universe minus the F&O set). Sliced by the
    # app_config staging dial — 'off' by default, so merely deploying this changes nothing.
    ext_limit   = _ext_stage_limit(conn)
    ext_symbols = get_extended_universe(conn, symbols, ext_limit)

    ensure_schemas(conn)
    _boot_gap_report(conn)   # cc#339 fix_3: self-document any outage window at boot
    threading.Thread(target=_phase_a_worker_daemon, name="cc390-phasea", daemon=True).start()   # cc#390
    log.info(f"Universe: {len(symbols)} equity + {len(fut_codes)} futures "
             f"({len(index_fut_codes)} index: {index_fut_codes}) + options")
    log.info(f"cc#809 extended leg: stage={ext_limit if ext_limit < 10**9 else 'all'} -> "
             f"{len(ext_symbols)} symbols (source={EXT_SOURCE}, retention={EXT_RETENTION_DAYS}d)")

    # cc#352 (id166 family): seed cmp_prices SPOT from Fyers REST on a cold/stale boot BEFORE the
    # WS subscribes, so the worker is never price-less (the options live-price gate needs a
    # populated cmp_prices, and an empty cache was the root of the connected-but-deaf zombie).
    # Fully guarded — never raises, only adds prices, never alters subscribe logic.
    _seed_cmp_from_rest(conn, token, symbols)

    # Skip-if-fresh: only backfill when today's intraday data is missing.
    # When fresh, skip the ~40-min sequential backfill entirely (instant restart).
    def _intraday_fresh():
        try:
            with conn.cursor() as c:
                # cc_task #88 GAP_1: only count actual market-hours bars (ts::time >= 09:15)
                # so a stale pre-market bar (e.g. 07:15) never makes the boot-backfill skip.
                c.execute("SELECT COUNT(DISTINCT symbol) FROM intraday_prices "
                          "WHERE ts::date = CURRENT_DATE AND timeframe='5m' "
                          "AND ts::time >= '09:15:00'")
                n = c.fetchone()[0] or 0
            return n >= 150   # most of universe already has today's bars
        except Exception:
            return False

    if _intraday_fresh():
        log.info("Boot backfill SKIPPED — today's intraday already fresh (>=150 symbols)")
    else:
        # Defer backfill to a background thread so the live WS connects immediately.
        def _deferred_backfill():
            # cc_task #87: Yahoo/REST backfill must NEVER write during market hours
            # (09:15-15:30 IST) — stale history bars caused a wrong-price paper entry.
            # If this thread wakes inside the live session, hold it until 15:35 IST.
            now = datetime.now(IST)
            if now.weekday() < 5 and MARKET_OPEN <= now.time() <= SESSION_END:
                # cc#855: hold to 15:45, past FUT_CLOSE — 15:35 now lands INSIDE the
                # derivatives session and a backfill write there would race live bars.
                run_at  = now.replace(hour=15, minute=45, second=0, microsecond=0)
                wait_s  = max(0, (run_at - now).total_seconds())
                log.info(f"Deferred backfill held to 15:35 IST (market open) — sleeping {wait_s/60:.0f} min")
                time.sleep(wait_s)
            log.info("Deferred backfill (7-day equity, sequential, background)...")
            try:
                fyers_backfill.backfill_7day(token, conn)
                log.info("Deferred backfill complete")
            except Exception as e:
                log.error(f"Deferred backfill failed (continuing): {e}")
        threading.Thread(target=_deferred_backfill, daemon=True).start()

    expiry = current_expiry()
    log.info(f"Active expiry: {expiry}")

    equity_fyers_syms  = [fyers_eq_symbol(s) for s in symbols]
    futures_fyers_syms = [futures_fyers_symbol(s, expiry) for s in fut_codes]   # cc#162: + index futures
    ext_fyers_syms     = [fyers_eq_symbol(s) for s in ext_symbols]              # cc#809: extended equity leg

    master = OptionMaster()
    master.load()

    opt_mgr     = OptionSymbolManager(conn, token=token, master=master)
    # cc#189 (founder redesign): options are NOT built/subscribed at boot. They
    # subscribe later — only once the market is open AND cmp_prices is fresh — via
    # the gate in housekeeping(). This eliminates the cold-boot bug where an empty
    # cmp_prices silently produced zero option subscriptions on a pre-market restart.
    option_syms = []

    all_syms    = equity_fyers_syms + futures_fyers_syms + ext_fyers_syms
    log.info(f"WS: {len(equity_fyers_syms)} eq + {len(futures_fyers_syms)} fut + "
             f"{len(ext_fyers_syms)} ext + 0 opt (options deferred to live-price gate) "
             f"= {len(all_syms)} total")

    equity_set  = set(equity_fyers_syms)
    futures_set = set(futures_fyers_syms)
    # cc#809: a symbol can only ever be in ONE of these. get_extended_universe already excludes the
    # F&O equity codes, but the sets are the thing on_message actually dispatches on, so the
    # invariant is enforced here too — an overlap would tag the same bar under two sources and
    # double-count it in every per-source health read.
    ext_set     = set(ext_fyers_syms) - equity_set - futures_set

    agg       = BarAggregator(conn)
    opt_store = OptionBarStore(conn, opt_mgr)
    access    = f"{FYERS_CLIENT_ID}:{token}"

    # cc#770 CAPTURE: non-tick WS frames (subscribe acks, status, embedded rejects) were silently dropped
    # in on_message — we were blind to WHY the futures leg delivers nothing. Log the first 40 verbatim,
    # then one every 30s (rate-limited so a reject storm can't flood the log, but nothing is invisible).
    _frame_diag = {'n': 0, 'last_log': 0.0}
    def _capture_frame(msg):
        try:
            _frame_diag['n'] += 1
            now = time.monotonic()
            if _frame_diag['n'] <= 40 or (now - _frame_diag['last_log']) >= 30:
                _frame_diag['last_log'] = now
                log.info(f"WS non-tick frame #{_frame_diag['n']} (cc#770 capture): {str(msg)[:400]}")
        except Exception:
            pass

    def on_message(msg):
        try:
            fsym = msg.get('symbol', '')
            ltp  = msg.get('ltp')
            vol  = msg.get('vol_traded_today') or msg.get('volume') or 0
            if not fsym or not ltp:
                _capture_frame(msg)   # cc#770: surface ack/status/reject frames instead of discarding them
                return

            if fsym in equity_set:
                _note_ws_tick('fyers_eq')   # cc#883 item 2: proof the socket is delivering
                agg.on_tick(from_fyers_symbol(fsym), float(ltp), float(vol), source='fyers_eq')
            elif fsym in ext_set:
                # cc#809: extended (non-F&O) equity — identical spot handling, own source tag so it
                # gets the 30d tier and never inflates the watchdog's fyers_eq count.
                agg.on_tick(from_fyers_symbol(fsym), float(ltp), float(vol), source=EXT_SOURCE)
            elif fsym in futures_set:
                # OI not in WS — sourced from depth REST poll (agg.last_oi)
                nse = from_fyers_symbol(fsym)
                _note_ws_tick('fyers_fut')   # cc#883 item 2
                agg.on_tick(nse, float(ltp), float(vol),
                            source='fyers_fut', oi=agg.last_oi.get(nse))
            else:
                opt_store.on_tick(fsym, float(ltp),
                                  vol=float(vol),
                                  bid=msg.get('bid'), ask=msg.get('ask'))
        except Exception as e:
            log.warning(f"on_message: {e}")

    def on_connect():
        _RECONNECT_CONFIRMED.set()   # cc#876: satisfies the forced-reconnect deadline timer
        # cc#497 fix_1_TIMING_FINAL_FOUNDER_17JUL (root_cause_1_ws_premarket_zombie):
        # boot-time/connect-time auto-subscribe is REMOVED ENTIRELY. Subscriptions made on a
        # PRE-OPEN Fyers WS session silently do not survive to market open — the OPEN_RACE_GUARD
        # wait-then-subscribe-here pattern this replaced was exactly that trap on any pre-open or
        # overnight connect. Subscription is now owned by the two-stage canary/full sequence
        # (_run_subscribe_sequence, fired by housekeeping's wall-clock/boot-time triggers), NOT
        # by this callback — EXCEPT once we're already on the safe side of the pre-open trap:
        # either the market is currently open (any reconnect there, e.g. the watchdog's rung1,
        # must resubscribe immediately to actually recover — waiting on _sub_state['done'] would
        # make a rung1 reconnect a no-op if the initial sequence hadn't formally finished yet),
        # or today's initial sequence already completed (a later off-hours reconnect, e.g. an
        # 18:00 heal-adjacent blip, is also safe to just resubscribe).
        now_t     = datetime.now(IST)
        _conn_state['established_at'] = now_t   # cc#843: anchors the sequence + the pre-open check
        in_market = now_t.weekday() < 5 and MARKET_OPEN <= now_t.time() <= SESSION_END   # cc#855
        if in_market or (_sub_state.get('day') == now_t.date() and _sub_state.get('done')):
            # cc#759 fix1/fix4: a reconnect MUST re-subscribe (never leave a connected WS with zero subs),
            # but must not race a running sequence on the same socket — take the SAME mutex. If a sequence
            # holds it, that sequence owns the (re)subscribe, so defer to it rather than double-subscribe.
            if _subscribe_lock.acquire(blocking=False):
                # cc#884 item 3: NAME THE HOLDER. This path took the mutex without ever writing
                # _sub_state['holder'], so when it won the race the scheduled sequence logged
                # "blocked, holder=None" (ops_log 17056, 09:16:02) — a mutex report that names
                # nobody is a mutex report that cannot be acted on. Set on acquire, cleared in
                # the same finally as the release, so the two can never drift apart.
                _sub_state['holder'] = 'reconnect'
                try:
                    # cc#884 item 3: ONE deduped union, built by the shared helper the scheduled
                    # sequence also uses, so a reconnect landing inside the scheduled window can
                    # no longer produce a different (or doubled) symbol set.
                    sub_list = _subscribe_union(equity_fyers_syms, futures_fyers_syms,
                                                ext_fyers_syms, option_syms)
                    log.info(f"WS reconnected at {now_t.strftime('%H:%M:%S')} IST — re-subscribing "
                             f"{len(sub_list)} symbols ({len(option_syms)} options; post-sequence "
                             "reconnect, safe side of the pre-open trap)")
                    _log_feed_incident("feed_ws_connect", f"reconnect re-subscribe: {len(sub_list)} symbols")
                    _batched_subscribe(fyers_ws, sub_list, action='sub', label='reconnect')
                    _sub_state['last_ok'] = time.monotonic()
                    threading.Thread(target=_verify_subscribe_survivors, args=('reconnect',), daemon=True).start()
                finally:
                    _sub_state['holder'] = None
                    _subscribe_lock.release()
            else:
                log.info(f"WS reconnected at {now_t.strftime('%H:%M:%S')} IST — a subscribe sequence is "
                         "running; deferring the reconnect re-subscribe to it (cc#759 fix1 mutex)")
                _log_feed_incident("feed_ws_connect", "reconnect deferred to the running subscribe sequence")
        else:
            log.info(f"WS connected at {now_t.strftime('%H:%M:%S')} IST — NOT subscribing yet "
                     "(cc#497: the scheduled canary/full sequence owns today's initial subscribe)")
            _log_feed_incident("feed_ws_connect", f"connected pre-sequence at {now_t.strftime('%H:%M:%S')}")
        fyers_ws.keep_running()

    # ── cc#843 fix_2: THE -300 PROBE ─────────────────────────────────────────────────────────
    # The 03-Aug diagnosis turned on an ABSENCE: a sick connection acks everything and rejects
    # nothing, so "Subscribed" tells you precisely nothing. The only reliable signal is whether the
    # broker still REJECTS garbage. After a subscribe sequence completes we hand the socket one
    # known-invalid symbol and wait: a healthy connection answers with a -300 naming it within
    # seconds; a subscribe-dead connection stays silent. Silence therefore means dead, and we
    # reconnect immediately rather than waiting for the 09:24 bar-count backstop to notice.
    #
    # The probe symbol comes from feed_ext_blacklist — symbols the broker has ALREADY told us are
    # invalid — so the probe can never accidentally subscribe something real, and it costs one
    # request. The bar-count check stays as the backstop it always was.
    _probe_state = {'awaiting': None, 'seen': False}
    PROBE_WAIT_SEC = 12

    def _probe_symbol(c):
        try:
            with c.cursor() as cur:
                # cc#884 item 2: this said ORDER BY created_at. That column has never existed —
                # EXT_BLACKLIST_SCHEMA_SQL (line ~318) creates `added_at`. So the probe threw
                # "column created_at does not exist" on EVERY boot and the cc#843 -300 probe never
                # ran once, silently, since it shipped. Fixed by naming the real column rather than
                # adding a second one: no DDL, nothing to migrate, and the schema stays the source.
                cur.execute("SELECT symbol FROM feed_ext_blacklist ORDER BY added_at DESC LIMIT 1")
                r = cur.fetchone()
            if r and r[0]:
                return fyers_eq_symbol(r[0])
        except Exception as e:
            log.warning(f"cc#843 probe symbol lookup failed: {e}")
        return None

    def _probe_subscribe_alive(label):
        """Returns True if the connection provably still rejects garbage; False if it is
        subscribe-dead; None if the probe could not run (no blacklist member yet)."""
        pc = None
        try:
            pc = get_db()
            psym = _probe_symbol(pc)
        except Exception:
            psym = None
        finally:
            if pc is not None:
                try:
                    pc.close()
                except Exception:
                    pass
        if not psym:
            log.info(f"cc#843 probe ({label}): no blacklist member available — probe skipped, "
                     "bar-count backstop still applies")
            return None
        _probe_state['awaiting'] = psym
        _probe_state['seen'] = False
        log.info(f"cc#843 probe ({label}): subscribing known-invalid {psym}, expecting a -300 "
                 f"within {PROBE_WAIT_SEC}s")
        try:
            _batched_subscribe(fyers_ws, [psym], action='sub', label=f'probe-{label}')
        except Exception as e:
            log.warning(f"cc#843 probe ({label}) subscribe failed: {e}")
            _probe_state['awaiting'] = None
            return None
        deadline = time.monotonic() + PROBE_WAIT_SEC
        while time.monotonic() < deadline:
            if _probe_state['seen']:
                break
            time.sleep(0.5)
        alive = _probe_state['seen']
        _probe_state['awaiting'] = None
        if alive:
            log.info(f"cc#843 probe ({label}): -300 received — subscribe pipeline is ALIVE")
            _log_feed_incident("feed_probe_ok", f"{label}: -300 received for {psym}")
        else:
            # ── cc#886 HOTFIX, 07-Aug 12:45 IST — THE PROBE IS REPORT-ONLY UNTIL IT IS TRUSTED ──
            # This probe shipped with cc#843 but NEVER RAN: it read feed_ext_blacklist ORDER BY
            # created_at, a column that has never existed, so it threw on every boot for days.
            # cc#884 item 2 fixed the column name — and the very first live run, 12:39:28 today,
            # declared the connection subscribe-dead and forced a close. The feed then did not
            # come back and the tape was dark from 12:35.
            #
            # I do not yet know whether that verdict was RIGHT. Both readings are live:
            #   * correct — a mid-market boot connection really can come up poisoned; that is the
            #     whole cc#843 doctrine, and QPOWER-EQ was genuinely -300'd on 05-Aug, so the
            #     probe symbol is sound.
            #   * false   — the probe fired 20s after a 780-symbol subscribe finished and waits
            #     only 12s for the -300 frame. Under that load the frame may simply be late.
            # Either way, a code path with ZERO production runs behind it must not be the thing
            # that can close the live socket. It keeps its eyes and loses its hands: the verdict is
            # still logged every time, so the evidence to settle the question accumulates on real
            # connections, and the cc#876 deadline guard plus the guardian's own rungs remain the
            # actual recovery path — both of which HAVE run in production.
            # Re-arm only after feed_probe_ok / feed_probe_dead rows show it agrees with reality.
            log.error(f"cc#843 probe ({label}): NO -300 within {PROBE_WAIT_SEC}s for a known-invalid "
                      f"symbol — would have called this connection SUBSCRIBE-DEAD. cc#886: "
                      f"REPORT-ONLY, no forced reconnect (see the note at this line).")
            _log_feed_incident("feed_probe_dead_reportonly",
                               f"{label}: no -300 for {psym} in {PROBE_WAIT_SEC}s — verdict logged, "
                               f"NO action taken (cc#886 hotfix)")
        return alive

    # ── cc#843 fix_3a: ONE pending corrected-universe resubscribe ────────────────────────────
    # On 03-Aug three full-universe resubscribes interleaved on the same socket (1150/1149/1146
    # symbols, ~3,400 requests in 45 seconds) because each -300 frame triggered its own recovery.
    # That is a self-inflicted burst at exactly the moment the connection is already unhappy — and
    # cc#759 proved concurrent subscribe sequences are how the futures leg gets dropped. Collapse:
    # at most ONE resubscribe in flight; invalids that arrive while it runs are coalesced into a
    # single follow-up, which then uses the universe as it stands after every drop is applied.
    _resub_state = {'in_flight': False, 'pending': False}
    _resub_lock = threading.Lock()

    def _resubscribe_corrected(reason):
        with _resub_lock:
            if _resub_state['in_flight']:
                _resub_state['pending'] = True
                log.info(f"cc#843: corrected-universe resubscribe already in flight — coalescing "
                         f"({reason})")
                return
            _resub_state['in_flight'] = True

        def _worker():
            try:
                while True:
                    # cc#884 item 3: same single deduped builder as the reconnect and scheduled paths.
                    sub_list = _subscribe_union(equity_fyers_syms, futures_fyers_syms,
                                                ext_fyers_syms, option_syms)
                    log.info(f"WS -300 recovery: re-subscribing corrected universe "
                             f"({len(sub_list)} symbols) [{reason}]")
                    _batched_subscribe(fyers_ws, sub_list, action='sub',
                                       label='invalid_symbol_recovery')
                    with _resub_lock:
                        if not _resub_state['pending']:
                            _resub_state['in_flight'] = False
                            return
                        _resub_state['pending'] = False   # one more pass, with the latest universe
            except Exception as e:
                log.warning(f"cc#843 coalesced resubscribe failed: {e}")
                with _resub_lock:
                    _resub_state['in_flight'] = False
                    _resub_state['pending'] = False

        threading.Thread(target=_worker, daemon=True).start()

    def _blacklist_extended(fsym, reason):
        """cc#809: persist an extended-leg symbol drop to feed_ext_blacklist so it is excluded from
        every future boot (get_extended_universe subtracts this table). Own short-lived connection —
        on_error runs on the WS callback thread and must never touch the shared worker conn (the
        cc#489 lesson). Returns the nse_code either way; an in-memory drop already happened, so a
        failed persist only costs us one repeat -300 on the next boot."""
        nse_code = from_fyers_symbol(fsym)
        try:
            bconn = get_db()
            with bconn.cursor() as cur:
                cur.execute(EXT_BLACKLIST_SCHEMA_SQL)
                cur.execute("INSERT INTO feed_ext_blacklist (symbol,reason) VALUES (%s,%s) "
                            "ON CONFLICT (symbol) DO UPDATE SET reason=EXCLUDED.reason",
                            (nse_code, reason))
            bconn.commit()
            bconn.close()
        except Exception as e:
            log.warning(f"_blacklist_extended({fsym}): persist failed (in-memory drop still applied): {e}")
        return nse_code

    def _blacklist_symbol(fsym, reason):
        """cc#495 change_2/1_amended: persist the drop to futures_universe.is_active so
        it's excluded from EVERY future boot too (equity_fyers_syms/futures_fyers_syms
        both derive from get_universe()/get_index_futures_universe(), both WHERE
        is_active=TRUE — this is the same mechanism already used for SAMMAANCAP by
        Claude web after the 16-Jul incident). Uses its OWN short-lived connection —
        on_error runs on the WS client's callback thread, never the shared worker conn
        (same lesson as cc#489's auto_login fix: never touch a conn from another
        thread's context)."""
        nse_code = from_fyers_symbol(fsym)
        try:
            bconn = get_db()
            with bconn.cursor() as cur:
                cur.execute("ALTER TABLE futures_universe ADD COLUMN IF NOT EXISTS blacklist_reason TEXT")
                cur.execute("""UPDATE futures_universe SET is_active=false, blacklist_reason=%s
                               WHERE symbol=%s""", (reason, nse_code))
            bconn.commit()
            bconn.close()
        except Exception as e:
            log.warning(f"_blacklist_symbol({fsym}): persist failed (in-memory drop still applied): {e}")
        return nse_code

    def on_error(msg):
        log.error(f"WS error: {msg}")
        # cc#770 CAPTURE: persist EVERY error frame (not just -300) to ops_log so an entitlement/throttle
        # reject on the futures channel is visible in the record, not only on stdout.
        try: _log_feed_incident("feed_ws_error", str(msg)[:400])
        except Exception: pass
        # cc#489 step_6 + cc#495 change_1/1_amended: on a -300 invalid-symbol
        # rejection, Fyers appears to reject the WHOLE subscribe batch the bad
        # symbol was in, not just that one symbol (16-Jul: SAMMAANCAP26JULFUT alone
        # killed the entire futures leg). Drop the invalid symbol(s) from every
        # in-process tracking structure, persist the drop (blacklist, never
        # resubscribed again on any future boot), then immediately re-subscribe the
        # corrected universe so nothing else in that batch stays silently dropped.
        try:
            # cc#843 fix_2: the probe watches for its own symbol in any -300 frame. Recorded
            # BEFORE the early return so a probe-only frame still counts as proof of life.
            if isinstance(msg, dict) and msg.get('code') == -300:
                _pa = _probe_state.get('awaiting')
                if _pa and _pa in (msg.get('invalid_symbols') or []):
                    _probe_state['seen'] = True
            if not (isinstance(msg, dict) and msg.get('code') == -300):
                return
            invalid = msg.get('invalid_symbols') or []
            if not invalid:
                return
            dropped = []
            for fsym in invalid:
                if fsym in equity_set:
                    equity_set.discard(fsym)
                    if fsym in equity_fyers_syms: equity_fyers_syms.remove(fsym)
                elif fsym in futures_set:
                    futures_set.discard(fsym)
                    if fsym in futures_fyers_syms: futures_fyers_syms.remove(fsym)
                elif fsym in ext_set:
                    # cc#809: extended leg. Its drop persists to feed_ext_blacklist, NOT to
                    # futures_universe — an extended symbol has no row there, so _blacklist_symbol
                    # would update zero rows and the same dead symbol would come back every boot.
                    ext_set.discard(fsym)
                    if fsym in ext_fyers_syms: ext_fyers_syms.remove(fsym)
                    nse_code = _blacklist_extended(fsym, f"WS -300 invalid_symbols: {msg}"[:200])
                    dropped.append(nse_code)
                    log.warning(f"WS -300: dropped+blacklisted EXTENDED {fsym} (nse_code={nse_code})")
                    continue
                elif fsym in option_syms:
                    option_syms.remove(fsym)   # options: in-memory drop only, no persistent table
                else:
                    log.warning(f"invalid_symbol {fsym} not found in any active tracking set (already dropped?)")
                    continue
                nse_code = _blacklist_symbol(fsym, f"WS -300 invalid_symbols: {msg}"[:200])
                dropped.append(nse_code)
                log.warning(f"WS -300: dropped+blacklisted {fsym} (nse_code={nse_code})")
            if dropped:
                _log_feed_incident("feed_invalid_symbol_dropped",
                                   {"dropped": dropped, "raw": str(msg)[:300]})
                # cc#809: the corrected universe MUST carry the extended leg too — omitting it here
                # would silently unsubscribe ~1,600 symbols on the first invalid-symbol recovery.
                # cc#843 fix_3a: ONE coalesced resubscribe, never three interleaved.
                _resubscribe_corrected(f"invalid_symbols x{len(dropped)}")
        except Exception as e:
            log.warning(f"on_error invalid-symbol handling failed: {e}")
    def on_close(msg):
        log.warning(f"WS closed: {msg}")
        _log_feed_incident("feed_ws_close", str(msg)[:200])   # cc#495 change_4

    fyers_ws = data_ws.FyersDataSocket(
        access_token=access, log_path="",
        litemode=False, write_to_file=False, reconnect=True,
        on_connect=on_connect, on_close=on_close,
        on_error=on_error, on_message=on_message,
    )

    # ── feed heartbeat helpers (cc_task #84) ──────────────────────────────────
    def _recent_symbol_counts_by_source(minutes=HEARTBEAT_STALE_MINS):
        """Per-source distinct symbol counts (eq, fut) whose latest live 5-min bar
        bucket falls within the last `minutes`. Returns {'fyers_eq': -1, 'fyers_fut': -1}
        on DB error so a failed read never triggers a false watchdog action.

        cc#497 fix_2a: this is THE watchdog's own health read — it now uses its OWN fresh
        short-lived connection (open/query/close) every call, never the shared housekeeping
        conn. Root cause of the 17-Jul zombie: the shared conn died once, silently, and this
        (plus every other conn-based path) returned -1/-1 forever after — the watchdog's own
        "no false action on a bad read" safety rail became the thing that blinded it, because
        every read was bad for the same reason all day. A fresh conn per call means a failed
        read now genuinely means the DB/network is down, not a stale shared conn."""
        hc = None
        try:
            now_ist = datetime.now(IST).replace(tzinfo=None)
            cutoff = now_ist - timedelta(minutes=minutes)
            # ── cc#980 item 1: SESSION-AWARE COUNTING ──────────────────────────────────────────
            # cc#855 retags Category I cash bars as fyers_eq_auction (15:15-15:30) and auction
            # (15:30-15:35). This function only counted fyers_eq/fyers_fut/fyers_ext, so during
            # the auction the eq leg read ZERO while bars were landing perfectly well under the
            # auction tags. On 07-Aug a 15:28 boot ran the canary, saw that zero, called ZERO
            # TICKS, forced a close at 15:30:11 and killed the worker for three days.
            # The auction sources are counted TOWARD THE eq LEG, and only when the query window
            # actually overlaps 15:15-15:35 on a trading day — outside that window an auction-
            # tagged row is stale data and must not prop the eq count up. Reported MERGED, because
            # every caller is asking "is the equity leg alive", and during the auction these tags
            # ARE the equity leg. Nothing about tagging or retention changes here (do_not_touch).
            auction_ok = False
            try:
                auction_ok = (is_trading_day(now_ist.date())
                              and cutoff.time() <= EQ_AUCTION_END
                              and now_ist.time() >= EQ_CONTINUOUS_END)
            except Exception:
                auction_ok = False
            wanted = ['fyers_eq', 'fyers_fut', EXT_SOURCE]
            if auction_ok:
                wanted += [AUCTION_WINDOW_SOURCE, AUCTION_CLOSE_SOURCE]
            hc = get_db()
            with hc.cursor() as cur:
                cur.execute("""
                    SELECT source, COUNT(DISTINCT symbol) FROM intraday_prices
                    WHERE timeframe='5m' AND source = ANY(%s)
                      AND ts >= %s
                    GROUP BY source
                """, (wanted, cutoff))
                # cc#809: fyers_ext is REPORTED here but deliberately NOT part of the watchdog's
                # health test below — that test stays eq/fut only. The extended leg is additive
                # context; a healthy 1,500-symbol extended count must never be able to mask a dead
                # F&O leg, and an extended leg that never subscribes must never trip a restart.
                counts = {'fyers_eq': 0, 'fyers_fut': 0, EXT_SOURCE: 0}
                raw = {row[0]: row[1] for row in cur.fetchall()}
                counts.update({k: v for k, v in raw.items() if k in counts})
                if auction_ok:
                    # merged into the eq leg, and the parts kept alongside so a log line can still
                    # say WHY eq is non-zero during the auction rather than looking like magic.
                    a_win = raw.get(AUCTION_WINDOW_SOURCE, 0)
                    a_cls = raw.get(AUCTION_CLOSE_SOURCE, 0)
                    if a_win or a_cls:
                        counts['fyers_eq'] = counts.get('fyers_eq', 0) + a_win + a_cls
                        counts['_auction_merged'] = a_win + a_cls
                return counts
        except Exception as e:
            log.warning(f"_recent_symbol_counts_by_source: {e}")
            return {'fyers_eq': -1, 'fyers_fut': -1, EXT_SOURCE: -1}
        finally:
            if hc is not None:
                try:
                    hc.close()
                except Exception:
                    pass

    def _recent_symbol_count(minutes=HEARTBEAT_STALE_MINS):
        """Combined total (eq+fut), used only for the post-subscribe verification
        log line below — the watchdog itself uses per-source counts."""
        counts = _recent_symbol_counts_by_source(minutes)
        eq, fut = counts.get('fyers_eq', -1), counts.get('fyers_fut', -1)
        return -1 if (eq < 0 or fut < 0) else eq + fut

    def _heal_gap_bg():
        """change_2: REST-backfill each symbol from its newest stored bar -> now, on a
        FRESH connection (never share the worker conn across threads)."""
        try:
            hc = get_db()
            try:
                fyers_backfill.heal_gap(token, hc, symbols)
            finally:
                hc.close()
        except Exception as e:
            log.error(f"heartbeat heal_gap failed: {e}")

    def _forced_close(reason, timeout=20):
        """cc#759 fix3: fyers_ws.close_connection() HUNG the worker on 29-Jul (watchdog rung-1 fired the
        forced close, then the process sat alive-but-silent inside it — the remedy killed the patient).
        Run the close on a daemon thread with a HARD timeout; if it doesn't return, os._exit(1) so Railway
        restarts cleanly. Guarantee: the worker is either ticking or dead-and-restarting, never silent."""
        log.error(f"FEED: forced close_connection ({reason})")
        done = threading.Event()
        def _c():
            try:
                fyers_ws.close_connection()
            except Exception as e:
                log.warning(f"forced close ({reason}): {e}")
            finally:
                done.set()
        threading.Thread(target=_c, daemon=True).start()
        if not done.wait(timeout):
            log.critical(f"FEED: close_connection hung >{timeout}s ({reason}) — os._exit(1) for a clean "
                         "Railway restart (cc#759 fix3)")
            try: _log_feed_incident("feed_close_hang_exit", f"{reason}: close hung >{timeout}s — exiting")
            except Exception: pass
            os._exit(1)

    # cc#876 item 2 — THE WATCHDOG OF THE WATCHDOG.
    # cc#759 fix3 put a deadline on close_connection() itself, and that guard worked on 05-Aug:
    # the close returned and its ops_log row was written. What had NO deadline was everything
    # AFTER it — the SDK's own reconnect, on_connect, and the re-subscribe. The recovery sequence
    # started and simply never finished, and because a hang raises nothing, no `except` anywhere
    # could see it. Rung 2 could not save us either: rung 1 fired at 15:28 and the next health
    # check is HEALTH_LOG_MINS(5) later, past the close — the ladder had no clock left to climb.
    #
    # So the whole sequence now carries its own completion deadline, on its own thread, and a
    # sequence that has not confirmed a reconnect by then becomes a CRASH. That is the trade this
    # incident bought: a crash gets a free Railway restart, a hang gets 22 hours of silence.
    def _force_reconnect():
        """change_1: drop the socket so the SDK (reconnect=True) re-establishes and on_connect
        re-subscribes the full universe. cc#759 fix3: the close is timeout-guarded (os._exit on
        hang). cc#876: the RECONNECT that follows is now deadline-guarded too."""
        _RECONNECT_CONFIRMED.clear()
        # ── cc#980 item 3: THE RECONNECT NOBODY WAS MAKING ────────────────────────────────────
        # The cc#876 deadline thread waited RECONNECT_DEADLINE_S for an on_connect that could
        # never arrive: an explicit close_connection() STOPS the SDK's reconnect=True loop, so
        # after a forced close nothing was dialling out. The deadline was therefore not a guard,
        # it was a countdown to os._exit(1) — which is precisely how 07-Aug ended.
        # Now the deadline thread makes the call itself, after the close has returned, so the
        # guard has a real reconnect to confirm. A connect() that RAISES still exits(1): that is
        # the cc#876 contract (ticking or dead-and-restarting, never silent) and it stays.
        _closed = threading.Event()

        def _deadline():
            # never dial out while the close is still in flight — that is the one ordering that
            # could leave two sockets racing. _forced_close self-exits if it hangs, so a timeout
            # here means the process is already on its way down.
            _closed.wait(30)
            try:
                fyers_ws.connect()
                log.info("FEED: reconnect dialled after forced close (cc#980)")
            except Exception as e:
                log.critical(f"FEED: reconnect connect() raised after forced close: {e} "
                             "— os._exit(1) for a clean Railway restart (cc#876 contract).")
                try:
                    _log_feed_incident("feed_reconnect_connect_failed", str(e)[:200])
                except Exception:
                    pass
                os._exit(1)
            if _RECONNECT_CONFIRMED.wait(RECONNECT_DEADLINE_S):
                log.info(f"FEED: reconnect confirmed within {RECONNECT_DEADLINE_S}s (cc#876)")
                return
            log.critical(f"FEED: forced reconnect did NOT confirm within {RECONNECT_DEADLINE_S}s "
                         "— os._exit(1) for a clean Railway restart. A hung recovery must become a "
                         "crash (cc#876 incident 9).")
            try:
                _log_feed_incident("feed_reconnect_deadline_exit",
                                   f"no on_connect within {RECONNECT_DEADLINE_S}s of forced close")
            except Exception:
                pass
            os._exit(1)

        threading.Thread(target=_deadline, daemon=True).start()
        try:
            _forced_close("watchdog/force reconnect")
        finally:
            _closed.set()

    def _log_feed_incident(kind, detail):
        """cc_task #112: record each watchdog action to ops_log (category=alert)
        so every recurrence is visible after the fact. cc#156: telemetry categories
        moved off session_log to ops_log.

        cc#497 fix_2a: tries the shared housekeeping conn first (cheap, avoids opening a new
        conn on every alert), but falls back to a FRESH short-lived connection if that write
        fails — so an incident is never silently lost just because the shared conn happens to
        be dead (the exact mechanism that hid the 17-Jul zombie: the watchdog's own alert path
        shared the same dead conn as the read it was trying to alert about)."""
        payload = json.dumps({"detail": detail, "ist": datetime.now(IST).isoformat()})
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), 'alert', %s, %s::jsonb)", (kind, payload))
            conn.commit()
            return
        except Exception as e:
            log.warning(f"_log_feed_incident (shared conn): {e} — retrying on a fresh connection")
        hc = None
        try:
            hc = get_db()
            with hc.cursor() as cur:
                cur.execute(
                    "INSERT INTO ops_log (session_date, session_ts, category, title, details) "
                    "VALUES (CURRENT_DATE, NOW(), 'alert', %s, %s::jsonb)", (kind, payload))
            hc.commit()
        except Exception as e2:
            log.warning(f"_log_feed_incident (fresh conn fallback also failed): {e2}")
        finally:
            if hc is not None:
                try:
                    hc.close()
                except Exception:
                    pass

    def _ext_recover(where, eq, fut, ext, uni_ext):
        """cc#1017: recover a dead / near-dead EXTENDED equity leg while the legacy legs are alive — the
        14-Aug partial subscribe-dead signature (batch re-subscribe ACKNOWLEDGED but only honoured for the
        legacy legs; the extended 909 never resumed ticking). A partial subscribe-dead socket does not
        recover in place, so REBUILD the whole connection (on_connect re-subscribes the full universe
        fresh) up to EXT_RECOVERY_MAX_REBUILDS times. If it still will not honour the extended subscribe,
        RETREAT the stage and alarm CRITICAL rather than keep running 43% dark (FYERS_INTEGRATION_LEARNINGS:
        loud failure over quiet degradation). Shared by the post-reconnect probe AND the periodic watchdog
        so the rebuild budget is one counter, never a runaway reconnect loop."""
        _EXT_RECOVERY["attempts"] += 1
        n = _EXT_RECOVERY["attempts"]
        frac = ext / float(uni_ext) if uni_ext else 0.0
        detail = f"{where}: eq={eq} fut={fut} ext={ext}/{uni_ext} ({frac*100:.0f}%)"
        if n <= EXT_RECOVERY_MAX_REBUILDS:
            log.error(f"cc#1017 EXTENDED LEG DEAD — {detail}; legacy legs alive. Rebuild "
                      f"{n}/{EXT_RECOVERY_MAX_REBUILDS} (tearing down the connection).")
            _log_feed_incident("feed_ext_dead_rebuild", f"{detail} rebuild {n}/{EXT_RECOVERY_MAX_REBUILDS}")
            _force_reconnect()
        else:
            _cur = _ext_stage_limit(conn)
            _lg = _ext_last_good(conn)
            _retreat = str(_lg) if (_lg > 0 and _cur > _lg) else 'off'
            log.critical(f"cc#1017 EXTENDED LEG UNRECOVERABLE after {EXT_RECOVERY_MAX_REBUILDS} rebuilds — "
                         f"{detail}; retreating {EXT_STAGE_FLAG}={_retreat} and alarming CRITICAL.")
            try:
                _flag_set(conn, EXT_STAGE_FLAG, _retreat)
            except Exception as _fe:
                log.warning(f"cc#1017 retreat flag set failed: {_fe}")
            _log_feed_incident("feed_ext_unrecoverable",
                               f"{detail} — {EXT_RECOVERY_MAX_REBUILDS} rebuilds failed; retreated to {_retreat}")
            _EXT_RECOVERY["attempts"] = 0

    def _verify_subscribe_survivors(label):
        """cc#151: after ANY batched (re)subscribe — on_connect reconnect or the monthly-roll
        path — confirm futures are actually ticking and log it to ops_log, so every re-subscribe
        is auditable instead of just assumed. Acceptance: >=205/212 futures ticking within
        15min; this samples the last 15min window.

        cc#188: only raise the ops_log alert during market hours (09:15-15:30 IST) on a trading
        day — same gate pattern as the ADR fix. A (re)subscribe off-hours (e.g. an evening
        reconnect) naturally shows ~0 ticking because the feed is idle; that is NOT an incident,
        so it must not fire a 0/212 alert. Off-hours we log at info level only.

        cc#495 change_3 (the >200-combined-floor forced-reconnect this function used to also do
        on top of its own logging) is REMOVED by cc#497's course-correct (CLAUDE_WEB_REVIEW
        17-Jul): the two-stage canary/full subscribe sequence (_run_subscribe_sequence) already
        verifies+retries the FRESH subscribe itself, and the periodic per-source watchdog
        (rung1 reconnect -> rung2 exit(1)) is the one ongoing-health enforcement path — a THIRD,
        overlapping floor-check-and-reconnect here was exactly the redundant complexity the
        founder killed. Observation/logging only, same as before cc#495."""
        time.sleep(120)
        try:
            counts = _recent_symbol_counts_by_source(15)
            eq, fut = counts.get('fyers_eq', -1), counts.get('fyers_fut', -1)
            ext = counts.get(EXT_SOURCE, -1)   # cc#1017: the extended equity leg is now MEASURED, not blind
            recent = -1 if (eq < 0 or fut < 0) else eq + fut
            uni_eq, uni_fut = len(equity_fyers_syms), len(futures_fyers_syms)
            uni_ext = len(ext_fyers_syms)      # cc#1017: registry-derived expected ext count (stage-limited)
            now = datetime.now(IST)
            in_market = is_trading_day(now.date()) and MARKET_OPEN <= now.time() <= SESSION_END   # cc#855
            # cc#1017: the ext leg is now IN this line. The 14-Aug blindness was that it never was — so
            # 'eq 212/206, fut 208/208' read healthy while 909 extended symbols were dead off the same socket.
            msg = f"{label}: eq {eq}/{uni_eq}, fut {fut}/{uni_fut}, ext {ext}/{uni_ext} writing bars"
            # cc#759 fix2: a ticking count that EXCEEDS its universe leg is a double-subscription / counting
            # bug, NEVER health — it must ALERT and be treated as a failure (the 224/212 tell, LEARNING 2).
            if (uni_fut > 0 and fut > uni_fut) or (uni_eq > 0 and eq > uni_eq):
                _log_feed_incident("feed_ticking_exceeds_universe",
                                   f"{label}: ticking EXCEEDS universe ({msg}) — double-subscription, FAILURE")
            elif in_market and uni_fut > 0 and fut == 0 and eq > 0:
                # cc#770: the FUTURES leg is silent while EQUITY flows on the SAME socket. Dump the exact
                # futures subscribe payload (so a malformed/expired AUG contract is visible) + raise a
                # HIGH-URGENCY escalation. Cross-reference the captured non-tick/error frames: a reject
                # frame names the cause; if a single-symbol subscribe is ALSO silent it is a Fyers
                # entitlement issue (founder -> Fyers support), else a batch-composition/invalid-symbol one.
                _log_feed_incident("feed_futures_silent_eq_alive",
                                   {"msg": msg, "fut_universe": uni_fut,
                                    "fut_payload_sample": futures_fyers_syms[:20],
                                    "hint": "eq flows on same WS; inspect captured feed_ws_error / non-tick "
                                            "frames; single-symbol subscribe also silent => Fyers entitlement"})
                log.error(f"cc#770 FUTURES SILENT ({label}): {msg} — full fut payload ({uni_fut}): "
                          f"{futures_fyers_syms}")
            elif in_market:
                _log_feed_incident("subscribe_verify", msg)
            elif recent == 0:
                # cc#759 fix6: off-hours verification FAILURE (0 bars after a subscribe) still alerts at low
                # urgency — the 28-Jul 0/212 post-roll result predicted the 29-Jul outage 15h ahead but was
                # silently suppressed (LEARNING 5). Never silent.
                _log_feed_incident("subscribe_verify_offhours_fail", f"OFF-HOURS 0 bars after subscribe: {msg}")
            else:
                log.info(f"Post-{label} verification (off-hours): {msg}")
            # ── cc#1017 EXTENDED-LEG TICK-RESUMPTION PROBE ─────────────────────────────────────────
            # The 14-Aug root cause: a batch re-subscribe ACK is not evidence — the extended leg never
            # resumed ticking while the legacy legs did. This is the probe: a 120s-settled read of the
            # fyers_ext source SPECIFICALLY. It acts only when the ext stage is ON (uni_ext>0), inside the
            # equity session, and the LEGACY legs are healthy (a partial ext death, not a whole-feed outage
            # the core watchdog already owns). ext_ok mirrors _subscribe_extended's own drop threshold.
            _ext_win = is_trading_day(now.date()) and MARKET_OPEN <= now.time() <= EQ_CONTINUOUS_END
            _legacy_ok = (uni_eq == 0 or eq >= WATCHDOG_MIN_SYMBOLS) and (uni_fut == 0 or fut >= WATCHDOG_MIN_SYMBOLS)
            # cc#1017: gate on the stage being ON (flag>0). After a retreat sets feed_ext_stage=off the
            # startup ext list length stays >0, so without this the recovery would loop against a leg we
            # deliberately disabled.
            if uni_ext > 0 and ext >= 0 and _ext_win and _ext_stage_limit(conn) > 0:
                if ext < EXT_MIN_TICK_FRACTION * uni_ext and _legacy_ok:
                    _ext_recover(f"post-{label} probe", eq, fut, ext, uni_ext)
                elif ext >= EXT_MIN_TICK_FRACTION * uni_ext and _EXT_RECOVERY["attempts"]:
                    log.info(f"cc#1017 extended leg recovered ({ext}/{uni_ext} ticking) — reset rebuild counter")
                    _EXT_RECOVERY["attempts"] = 0
                # cc#1017 (completing spec item 1): the rebuild trigger above is a COLLAPSE threshold —
                # 25%, mirroring _subscribe_extended's own drop rule. On its own it would still have
                # let a 45%-dead leg (500/909) report a clean pass, which is the same shape of
                # blindness as the retired floor=100, just further along the scale. So ANY shortfall
                # against the registry-derived expected count is now reported against that count,
                # loudly, whether or not it is big enough to rebuild.
                #
                # It is reported, not rebuilt, and that distinction is deliberate: some extended
                # symbols genuinely do not print in a 15-min window (the 14-Aug backfill found
                # QUADFUTURE-EQ with no Fyers candles at all and SAYAJIHOTL trading in bursts), so
                # tearing the socket down for a handful of quiet illiquid names would be a self-
                # inflicted outage. What must never happen again is a verification that says PASS
                # while a large slice of the universe is dead — that is what this line ends.
                elif ext < uni_ext:
                    _short = uni_ext - ext
                    _pct = 100.0 * _short / uni_ext
                    _detail = (f"{label}: extended leg SHORT by {_short} of {uni_ext} "
                               f"({_pct:.1f}%) — {ext} writing bars. Below the "
                               f"{EXT_MIN_TICK_FRACTION:.0%} collapse threshold this rebuilds; "
                               f"above it, quiet illiquid symbols are expected, so this is "
                               f"reported and NOT treated as a reason to tear down the socket.")
                    log.error(f"cc#1017 EXT SHORTFALL {_detail}")
                    _log_feed_incident("subscribe_verify_ext_shortfall", _detail)
            log.info(f"Post-{label} verification: {msg}")
        except Exception as e:
            log.warning(f"post-{label} verify failed: {e}")

    def _run_subscribe_sequence(trigger):
        """cc#759 fix1: process-level MUTEX + idempotency guard around the whole subscribe sequence — the
        one gate that prevents two triggers (guardian + scheduled-0920) from subscribing the same WS
        concurrently (the 29-Jul double-subscription that collapsed the futures leg). A second caller
        NO-OPs (logged, naming the holder). A non-recovery re-trigger within SUBSCRIBE_IDEMPOTENCY_SEC of
        a success is also a no-op. guardian-* / *-retry / midmarket-boot are recovery triggers (always run)."""
        is_recovery = (trigger.startswith('guardian') or trigger.endswith('-retry')
                       or trigger == 'midmarket-boot')
        if not _subscribe_lock.acquire(blocking=False):
            holder = _sub_state.get('holder')
            log.error(f"subscribe sequence ({trigger}): another sequence (holder={holder}) holds the "
                      f"subscribe lock — NO-OP (cc#759 fix1 mutex)")
            try: _log_feed_incident("feed_subscribe_mutex_blocked", f"{trigger}: blocked, holder={holder}")
            except Exception: pass
            return
        _sub_state['holder'] = trigger
        try:
            now_mono = time.monotonic()
            last_ok = _sub_state.get('last_ok', 0.0)
            if (not is_recovery) and last_ok and (now_mono - last_ok) < SUBSCRIBE_IDEMPOTENCY_SEC:
                log.info(f"subscribe sequence ({trigger}): a sequence succeeded {int(now_mono - last_ok)}s "
                         f"ago (<{SUBSCRIBE_IDEMPOTENCY_SEC}s) — NO-OP (cc#759 fix1 idempotency)")
                try: _log_feed_incident("feed_subscribe_idempotent_skip", f"{trigger}: recent success")
                except Exception: pass
                return
            _run_subscribe_sequence_inner(trigger)
        finally:
            _sub_state['holder'] = None
            _subscribe_lock.release()

    def _subscribe_extended(trigger):
        """cc#809 stage 3 + AUTO-HALT. Subscribes the extended equity leg after the core universe is
        up, waits for it to settle, then grades it on two independent questions:

          1. Did the CORE degrade? The F&O equity leg is what live signals, paper trading and the
             option gate run on. If its symbol count fell below the watchdog floor after we piled on
             the extended leg, the expansion is the prime suspect — so we unsubscribe the extended
             leg immediately and flip the app_config dial to 'off' so the NEXT boot comes up clean
             without anyone having to intervene at 09:20.
          2. Did the extended leg itself actually work? Below EXT_MIN_TICK_FRACTION of it writing
             bars means the subscribe was silently dropped (the failure mode cc#151 documents), so
             the same halt applies rather than leaving a half-subscribed universe that quietly
             under-reports sector aggregates.

        Grading is REPORT-ONLY in every other case. This thread can disable the extended leg; it can
        never reconnect, restart or otherwise touch the core feed."""
        try:
            log.info(f"cc#809 stage 3 ({trigger}): subscribing {len(ext_fyers_syms)} extended symbols")
            _log_feed_incident("feed_ext_subscribe", f"{trigger}: {len(ext_fyers_syms)} extended symbols")
            _batched_subscribe(fyers_ws, ext_fyers_syms, action='sub', label=f'ext-{trigger}')
            time.sleep(EXT_VERIFY_WAIT_SEC)
            counts  = _recent_symbol_counts_by_source(15)
            eq, fut = counts.get('fyers_eq', -1), counts.get('fyers_fut', -1)
            ext     = counts.get(EXT_SOURCE, -1)
            if eq < 0 or fut < 0 or ext < 0:
                log.warning("cc#809 stage 3: DB read failed — no grading, no action (extended leg left up)")
                return
            # ── cc#884 item 1: MARKET-HOURS GUARD ON THE HALT ──────────────────────────────
            # 06-Aug: the ext leg was working — 495 of 500 ticking at 13:57 (ops_log 16773). The
            # cc#876 worker deploys rebooted at 15:36 and 15:40, verification ran at 15:42, read
            # eq 0 and ext 0 because the equity session had ENDED at 15:15, graded that as
            # core-below-floor, unsubscribed the ext leg and set feed_ext_stage=off (16898/16899).
            # Nothing was broken. The clock was.
            #
            # A count of zero after the close is not a failure, it is the market being shut. So a
            # leg is only GRADED inside its own session (cc#855 constants): the equity legs
            # 09:15-15:15, futures 09:15-15:40, trading days only. Outside that the verification
            # reports and returns, and feed_ext_stage is left EXACTLY as found — a post-close boot
            # must never be able to disable the leg.
            _n = datetime.now(IST)
            _trading = _n.weekday() < 5 and is_trading_day(_n.date())
            _eq_gradeable  = _trading and MARKET_OPEN <= _n.time() <= EQ_CONTINUOUS_END
            _fut_gradeable = _trading and MARKET_OPEN <= _n.time() <= FUT_CLOSE
            if not (_eq_gradeable and _fut_gradeable):
                log.info(
                    f"cc#809 stage 3 ({trigger}): OFF-HOURS at {_n.strftime('%H:%M:%S')} IST "
                    f"(trading_day={_trading}, eq_gradeable={_eq_gradeable}, "
                    f"fut_gradeable={_fut_gradeable}) — ext={ext} eq={eq} fut={fut} reported, "
                    f"NOT graded. {EXT_STAGE_FLAG} left untouched (cc#884 item 1).")
                _ops_log(conn, 'feed', 'feed_ext_stage_offhours',
                         {"trigger": trigger, "eq": eq, "fut": fut, "ext": ext,
                          "ext_subscribed": len(ext_fyers_syms), "trading_day": _trading,
                          "eq_gradeable": _eq_gradeable, "fut_gradeable": _fut_gradeable,
                          "action": "none — flag untouched", "ist": _ist_now_str()})
                return
            frac = ext / float(len(ext_fyers_syms)) if ext_fyers_syms else 0.0
            core_ok = eq >= WATCHDOG_MIN_SYMBOLS and fut >= WATCHDOG_MIN_SYMBOLS
            ext_ok  = frac >= EXT_MIN_TICK_FRACTION
            report = {"trigger": trigger, "ext_subscribed": len(ext_fyers_syms), "ext_ticking": ext,
                      "ext_fraction": round(frac, 3), "eq": eq, "fut": fut,
                      "floor": WATCHDOG_MIN_SYMBOLS, "core_ok": core_ok, "ext_ok": ext_ok,
                      "retention_days": EXT_RETENTION_DAYS, "ist": _ist_now_str()}
            log.info(f"cc#809 stage 3 report: ext={ext}/{len(ext_fyers_syms)} ({frac*100:.0f}%) "
                     f"eq={eq} fut={fut} core_ok={core_ok} ext_ok={ext_ok}")
            if core_ok and ext_ok:
                # cc#1002: this stage held a clean in-session grade — record it as the last-good floor
                # so a later ramp that destabilises has a size to fall back to (not all the way off).
                _set_ext_last_good(conn, _ext_stage_limit(conn))
                _ops_log(conn, 'feed', 'feed_ext_stage_ok', report)
                return
            report["halted"] = True
            report["reason"] = ("core F&O leg below floor after extended subscribe"
                                if not core_ok else
                                f"extended leg only {frac*100:.0f}% ticking (<{EXT_MIN_TICK_FRACTION*100:.0f}%)")
            # cc#1002: RETREAT to the LAST-GOOD stage, not all the way to 'off' — but only when the
            # current stage is ABOVE that floor (a genuine ramp that failed). At or below the proven
            # baseline, keep the cc#809 'off' behaviour so a persistently-failing baseline cannot loop
            # by re-setting itself. This is the engineered retreat path: as much the deliverable as the
            # ramp (the incident-8 lesson — a big subscribe that force-closes must fall back cleanly).
            _cur_stage = _ext_stage_limit(conn)
            _lg        = _ext_last_good(conn)
            _retreat_to = str(_lg) if (_lg > 0 and _cur_stage > _lg) else 'off'
            report["retreat_to"] = _retreat_to
            report["from_stage"] = (_cur_stage if _cur_stage < 10 ** 9 else 'all')
            log.error(f"cc#1002 AUTO-RETREAT: {report['reason']} — unsubscribing extended leg and "
                      f"setting {EXT_STAGE_FLAG}={_retreat_to} (last-good {_lg})")
            try:
                _batched_subscribe(fyers_ws, ext_fyers_syms, action='unsub', label=f'ext-halt-{trigger}')
            except Exception as e:
                log.warning(f"cc#1002 auto-retreat unsubscribe failed (flag still being set): {e}")
            ext_set.clear()   # stop tagging any straggler ticks as extended
            _flag_set(conn, EXT_STAGE_FLAG, _retreat_to)
            _ops_log(conn, 'alert', 'feed_stage_retreat', report)
            _log_feed_incident("feed_stage_retreat", report["reason"])
        except Exception as e:
            log.warning(f"cc#809 _subscribe_extended({trigger}): {e}")

    def _run_subscribe_sequence_inner(trigger):
        """cc#497 fix_1_TIMING_FINAL_FOUNDER_17JUL: the two-stage subscribe that replaces the old
        on_connect-driven immediate subscribe. Runs on its own daemon thread (fired by
        housekeeping's wall-clock/boot-time triggers below, NEVER by on_connect):

          stage 1 — subscribe a canary batch (~WS_SUB_BATCH top-liquidity equity symbols by
            mcap-rank), wait for the ~60-90s verification window, confirm at least some of them
            are actually ticking.
          stage 2 — canary ticked: subscribe the rest of the universe (remaining equity + the
            full futures leg). Options are NOT touched here — they keep their own separate
            cc#189/cc#241 live-price gates in housekeeping(), unchanged.
            canary did NOT tick: do not pile the full universe onto a dead session — force ONE
            fresh reconnect and retry the whole sequence once. If the retry also shows zero
            ticks, hand off to the periodic watchdog ladder (rung1 reconnect -> rung2 exit(1))
            rather than retrying forever here.

        `trigger` is a label for logging/ops_log only, e.g. 'scheduled-0920', 'midmarket-boot'."""
        canary_codes = _canary_symbols(conn, symbols, WS_SUB_BATCH)
        canary_syms  = [fyers_eq_symbol(s) for s in canary_codes]
        canary_set   = set(canary_syms)

        def _attempt(label):
            remaining = [s for s in equity_fyers_syms if s not in canary_set] + list(futures_fyers_syms)
            log.info(f"subscribe sequence ({label}): canary batch ({len(canary_syms)} top-liquidity equity)")
            _log_feed_incident("feed_subscribe_canary", f"{label}: {len(canary_syms)} symbols")
            _batched_subscribe(fyers_ws, canary_syms, action='sub', label=f'canary-{label}')
            time.sleep(75)   # ~60-90s verification window
            # cc#497 live-tested bugfix (17-Jul, same-day midmarket-boot run): a 2-min lookback
            # is too tight against 5-min BUCKETED bars — a bucket is keyed by its START time, so
            # a bar for the CURRENT bucket (e.g. ts=14:00:00, actively upserted as ticks land)
            # ages out of a 2-min window before the bucket period even ends, producing a false
            # "zero ticks" reading ~75s after a genuinely healthy subscribe. Widened to match
            # _verify_subscribe_survivors' existing 15-min window margin (also used elsewhere in
            # this file for exactly this reason) — comfortably covers one full bucket + slack.
            recent = _recent_symbol_count(8)
            ticking = recent > 0
            log.info(f"subscribe sequence ({label}): canary check — {recent} symbols writing bars "
                     f"({'OK' if ticking else 'ZERO TICKS'})")
            if not ticking:
                return False
            log.info(f"subscribe sequence ({label}): canary ticking — subscribing remaining "
                     f"{len(remaining)} symbols")
            _log_feed_incident("feed_subscribe_full", f"{label}: {len(remaining)} remaining symbols")
            _batched_subscribe(fyers_ws, remaining, action='sub', label=f'full-{label}')
            threading.Thread(target=_verify_subscribe_survivors, args=(label,), daemon=True).start()
            # cc#809 STAGE 3 — the extended leg goes on LAST, on its own thread, only after the core
            # F&O universe is subscribed and verified. Ordering is the safety property: if the WS
            # cannot carry ~1,800 symbols, the thing that degrades is the leg we added, never the
            # F&O feed that everything else depends on.
            if ext_fyers_syms:
                threading.Thread(target=_subscribe_extended, args=(label,), daemon=True).start()
            # cc#843 fix_2: probe AFTER the sequence completes. It is the primary health check now —
            # the canary bar-count above proves EQUITY is streaming, which the sick 03-Aug connection
            # also did while every derivative subscribe was silently discarded. Only the probe
            # distinguishes "acking and working" from "acking and dead".
            threading.Thread(target=_probe_subscribe_alive, args=(label,), daemon=True).start()
            return True

        try:
            _sub_state['day'] = datetime.now(IST).date()
            if _attempt(trigger):
                _sub_state['done'] = True
                _sub_state['last_ok'] = time.monotonic()   # cc#759 fix1: idempotency anchor
                return
            # ── cc#980 item 2: LATE-SESSION SEQUENCE GUARD ────────────────────────────────
            # A forced reconnect minutes before the close has negative expected value. Best case
            # it recovers ~5 minutes of bars; worst case it is exactly what happened on 07-Aug —
            # close_connection stopped the SDK's reconnect loop, the deadline guard fired
            # os._exit(1) at ~15:32, Railway's restart budget was already spent, and the worker
            # stayed dead for three days over the weekend. So from 15:10 the verdict is
            # REPORT-ONLY: log it, record it, and let the session end on its own.
            try:
                _late = (is_trading_day(datetime.now(IST).date())
                         and datetime.now(IST).time() >= dt_time(15, 10))
            except Exception:
                _late = False
            if _late:
                log.error(f"subscribe sequence ({trigger}): canary showed ZERO ticks after 15:10 — "
                          "REPORT ONLY, no reconnect (cc#980). Session ends shortly; a reconnect "
                          "here can only cost the worker, not save the day.")
                _log_feed_incident("feed_canary_late_session",
                                   f"{trigger}: zero ticks after 15:10 — report-only, no reconnect")
                return
            log.error(f"subscribe sequence ({trigger}): canary showed ZERO ticks — forcing "
                      "reconnect and retrying once")
            _log_feed_incident("feed_subscribe_canary_dead",
                               f"{trigger}: zero ticks, forcing reconnect + retry")
            _force_reconnect()
            time.sleep(15)   # let the SDK's reconnect=True actually re-establish first
            if _attempt(f"{trigger}-retry"):
                _sub_state['done'] = True
                _sub_state['last_ok'] = time.monotonic()   # cc#759 fix1: idempotency anchor
            else:
                log.error(f"subscribe sequence ({trigger}): retry ALSO showed zero ticks — "
                          "handing off to the periodic watchdog ladder")
                _log_feed_incident("feed_subscribe_retry_failed", f"{trigger}: retry also zero ticks")
        except Exception as e:
            log.error(f"subscribe sequence ({trigger}) failed: {e}")
            try:
                _log_feed_incident("feed_subscribe_sequence_error", f"{trigger}: {str(e)[:200]}")
            except Exception:
                pass

    def _hard_restart(reason):
        """cc_task #112 — the missing auto-restart. Socket-reconnect failed to revive
        the feed (rejected as a fix on its own), so RE-EXEC the whole process: a clean
        boot re-auths (same-day token reused), rebuilds the WS and re-subscribes all 212
        symbols from scratch. Railway also relaunches the worker if execv ever fails."""
        log.error(f"FEED WATCHDOG: HARD RESTART — {reason}")
        _log_feed_incident("feed_hard_restart", reason)
        try:
            agg.flush_all(); opt_store.flush_all()   # persist whatever bars we hold
        except Exception:
            pass
        _forced_close("hard restart pre-execv", timeout=15)   # cc#759 fix3: timeout-guarded (os._exit on hang)
        try:
            os.execv(sys.executable, [sys.executable] + sys.argv)
        except Exception as e:
            log.error(f"os.execv failed ({e}) — exiting for Railway to relaunch")
            os._exit(1)

    def _self_heal_token(reason):
        """cc#473 items 2-4: dead/stale token recovery WITHOUT manual intervention.
        Inline breaker-safe relogin (fyers_autologin.try_relogin never raises SystemExit,
        so a cooldown-skip can't crash-loop the loop — item 3). On success, mint is stored
        and we _hard_restart: the clean reboot's get_valid_token finds the just-minted
        SAME-DAY token, verifies it live and REUSES it (no second TOTP, no re-auth
        coin-flip) then rebuilds+re-subscribes the WS — bars resume in seconds. On a
        breaker-skip or failure we log + back off 90s and let the detector re-trip
        (never a hard-exit crash-loop). Every event -> ops_log(category=feed_auth)."""
        import fyers_autologin
        _ops_log(conn, 'alert', 'feed_auth',
                 {'event': 'dead_token_detected', 'reason': reason,
                  'consecutive_threshold': DEAD_TOKEN_THRESHOLD, 'ist': _ist_now_str()})
        log.critical(f"cc#473 TOKEN SELF-HEAL triggered: {reason} — attempting inline relogin")
        res = fyers_autologin.try_relogin(conn)
        if res.get('ok'):
            _ops_log(conn, 'info', 'feed_auth',
                     {'event': 'relogin_ok', 'reason': reason, 'recovery': 'reboot_reuses_fresh_token',
                      'ist': _ist_now_str()})
            # cc#564: DATA-observable re-mint marker (the reboot's boot self-test then confirms live).
            _ops_log(conn, 'info', 'token_reminted_live',
                     {'stage': 'self_heal', 'reason': reason, 'verify': 'on_reboot_selftest',
                      'ist': _ist_now_str()})
            log.info("cc#473 relogin OK — restarting to rebuild WS on the fresh token (reused, no re-auth)")
            _hard_restart(f"token self-heal — fresh token minted ({reason})")
            return  # not reached (execv)
        if res.get('skipped'):
            _ops_log(conn, 'info', 'feed_auth',
                     {'event': 'relogin_skipped_cooldown', 'reason': reason, 'ist': _ist_now_str()})
            log.warning("cc#473 relogin SKIPPED by 90s account-block breaker — backoff 90s, detector will re-trip")
        else:
            _ops_log(conn, 'alert', 'feed_auth',
                     {'event': 'relogin_failed', 'reason': reason,
                      'error': str(res.get('error'))[:180], 'ist': _ist_now_str()})
            log.error(f"cc#473 relogin FAILED ({res.get('error')}) — backoff 90s, detector will re-trip")
        time.sleep(90)

    def housekeeping():
        nonlocal conn
        last_atm_check  = None
        last_purge_day  = None
        last_heal_day   = None
        last_roll_check = None
        last_oi_poll    = None
        last_cmp_flush  = None
        last_health_log = None        # cc_task #84
        watchdog_rung   = 0           # cc#489: 0=healthy, 1=reconnect already tried this failure episode
        opt_subscribed       = False  # cc#189: INDEX options subscribed once live prices went fresh
        opt_stock_subscribed = False  # cc#241: STOCK options subscribed once >=09:25 + cmp fresh
        opt_deadline_alerted = False  # cc#189: fired the 09:30 not-subscribed CRITICAL once (per day)
        opt_gate_day         = None   # cc#189: reset the gate each trading day
        starvation_day       = None   # cc#228: fyers_eq starvation check fired once per trading day
        relogin_day          = None   # cc#473: 09:05 daily staleness re-login fired once per trading day
        consecutive_db_failures = 0   # cc#497 fix_2b: un-blind the watchdog — see _mark_db_error below
        sub_bounce_day       = None   # cc#497 fix_1_TIMING_FINAL: 09:14 pre-open socket bounce, once/day
        sub_seq_day          = None   # cc#497 fix_1_TIMING_FINAL: canary/full sequence trigger, once/day
        last_fut_check       = None   # cc#605 fix_2: throttle the 09:24 futures-delivery probe (>=90s apart)
        fut_alerted          = False  # cc#605 fix_2: fired the "still 0 after the one restart" alert once
        last_heartbeat       = None   # cc#660: last worker_heartbeat write (every HEARTBEAT_WRITE_MIN)
        guardian_cmd_nonce   = None   # cc#660: highest feed_guardian_cmd nonce already executed

        def _mark_db_error(e, where):
            """cc#497 fix_2b: the 17-Jul root cause — every conn-based call in this loop caught
            its OWN exception locally and just logged+returned a degraded value forever, so a
            single dead shared conn silently disabled the watchdog, the options gate, the 09:05
            staleness check and all alerting for the rest of the day, with nothing ever
            escalating. This flags a psycopg2 connection-class error (as opposed to some other,
            unrelated exception) so the loop bottom can reconnect once and count consecutive
            failures toward a loud exit — 'the -1 skip rail may remain for a single bad read but
            must escalate, never loop forever.' Returns True if it recognized/flagged a DB error
            (caller should skip its own generic log.warning to avoid double-logging)."""
            if isinstance(e, (psycopg2.InterfaceError, psycopg2.OperationalError,
                              psycopg2.errors.InFailedSqlTransaction)):
                log.error(f"{where}: DB conn error ({e}) — flagged for reconnect")
                nonlocal db_error_this_iter
                db_error_this_iter = (where, e)
                return True
            return False

        while True:
            db_error_this_iter = None
            now    = datetime.now(IST)
            today  = now.date()
            now_dt = now.replace(tzinfo=None)
            in_market = (now.weekday() < 5 and MARKET_OPEN <= now.time() <= SESSION_END)   # cc#855

            # ── cc#660: worker heartbeat (every HEARTBEAT_WRITE_MIN, ANY hour) ────────────────
            if last_heartbeat is None or (now_dt - last_heartbeat).total_seconds() >= HEARTBEAT_WRITE_MIN * 60:
                if _write_heartbeat(conn, boot_time.replace(tzinfo=None)):
                    last_heartbeat = now_dt

            # ── cc#660: feed_guardian resubscribe/restart command poll (once per nonce) ───────
            # The app-side guardian sets feed_guardian_cmd when a leg is stale while another flows.
            # We execute at most once per monotonic nonce: RESUBSCRIBE -> run the two-stage subscribe
            # sequence (re-subscribes equity + full futures leg); RESTART -> hard re-exec. Outcome is
            # written back to feed_guardian_cmd_ack + ops_log so the guardian can see it landed.
            try:
                raw_cmd = _flag_get(conn, _GUARDIAN_CMD_KEY)
                if raw_cmd:
                    cmd = json.loads(raw_cmd)
                    nonce = int(cmd.get("nonce", 0))
                    if guardian_cmd_nonce is None:
                        # first poll after a (re)boot: adopt the current nonce WITHOUT acting, so a
                        # restart can never replay a stale command into an execv loop.
                        guardian_cmd_nonce = nonce
                    elif nonce > guardian_cmd_nonce:
                        guardian_cmd_nonce = nonce
                        action = (cmd.get("action") or "resubscribe").lower()
                        leg    = cmd.get("leg", "all")
                        log.error(f"cc#660 guardian cmd nonce={nonce} action={action} leg={leg} "
                                  f"— {cmd.get('reason','')}")
                        _flag_set(conn, _GUARDIAN_ACK_KEY, json.dumps(
                            {"nonce": nonce, "action": action, "leg": leg,
                             "status": "executing", "ist": _ist_now_str()}))
                        _ops_log(conn, 'alert', 'feed_guardian_cmd',
                                 {'nonce': nonce, 'action': action, 'leg': leg,
                                  'reason': cmd.get('reason', ''), 'ist': _ist_now_str()})
                        if action == "restart":
                            _flag_set(conn, _GUARDIAN_ACK_KEY, json.dumps(
                                {"nonce": nonce, "action": action, "leg": leg,
                                 "status": "restarting", "ist": _ist_now_str()}))
                            _hard_restart(f"cc#660 guardian escalation: {leg} — {cmd.get('reason','')}")
                        else:
                            threading.Thread(target=_run_subscribe_sequence,
                                             args=(f"guardian-{leg}",), daemon=True).start()
                            _flag_set(conn, _GUARDIAN_ACK_KEY, json.dumps(
                                {"nonce": nonce, "action": action, "leg": leg,
                                 "status": "resubscribe_dispatched", "ist": _ist_now_str()}))
            except Exception as e:
                if not _mark_db_error(e, 'guardian cmd poll'):
                    log.warning(f"cc#660 guardian cmd poll failed: {e}")

            # ── cc#497 fix_1_TIMING_FINAL_FOUNDER_17JUL: subscription sequencing ──────────────
            # (replaces the old on_connect auto-subscribe; root_cause_1_ws_premarket_zombie).
            if is_trading_day(today):
                # 09:14 IST: bounce a socket that's been open since before 09:00 (pre-open/
                # overnight) once, so the canary stage rides a fresh at-open session instead of
                # a stale one Fyers may have silently dropped subscriptions on. A boot that
                # happens AFTER 09:00 never needs this — its own session is already fresh.
                # cc#843 fix_1 REPLACES the cc#497 09:14-bounce + 09:20-scheduled pair.
                # The invariant is now simply: TODAY'S SUBSCRIBE SEQUENCE ONLY EVER RUNS ON A
                # CONNECTION ESTABLISHED AT OR AFTER 09:16. Two ways a socket can be older than
                # that: a weekend/holiday boot whose connection survived into Monday pre-open, or a
                # reconnect the SDK made on its own before the gate released. Both get bounced once
                # at 09:16, then the sequence runs on the fresh session.
                if (sub_bounce_day != today and now.time() >= WS_FIRST_CONNECT):
                    est = _conn_state.get('established_at')
                    stale_conn = (est is not None and (est.date() < today
                                  or est.time() < WS_FIRST_CONNECT))
                    sub_bounce_day = today
                    if stale_conn:
                        log.info("cc#843: connection predates 09:16 (established "
                                 f"{est.strftime('%Y-%m-%d %H:%M:%S')}) — bouncing to a fresh "
                                 "post-open session before any subscribe")
                        _log_feed_incident("feed_preopen_bounce",
                                           f"cc#843 09:16 bounce; conn established {est.isoformat()}")
                        _force_reconnect()

                # 09:16 IST first-and-only sequence for a boot that came up before the open.
                if (sub_seq_day != today and boot_time.time() < WS_FIRST_CONNECT
                        and now.time() >= WS_FIRST_CONNECT):
                    sub_seq_day = today
                    threading.Thread(target=_run_subscribe_sequence, args=('scheduled-0916',),
                                     daemon=True).start()

                # midmarket_boot_rule: the worker booted AT OR AFTER market open (already on the
                # safe side of the pre-open trap — a boot at say 09:17 needs the SAME immediate
                # treatment, not just >=09:22 as the spec's literal example states, since there's
                # no "wait for 09:20" scheduled path left to catch it). Run canary->full NOW
                # instead of waiting for tomorrow — this also makes a same-day cc#497 deploy
                # itself the recovery restart for an already-dead feed.
                elif (sub_seq_day != today and boot_time.time() >= WS_FIRST_CONNECT and in_market):
                    sub_seq_day = today
                    threading.Thread(target=_run_subscribe_sequence, args=('midmarket-boot',),
                                     daemon=True).start()

            # ── cc#605 fix_2: 09:24 FUTURES-DELIVERY verify + ONE clean restart ──────────────────
            # 22-Jul futures were dead all day (a restart-storm zombie session silently swallowed the
            # 210-fut subscribe batch) and it went undetected because post-subscribe verification checks
            # EQUITY only. Blunt cc#497 recovery: if no fut bars by 09:24, do ONE full clean process
            # restart (the fresh at-open boot resubscribes ALL sets). A persisted flag caps it at a
            # single restart; if futures are STILL zero by 09:30, alert once and stop (no restart loop).
            if (is_trading_day(today) and in_market and now.time() >= dt_time(9, 24)
                    and (last_fut_check is None or (now_dt - last_fut_check).total_seconds() >= 90)):
                last_fut_check = now_dt
                if _fut_bars_today() == 0:
                    if _flag_get(conn, 'feed_fut_restart_day') == today.isoformat():
                        if not fut_alerted and now.time() >= dt_time(9, 30):
                            fut_alerted = True
                            _ops_log(conn, 'alert', 'feed_fut',
                                     {'event': 'fut_zero_after_clean_restart',
                                      'note': 'futures delivery still 0 after the one cc#605 09:24 restart — manual check',
                                      'ist': _ist_now_str()})
                            _log_feed_incident('feed_fut_dead_after_restart',
                                               'futures delivery still 0 after the one cc#605 09:24 clean restart')
                    else:
                        _flag_set(conn, 'feed_fut_restart_day', today.isoformat())   # persist BEFORE restart
                        _ops_log(conn, 'alert', 'feed_fut',
                                 {'event': 'fut_zero_0924_clean_restart', 'ist': _ist_now_str()})
                        _hard_restart('cc#605: 09:24 futures delivery = 0 — one clean restart + full resubscribe')

            # ── cc#473 item 1 / cc#540: 09:05 IST daily token-staleness re-login (once/day).
            # Never start the day on yesterday's token. Boot staleness is already covered by
            # get_valid_token; this handles a worker running since before the IST midnight
            # rollover (its in-memory token silently went stale).
            # cc#540: dropped the is_trading_day(today) gate so this fires EVERY day incl.
            # Sat/Sun/holidays — the founder works weekends and weekend research/backfill jobs
            # (e.g. cc#538) were blocking on auth_error because no fresh token was ever minted.
            # Fyers TOTP autologin is one 2FA/day (not per-trading-day) and the historical-data
            # API is 24/7. The 09:05<=t<MARKET_OPEN(09:15) window applies uniformly every day;
            # the token-created-today short-circuit below still avoids any needless 2FA.
            if (relogin_day != today
                    and dt_time(9, 5) <= now.time() < MARKET_OPEN):
                relogin_day = today
                try:
                    row = load_tokens(conn)
                    created = row[2] if row else None
                    if is_trading_day(today):
                        # cc#564: UNCONDITIONAL fresh TOTP mint before the open on TRADING days.
                        # The open must never depend on an overnight/weekend-surviving token — the
                        # 20-Jul incident was a Sunday-minted token reused Monday whose data-endpoint
                        # quotes came back empty. A pre-open (09:05<t<09:15) mint + clean reboot lands
                        # a guaranteed-fresh, self-tested token before the 09:15 first tick.
                        # cc#605 fix_1: MINT-ONCE across restarts. relogin_day is in-memory and resets
                        # on every hard restart, so pre-cc#605 the mint->restart re-entered this
                        # 09:05-09:15 window and re-minted+restarted (3x on 22-Jul, halted only by the
                        # 90s account-block breaker — THE root-cause restart storm). A persisted
                        # IST-dated flag makes the mint fire exactly once per day even across restarts;
                        # the post-restart boot reads it and skips, ending the loop.
                        if _flag_get(conn, 'feed_preopen_mint_day') == today.isoformat():
                            log.info("cc#605: 09:05 pre-open mint already done today (persisted flag) — "
                                     "skip re-mint (restart-loop guard)")
                        else:
                            _flag_set(conn, 'feed_preopen_mint_day', today.isoformat())   # persist BEFORE the restart
                            _ops_log(conn, 'info', 'feed_auth',
                                     {'event': 'preopen_unconditional_remint',
                                      'token_created': str(created) if created else None, 'ist': _ist_now_str()})
                            log.info("cc#564 09:05 pre-open UNCONDITIONAL fresh token mint (trading day)")
                            _self_heal_token('09:05 pre-open unconditional fresh mint (trading day)')
                    elif not (created and created.date() == today):
                        # Non-trading day: keep the cc#540 behaviour — mint only if the token isn't
                        # from today (weekend research/backfill needs a live token, but no forced churn).
                        _ops_log(conn, 'alert', 'feed_auth',
                                 {'event': 'stale_token_0905',
                                  'token_created': str(created) if created else None, 'ist': _ist_now_str()})
                        log.warning(f"cc#473 09:05 staleness: token created {created} (not today) — re-login")
                        _self_heal_token('09:05 daily staleness — token not from today')
                    else:
                        log.info("cc#473 09:05 staleness check OK — token is from today (non-trading day)")
                except Exception as _sc:
                    if not _mark_db_error(_sc, '09:05 staleness check'):
                        log.warning(f"cc#473 09:05 staleness check failed (non-fatal): {_sc}")

            # ── cc#473 item 2: in-process dead-token detector. A 30s canonical REST probe
            # feeds the same consecutive-empty counter as the OI poll; at DEAD_TOKEN_THRESHOLD
            # consecutive char-0/401 responses the token is dead -> self-heal. Market hours only
            # (off-hours the API is quiet and empties are expected).
            if in_market:
                try:
                    _p_ok, _p_detail = _rest_quote_ok(token)
                    _note_api(dead=((not _p_ok) and _p_detail == 'EMPTY_BODY'))
                except Exception as _pe:
                    log.warning(f"cc#473 dead-token probe failed (non-fatal): {_pe}")
                if _consume_dead_flag():
                    # cc#883 item 2: the cross-check that was missing on 07-Aug. Ten empty REST
                    # responses only mean the TOKEN is dead if the WS is dead too. If the socket
                    # is still delivering ticks, this is the REST endpoint throttling us, and
                    # relogging in cannot fix it — it can only burn broker attempts, which is
                    # exactly what happened at 09:55 and cost the account a block at 10:07.
                    _alive, _ws_detail = _ws_alive()
                    if _alive:
                        _mark_rest_degraded()
                        log.error(
                            f"cc#883: {DEAD_TOKEN_THRESHOLD} consecutive empty REST responses, but "
                            f"the WS is ALIVE ({_ws_detail['fresh_legs']} fresh within "
                            f"{_ws_detail['window_sec']}s) — this is a REST problem, NOT a dead "
                            f"token. Backing REST off {REST_DEGRADED_SEC}s. No relogin.")
                        _log_feed_incident(
                            "feed_rest_degraded",
                            f"REST empty x{DEAD_TOKEN_THRESHOLD} while WS alive "
                            f"{_ws_detail} — backoff {REST_DEGRADED_SEC}s, relogin suppressed")
                    else:
                        _self_heal_token(
                            f"{DEAD_TOKEN_THRESHOLD} consecutive dead-token (empty/401) REST "
                            f"responses AND the WS is silent ({_ws_detail})")

            # cc#189: reset the once-per-day 09:30 deadline alert each trading day.
            if opt_gate_day != today:
                opt_gate_day = today
                opt_deadline_alerted = False

            # cc#228: fyers_eq starvation watchdog. fyers_eq (live WS) is now the SOLE equity
            # source (legacy fyers backfill is dormant), and it is new — only 03-Jul is proven
            # full (~15,700 bars/day). If it wrote < 10,000 5m bars by 11:00 IST on a trading
            # day, the equity feed is starving -> fire a one-per-day ops_log alert so the
            # dormant legacy path can be manually re-armed if needed.
            if (starvation_day != today and now.time() >= dt_time(11, 0)
                    and is_trading_day(today)):
                starvation_day = today
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM intraday_prices "
                                    "WHERE source='fyers_eq' AND timeframe='5m' "
                                    "AND ts >= %s AND ts < %s",
                                    (today, today + timedelta(days=1)))
                        eq_bars = cur.fetchone()[0]
                    if eq_bars < 10000:
                        _log_feed_incident("fyers_eq_starvation",
                            f"fyers_eq wrote only {eq_bars} 5m bars by 11:00 IST (<10000; ~15700 "
                            f"expected) — equity feed may be starving. Legacy fyers backfill is "
                            f"dormant (cc#228); re-arm manually (force=True / LEGACY_EQUITY_BACKFILL) "
                            f"if the WS cannot recover.")
                        log.error(f"FYERS_EQ STARVATION: only {eq_bars} 5m bars by 11:00 IST (<10000)")
                    else:
                        log.info(f"fyers_eq starvation check OK: {eq_bars} 5m bars by 11:00 IST")
                except Exception as _sv:
                    if not _mark_db_error(_sv, 'fyers_eq starvation watchdog'):
                        log.warning(f"fyers_eq starvation watchdog: {_sv}")

            if in_market:
                try:
                    update_index_ltp(conn, token, agg)
                except Exception as e:
                    if not _mark_db_error(e, 'update_index_ltp'):
                        log.warning(f"update_index_ltp failed: {e}")
                agg.flush_all()
                # CMP flush throttled 30s -> 5-min (14-Jun-2026): cmp_prices is a
                # 218-row UPSERT (no growth); sub-minute freshness not needed
                # (ATM drift check is 15-min). flush_all still writes 5-min bars
                # every pass (dedupes by bucket).
                if (last_cmp_flush is None or
                        (now_dt - last_cmp_flush).total_seconds() >= CMP_FLUSH_MINS * 60):
                    agg.flush_cmp()
                    last_cmp_flush = now_dt
                opt_store.flush_all()
                # Index option OI poll added alongside the futures OI poll below.

                # ── cc#189: options subscribe ONLY when live prices are fresh ──
                # Founder redesign: no boot/REST hydration. Once the market is open
                # and >=80% of option underlyings have a cmp_prices tick in the last
                # 10 min, compute ATM strikes from LIVE prices and subscribe. Retries
                # every loop (30s); a CRITICAL alert fires if still unsubscribed by
                # 09:30. On a gap day live prices beat yesterday-close for ATM too.
                if not opt_subscribed:
                    fresh = _cmp_fresh_fraction(opt_mgr, kind='index')
                    if fresh >= OPT_FRESH_MIN_FRAC:
                        try:
                            new_opts = opt_mgr.build_initial(kind='index')   # ATM from LIVE cmp_prices
                            if new_opts:
                                _batched_subscribe(fyers_ws, new_opts, action='sub', label='options-live')
                                option_syms.clear(); option_syms.extend(new_opts)
                                opt_subscribed = True
                                total, ok, missing, contracts = opt_mgr.subscribe_health(kind='index')
                                log.info(f"cc#189 INDEX options subscribed LIVE at {now.time().strftime('%H:%M')}: "
                                         f"{contracts} contracts, {ok}/{total} underlyings (cmp fresh {fresh:.0%})")
                                if total and ok < OPT_FRESH_MIN_FRAC * total:
                                    _log_feed_incident("options_subscribe_critical",
                                        f"CRITICAL: only {ok}/{total} INDEX option underlyings subscribed "
                                        f"({contracts} contracts); missing: {', '.join(missing) or 'none'}")
                            else:
                                log.warning("cc#189 gate: cmp fresh but build produced 0 option symbols")
                        except Exception as e:
                            if not _mark_db_error(e, 'cc#189 options live-subscribe'):
                                log.warning(f"cc#189 options live-subscribe failed: {e}")
                    elif now.time() >= OPT_SUB_DEADLINE and not opt_deadline_alerted:
                        _log_feed_incident("options_not_subscribed_0930",
                            f"CRITICAL: options unsubscribed at {now.time().strftime('%H:%M')} — cmp_prices "
                            f"fresh for only {fresh:.0%} of underlyings (need {OPT_FRESH_MIN_FRAC:.0%})")
                        opt_deadline_alerted = True

                # ── cc#241: STOCK options — HARD 09:25 floor, subscribed SEPARATELY from index
                # so index goes early (above) and stocks anchor ATM off the settled 09:25 print.
                # Config-gated (app_config, default OFF); founder flips enabled=true + limit=0 on
                # a watched morning. Additive: never touches the index/eq/fut subscription.
                if (not opt_stock_subscribed) and opt_subscribed and now.time() >= OPT_STOCK_SUB_MIN_TIME:
                    s_enabled, s_limit, _s_n = _stock_options_config(conn)
                    if s_enabled:
                        s_fresh = _cmp_fresh_fraction(opt_mgr, kind='stock')
                        if s_fresh >= OPT_FRESH_MIN_FRAC:
                            try:
                                new_stock = opt_mgr.build_initial(kind='stock')   # ATM off 09:25 print
                                if new_stock:
                                    _batched_subscribe(fyers_ws, new_stock, action='sub', label='stock-options-0925')
                                    option_syms.extend(new_stock)
                                    opt_stock_subscribed = True
                                    s_total, s_ok, s_missing, s_contracts = opt_mgr.subscribe_health(kind='stock')
                                    log.info(f"cc#241 STOCK options subscribed at {now.time().strftime('%H:%M')}: "
                                             f"{s_contracts} contracts, {s_ok}/{s_total} underlyings "
                                             f"(cmp fresh {s_fresh:.0%}, limit={s_limit})")
                                    # decision_3: alert-only if the WS silently dropped subs (<95%).
                                    if s_total and s_ok < OPT_STOCK_OVERFLOW_FRAC * s_total:
                                        _log_feed_incident("stock_options_ws_overflow",
                                            f"stock options: only {s_ok}/{s_total} underlyings subscribed "
                                            f"({s_contracts} contracts, {len(s_missing)} missing) — WS may have "
                                            f"silently dropped subs at scale. Reduce app_config "
                                            f"stock_options_limit LIVE if needed (no redeploy).")
                                else:
                                    log.warning("cc#241 stock gate: enabled + fresh but built 0 stock option symbols")
                            except Exception as e:
                                if not _mark_db_error(e, 'cc#241 stock options subscribe'):
                                    log.warning(f"cc#241 stock options subscribe failed: {e}")

                # Futures OI poll every OI_POLL_MINS via DEPTH API (quotes has NO OI).
                # Background thread: 208 depth calls ≈ 75s — must not block flushes.
                if (last_oi_poll is None or
                        (now_dt - last_oi_poll).total_seconds() >= OI_POLL_MINS * 60):
                    threading.Thread(target=poll_futures_oi,
                                     args=(token, list(futures_fyers_syms), agg),
                                     daemon=True).start()
                    threading.Thread(target=poll_options_oi,
                                     args=(token, opt_mgr.index_option_syms(list(option_syms)), opt_store),
                                     daemon=True).start()
                    # cc#375: also poll OI for the SUBSCRIBED stock options (separate lock/thread so it
                    # never delays the index poll). Without this their WS bars carry no OI -> option_chain
                    # oi stays NULL and the cockpit ATM OI d/d is always '--'. Only when stock options are
                    # actually subscribed; bounded by app_config stock_options_limit (pilot default 20).
                    # cc#482 fix_1/fix_5: ATM CE+PE only (not the full subscribed chain — 13-Jul open-burst
                    # empty-body incident), AND held off until STOCK_OI_POLL_MIN_TIME (09:30) — skips the
                    # noisiest opening 15 min where Fyers depth-API empty-response rate is highest. Index
                    # OI poll above is UNCHANGED (still fires at market open, full depth).
                    if opt_stock_subscribed and now.time() >= STOCK_OI_POLL_MIN_TIME:
                        threading.Thread(target=poll_options_oi,
                                         args=(token, opt_mgr.stock_atm_option_syms(list(option_syms)), opt_store),
                                         kwargs={"lock": _STOCK_OPT_OI_POLL_LOCK, "label": "stock"},
                                         daemon=True).start()
                    last_oi_poll = now_dt

                # ATM drift check every ATM_CHECK_MINS
                if (last_atm_check is None or
                        (now_dt - last_atm_check).total_seconds() >= ATM_CHECK_MINS * 60):
                    try:
                        add_syms, rem_syms = opt_mgr.check_atm_drift()
                        if add_syms or rem_syms:
                            if rem_syms: fyers_ws.unsubscribe(symbols=rem_syms)
                            if add_syms:
                                fyers_ws.subscribe(symbols=add_syms, data_type="SymbolUpdate")
                                option_syms.extend(add_syms)
                            log.info(f"ATM rebalance: +{len(add_syms)} -{len(rem_syms)}")
                    except Exception as e:
                        if not _mark_db_error(e, 'ATM drift check'):
                            log.warning(f"ATM drift check failed: {e}")
                    last_atm_check = now_dt

                # ── feed watchdog (cc#489 WATCHDOG_SIMPLIFICATION, ARPIT DIRECTIVE) ──
                # ONE linear model, every HEALTH_LOG_MINS: check per-source counts ->
                # if either < WATCHDOG_MIN_SYMBOLS, reconnect once -> if still bad on
                # the NEXT check, os._exit(1) and let Railway restart clean. No other
                # recovery paths. Suppressed for STARTUP_GRACE_MINS after 09:15 so the
                # first bar cycle has time to form.
                #
                # cc#497 root_cause_3_HOTFIX_FIRST (verified live 10:27 IST): mins_open is
                # wall-clock-relative to 09:15, NOT boot-relative — a MID-MARKET boot got ZERO
                # grace (mins_open was already >> STARTUP_GRACE_MINS the instant it started), so
                # the watchdog fired on an 8-second-old process before its first tick could land,
                # AND the HEARTBEAT_STALE_MINS lookback window still reflected the PRE-restart
                # dead session. rung 1 then hung the process (close_connection SDK quirk),
                # rung 2 never fired, and every restart looped identically. Gate on
                # mins_since_boot too, so the lookback window can never predate the current boot.
                mins_open       = (now_dt - now_dt.replace(hour=9, minute=15, second=0, microsecond=0)).total_seconds() / 60
                mins_since_boot = (now_dt - boot_time.replace(tzinfo=None)).total_seconds() / 60
                if (mins_open >= STARTUP_GRACE_MINS
                        and mins_since_boot >= max(STARTUP_GRACE_MINS, HEARTBEAT_STALE_MINS)
                        and (last_health_log is None or
                             (now_dt - last_health_log).total_seconds() >= HEALTH_LOG_MINS * 60)):
                    last_health_log = now_dt
                    src_counts = _recent_symbol_counts_by_source(HEARTBEAT_STALE_MINS)
                    eq, fut = src_counts.get('fyers_eq', -1), src_counts.get('fyers_fut', -1)
                    if eq < 0 or fut < 0:
                        log.warning("Watchdog check skipped — DB read failed (no false action on a bad read)")
                    else:
                        ext = src_counts.get(EXT_SOURCE, -1)   # cc#1017: extended leg now part of health
                        uni_ext = len(ext_fyers_syms)          # cc#1017: registry-derived expected count
                        core_ok = eq >= WATCHDOG_MIN_SYMBOLS and fut >= WATCHDOG_MIN_SYMBOLS
                        # cc#1017: the extended leg is now held against feed health (reversing the cc#809
                        # report-only exclusion — the 14-Aug incident IS a dead ext leg this watchdog was
                        # blind to). It counts only when the stage is ON (uni_ext>0) and inside the equity
                        # session; ext_ok mirrors _subscribe_extended's drop threshold, so genuine near-
                        # total death (909->0) fails while staged/illiquid partial ticking does not.
                        _ext_win = MARKET_OPEN <= now_dt.time() <= EQ_CONTINUOUS_END
                        # cc#1017: expected only when the stage is ON (flag>0) — a retreated 'off' stage
                        # keeps the startup list length, so gate on the live flag to avoid a retreat loop.
                        _ext_expected = uni_ext > 0 and ext >= 0 and _ext_win and _ext_stage_limit(conn) > 0
                        ext_ok = (not _ext_expected) or ext >= EXT_MIN_TICK_FRACTION * uni_ext
                        log.info(f"Feed health: eq={eq}/{len(equity_fyers_syms)} "
                                 f"fut={fut}/{len(futures_fyers_syms)} ext={ext}/{uni_ext} "
                                 f"(core_floor={WATCHDOG_MIN_SYMBOLS}, ext_frac>={EXT_MIN_TICK_FRACTION}, "
                                 f"core_ok={core_ok} ext_ok={ext_ok})")
                        if not core_ok:
                            # CORE (eq/fut) failure keeps its proven ladder: reconnect, then os._exit(1).
                            if watchdog_rung == 0:
                                log.error(f"FEED WATCHDOG rung 1: eq={eq} fut={fut} below floor — forcing reconnect")
                                _force_reconnect()
                                _log_feed_incident("feed_watchdog_reconnect", f"eq={eq} fut={fut} ext={ext}/{uni_ext}")
                                watchdog_rung = 1
                            else:
                                log.critical(f"FEED WATCHDOG rung 2: eq={eq} fut={fut} still below floor "
                                             "after reconnect — os._exit(1) for a clean Railway restart "
                                             "(cc#501 finding_2_17jul_1550: sys.exit(1) from this housekeeping "
                                             "thread only kills the thread, leaving a zombie process)")
                                _log_feed_incident("feed_watchdog_exit", f"eq={eq} fut={fut} ext={ext}/{uni_ext}")
                                os._exit(1)
                        elif not ext_ok:
                            # cc#1017: core alive, extended leg dead — rebuild (bounded) then retreat +
                            # CRITICAL via _ext_recover. Deliberately does NOT os._exit: killing the worker
                            # would drop the healthy options/futures/eq legs the founder protected.
                            _ext_recover("watchdog", eq, fut, ext, uni_ext)
                        else:
                            watchdog_rung = 0
                            if _EXT_RECOVERY["attempts"]:
                                log.info(f"cc#1017 extended leg healthy ({ext}/{uni_ext}) — reset rebuild counter")
                                _EXT_RECOVERY["attempts"] = 0

            # Monthly roll — once per day
            if last_roll_check != today:
                try:
                    if opt_mgr.check_monthly_roll():
                        new_expiry   = opt_mgr.expiry
                        new_fut_syms = [futures_fyers_symbol(s, new_expiry) for s in fut_codes]   # cc#162: + index futures
                        new_opt_syms = opt_mgr.build_initial()
                        # cc#151: batched unsub/sub (same helper as on_connect) — the old
                        # single bulk unsubscribe+subscribe silently dropped symbols under
                        # load (1-Jul roll: only 3/212 futures survived).
                        _batched_subscribe(fyers_ws, futures_fyers_syms + option_syms,
                                           action='unsub', label='roll-old')
                        _batched_subscribe(fyers_ws, new_fut_syms + new_opt_syms,
                                           action='sub', label='roll-new')
                        futures_fyers_syms.clear(); futures_fyers_syms.extend(new_fut_syms)
                        futures_set.clear();        futures_set.update(new_fut_syms)
                        option_syms.clear();        option_syms.extend(new_opt_syms)
                        log.info(f"Monthly roll complete: {new_expiry}")
                        threading.Thread(target=_verify_subscribe_survivors, args=('roll',), daemon=True).start()
                except Exception as e:
                    if not _mark_db_error(e, 'Monthly roll check'):
                        log.warning(f"Monthly roll check failed: {e}")
                last_roll_check = today

            # Daily 18:00 IST — heal equity gaps
            if now.hour == 18 and now.minute < 1 and last_heal_day != today:
                log.info("18:00 IST: Running daily heal_gap for equity")
                try:
                    fyers_backfill.heal_gap(token, conn, symbols)
                    last_heal_day = today
                except Exception as e:
                    if not _mark_db_error(e, 'Daily heal_gap'):
                        log.error(f"Daily heal_gap failed: {e}")

            if last_purge_day != today:
                try:
                    purge_old_bars(conn)
                    last_purge_day = today
                except Exception as e:
                    if not _mark_db_error(e, 'purge_old_bars'):
                        log.error(f"purge_old_bars failed: {e}")

            # cc#497 fix_2b: un-blind the watchdog. Every conn-based call above that hit a
            # psycopg2 connection-class error flagged it via _mark_db_error instead of silently
            # swallowing it — reconnect the shared conn ONCE per bad iteration, and escalate to a
            # loud exit(1) (Railway restart) after 3 CONSECUTIVE bad iterations rather than
            # looping blind forever (the 17-Jul root cause). A clean iteration resets the count.
            if db_error_this_iter is not None:
                where, _e = db_error_this_iter
                consecutive_db_failures += 1
                log.error(f"housekeeping loop: {consecutive_db_failures}/3 consecutive DB-conn "
                          f"failures (latest: {where}) — reconnecting shared conn")
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    conn = get_db()
                    opt_mgr.conn = conn   # opt_mgr was built on the same originally-shared conn
                    log.info("housekeeping loop: shared conn reconnected")
                except Exception as e2:
                    log.error(f"housekeeping loop: reconnect FAILED ({e2})")
                if consecutive_db_failures >= 3:
                    log.critical(f"housekeeping loop: DB conn still dead after "
                                 f"{consecutive_db_failures} consecutive failures — os._exit(1) for a "
                                 "clean Railway restart")
                    try:
                        _log_feed_incident("housekeeping_db_dead_exit",
                            f"{consecutive_db_failures} consecutive failures, latest at {where}: {_e}")
                    except Exception:
                        pass
                    os._exit(1)
            else:
                consecutive_db_failures = 0

            time.sleep(30)

    # cc_task #112 — NEVER DIE SILENT. The watchdog lives inside housekeeping(); if that
    # thread ever raises and dies, the feed loses its only auto-recovery and freezes
    # unnoticed (the failure mode behind the 4 recurrences). Supervise it: any crash or
    # unexpected return is logged and the loop is restarted after a short backoff.
    def _housekeeping_supervised():
        while True:
            try:
                housekeeping()   # normally an infinite loop — should never return
                log.error("housekeeping() returned unexpectedly — restarting in 5s")
            except Exception as e:
                log.error(f"housekeeping THREAD crashed: {e} — restarting in 5s")
                try:
                    _log_feed_incident("housekeeping_crash", str(e))
                except Exception:
                    pass
            time.sleep(5)
    threading.Thread(target=_housekeeping_supervised, daemon=True).start()
    # ── cc#843 fix_1: the pre-open connect gate ──────────────────────────────────────────────
    # A trading-day boot BEFORE 09:16 waits. A boot at or after 09:16 (and before close) is already
    # on the safe side and connects immediately — today's 09:24 boot proved that path healthy. A
    # non-trading-day / off-hours boot connects normally: the rule is about trading-day session
    # starts, not about keeping the socket down.
    _now_gate = datetime.now(IST)
    if is_trading_day(_now_gate.date()) and _now_gate.time() < WS_FIRST_CONNECT:
        _target = _now_gate.replace(hour=WS_FIRST_CONNECT.hour, minute=WS_FIRST_CONNECT.minute,
                                    second=0, microsecond=0)
        _wait = (_target - _now_gate).total_seconds()
        log.info(f"cc#843: trading day and it is {_now_gate.strftime('%H:%M:%S')} IST — holding the WS "
                 f"connect until {WS_FIRST_CONNECT.strftime('%H:%M')} ({int(_wait)}s). A pre-open "
                 "connection comes up subscribe-dead; auth was already proven by the REST self-test.")
        try:
            _log_feed_incident("feed_preopen_hold",
                               f"holding connect until {WS_FIRST_CONNECT.strftime('%H:%M')} IST "
                               f"({int(_wait)}s from boot at {_now_gate.strftime('%H:%M:%S')})")
        except Exception:
            pass
        while True:
            _n = datetime.now(IST)
            if _n.time() >= WS_FIRST_CONNECT or not is_trading_day(_n.date()):
                break
            time.sleep(min(20, max(1, (_target - _n).total_seconds())))
        log.info(f"cc#843: {datetime.now(IST).strftime('%H:%M:%S')} IST — releasing the connect gate")

    log.info("Connecting WebSocket (live)...")
    _conn_state['connect_called_at'] = datetime.now(IST)
    fyers_ws.connect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auth-code', type=str, default=None)
    args = parser.parse_args()
    # cc#883 item 3: ALL auth-path schema work happens HERE, once, before run() starts any WS,
    # housekeeping or auth thread. It used to run inside every login (ALTER TABLE fyers_tokens
    # ADD COLUMN IF NOT EXISTS last_attempt) — on 07-Aug that ALTER queued behind an
    # idle-in-transaction reader on the same table and killed four minutes of reloginsics.
    # It never raises: a migration that cannot get its lock alerts and returns False, because
    # the feed's job is to tick and refusing to boot would be the worse failure.
    try:
        import fyers_autologin as _autologin_boot
        _autologin_boot.migrate_schema()
    except Exception as _mig_e:
        log.error(f"cc#883 boot migration could not run: {_mig_e}")
    run(auth_code=args.auth_code)
