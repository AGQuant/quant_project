"""mobile_home_derivatives.py — cc#1875 phase 1: Home 3rd card, DERIVATIVES (index option chain
summary, NIFTY + BANKNIFTY only). ADDITIVE — the existing home cards (My Portfolio / SmartGain,
the cc#1867 Approved Trades slider, Card 1/2/3 of the mood swipe deck) are untouched. Phase 2 (IV
percentile) is NOT built here — gated on cc#1863 proving bg_option_iv_daily's first run tonight
(ENGINE_LIVENESS_RULE, session_log 13829): the job is registered in scheduler_master but
last_run_at is NULL, so option_iv_daily is nine days stale and there is no live IV source to
derive a percentile from without inventing a number.

ONE SOURCE, NOT A SECOND COMPOSER: this endpoint is a thin wrapper over oi_structure.oi_structure(),
the SAME composer Home's own Max Pain (i) card and the web Index Intel card already read (cc#1575).
PCR by OI, the OI walls, max pain, expiry and spot are ALL that function's own numbers — nothing
here recomputes any of them a second time. The raw CE/PE OI totals the card also asks for
(item 2) were already summed inside oi_structure.structure_from_rows() but never returned before
this card needed them; that function now returns them (two new, purely additive dict keys — see
oi_structure.py), and this file just forwards what the composer already computed.

DOES NOT DUPLICATE THE HOME TAPE'S PCR (card's own do_not_touch clause). The tape's NIFTY PCR
comes from pcr_mood.latest_pcr() — a different, banded composer built for the mood-word framing
(EXTREME GREED / CAUTIOUS / etc., cc#1568), reading the LATEST usable option_chain tick with its
own thin-snapshot guard. This card's "PCR by OI" is oi_structure()'s own chain-wide ratio
(total PE OI / total CE OI over the near-expiry chain at the LATEST_CHAIN_SQL tick) — a plainer
number, from the same underlying option_chain rows, already used elsewhere on Home for Max Pain.
The two are independently-existing composers over the same table, not something this card
introduces; reconciliation at spec time (09-Sep-2026 ~12:25 IST) confirmed both read the same
totals. What THIS card adds that the tape does NOT have at all: BANKNIFTY (the tape is NIFTY-only),
the raw OI totals, the OI walls (call wall / put wall + their OI), max pain, expiry and
days-to-expiry.

INDEX ONLY. option_chain holds NIFTY and BANKNIFTY only — 41 strikes each on the near expiry at
spec time, no stock options anywhere in that table. is_index_only + the note field say so
explicitly so no surface can present this as whole-market derivatives coverage.

AS-OF, NEVER SILENT. as_of is oi_structure()'s own tick timestamp (option_chain.ts of the chain
actually read), carried through unchanged — a client renders staleness from this, exactly as
every other live card on this app does (scorrAsofStamp), never a fabricated "now".

ITEM 5 ("tapping the card opens the fuller option chain view") is served from THIS SAME payload,
not a second round trip: each underlying also carries a `chain` array (one row per strike, CE/PE
OI, near expiry) alongside the summary fields, so the home card's tap-through sheet has something
to open without a second fetch. The per-strike rows are read with max_pain.LATEST_CHAIN_SQL — the
exact SQL oi_structure() and pcr_mood.live_pcr() already use for this same purpose — not a fourth
copy of the query.

READ-ONLY. Nothing here writes to option_chain, option_iv_daily, or any table the feed owns."""
import os

import psycopg
from fastapi import APIRouter

import max_pain
import oi_structure

router = APIRouter(tags=["mobile-home-derivatives"])
UNDERLYINGS = ("NIFTY", "BANKNIFTY")


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _chain_rows(cur, underlying):
    """Per-strike CE/PE OI for the near expiry, SAME query oi_structure()/live_pcr() read.
    Sorted by strike; empty list when the chain has nothing (matches oi_structure()'s own
    'no_data' handling, so the sheet can say the same thing the card already says)."""
    cur.execute(max_pain.LATEST_CHAIN_SQL, {"u": underlying})
    rows = cur.fetchall()
    return sorted(
        [{"strike": float(r[0]), "ce_oi": int(r[1] or 0), "pe_oi": int(r[2] or 0)} for r in rows],
        key=lambda x: x["strike"],
    )


@router.get("/api/mobile/home/derivatives")
def home_derivatives():
    rows = []
    with _conn() as conn, conn.cursor() as cur:
        for u in UNDERLYINGS:
            st = oi_structure.oi_structure(underlying=u)   # opens its own connection -- unchanged, untouched
            chain = _chain_rows(cur, u)
            rows.append({
                "underlying": u,
                "status": st.get("status"),
                "as_of": st.get("as_of"),
                "spot": st.get("spot"), "spot_basis": st.get("spot_basis"),
                "expiry": st.get("expiry"), "days_to_expiry": st.get("days_to_expiry"),
                "pcr": st.get("pcr"), "ce_oi": st.get("ce_oi"), "pe_oi": st.get("pe_oi"),
                "call_wall": st.get("call_wall"), "call_wall_oi": st.get("call_wall_oi"),
                "put_wall": st.get("put_wall"), "put_wall_oi": st.get("put_wall_oi"),
                "max_pain": st.get("max_pain"), "strikes": st.get("strikes"),
                "one_sided": st.get("one_sided"), "note": st.get("note"),
                "chain": chain,
            })
    return {
        "underlyings": rows,
        "is_index_only": True,
        "index_note": "Index options only — NIFTY and BANKNIFTY. No stock options in this table.",
        "spec": "cc#1875 phase 1 — option_chain summary, over oi_structure.oi_structure() (cc#1575)",
    }
