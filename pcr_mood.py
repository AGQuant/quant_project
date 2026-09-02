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
    """(pcr, basis, as_of) — basis LIVE when today has an intraday bar, else EOD."""
    cur.execute("""
        SELECT pcr_total, ts FROM pcr_intraday
        WHERE underlying=%s AND pcr_total IS NOT NULL
          AND ts::date = (NOW() AT TIME ZONE 'Asia/Kolkata')::date
        ORDER BY ts DESC LIMIT 1
    """, (underlying,))
    r = cur.fetchone()
    if r and r[0] is not None:
        return _f(r[0]), "LIVE", r[1].strftime("%Y-%m-%d %H:%M")
    cur.execute("""
        SELECT pcr, price_date FROM pcr_daily
        WHERE underlying=%s AND pcr IS NOT NULL
        ORDER BY price_date DESC LIMIT 1
    """, (underlying,))
    r = cur.fetchone()
    if r and r[0] is not None:
        return _f(r[0]), "EOD", str(r[1])
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
    out["framework_only"] = not ev["scored"]
    out["note"] = "Descriptive read only. Not a trading signal."
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
    print("ALL PASS" if ok else "FAILURES")
    raise SystemExit(0 if ok else 1)
