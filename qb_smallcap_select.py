"""
qb_smallcap_select.py — Small Cap V2 selection/proposal engine.
cc#554, canonical spec session_log id=6094 (SMALL_CAP_V2_LOCKED).

DRY-RUN ONLY + ENTRY-ONLY. Proposes the quarterly-rebalance NEW ENTRIES for the `small_cap`
basket. It NEVER proposes exits — existing holdings live under the unchanged exit rules
(HS1 -20% / HS2 -10% vs Nifty / quarterly theme+GVM screen), so a tightened entry gate can
never one-shot flip the whole book. Execution stays founder-confirmed (same as alpha propose).
Reads screener_raw / gvm_history / sector_ratings only.

Spec id=6094 rules encoded here:
  universe    small cap = mcap rank > 250 (screener_raw.market_cap, ranked desc)
  hard gates  GVM >= 8.0 AND V >= 7.5 AND (gvm_now - gvm_180d_ago) > +0.5
              AND segment avg GVM >= 6.0 (sector_ratings.mcap_weighted_gvm, stage-1)
              dGVM 180d lookback from gvm_history: nearest score in [as_of-200, as_of-180]
  theme       qualifier's segment must map to one of the 8 themes (else excluded)
  sizing      N qualifiers, capped at 20, ranked by gvm_score:
                10 <= N <= 20 -> equal weight Rs 5L / N, fully invested
                N < 10        -> Rs 50,000/name (5L/10 ceiling), remaining slots CASH
  exit        UNCHANGED (HS1/HS2/quarterly screen) — NOT touched here (entry-only)
"""
import os
import logging

import psycopg

log = logging.getLogger("qb_smallcap_select")

BASKET      = "small_cap"
MAX_STOCKS  = 20
BRAKE_N     = 10          # below this, size at 5L/10 and hold the rest in cash
CAPITAL     = 500000.0
GVM_MIN     = 8.0
V_MIN       = 7.5
DGVM_MIN    = 0.5         # strict > : (gvm_now - gvm_180d_ago) > 0.5
SEGAVG_MIN  = 6.0         # segment mcap-weighted avg GVM floor (stage-1)

# Segment -> theme map for the 8 Small Cap V2 themes (spec id=6094). Each segment maps to at most
# ONE theme. The 3 NEW themes (Gold & Jewellery / Consumption & Lifestyle / Cement & Industrial
# Build-out) follow the spec's explicit segment groupings; Castings & Forgings + steel/engineering
# go to Cement & Industrial per spec (not EV Auto). Segment strings are exact gvm_history values.
THEME_SEGMENTS = {
    "Energy Transition & Grid": [
        "Electrical Equipment Small", "Electrical Cables", "Capital Goods - Heavy Electrical",
        "Electronics - Heavy Electrical & Industrial", "Power Generation - Large", "Power - Mid",
        "Power - Small", "Power Services & Trading", "Solar & Renewable Equipment Small",
        "Renewable Energy - Mid", "Renewable Energy - Small",
    ],
    "EV Auto Tech & Precision": [
        "Auto OEM", "Auto - Drivetrain & Precision", "Auto - Engines & Thermal",
        "Auto - Wiring & Electricals", "Auto - Body & Stampings",
    ],
    "Financial Inclusion & Digital Finance": [
        "Small Finance Banks", "Microfinance & MSME", "MSME Finance - Large",
        "MSME Finance - Mid", "MSME Finance - Small", "Housing Finance",
    ],
    "Healthcare & Life Sciences": [
        "Pharma - Bulk & API", "Pharma - Formulations", "Pharma - Large Formulations",
        "Pharma - Mid Formulations", "Pharma - Small", "Pharma - Micro", "CDMO & Contract Mfg",
        "Hospitals - Large", "Hospitals - Mid & Small", "Diagnostics & Healthcare Services",
    ],
    "Digital India & Tech": [
        "IT - Large", "IT - Mid", "IT - Small", "IT - Micro", "Internet & Digital Small",
        "Digital Aggregators & E-Commerce", "Broadcasting & OTT", "Entertainment, Content & Digital",
        "Electronics - Consumer & Smart", "Telecom Equipment & Services", "Telecom Services",
    ],
    "Gold & Jewellery Consumption": [
        "Gems & Jewellery - Large", "Gems & Jewellery - Small",
    ],
    "Consumption & Lifestyle": [
        "FMCG - Large", "FMCG - Small", "Retail - Large", "Retail - Mid", "Beverages & Spirits",
        "Packaged Foods & Dairy", "Home Textiles & Technical", "Textiles - Large",
        "Textiles Smallcap", "Footwear", "Consumer Plastics & Others", "Consumer Durables - Large",
        "Consumer Durables - Small", "Consumer Goods Trading",
    ],
    "Cement & Industrial Build-out": [
        "Cement - Small", "Cement - Large & Mid", "Building Materials - Glass, Ceramics & Ply",
        "Steel Tubes & Wires", "Pipes & Tubes", "Engineering - Industrial Mfg A",
        "Engineering - Industrial Mfg B", "Engineering - EPC Civil Small", "Engineering - EPC Mid",
        "Engineering - EPC Small", "Engineering - Large", "Castings & Forgings",
        "Iron and Steel - Smallcap", "Steel - Mid & Small", "Steel Products & Misc",
        "Integrated Steel - Large",
    ],
}
# reverse lookup segment -> theme (built once)
_SEG_TO_THEME = {seg: theme for theme, segs in THEME_SEGMENTS.items() for seg in segs}

_QUALIFY_SQL = """
WITH mcap AS (
    SELECT nse_code AS symbol,
           ROW_NUMBER() OVER (ORDER BY market_cap DESC NULLS LAST) AS mrank
    FROM screener_raw WHERE nse_code IS NOT NULL AND market_cap IS NOT NULL
),
latest AS (
    SELECT symbol, gvm_score, v_score, segment
    FROM gvm_history WHERE score_date = (SELECT MAX(score_date) FROM gvm_history)
),
dgvm AS (   -- gvm 180d ago: nearest gvm_history score in [as_of-200, as_of-180]
    SELECT DISTINCT ON (symbol) symbol, gvm_score AS gvm_180
    FROM gvm_history
    WHERE score_date BETWEEN %(asof)s::date - 200 AND %(asof)s::date - 180
    ORDER BY symbol, score_date DESC
),
segavg AS (
    SELECT segment, mcap_weighted_gvm AS seg_gvm
    FROM sector_ratings WHERE score_date = (SELECT MAX(score_date) FROM sector_ratings)
)
SELECT l.symbol, m.mrank, l.gvm_score, l.v_score, l.segment,
       (l.gvm_score - d.gvm_180) AS dgvm, sa.seg_gvm
FROM latest l
JOIN mcap m ON m.symbol = l.symbol
JOIN dgvm d ON d.symbol = l.symbol
LEFT JOIN segavg sa ON sa.segment = l.segment
WHERE m.mrank > 250
  AND l.gvm_score >= %(gvm)s
  AND l.v_score >= %(v)s
  AND (l.gvm_score - d.gvm_180) > %(dgvm)s
  AND COALESCE(sa.seg_gvm, 0) >= %(segavg)s
ORDER BY l.gvm_score DESC, l.symbol
"""


def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


def _f(v):
    return round(float(v), 3) if v is not None else None


def propose_rebalance(conn=None, as_of=None):
    """Small Cap V2 ENTRY proposal (dry-run, founder-confirmed to execute).

    Returns:
      {
        as_of, basket, capital, max_stocks,
        entries:      qualifiers (gates + theme + rank>250), capped at 20, gvm-desc; each
                      {symbol, mcap_rank, gvm, v, dgvm, segment, theme, seg_gvm, slot_value},
        n_qualified:  raw count passing all gates+theme (pre-cap),
        n_selected:   len(entries),
        slot_value:   Rs per name (5L/N normal, or 50k under the N<10 brake),
        cash_value:   uninvested cash (0 unless the N<10 brake fires),
        sizing_mode:  "equal_5L_div_N" | "brake_5L_div_10",
        holdings:     current open positions (for reference only — NOT flagged for exit),
        rules:        the encoded spec-6094 thresholds,
      }
    """
    own = conn is None
    if own:
        conn = _conn()
    if as_of is None:
        with conn.cursor() as cur:
            cur.execute("SELECT CURRENT_DATE")
            as_of = cur.fetchone()[0]
    try:
        with conn.cursor() as cur:
            cur.execute(_QUALIFY_SQL, {"asof": as_of, "gvm": GVM_MIN, "v": V_MIN,
                                       "dgvm": DGVM_MIN, "segavg": SEGAVG_MIN})
            cols = [c.name for c in cur.description]
            raw = [dict(zip(cols, r)) for r in cur.fetchall()]

            # theme constraint: keep only qualifiers whose segment maps to one of the 8 themes
            qualified = []
            for r in raw:
                theme = _SEG_TO_THEME.get(r["segment"])
                if not theme:
                    continue
                qualified.append({
                    "symbol": r["symbol"], "mcap_rank": int(r["mrank"]),
                    "gvm": _f(r["gvm_score"]), "v": _f(r["v_score"]),
                    "dgvm": _f(r["dgvm"]), "segment": r["segment"], "theme": theme,
                    "seg_gvm": _f(r["seg_gvm"]),
                })

            n_qualified = len(qualified)
            entries = qualified[:MAX_STOCKS]          # already gvm-desc; cap at 20
            n = len(entries)

            # sizing: N>=10 -> equal 5L/N fully invested; N<10 -> 5L/10 (50k) per name, rest cash
            if n == 0:
                slot_value, cash_value, mode = 0.0, CAPITAL, "empty"
            elif n < BRAKE_N:
                slot_value = round(CAPITAL / BRAKE_N, 2)          # 50,000
                cash_value = round(CAPITAL - slot_value * n, 2)
                mode = "brake_5L_div_10"
            else:
                slot_value = round(CAPITAL / n, 2)
                cash_value = 0.0
                mode = "equal_5L_div_N"
            for e in entries:
                e["slot_value"] = slot_value

            # current holdings — reference only, NOT flagged for exit (entry-only per spec)
            cur.execute("SELECT symbol FROM quant_paper_positions "
                        "WHERE basket_name=%s AND status='open'", (BASKET,))
            holdings = [row[0] for row in cur.fetchall()]

        return {
            "as_of": str(as_of),
            "basket": BASKET,
            "capital": CAPITAL,
            "max_stocks": MAX_STOCKS,
            "entries": entries,
            "n_qualified": n_qualified,
            "n_selected": n,
            "slot_value": slot_value,
            "cash_value": cash_value,
            "sizing_mode": mode,
            "holdings": holdings,
            "entry_only": True,
            "rules": {
                "universe": "small cap = mcap rank > 250 (screener_raw)",
                "hard_gates": "GVM>=8.0 AND V>=7.5 AND dGVM_180d>+0.5 AND segment_avg_GVM>=6.0",
                "dgvm_lookback": "gvm_history nearest score in [as_of-200, as_of-180]",
                "seg_avg_source": "sector_ratings.mcap_weighted_gvm",
                "themes": list(THEME_SEGMENTS.keys()),
                "sizing": f"N<={MAX_STOCKS} equal 5L/N; N<{BRAKE_N} -> 5L/{BRAKE_N} per name, rest cash",
                "exit": "UNCHANGED (HS1 -20% / HS2 -10% vs Nifty / quarterly screen); entry-only engine",
            },
        }
    finally:
        if own:
            conn.close()
