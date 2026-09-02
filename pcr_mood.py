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
    out.update({"underlying": underlying, "basis": basis, "as_of": as_of, "spec": "session_log 36200"})
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
