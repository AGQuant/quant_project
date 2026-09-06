"""pcr_mood.py — cc#1568 · ONE PCR mood composer for every surface (PCR_MOOD_BANDS_V2, session_log 36200).

WHY THIS FILE EXISTS
    The PCR mood word and the dial segments lived in ONE place before this card — but that place was
    the CLIENT (mobile/home.html: PCR_BANDS + pcrBand + pcrGaugeSvg), so the web Index Intel PCR card
    and the Digest PCR tile could not read it and each printed a bare number or its own vocabulary.
    On 02-Sep-2026 at 10:44 IST the app card read PCR 1.46 = EXTREME GREED while Nifty was -1.36% on
    the week. Founder ruling (36200): 1.46 is GREED; a high PCR on a falling week is put COVER, not
    greed. That reading needs a cross-input (the Nifty week return), which only the server has — so
    the composer moves server-side and every surface reads its output.

BANDS  (36200 upper cut; 18024 / 33334 lower bands unchanged — do_not_touch on the card)
    pcr <  0.50                 EXTREME FEAR   band 0
    0.50 <= pcr < 0.80          CAUTIOUS       band 1
    0.80 <= pcr < 1.00          NEUTRAL        band 2
    1.00 <= pcr <= 1.50         GREED          band 3
    pcr >  1.50                 band 4, the label depends on the week:
        nifty_week_pct <= -1.0  CAUTIOUS       (high put cover while the index is down on the week)
        nifty_week_pct >  -1.0  EXTREME GREED
        nifty_week_pct None     GREED, note "week return unavailable" — never a guess at the extreme.
    1.00 exactly: 18024 reads "1.0-1.4 Greed" with 0.8-1.0 Neutral below it, and the client
    implementation resolved the shared edge as GREED (x < 1.0 -> Neutral). Kept.

WEEK INPUT
    nifty_week_pct is nifty_dwm.live_nifty_dwm(cur, "NIFTY50")[1] — the Market Gate's own number.
    compose_live() below is the only path that fetches it; nothing here recomputes a week return.

DIAL  (0-200 = pcr x 100, visual only; the real PCR is printed unscaled beside the dial)
    cuts [0, 50, 80, 100, 150, 200]. Segments 0-50 red, 50-80 red (dimmed), 80-100 amber,
    100-150 grn (dimmed) = GREED, 150-200 follows the label: CAUTIOUS -> amber, EXTREME GREED ->
    grn (bright), GREED-with-note -> grn. Colours are TOKEN NAMES ('red' / 'amber' / 'grn'); the
    surface maps them to its own var(--token, fallback). No hex here.

READ PATH ONLY. Nothing here writes. Driver-agnostic cursor (psycopg2 in digest/mobile, psycopg3
in v8_endpoints) — the only query is live_nifty_dwm's own.
"""

from fastapi import APIRouter

EXTREME_FEAR = "EXTREME FEAR"
CAUTIOUS = "CAUTIOUS"
NEUTRAL = "NEUTRAL"
GREED = "GREED"
EXTREME_GREED = "EXTREME GREED"

WEEK_CAUTIOUS_CUT = -1.0          # 36200: week return at or below this turns a >1.50 PCR CAUTIOUS
UPPER_CUT = 1.50                  # 36200: GREED up to and including 1.50 (was 1.40 under 18024)
DIAL_CUTS = [0, 50, 80, 100, 150, 200]
_SEG_COLOUR = ["red", "red", "amber", "grn"]      # bands 0-3 fixed; band 4 follows the label
_LABEL_COLOUR = {EXTREME_FEAR: "red", CAUTIOUS: "red", NEUTRAL: "amber", GREED: "grn",
                 EXTREME_GREED: "grn"}
WEEK_NOTE = "week return unavailable"


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fmt_pct(v):
    """-1.36 -> '1.4' (abs, one decimal) for the plain-words reason line."""
    return f"{abs(v):.1f}"


def pcr_mood(pcr, nifty_week_pct):
    """{label, band, dial_cuts, dial_segments, label_colour, reason, note, pcr, nifty_week_pct}.

    pcr None -> label None, band None: NO label, never a guessed one (the client renders nothing).
    reason is set ONLY for the >1.50 CAUTIOUS reading (card scope 5), in plain words with the live
    numbers. note is set ONLY when the week input was missing at the extreme."""
    p = _f(pcr)
    wk = _f(nifty_week_pct)
    out = {"pcr": p, "nifty_week_pct": wk, "dial_cuts": list(DIAL_CUTS),
           "label": None, "band": None, "label_colour": None, "dial_segments": [],
           "reason": None, "note": None}
    if p is None:
        return out

    if p < 0.5:
        label, band = EXTREME_FEAR, 0
    elif p < 0.8:
        label, band = CAUTIOUS, 1
    elif p < 1.0:
        label, band = NEUTRAL, 2
    elif p <= UPPER_CUT:
        label, band = GREED, 3
    else:
        band = 4
        if wk is None:
            label = GREED
            out["note"] = WEEK_NOTE
        elif wk <= WEEK_CAUTIOUS_CUT:
            label = CAUTIOUS
            out["reason"] = (f"High put cover while Nifty is down {_fmt_pct(wk)}% on the week")
        else:
            label = EXTREME_GREED

    top_colour = "amber" if (band == 4 and label == CAUTIOUS) else "grn"
    colours = _SEG_COLOUR + [top_colour]
    out["dial_segments"] = [{"lo": DIAL_CUTS[i], "hi": DIAL_CUTS[i + 1], "colour": colours[i],
                             "band": i} for i in range(5)]
    out["label"] = label
    out["band"] = band
    # band 4 CAUTIOUS is the amber "cover" reading, not the red low-PCR one — colour by segment.
    out["label_colour"] = top_colour if band == 4 else _LABEL_COLOUR[label]
    return out


def compose_live(cur, pcr):
    """pcr_mood() with the week return fetched from the Market Gate's own source. One call, one
    source (nifty_dwm.live_nifty_dwm); on any failure the week is None and the composer says so."""
    wk = None
    try:
        from nifty_dwm import live_nifty_dwm
        _d, wk, _m, _src = live_nifty_dwm(cur, "NIFTY50")
    except Exception:
        wk = None
    return pcr_mood(pcr, wk)


# ── GET /api/pcr/mood — the composer over the live PCR, for the web card ──────────────────────
# Same read the app hero uses (mobile_home2 cc#1140): today's latest pcr_intraday.pcr_total, else
# the last pcr_daily row. Wired in main.py with ONE include_router line (rule 5).
router = APIRouter(prefix="/api/pcr", tags=["pcr"])


def latest_pcr(cur, underlying="NIFTY"):
    """(pcr, basis, as_of) — the LATEST pcr_intraday bar for `underlying`, whatever its date.

    cc#1670 (founder 04-Sep): the old query filtered `ts::date = today`, so on any tick before
    the day's FIRST 5-min bar landed (session open, or a feed gap) this fell straight to the
    pcr_daily EOD row and printed yesterday's number as "EOD" -- even though pcr_intraday still
    held yesterday's perfectly good last bar. Data-honesty is "show the newest real reading with
    its real timestamp", not "show today's reading or nothing" -- so the date filter is gone.

    basis: 'LIVE' when the returned bar is stamped TODAY and it is 09:15-15:30 IST on a weekday
           (cc#1576's session window); 'LAST' for any other real intraday bar (yesterday's close
           tick, or a today bar read outside the session); 'DAILY' only when pcr_intraday has NO
           rows at all for this underlying and the pcr_daily EOD table is the fallback -- as_of
           there is the bare price_date (no fabricated time)."""
    cur.execute("""
        SELECT pcr_total, ts FROM pcr_intraday
        WHERE underlying=%s AND pcr_total IS NOT NULL
        ORDER BY ts DESC LIMIT 1
    """, (underlying,))
    r = cur.fetchone()
    if r and r[0] is not None:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz, time as _time
        _now = _dt.now(_tz(_td(hours=5, minutes=30)))
        _open = (_now.weekday() < 5 and _time(9, 15) <= _now.time() <= _time(15, 30))
        _is_today = r[1].date() == _now.date()
        return _f(r[0]), ("LIVE" if (_is_today and _open) else "LAST"), r[1].strftime("%Y-%m-%d %H:%M")
    cur.execute("""
        SELECT pcr, price_date FROM pcr_daily
        WHERE underlying=%s AND pcr IS NOT NULL
        ORDER BY price_date DESC LIMIT 1
    """, (underlying,))
    r = cur.fetchone()
    if r and r[0] is not None:
        return _f(r[0]), "DAILY", str(r[1])
    return None, None, None


@router.get("/mood")
def pcr_mood_endpoint(underlying: str = "NIFTY"):
    """Composer output for the latest PCR of one underlying. The week cross-input is always the
    NIFTY week (36200 names it); BANKNIFTY gets the same bands with the same week."""
    import os
    import psycopg
    underlying = (underlying or "NIFTY").strip().upper()
    with psycopg.connect(os.getenv("DATABASE_URL")) as conn, conn.cursor() as cur:
        pcr, basis, as_of = latest_pcr(cur, underlying)
        out = compose_live(cur, pcr)
        # cc#1576: the (i) read rides on the same payload — existing fields untouched.
        try:
            out["interpret"] = compose_read(cur, underlying, pcr, as_of)
        except Exception as e:
            out["interpret"] = {"error": str(e)[:200], "state": None,
                                "headline": "The read is not available this tick.", "read": [], "evidence_line": None}
        # cc#1797: the app (i) sheet reads ONLY this block (sentences + footer + as_of).
        try:
            out["confidence"] = compose_confidence(cur, underlying)
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            out["confidence"] = {"error": str(e)[:200], "sentences": [], "text": None,
                                 "footer": CONFIDENCE_FOOTER, "as_of": None}
    out.update({"underlying": underlying, "basis": basis, "as_of": as_of,
                "spec": "session_log 36200 + 36294"})
    return out



# ══════════════════════════════════════════════════════════════════════════════════════════
# cc#1576 · PCR_READ_INTERPRET_V1 (session_log 36294) — the (i) read behind the PCR card.
#
# THE READ IS A THREE-INPUT RULE (36294 read_rule_v2), not a band alone:
#   PCR vs yesterday (pcr_daily; the last hour of pcr_intraday breaks a flat day)
#   Nifty day % on the previous-close basis (nifty_dwm.live_nifty_dwm, cc#1565)
#   India VIX now vs yesterday's close (intraday_prices INDIAVIX)
#   (1) PCR up, Nifty flat/up, VIX flat/down   -> STRENGTH     puts are being SOLD as support
#   (2) PCR up, Nifty down, VIX up             -> CAUTION      puts are being BOUGHT as a hedge
#   (3) PCR down, Nifty up                     -> COMPLACENCY  protection dropped into a rise
#   (4) PCR down, Nifty down                   -> WEAK_SUPPORT puts closed into a fall
#   anything else                              -> NEUTRAL
# The first paragraph is the state read, the second the band read (bands_for_read — READ bands,
# a different set from the 36200 LABEL cuts above, which are untouched).
#
# EVIDENCE is counted from history classified with the SAME rule: pcr_daily quality rows joined
# to raw_prices closes (Nifty day % and the underlying's next-day close) and the INDIAVIX daily
# close, last 120 sessions, grouped by state AND by read band. scored=false below 20 sessions;
# the line then reads "Only N such days on record. Too few to trust yet." Never a direction call
# while unscored; never a fabricated count.
#
# OPTION PRICE LINE (36294 amend_3): the D-card rule, reused — deriv_metrics.strike_chain's ATM
# row (ltp vs Black-Scholes fair at sigma=RV20, EXPENSIVE >+25% / REASONABLE / CHEAP). No second
# pricer here; the index path lives in that same handler.
#
# LANGUAGE (36283 plain_words_v2): one idea per sentence, ten words or fewer, "put buying" and
# "call buying", never "OI". A put is explained once: "A put is a bet on a fall."
# ══════════════════════════════════════════════════════════════════════════════════════════

STRENGTH, CAUTION_STATE, COMPLACENCY, WEAK_SUPPORT, NEUTRAL_STATE = (
    "STRENGTH", "CAUTION", "COMPLACENCY", "WEAK_SUPPORT", "NEUTRAL")
STATES = (STRENGTH, CAUTION_STATE, COMPLACENCY, WEAK_SUPPORT, NEUTRAL_STATE)
PCR_MOVE_CUT = 0.03      # PCR change smaller than this is "flat"
NIFTY_MOVE_CUT = 0.20    # Nifty day % inside +/- this is "flat"
VIX_MOVE_CUT = 2.0       # VIX % change vs prev close inside +/- this is "flat"
EVIDENCE_MIN = 20
EVIDENCE_SESSIONS = 120
READ_BANDS = ["<0.70", "0.70-0.90", "0.90-1.00", "1.00-1.20", "1.20-1.50", ">1.50"]
_STATE_COLOUR = {STRENGTH: "grn", CAUTION_STATE: "amber", COMPLACENCY: "amber",
                 WEAK_SUPPORT: "red", NEUTRAL_STATE: "mut"}
_EVIDENCE_CACHE = {}     # (underlying, date) -> tables


def _dir(delta, cut):
    if delta is None:
        return None
    return "up" if delta > cut else ("down" if delta < -cut else "flat")


def read_band(pcr):
    """The six READ bands of 36294 bands_for_read (not the 36200 label cuts)."""
    p = _f(pcr)
    if p is None:
        return None
    if p < 0.70:
        return READ_BANDS[0]
    if p < 0.90:
        return READ_BANDS[1]
    if p < 1.00:
        return READ_BANDS[2]
    if p < 1.20:
        return READ_BANDS[3]
    if p <= 1.50:
        return READ_BANDS[4]
    return READ_BANDS[5]


def read_state(pcr, pcr_prev_day, nifty_day_pct, vix_now=None, vix_prev_close=None, pcr_1h_ago=None):
    """The three-input state. None when PCR-yesterday or the Nifty day move is missing — an
    unknown input gives no state, never a guessed one. VIX missing counts as flat (stated by the
    caller as a caveat)."""
    p, pp, nd = _f(pcr), _f(pcr_prev_day), _f(nifty_day_pct)
    if p is None or pp is None or nd is None:
        return None
    pcr_dir = _dir(p - pp, PCR_MOVE_CUT)
    if pcr_dir == "flat" and _f(pcr_1h_ago) is not None:
        pcr_dir = _dir(p - _f(pcr_1h_ago), PCR_MOVE_CUT)      # the last hour breaks a flat day
    nifty_dir = _dir(nd, NIFTY_MOVE_CUT)
    vn, vp = _f(vix_now), _f(vix_prev_close)
    vix_dir = _dir((vn / vp - 1.0) * 100.0, VIX_MOVE_CUT) if (vn and vp) else "flat"
    if pcr_dir == "up" and nifty_dir in ("flat", "up") and vix_dir in ("flat", "down"):
        return STRENGTH
    if pcr_dir == "up" and nifty_dir == "down" and vix_dir == "up":
        return CAUTION_STATE
    if pcr_dir == "down" and nifty_dir == "up":
        return COMPLACENCY
    if pcr_dir == "down" and nifty_dir == "down":
        return WEAK_SUPPORT
    return NEUTRAL_STATE


_EXPLAIN = ["PCR compares put buying to call buying.", "A put is a bet on a fall."]

_STATE_TEXT = {
    STRENGTH: ("Puts are being sold as support under the market.",
               ["Put buying rose while the market held up.",
                "Fear did not rise with it.",
                "So these puts look sold, not bought.",
                "That works as support under the market."]),
    CAUTION_STATE: ("Big players are buying puts as a hedge.",
                    ["Put buying rose while the market fell.",
                     "Fear rose with it.",
                     "So these puts look bought for protection.",
                     "Treat the high PCR as caution, not greed."]),
    COMPLACENCY: ("Put buying is falling while the market rises.",
                  ["Fewer puts are held as prices climb.",
                   "Protection is being dropped.",
                   "That is comfort, not strength.",
                   "Note it. Do not lean on it alone."]),
    WEAK_SUPPORT: ("Puts are being closed into a fall.",
                   ["Put buying fell while the market fell.",
                    "Puts that acted as support are being closed.",
                    "Support under the market is weaker now."]),
    NEUTRAL_STATE: ("No clear put story today.",
                    ["Put buying and the market did not move together.",
                     "Nothing to read into it today."]),
}


def band_text(band, nifty_week_pct=None):
    if band == READ_BANDS[0]:
        return ["Very few puts are held.", "The crowd is not protecting itself.", "Fear of a fall is low."]
    if band == READ_BANDS[1]:
        return ["Fewer puts than calls are held.", "That is mild hope."]
    if band == READ_BANDS[2]:
        return ["Puts and calls are balanced."]
    if band == READ_BANDS[3]:
        return ["More puts than calls are held.", "Some protection is being bought."]
    if band == READ_BANDS[4]:
        return ["Many puts are held.", "The label reads GREED."]
    if band == READ_BANDS[5]:
        wk = _f(nifty_week_pct)
        if wk is not None and wk <= WEEK_CAUTIOUS_CUT:
            return ["Very many puts are held.", "Nifty is down on the week.", "So this reads as caution."]
        return ["Very many puts are held.", "The label reads EXTREME GREED."]
    return []


def evidence_line(n, up, down, avg=None):
    if n < EVIDENCE_MIN:
        return "Only %d such day%s on record. Too few to trust yet." % (n, "" if n == 1 else "s")
    a = (" Avg %s%%." % ("%+.2f" % avg)) if avg is not None else ""
    return "%d such days on record: next day up %d, down %d.%s" % (n, up, down, a)


def read(pcr, pcr_prev_day, pcr_1h_ago, nifty_day_pct, vix_now, vix_prev_close,
         week_range=None, nifty_week_pct=None, evidence=None):
    """{state, state_colour, band, headline, read, read_text, change_line, hour_line, range_line,
    caveats, evidence, evidence_line}. Pure — every input is passed in; compose_read() gathers
    them. `evidence` is the {n, up, down, avg_next_pct, scored} for the state (or None)."""
    p, pp, p1 = _f(pcr), _f(pcr_prev_day), _f(pcr_1h_ago)
    state = read_state(p, pp, nifty_day_pct, vix_now, vix_prev_close, p1)
    band = read_band(p)
    caveats = []
    if p is None:
        return {"state": None, "state_colour": "mut", "band": band, "headline": "No PCR reading yet.",
                "read": ["The put and call counts have not arrived.", "Check again on the next tick."],
                "read_text": "", "change_line": None, "hour_line": None, "range_line": None,
                "caveats": [], "evidence": evidence, "evidence_line": None}
    if state is None:
        headline, body = ("PCR is %.2f. The day read needs yesterday too." % p,
                          ["Yesterday's PCR or the Nifty move is missing.", "Only the band can be read today."])
        caveats.append("Day inputs missing; band read only.")
    else:
        headline, body = _STATE_TEXT[state]
    if vix_now is None or vix_prev_close is None:
        caveats.append("India VIX was not available; treated as flat.")
    para = _EXPLAIN + body + band_text(band, nifty_week_pct)
    # change vs yesterday, in words with the numbers
    if pp is not None:
        d = p - pp
        verb = "rose" if d > PCR_MOVE_CUT else ("fell" if d < -PCR_MOVE_CUT else "held")
        change_line = ("Put buying %s today: %.2f to %.2f." % (verb, pp, p)) if verb != "held" \
            else ("Put buying held today at %.2f." % p)
    else:
        change_line = "No reading for yesterday."
    if p1 is not None:
        d1 = p - p1
        hour_line = ("Last hour: %s, %.2f to %.2f." % (
            "up" if d1 > PCR_MOVE_CUT else ("down" if d1 < -PCR_MOVE_CUT else "flat"), p1, p))
    else:
        hour_line = None
    range_line = ("7-day range %.2f to %.2f." % (week_range[0], week_range[1])) \
        if (week_range and week_range[0] is not None and week_range[1] is not None) else None
    ev = evidence or {"n": 0, "up": 0, "down": 0, "avg_next_pct": None, "scored": False}
    return {"state": state, "state_colour": _STATE_COLOUR.get(state, "mut"), "band": band,
            "headline": headline, "read": para, "read_text": " ".join(para),
            "change_line": change_line, "hour_line": hour_line, "range_line": range_line,
            "caveats": caveats, "evidence": ev,
            "evidence_line": evidence_line(ev["n"], ev["up"], ev["down"], ev.get("avg_next_pct"))}


# ── history: the same rule over pcr_daily x raw_prices x INDIAVIX ─────────────────────────

_OWN_CLOSE_SYM = {"NIFTY": "NIFTY50", "BANKNIFTY": "BANKNIFTY"}


def evidence(cur, underlying="NIFTY", today=None):
    """{by_state: {state: {n, up, down, avg_next_pct, scored}}, by_band: {...}, sessions, from, to}.
    Cached per (underlying, date). NULL inputs skip a session; nothing is coerced to 0."""
    from datetime import date as _date, timedelta as _td
    today = today or _date.today()
    key = (underlying, today)
    if key in _EVIDENCE_CACHE:
        return _EVIDENCE_CACHE[key]
    own = _OWN_CLOSE_SYM.get(underlying, "NIFTY50")
    cur.execute("""SELECT price_date, pcr FROM pcr_daily
                   WHERE underlying=%s AND pcr IS NOT NULL AND quality='ok'
                   ORDER BY price_date DESC LIMIT %s""", (underlying, EVIDENCE_SESSIONS + 5))
    prow = [(r[0], float(r[1])) for r in cur.fetchall()]
    prow.reverse()
    out = {"by_state": {}, "by_band": {}, "sessions": 0, "from": None, "to": None}
    if len(prow) < 2:
        _EVIDENCE_CACHE[key] = out
        return out
    start = prow[0][0] - _td(days=10)
    cur.execute("SELECT price_date, close FROM raw_prices WHERE symbol='NIFTY50' AND price_date >= %s ORDER BY price_date", (start,))
    nifty = [(r[0], float(r[1])) for r in cur.fetchall() if r[1] is not None]
    if own == "NIFTY50":
        ownc = nifty
    else:
        cur.execute("SELECT price_date, close FROM raw_prices WHERE symbol=%s AND price_date >= %s ORDER BY price_date", (own, start))
        ownc = [(r[0], float(r[1])) for r in cur.fetchall() if r[1] is not None]
    cur.execute("""SELECT DISTINCT ON (ts::date) ts::date AS d, close FROM intraday_prices
                   WHERE symbol='INDIAVIX' AND ts >= %s ORDER BY ts::date, ts DESC""", (start,))
    vix = {r[0]: float(r[1]) for r in cur.fetchall() if r[1] is not None}
    nifty_prev = {}
    for i in range(1, len(nifty)):
        nifty_prev[nifty[i][0]] = (nifty[i][1], nifty[i - 1][1])
    own_idx = {d: i for i, (d, _c) in enumerate(ownc)}
    vix_dates = sorted(vix)
    vix_prev = {vix_dates[i]: (vix[vix_dates[i]], vix[vix_dates[i - 1]]) for i in range(1, len(vix_dates))}

    def _bucket(table, k, nxt):
        b = table.setdefault(k, {"n": 0, "up": 0, "down": 0, "_sum": 0.0})
        b["n"] += 1
        b["_sum"] += nxt
        if nxt > 0:
            b["up"] += 1
        elif nxt < 0:
            b["down"] += 1

    sessions = 0
    for i in range(1, len(prow)):
        d, p = prow[i]
        pd_, pp = prow[i - 1]
        if d not in nifty_prev or d not in own_idx or own_idx[d] + 1 >= len(ownc):
            continue
        c, cp = nifty_prev[d]
        nd = (c / cp - 1.0) * 100.0
        oc = ownc[own_idx[d]][1]
        onext = ownc[own_idx[d] + 1][1]
        nxt = round((onext / oc - 1.0) * 100.0, 3)
        vn, vp = vix_prev.get(d, (None, None))
        st = read_state(p, pp, nd, vn, vp)
        if st is None:
            continue
        sessions += 1
        out["from"] = out["from"] or str(d)
        out["to"] = str(d)
        _bucket(out["by_state"], st, nxt)
        _bucket(out["by_band"], read_band(p), nxt)
        if sessions >= EVIDENCE_SESSIONS:
            break
    for table in (out["by_state"], out["by_band"]):
        for k, b in table.items():
            b["avg_next_pct"] = round(b.pop("_sum") / b["n"], 2) if b["n"] else None
            b["scored"] = b["n"] >= EVIDENCE_MIN
    out["sessions"] = sessions
    _EVIDENCE_CACHE[key] = out
    return out


# ── live inputs + the option-price line ────────────────────────────────────────────────────

_OPT_CACHE = {}          # underlying -> (monotonic ts, payload)
_OPT_TTL = 300


def option_price(underlying="NIFTY"):
    """The D-card premium rule on the index ATM row (deriv_metrics.strike_chain, index path).
    Cached 5 minutes. None when the chain has no ATM row — said, not guessed."""
    import time as _t
    hit = _OPT_CACHE.get(underlying)
    if hit and (_t.monotonic() - hit[0]) < _OPT_TTL:
        return hit[1]
    out = None
    try:
        import deriv_metrics
        d = deriv_metrics.strike_chain(underlying)
        atm = next((r for r in (d.get("strikes") or []) if r.get("atm")), None)
        if atm:
            def _leg(o, name):
                if not o or o.get("ltp") is None:
                    return None
                return {"ltp": o.get("ltp"), "fair": o.get("fair"), "tag": o.get("tag"), "ratio": o.get("ratio"),
                        "line": ("ATM %s %s vs fair %s - %s %sx." % (
                            name, o.get("ltp"), o.get("fair") if o.get("fair") is not None else "?",
                            o.get("tag") or "?", o.get("ratio") if o.get("ratio") is not None else "?"))}
            out = {"strike": atm.get("strike"), "ce": _leg(atm.get("ce"), "call"), "pe": _leg(atm.get("pe"), "put"),
                   "spot": d.get("spot"), "expiry": d.get("expiry"), "days_to_expiry": d.get("days_to_expiry"),
                   "rv20": d.get("rv20"), "source": d.get("source", "fyers"),
                   "header": "Spot %s · expiry %s (%sd) · RV20 %s%%" % (
                       d.get("spot"), d.get("expiry"), d.get("days_to_expiry"), d.get("rv20")),
                   "footer": "Fair = what the option should cost from recent moves.",
                   "cuts": "EXPENSIVE above fair +25%. REASONABLE 0 to +25%. CHEAP below fair."}
    except Exception as e:
        out = {"error": str(e)[:160]}
    _OPT_CACHE[underlying] = (_t.monotonic(), out)
    return out


def writer_read(opt, pcr, pcr_prev_day, band, state, evidence=None):
    """cc#1740 (founder 06-Sep: "keep pcr commentary crisp and sell side perspective"). The (i)
    sheet's commentary, GENERATED from the live numbers for the reader's actual seat — Scorr's index
    engine is a net option WRITER. Order: (1) the writer's number first — ATM put and call against
    fair, as the multiples already computed, fair defined once in a clause; (2) what that means for a
    writer, branched on the PUT's pricing tag so cheap premium flips the framing to the buyer;
    (3) PCR in one or two lines — level, change, band, and an honest verdict that a NEUTRAL /
    unscored day carries no direction. No textbook paragraph (the definitions live in `help`), no
    instruction to trade, short sentences. Pure: every input is passed in, so both branches can be
    proven with fixtures (python pcr_mood.py)."""
    out = []
    p, pp = _f(pcr), _f(pcr_prev_day)
    pe = (opt or {}).get("pe") if isinstance(opt, dict) else None
    ce = (opt or {}).get("ce") if isinstance(opt, dict) else None

    def _leg(o, name):
        if not o or o.get("ltp") is None:
            return None
        r = _f(o.get("ratio"))
        if o.get("fair") is None or r is None:
            return "%s %s, fair n/a" % (name, o.get("ltp"))
        return "%s %s vs fair %s (%.2fx)" % (name, o.get("ltp"), o.get("fair"), r)

    put_line, call_line = _leg(pe, "put"), _leg(ce, "call")
    if put_line or call_line:
        out.append("ATM " + "; ".join([x for x in (put_line, call_line) if x])
                   + ". Fair = what the last 20 sessions' moves justify.")
        lead = pe if (pe and pe.get("ltp") is not None) else ce
        tag = str((lead or {}).get("tag") or "").upper()
        r = _f((lead or {}).get("ratio"))
        if tag == "EXPENSIVE" or (not tag and r is not None and r >= 1.25):
            out += ["Premium is above the historical cost of the risk, which favours writing while the range holds.",
                    "An edge, not a forecast: it pays only if the move stays away, and one gap can take a month of premium."]
        elif tag == "CHEAP" or (not tag and r is not None and r < 1.0):
            out += ["Premium is below the historical cost of the risk, which favours the buyer and warns the writer off.",
                    "Cheap premium does not pay for the move it insures."]
        elif tag or r is not None:
            out += ["Premium is about the historical cost of the risk; no edge either way."]
    else:
        out.append("ATM option prices are not available this tick, so there is no premium read.")

    if p is None:
        out.append("No PCR reading yet.")
        return out
    if pp is not None:
        d = p - pp
        verb = "up from" if d > PCR_MOVE_CUT else ("down from" if d < -PCR_MOVE_CUT else "flat against")
        out.append("PCR %.2f, %s %.2f, band %s." % (p, verb, pp, band or "n/a"))
    else:
        out.append("PCR %.2f, band %s; no reading for yesterday." % (p, band or "n/a"))
    ev = evidence or {}
    scored = bool(ev.get("scored")) and int(ev.get("n") or 0) >= EVIDENCE_MIN
    if state == NEUTRAL_STATE or state is None or not scored:
        out.append("No directional signal today.")
    else:
        head = _STATE_TEXT.get(state, ("", []))[0]
        if head:
            out.append(head + " A record, not a forecast.")
    return out


HELP_LINE = "PCR = put buying against call buying; a put is a bet on a fall."


def compose_read(cur, underlying, pcr, as_of=None):
    """Gather the live inputs and return read() + the evidence tables + the option line."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    ist = _tz(_td(hours=5, minutes=30))
    now = _dt.now(ist)
    today = now.date()
    as_of_date = today
    try:
        if as_of and len(str(as_of)) >= 10:
            as_of_date = _dt.strptime(str(as_of)[:10], "%Y-%m-%d").date()
    except Exception:
        as_of_date = today
    cur.execute("SELECT pcr FROM pcr_daily WHERE underlying=%s AND pcr IS NOT NULL AND price_date < %s ORDER BY price_date DESC LIMIT 1",
                (underlying, as_of_date))
    r = cur.fetchone()
    pcr_prev = _f(r[0]) if r else None
    cur.execute("""SELECT pcr_total FROM pcr_intraday WHERE underlying=%s AND pcr_total IS NOT NULL
                   AND ts >= %s AND ts <= %s ORDER BY ts DESC LIMIT 1""",
                (underlying, _dt.combine(today, _dt.min.time()), now.replace(tzinfo=None) - _td(minutes=60)))
    r = cur.fetchone()
    pcr_1h = _f(r[0]) if r else None
    nifty_day = None
    wk = None
    try:
        from nifty_dwm import live_nifty_dwm
        nifty_day, wk, _m, _src = live_nifty_dwm(cur, "NIFTY50")
    except Exception:
        nifty_day = None
    cur.execute("""SELECT close FROM intraday_prices WHERE symbol='INDIAVIX' AND ts >= %s ORDER BY ts DESC LIMIT 1""",
                (_dt.combine(today, _dt.min.time()),))
    r = cur.fetchone()
    vix_now = _f(r[0]) if r else None
    cur.execute("""SELECT close FROM intraday_prices WHERE symbol='INDIAVIX' AND ts < %s ORDER BY ts DESC LIMIT 1""",
                (_dt.combine(today, _dt.min.time()),))
    r = cur.fetchone()
    vix_prev = _f(r[0]) if r else None
    cur.execute("SELECT MIN(pcr), MAX(pcr) FROM (SELECT pcr FROM pcr_daily WHERE underlying=%s AND pcr IS NOT NULL ORDER BY price_date DESC LIMIT 7) t",
                (underlying,))
    r = cur.fetchone()
    week_range = (_f(r[0]), _f(r[1])) if r else None
    ev_tables = evidence(cur, underlying, today)
    state_now = read_state(pcr, pcr_prev, nifty_day, vix_now, vix_prev, pcr_1h)
    ev = (ev_tables["by_state"].get(state_now) if state_now else None) or \
         {"n": 0, "up": 0, "down": 0, "avg_next_pct": None, "scored": False}
    out = read(pcr, pcr_prev, pcr_1h, nifty_day, vix_now, vix_prev, week_range, wk, ev)
    out["inputs"] = {"pcr": _f(pcr), "pcr_prev_day": pcr_prev, "pcr_1h_ago": pcr_1h,
                     "nifty_day_pct": _f(nifty_day), "nifty_week_pct": _f(wk),
                     "vix_now": vix_now, "vix_prev_close": vix_prev,
                     "week_range": list(week_range) if week_range else None}
    out["evidence_by_state"] = ev_tables["by_state"]
    out["evidence_by_band"] = ev_tables["by_band"]
    out["evidence_sessions"] = ev_tables["sessions"]
    out["evidence_window"] = {"from": ev_tables["from"], "to": ev_tables["to"], "max_sessions": EVIDENCE_SESSIONS}
    out["option_price"] = option_price(underlying)
    # cc#1740: the commentary the sheet prints is the writer's read, generated from the live numbers
    # (premium first, then PCR). The pre-1740 paragraph stays under `legacy_read` for parity checks;
    # change_line folds into the read (the PCR line carries level + change + band).
    out["legacy_read"] = out["read"]
    out["read"] = writer_read(out["option_price"], pcr, pcr_prev, out["band"], out["state"], ev)
    out["read_text"] = " ".join(out["read"])
    out["change_line"] = None
    out["help"] = HELP_LINE
    out["framework_only"] = not ev["scored"]
    out["note"] = "Descriptive read only. Not a trading signal."
    return out

# ══════════════════════════════════════════════════════════════════════════════════════════
# cc#1797 · PCR_WRITER_CONFIDENCE_READ_V1 (session_log 39783) — the (i) read behind the app PCR card.
# Supersedes the cc#1576/cc#1740 `interpret` read for the APP sheet (that block stays above for the
# web Index Intel popover, which this card does not touch).
#
# FOUR RULES, each side INDEPENDENT, each needing a day-over-day rise in that side's OI from
# pcr_daily PLUS a price condition from the ONE shared return function (nifty_dwm.live_nifty_dwm —
# the same call that feeds the Home DAY/WK chips through v8_endpoints.market_mood):
#   put_confident   put OI up AND day > 0
#   put_cautious    put OI up AND day < 0 AND week < 0
#   call_confident  call OI up AND day < 0
#   call_cautious   call OI up AND day > 0 AND week > 0
# A side whose OI did not rise, or whose day/week do not cleanly satisfy one rule, says NOTHING.
# There is no nearer-bucket fallback: silence is a correct output for a side.
#
# ROLLOVER. pcr_daily put_oi/call_oi is the CURRENT MONTHLY series only, so the level resets when
# the series rolls (25-Aug-2026 143,020,085 -> 26-Aug 32,949,850 is the roll, not unwinding). The
# cycle of a row is pcr_backfill._current_expiry(price_date) — the engine's own last-Tuesday rule,
# the same one worker/fyers_feed.current_expiry builds the option symbols from (verified against
# option_chain.expiry = 2026-09-29 for every Sep row). Different cycle => today has NO PRIOR and
# no side can fire. No second rollover detector.
#
# DISPLAY RULE (a gate, founder-ruled twice): the surface prints ONLY the sentence(s) that fired,
# put first, plus the footer and the as-of. The `inputs` block below exists for Fable's
# verification query and is never rendered.
# ══════════════════════════════════════════════════════════════════════════════════════════

PUT_CONFIDENT = ("Put writers are confident. They expect the market to hold or rise, "
                 "so they are comfortable selling puts.")
PUT_CAUTIOUS = ("Put writers are turning cautious. Rising put buying here looks like protection "
                "for existing long positions, not confident writing.")
CALL_CONFIDENT = ("Call writers are confident. They expect the market to hold or fall, "
                  "so they are comfortable selling calls.")
CALL_CAUTIOUS = ("Call writers are turning cautious. Rising call buying here looks like protection "
                 "for existing short positions, not confident writing.")
NO_SHIFT = "No clear shift in put or call positioning today."
CONFIDENCE_FOOTER = "Describes today's mood, not a forecast."
CONFIDENCE_SPEC = "PCR_WRITER_CONFIDENCE_READ_V1 (session_log 39783)"
_CONF_TEXT = {("put", "confident"): PUT_CONFIDENT, ("put", "cautious"): PUT_CAUTIOUS,
              ("call", "confident"): CALL_CONFIDENT, ("call", "cautious"): CALL_CAUTIOUS}


def confidence_side(side, oi, oi_prev, day_pct, week_pct):
    """'confident' | 'cautious' | None for one side. Pure. None whenever an input is missing, the
    OI did not RISE (equal is not a rise), or neither rule is cleanly met."""
    o, op, d, w = _f(oi), _f(oi_prev), _f(day_pct), _f(week_pct)
    if o is None or op is None or d is None or o <= op:
        return None
    if side == "put":
        if d > 0:
            return "confident"
        if d < 0 and w is not None and w < 0:
            return "cautious"
        return None
    if side == "call":
        if d < 0:
            return "confident"
        if d > 0 and w is not None and w > 0:
            return "cautious"
        return None
    return None


def confidence_read(put_oi, put_oi_prev, call_oi, call_oi_prev, day_pct, week_pct, comparable=True):
    """{put, call, sentences, text}. `comparable` False (rollover, or no prior row) silences both
    sides. sentences is [] with text=NO_SHIFT when nothing fired; put sentence first when both do."""
    put = confidence_side("put", put_oi, put_oi_prev, day_pct, week_pct) if comparable else None
    call = confidence_side("call", call_oi, call_oi_prev, day_pct, week_pct) if comparable else None
    sentences = []
    if put:
        sentences.append(_CONF_TEXT[("put", put)])
    if call:
        sentences.append(_CONF_TEXT[("call", call)])
    return {"put": put, "call": call, "sentences": sentences,
            "text": None if sentences else NO_SHIFT}


def _expiry_cycle(d):
    """The engine's own monthly-series rule (pcr_backfill._current_expiry). None if unavailable —
    an unknown cycle is treated as not comparable, never guessed."""
    try:
        from pcr_backfill import _current_expiry
        return _current_expiry(d)
    except Exception:
        return None


def compose_confidence(cur, underlying="NIFTY"):
    """Gather the live inputs for the latest pcr_daily row and return confidence_read() plus the
    provenance a verifier needs (dates, cycles, the return source, the raw inputs)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    ist = _tz(_td(hours=5, minutes=30))
    today = _dt.now(ist).date()
    out = {"spec": CONFIDENCE_SPEC, "underlying": underlying, "date": None, "prev_date": None,
           "cycle": None, "prev_cycle": None, "comparable": False, "reason": None,
           "put": None, "call": None, "sentences": [], "text": None,
           "footer": CONFIDENCE_FOOTER, "as_of": None, "returns_source": None, "inputs": None}
    cur.execute("""SELECT price_date, put_oi, call_oi, computed_at FROM pcr_daily
                   WHERE underlying=%s AND put_oi IS NOT NULL AND call_oi IS NOT NULL
                     AND quality='ok'
                   ORDER BY price_date DESC LIMIT 2""", (underlying,))
    rows = cur.fetchall()
    if not rows:
        out["text"] = "No put and call positioning data yet."
        out["reason"] = "no pcr_daily row"
        return out
    d, put_oi, call_oi, computed_at = rows[0]
    out["date"] = str(d)
    out["as_of"] = computed_at.strftime("%Y-%m-%d %H:%M") if computed_at else str(d)
    prev = rows[1] if len(rows) > 1 else None
    put_prev = call_prev = None
    if prev:
        dp, put_prev, call_prev, _c = prev
        out["prev_date"] = str(dp)
        cyc, cycp = _expiry_cycle(d), _expiry_cycle(dp)
        out["cycle"] = str(cyc) if cyc else None
        out["prev_cycle"] = str(cycp) if cycp else None
        if cyc is None or cycp is None:
            out["reason"] = "expiry cycle unknown; no prior to compare"
        elif cyc != cycp:
            out["reason"] = "first session of a new expiry cycle; the series reset, so no prior to compare"
        else:
            out["comparable"] = True
    else:
        out["reason"] = "no prior pcr_daily row"
    # Day/week for THE ROW'S DATE through the one shared function. A past date reads the EOD
    # closes anchored on that date; today reads live in session. Outside the session on a
    # trading day raw_prices may not yet hold today's close (the EOD engine runs ~21:00 IST), and
    # the fallback would then describe YESTERDAY's move — so the read checks the newest close
    # date and stays silent rather than fire on the wrong day.
    day_pct = week_pct = None
    src = None
    try:
        from nifty_dwm import live_nifty_dwm
        day_pct, week_pct, _m, src = live_nifty_dwm(cur, "NIFTY50", as_of=d)
        if src == "eod":
            cur.execute("SELECT MAX(price_date) FROM raw_prices WHERE symbol='NIFTY50' AND price_date <= %s", (d,))
            r = cur.fetchone()
            if not r or r[0] != d:
                day_pct = week_pct = None
                out["reason"] = (out["reason"] or "") + ("; " if out["reason"] else "") + \
                    "closing price for %s has not arrived yet" % d
    except Exception as e:
        out["reason"] = (out["reason"] or "") + ("; " if out["reason"] else "") + ("returns unavailable: " + str(e)[:120])
    out["returns_source"] = src
    out["inputs"] = {"put_oi": put_oi, "put_oi_prev": put_prev, "call_oi": call_oi, "call_oi_prev": call_prev,
                     "day_pct": _f(day_pct), "week_pct": _f(week_pct), "today": str(today)}
    out.update(confidence_read(put_oi, put_prev, call_oi, call_prev, day_pct, week_pct, out["comparable"]))
    if out["comparable"] and day_pct is None:
        out["text"] = "The market move for this day is not available yet, so there is no read."
    return out


if __name__ == "__main__":
    # Unit table from the card (P2). Run: python pcr_mood.py
    cases = [(1.46, -1.36, GREED), (1.55, -1.36, CAUTIOUS), (1.55, 0.2, EXTREME_GREED),
             (1.55, None, GREED), (1.50, -1.36, GREED), (1.51, -1.0, CAUTIOUS),
             (0.49, None, EXTREME_FEAR), (0.5, None, CAUTIOUS), (0.8, None, NEUTRAL),
             (1.0, None, GREED), (None, -1.36, None)]
    ok = True
    for pcr, wk, want in cases:
        got = pcr_mood(pcr, wk)
        flag = "PASS" if got["label"] == want else "FAIL"
        ok = ok and flag == "PASS"
        print(f"{flag}  pcr={pcr!s:>5} week={wk!s:>6} -> {got['label']!s:<13} band={got['band']!s:<4}"
              f" top={got['dial_segments'][-1]['colour'] if got['dial_segments'] else '-':<5}"
              f" reason={got['reason'] or ''}{(' note=' + got['note']) if got['note'] else ''}")
    # cc#1797: the 39783 clean window (pcr_daily NIFTY x raw_prices NIFTY50, 26-Aug..04-Sep-2026),
    # day = t vs t-1 close, week = t vs t-5 close — the EOD formula nifty_dwm uses. 26-Aug is the
    # rollover row: comparable=False, both sides silent by construction.
    window = [  # (date, put_oi, call_oi, day_pct, week_pct, comparable, want_put, want_call)
        ("2026-08-26", 32949850, 31405855, -0.521, -0.099, False, None, None),
        ("2026-08-27", 35949990, 33848620, -0.483, -0.582, True, "cautious", "confident"),
        ("2026-08-28", 36776365, 33532395, 0.352, -0.315, True, "confident", None),
        ("2026-08-31", 38665270, 35069905, -0.394, -0.573, True, "cautious", "confident"),
        ("2026-09-01", 46135015, 36868910, -0.102, -1.146, True, "cautious", "confident"),
        ("2026-09-02", 46945990, 32481280, -0.588, -1.212, True, "cautious", None),
        ("2026-09-03", 47240920, 35192495, -0.171, -0.902, True, "cautious", "confident"),
        ("2026-09-04", 46698270, 34834410, 0.102, -1.150, True, None, None),
    ]
    prev_p, prev_c = 143020085, 137664735       # 25-Aug, the old series' last row
    for d, p_oi, c_oi, dp, wp, comp, wp_, wc_ in window:
        got = confidence_read(p_oi, prev_p, c_oi, prev_c, dp, wp, comp)
        flag = "PASS" if (got["put"], got["call"]) == (wp_, wc_) else "FAIL"
        ok = ok and flag == "PASS"
        print(f"{flag}  {d} put={got['put']!s:<9} call={got['call']!s:<9} lines={len(got['sentences'])} {'' if got['sentences'] else got['text']}")
        prev_p, prev_c = p_oi, c_oi
    print("ALL PASS" if ok else "FAILURES")
    raise SystemExit(0 if ok else 1)
