"""
Time-adjusted intraday volume ratio for Trade Check R6/R7 Volume rules
(cc_task #145, 01-Jul-2026). Supersedes the old 30-day up-day/down-day average
volume comparison.

FORMULA:
  Baseline     = AVG(raw_prices.volume) over the last 5 trading days (simple mean).
  T_factor     = elapsed_market_minutes / 375  (market_start=09:15 IST, full day=375min).
  Expected_vol = Baseline * T_factor.
  Today_vol    = source-agnostic + semantics-aware today volume (see cc#150 below).
  Ratio        = Today_vol / Expected_vol.

Thresholds (same for LONG and SHORT -- high volume confirms conviction either way):
  ratio >  1.2        -> PASS  (1.0)
  1.0 <= ratio <= 1.2  -> WATCH (0.5)
  ratio <  1.0         -> FAIL  (0.0)

After market close (or outside market hours): fallback = raw_prices today volume
(if the EOD row already exists) / Baseline, no T_factor.

CALIBRATION NOTE (cc#145): the spec's "Today_vol = SUM(intraday_prices.volume)"
wording would double-count -- intraday_prices.volume is a CUMULATIVE running
total per bar (verified: values strictly increase through the day for a given
symbol/day), not a per-5-min increment. Summing 22 already-cumulative bars for
CGPOWER on 01-Jul produced ~29M, matching the task's flagged anomaly exactly --
this is a SUM-of-cumulative artifact, not a genuine fyers_eq/raw_prices scale
mismatch (confirmed: CGPOWER Jun30 raw_prices=3,297,252 is in the same scale as
fyers_eq ticks). Fix: Today_vol = the LATEST tick's volume (already cumulative
to that point), not SUM across bars. No normalization factor needed.

cc#150 (02-Jul-2026, fault_A/B): filtering strictly on source='fyers_eq' matched
only ~13 symbols (the main live stream tags most equity ticks source='fyers'),
so ratio came back None for ~95% of the futures universe during market hours.
Worse, 'fyers'-tagged rows in intraday_prices are PER-BAR volume (not cumulative
like fyers_eq), so a blanket "latest tick" read would silently under-count for
those symbols. Fix (OPTION B, locked): accept source IN ('fyers','fyers_eq'),
dedupe same-bucket rows preferring fyers_eq, then auto-detect cumulative vs
per-bar semantics per symbol/day from today's sampled bars (monotonic
non-decreasing -> cumulative -> Today_vol=latest; else per-bar -> Today_vol=SUM).
Immune to future source/semantics drift by construction.
"""

from datetime import datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

_MKT_OPEN = time(9, 15)
_MKT_CLOSE = time(15, 30)
_FULL_DAY_MIN = 375


def _ist_now() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def _is_market_hours(now: datetime) -> bool:
    if now.weekday() >= 5:
        return False
    return _MKT_OPEN <= now.time() <= _MKT_CLOSE


def _baseline_5d(cur, symbol: str):
    cur.execute("""
        SELECT AVG(volume) FROM (
            SELECT volume FROM raw_prices
            WHERE symbol=%s AND price_date < CURRENT_DATE AND volume IS NOT NULL
            ORDER BY price_date DESC LIMIT 5
        ) t
    """, (symbol,))
    r = cur.fetchone()
    return float(r[0]) if r and r[0] is not None else None


_MIN_SAMPLE_FOR_DETECTION = 3  # fewer bars than this -> not enough to trust monotonicity


def _today_volume_rows(cur, symbol: str):
    """Today's 5m volume bars, source IN (fyers, fyers_eq), deduped per ts bucket
    preferring fyers_eq when both exist for the same bucket (mixed-source day)."""
    cur.execute("""
        SELECT ts, volume, source FROM (
            SELECT ts, volume, source,
                   ROW_NUMBER() OVER (PARTITION BY ts ORDER BY (source = 'fyers_eq') DESC) AS rn
            FROM intraday_prices
            WHERE symbol=%s AND source IN ('fyers','fyers_eq') AND timeframe='5m'
              AND ts::date = CURRENT_DATE
        ) t
        WHERE rn = 1
        ORDER BY ts ASC
    """, (symbol,))
    return cur.fetchall()


def _detect_today_vol(rows):
    """rows: [(ts, volume, source), ...] ascending by ts. Returns (today_vol, semantics)
    where semantics is 'cumulative' or 'per_bar', or (None, None) if no usable data."""
    vols = [(v, src) for _, v, src in rows if v is not None]
    if not vols:
        return None, None
    if len(vols) < _MIN_SAMPLE_FOR_DETECTION:
        # Too few bars (first 1-2 bars of the day) to trust a monotonicity read --
        # default by source: fyers_eq is cumulative, plain fyers is per-bar.
        if vols[-1][1] == 'fyers_eq':
            return vols[-1][0], 'cumulative'
        return sum(v for v, _ in vols), 'per_bar'
    values = [v for v, _ in vols]
    is_monotonic = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
    if is_monotonic:
        return values[-1], 'cumulative'
    return sum(values), 'per_bar'


def volume_ratio(cur, symbol: str) -> dict:
    """Returns dict: ratio, today_vol, expected_vol, baseline, t_factor, source, semantics.
    ratio is None when there isn't enough data to compute one (never fabricated).
    source = "live_intraday" | "eod" | None.
    """
    now = _ist_now()
    baseline = _baseline_5d(cur, symbol)
    out = {"ratio": None, "today_vol": None, "expected_vol": None,
           "baseline": baseline, "t_factor": None, "source": None, "semantics": None}
    if baseline is None or baseline <= 0:
        return out

    if _is_market_hours(now):
        rows = _today_volume_rows(cur, symbol)
        today_vol, semantics = _detect_today_vol(rows)
        if today_vol is None:
            return out
        elapsed_min = max((now.hour * 60 + now.minute) - (9 * 60 + 15), 1)
        t_factor = min(elapsed_min, _FULL_DAY_MIN) / _FULL_DAY_MIN
        expected_vol = baseline * t_factor
        ratio = (today_vol / expected_vol) if expected_vol > 0 else None
        out.update(ratio=ratio, today_vol=today_vol, expected_vol=expected_vol,
                    t_factor=t_factor, source="live_intraday", semantics=semantics)
        return out

    # Outside market hours (after close / before open / weekend): EOD fallback,
    # no T-factor. Only fires if today's raw_prices row already exists.
    cur.execute("SELECT volume FROM raw_prices WHERE symbol=%s AND price_date=CURRENT_DATE",
                (symbol,))
    r = cur.fetchone()
    if not r or r[0] is None:
        return out
    today_vol = float(r[0])
    ratio = today_vol / baseline
    out.update(ratio=ratio, today_vol=today_vol, expected_vol=baseline, source="eod")
    return out


def r6_state(ratio):
    """True=PASS, "watch"=WATCH, False=FAIL, None=no data. Same for LONG/SHORT."""
    if ratio is None:
        return None
    if ratio > 1.2:
        return True
    if ratio >= 1.0:
        return "watch"
    return False


def r6_label(ratio) -> str:
    return f"Time Vol x{ratio:.2f}" if ratio is not None else "Time Vol —"
