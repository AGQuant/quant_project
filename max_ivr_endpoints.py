"""
max_ivr_endpoints.py — cc#836 MAX IVR GUIDED CIO (P1).

Authoritative design chain (read in this order, per MAX_FRAMEWORK_INDEX_V1 session_log id=13823):
  13823 index -> 13822 MAX_IVR_TREE_V2 (canonical node list, supersedes 13820)
  -> 13816 MAX_IVR_ARCHITECTURE_V1 (mechanics) -> 851 economics.

What this module owns:
  * max_ivr_tree  — the config-driven node table. The UI renders THIS; the tree is never
                    hardcoded in HTML (13816 "never" list). Reorder/relabel = one SQL UPDATE,
                    zero deploy (the V13-presets pattern).
  * max_ivr_taps  — per-node telemetry. Feeds the future "★ Your Shortcuts" row on L0 and the
                    weekly improvisation cycle in 13823; the row itself is a later task.
  * GET  /api/max/ivr/tree  — active nodes, ordered.
  * POST /api/max/ivr/tap   — log a tap.

THE ECONOMICS INVARIANT (13823): a tappable leaf NEVER calls a paid model. Every leaf carries
action_params.q — a natural-language string that native_intent.classify() already resolves through
the existing Layer-0 handlers. That is deliberately the SAME mechanism the 50-card grid used, so
this build adds a navigation model, not a second execution path that could drift from the first.

STAGING. 13822 stages the work: stage 1 = the branch tree, stage 2 = a native card template for
every leaf, stage 3 = Gemini (DEFERRED — do not wire). Stage 1 shipped in cc#836 phase A; PHASE B
completes stage 2, so the stage-2 marker that made 41 leaves render "building this week" is retired.
Every leaf now answers. A leaf whose action_intent appears in max_native_cards.CARDS is flagged
has_card on this payload and dispatches to /api/max/card/{intent}; the rest still go through the
existing native query path. One flag, derived from the CARDS registry itself, so the tree and the
card layer cannot drift apart.

SEED IS ADDITIVE. seed_tree() inserts only nodes that are absent. It never UPDATEs and never
DELETEs, because max_ivr_tree is RUNTIME TRUTH (13823 sync rule) — a founder's SQL relabel or
reorder must survive the next deploy. Re-syncing the table to the doc is a deliberate SQL act,
never a side effect of a restart.
"""

import os
import json
import logging
from typing import Optional

import psycopg
from fastapi import APIRouter
from pydantic import BaseModel

log = logging.getLogger("scorr.max_ivr")
router = APIRouter()
_DB = os.getenv("DATABASE_URL", "")


def _conn():
    return psycopg.connect(_DB)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS max_ivr_tree (
    node_id       TEXT PRIMARY KEY,
    parent_id     TEXT,
    label         TEXT NOT NULL,
    sublabel      TEXT,
    icon          TEXT,
    sort          INTEGER NOT NULL DEFAULT 0,
    action_intent TEXT,
    action_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    active        BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_max_ivr_tree_parent ON max_ivr_tree(parent_id, sort);

CREATE TABLE IF NOT EXISTS max_ivr_taps (
    id         BIGSERIAL PRIMARY KEY,
    node_id    TEXT,
    ts         TIMESTAMP NOT NULL DEFAULT (NOW() AT TIME ZONE 'Asia/Kolkata'),
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_max_ivr_taps_node ON max_ivr_taps(node_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_max_ivr_taps_ts   ON max_ivr_taps(ts DESC);
"""

# ── SEED: MAX_IVR_TREE_V2, session_log id=13822, node-for-node ────────────────────────────────────
# Tuple shape: (node_id, parent_id, label, sublabel, icon, sort, action_intent, action_params)
#
# action_params keys:
#   q         natural-language query handed to the existing native layer ({sym} / {seg} templated)
#   needs     "symbol" | "symbol2" | "segment" => a picker runs before the leaf fires
#   needs_sym / needs_seg  => this native CARD needs that context; the UI passes it to the card
#             endpoint and, when reached out of context (e.g. via search), asks for it first
#   basket    v8_qualified basket filter
#   futures   true => leaf only offered for futures-universe symbols (strip rule)
#   href      deep-link instead of a native answer
_L0 = [
    ("analyze",   None, "Analyze a Stock",  "GVM · Chart · Results · Trade Check",   "◈", 1,  "symbol_picker", {"needs": "symbol"}),
    ("signals",   None, "Trade Signals",    "V8 live engine · baskets · paper book", "⚡", 2,  None, {}),
    ("market",    None, "Today's Market",   "Digest · Mood · ADR/PCR · Global",      "◉", 3,  None, {}),
    ("sectors",   None, "Explore Sectors",  "Ratings · rotation · top picks",        "▦", 4,  None, {}),
    ("screens",   None, "Screen Stocks",    "Predefined screens · Top GVM",          "⌖", 5,  None, {}),
    ("results",   None, "Results Season",   "Season shape · sector table · cards",   "▤", 6,  None, {}),
    ("mf",        None, "Mutual Funds",     "V15 · MQS · fund deep-dive",            "◇", 7,  None, {}),
    ("portfolio", None, "My Portfolio",     "Health check · QB baskets · journal",   "▣", 8,  None, {}),
    ("learn",     None, "Learn Scorr",      "GVM · Trade Check · PCR explained",     "◎", 9,  None, {}),
    # 13816 hard rule: free-text is ALWAYS the last card on EVERY level, never removed, never
    # prominent. sort=99 and the UI appends it per level, so it cannot be reordered away.
    ("freetext",  None, "Ask anything else…", "type your question",                  "✎", 99, "freetext", {}),
]

_ANALYZE = [
    ("an_gvm",     "GVM Report",                     "gvm_company",          1,  {"q": "gvm report for {sym}"}),
    ("an_tc_long", "Should I Buy? (Trade Check)",    "tc_v4",                2,  {"q": "trade check {sym} long"}),
    ("an_tc_shrt", "Should I Sell / Short?",         "tc_v4",                3,  {"q": "trade check {sym} short"}),
    ("an_why",     "Why is it moving today?",        "why_moving",           4,  {"needs_sym": True}),
    ("an_chart",   "Chart",                          "card_C",               5,  {"card": "C"}),
    ("an_result",  "Result Card",                    "card_R",               6,  {"card": "R"}),
    ("an_levels",  "Levels · 52W & Pivots",          "levels",               7,  {"needs_sym": True}),
    ("an_fin",     "Financials Snapshot",            "fin_snapshot",         8,  {"needs_sym": True}),
    ("an_shp",     "Shareholding Trend",             "shareholding",         9,  {"needs_sym": True}),
    ("an_div",     "Dividend & Yield",               "dividend",             10, {"needs_sym": True}),
    ("an_peers",   "Peers",                          "segment_peers",        11, {"q": "peers of {sym}"}),
    ("an_compare", "Compare with another stock",     "compare",              12, {"q": "compare {sym} vs {sym2}", "needs": "symbol2"}),
    ("an_cockpit", "Derivative Cockpit",             "card_D",               13, {"card": "D", "futures": True}),
    ("an_news",    "Polished News",                  "polished_news_symbol", 14, {"needs_sym": True}),
]

_SIGNALS = [
    ("sg_mood",    "Market Mood Gate",        "v8_mood",         1,  {"q": "v8 market mood gate and slot allocation"}),
    ("sg_qual",    "Qualified Now",           "v8_qualified",    2,  {"q": "qualified now"}),
    ("sg_buyrev",  "Buy Reversal",            "v8_qualified",    3,  {"q": "v8 buy reversal basket", "basket": "BUY_REVERSAL"}),
    ("sg_buymom",  "Buy Momentum",            "v8_qualified",    4,  {"q": "v8 buy momentum basket", "basket": "BUY_MOMENTUM"}),
    ("sg_selrev",  "Sell Reversal",           "v8_qualified",    5,  {"q": "v8 sell reversal basket", "basket": "SELL_REVERSAL"}),
    ("sg_selob",   "Sell Overbought",         "v8_qualified",    6,  {"q": "v8 sell overbought basket", "basket": "SELL_OVERBOUGHT"}),
    ("sg_paper",   "Paper Book",              "v8_paper",        7,  {"q": "v8 paper book open and closed positions"}),
    ("sg_pivots",  "Pivot Levels",            "paper_pivots",    8,  {}),
    ("sg_oi",      "OI Long / Short Buildup", "oi_buildup",      9,  {}),
    ("sg_rvol",    "Volume Spikes (RVOL)",    "rvol_spikes",     10, {}),
    ("sg_brk",     "52W-High Breakouts Today", "breakouts_today", 11, {}),
    ("sg_v10",     "Index Intel (V10)",       "v10_signal",      12, {}),
    ("sg_v9",      "Pairs (V9)",              "v9_pairs",        13, {}),
    ("sg_tc",      "Intraday Scan (TC)",      "tc_scan",         14, {}),
]

_MARKET = [
    ("mk_digest",  "Daily Digest",                        "daily_digest",          1, {"q": "daily digest"}),
    ("mk_mood",    "Market Mood",                         "v8_mood",               2, {"q": "market mood"}),
    ("mk_idxlvl",  "Nifty / BankNifty Support & Resistance", "index_levels",        3, {}),
    ("mk_pcr",     "ADR & PCR",                           "pcr",                   4, {"q": "pcr and adr today"}),
    ("mk_fii",     "FII / DII Flows",                     "fii_dii_positioning",   5, {}),
    ("mk_global",  "Global · Commodities · Currency",     "global_indices_card",   6, {}),
    ("mk_rtoday",  "Results Today",                       "results_today",         7, {}),
    ("mk_rweek",   "Results This Week",                   "results_week",          8, {}),
]

_SECTORS = [
    ("sc_ratings", "Sector Ratings",           "gvm_sector",      1, {"q": "sector ratings"}),
    ("sc_topseg",  "Top Segments by GVM",      "gvm_sector",      2, {"q": "top segments by gvm"}),
    ("sc_pick",    "Pick a Segment",           "segment_picker",  3, {"needs": "segment"}),
    ("sc_best",    "Best Stock in a Sector",   "segment_picker",  4, {"needs": "segment", "q": "best stock in {seg}"}),
    ("sc_rot",     "Sector Rotation",          "sector_rotation", 5, {}),
]

_SECTOR_L2 = [
    ("sc_members", "Members by GVM",       "sector_segment", 1, {"q": "stocks in {seg} by gvm"}),
    ("sc_picks",   "Top Picks",            "sector_segment", 2, {"q": "top stocks in {seg}"}),
    ("sc_move",    "Segment Move",         "segment_move",   3, {"needs_seg": True}),
    ("sc_brief",   "Segment Brief",        "sector_brief",   4, {"needs_seg": True}),
]

_SCREENS = [
    ("sn_screens", "The predefined screens",  "screen_members",   1,  {}),
    ("sn_excel",   "Excellent (8+)",          "gvm_strong",       2,  {"q": "stocks with gvm above 8"}),
    ("sn_top10",   "Top 10 by GVM",           "gvm_top",          3,  {"q": "top 10 stocks by gvm"}),
    ("sn_div",     "High Dividend Yield",     "filter_dividend",  4,  {}),
    ("sn_debt",    "Debt-Free Quality",       "filter_debtfree",  5,  {}),
    ("sn_52w",     "Near 52W High",           "filter_near52w",   6,  {}),
    ("sn_osold",   "Oversold Bounce Candidates", "filter_oversold", 7, {}),
    ("sn_small",   "Small Caps by GVM",       "gvm_top_smallcap", 8,  {}),
    ("sn_own",     "Build your own",          "deeplink",         9,  {"href": "/v13"}),
]

_RESULTS = [
    ("rs_season",  "Season Summary",          "result_season",       1, {}),
    ("rs_sector",  "Sector Table",            "result_sector_table", 2, {}),
    ("rs_beats",   "Beats & Misses",          "result_beats",        3, {}),
    ("rs_today",   "Reported Today",          "results_today",       4, {}),
    ("rs_lookup",  "Look up a company",       "card_R",              5, {"card": "R", "needs": "symbol"}),
]

_MF = [
    ("mf_screen",  "Curated MF Screener",     "mf_screener",  1, {}),
    ("mf_mqs",     "Top Funds by MQS",        "mf_top_mqs",   2, {}),
    ("mf_mine",    "Is My Fund Good?",        "mf_fund",      3, {}),
    ("mf_large",   "Large Cap",               "mf_category",  4, {}),
    ("mf_mid",     "Mid Cap",                 "mf_category",  5, {}),
    ("mf_small",   "Small Cap",               "mf_category",  6, {}),
    ("mf_flexi",   "Flexi Cap",               "mf_category",  7, {}),
    ("mf_elss",    "ELSS / Tax Saver",        "mf_category",  8, {}),
]

_PORTFOLIO = [
    ("pf_check",   "Portfolio Health Check",  "hr_check",              1, {"href": "/health"}),
    ("pf_saved",   "Saved Reports",           "hr_reports_list",       2, {"href": "/adaptive"}),
    ("pf_exit",    "Exit Check a Holding",    "exit_check_composite",  3, {"needs": "symbol", "needs_sym": True}),
    ("pf_qb",      "QB Summary",              "qb_summary",            4, {"q": "qb summary"}),
    ("pf_qbpos",   "QB Positions",            "qb_positions",          5, {}),
    ("pf_jopen",   "Journal · Open",          "journal_open",          6, {"q": "my personal journal open trades"}),
    ("pf_jclosed", "Journal · Closed + P&L",  "journal_closed",        7, {"q": "my personal journal closed trades pnl"}),
]

# 13822: "pure static native glossary cards — highest retail value, zero token cost". These are the
# only leaves whose answer is authored text rather than a DB read, so they ship complete in phase A.
_LEARN = [
    ("ln_gvm",   "What is GVM?",               1, "**GVM** is Scorr's single quality score, 0-10, rebuilt every night.\n\n"
     "- **G — Growth**: sales and profit CAGR, QoQ momentum, margins, and the two institutional-holding metrics.\n"
     "- **V — Valuation**: PE against the company's OWN history first, then against its segment median.\n"
     "- **M — Momentum**: 1Y/3Y returns, 52W vs index, distance from the 50 and 200 DMA, monthly RSI.\n\n"
     "Each metric is scored against the fine-grained SEGMENT peer median — not the whole market — so a "
     "bank is judged against banks. GVM is the mcap-weighted blend of the three pillars."),
    ("ln_tc",    "How to read Trade Check",    2, "**Trade Check** answers one question: is this a clean entry *right now*, long or short?\n\n"
     "It reads the live 5-minute feed, not the nightly snapshot. A verdict combines the market-mood gate, "
     "the stock's own structure (pivots, DMA position, RVOL) and its derivative posture where futures exist.\n\n"
     "Read the REASONS, not just the verdict — a PASS with three thin reasons is not the same trade as a "
     "PASS with eight."),
    ("ln_mood",  "What is the Market Mood Gate", 3, "**Market Mood Gate** is the daily risk switch on the V8 engine.\n\n"
     "It reads the advance/decline ratio and Nifty's daily/weekly/monthly posture, then caps how many slots "
     "the engine may allocate. In a weak tape the gate narrows to a couple of slots or closes entirely.\n\n"
     "It exists so that position SIZE responds to the market, not just to how good an individual signal looks."),
    ("ln_pcr",   "PCR & ADR explained",        4, "**PCR** (Put-Call Ratio) is open interest in puts divided by open interest in calls. "
     "High PCR = more puts written = positioning that leans bullish; low PCR leans bearish. It is a "
     "positioning read, not a prediction.\n\n"
     "**ADR** (Advance-Decline Ratio) is advancing stocks over declining stocks. It measures BREADTH — "
     "whether a Nifty move is carried by the whole market or by five heavyweights."),
    ("ln_piv",   "Pivots & S/R explained",     5, "**Pivot levels** are arithmetic, not opinion. From the prior session's high, low and close:\n\n"
     "- **PP** = (H + L + C) / 3 — the session's centre of gravity\n"
     "- **R1, R2** above it; **S1, S2** below\n\n"
     "Their value is that everyone computes the same numbers, so they become real decision points. "
     "Price holding above PP is a different session from price rejecting it."),
    ("ln_rvol",  "RVOL & OI buildup explained", 6, "**RVOL** (Relative Volume) is today's volume against its own recent average. RVOL 3x means "
     "three times the usual participation — a move on 0.6x RVOL is noise wearing a candle.\n\n"
     "**OI buildup** pairs price direction with open-interest direction:\n\n"
     "- Price up + OI up = **long buildup**\n- Price down + OI up = **short buildup**\n"
     "- Price up + OI down = **short covering**\n- Price down + OI down = **long unwinding**"),
    ("ln_scr",   "How Scorr screens work",     7, "A Scorr screen is a STORED nightly result set, not a live query.\n\n"
     "The engine evaluates each screen after the close and writes its members, so every tap reads the same "
     "membership the whole platform saw that evening — and an Added-On date travels with each member, so "
     "you can see how long a name has been qualifying.\n\n"
     "Screens never re-run on tap. That is deliberate: a screen that changes while you read it is not a screen."),
    ("ln_mqs",   "MQS for mutual funds",       8, "**MQS** (Mutual-fund Quality Score) scores a fund by LOOKING THROUGH to what it actually holds.\n\n"
     "Every disclosed holding is matched to its Scorr GVM score and weighted by portfolio weight, then "
     "combined with the fund's own return and consistency record.\n\n"
     "So a fund is not judged only by past NAV — it is judged by the quality of the businesses it owns today."),
    ("ln_card",  "The C·A·R·D strip",          9, "Every symbol on Scorr carries the same four-letter strip:\n\n"
     "- **C — Chart**: price, volume, pivots, VWAP\n- **A — Analysis**: the GVM deep-dive\n"
     "- **R — Result**: the latest quarter, peers and estimates\n- **D — Derivative Cockpit**: futures and options posture\n\n"
     "It is ONE shared component everywhere it appears, so the card you open from a screener row is "
     "byte-identical to the one you open from a company page. **D** appears only for symbols in the "
     "futures universe."),
]


def _seed_rows():
    rows = list(_L0)
    for parent, items in (("analyze", _ANALYZE), ("signals", _SIGNALS), ("market", _MARKET),
                          ("sectors", _SECTORS), ("screens", _SCREENS), ("results", _RESULTS),
                          ("mf", _MF), ("portfolio", _PORTFOLIO)):
        for node_id, label, intent, sort, params in items:
            rows.append((node_id, parent, label, None, None, sort, intent, params))
    for node_id, label, intent, sort, params in _SECTOR_L2:
        rows.append((node_id, "sc_pick", label, None, None, sort, intent, params))
    for node_id, label, sort, body in _LEARN:
        rows.append((node_id, "learn", label, None, None, sort, "learn_glossary", {"body": body}))
    return rows


def ensure_schema(conn):
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


def seed_tree(conn) -> dict:
    """Insert only the nodes that are missing. Never UPDATE, never DELETE — see module docstring."""
    rows = _seed_rows()
    inserted = []
    with conn.cursor() as cur:
        cur.execute("SELECT node_id FROM max_ivr_tree")
        have = {r[0] for r in cur.fetchall()}
        for node_id, parent, label, sub, icon, sort, intent, params in rows:
            if node_id in have:
                continue
            cur.execute("""INSERT INTO max_ivr_tree
                             (node_id, parent_id, label, sublabel, icon, sort, action_intent, action_params)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""",
                        (node_id, parent, label, sub, icon, sort, intent, json.dumps(params)))
            inserted.append(node_id)
    conn.commit()
    return {"seed_total": len(rows), "inserted": len(inserted), "already_present": len(rows) - len(inserted)}


_BOOTSTRAPPED = False


def _bootstrap():
    """Create + seed once per process. Failure must never block import — the page degrades to the
    free-text card, which is exactly the escape hatch 13816 says can never be removed."""
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED or not _DB:
        return
    try:
        with _conn() as conn:
            ensure_schema(conn)
            r = seed_tree(conn)
        log.info("max_ivr bootstrap: %s", r)
        _BOOTSTRAPPED = True
    except Exception as e:
        log.warning("max_ivr bootstrap deferred: %s", e)


@router.get("/api/max/ivr/tree")
def ivr_tree():
    """The whole active tree in one call — it is ~85 small rows, so paging it per level would cost a
    round-trip per tap for no benefit. The client keeps it in memory and renders each level from it."""
    _bootstrap()
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT node_id, parent_id, label, sublabel, icon, sort,
                                  action_intent, action_params
                           FROM max_ivr_tree WHERE active
                           ORDER BY COALESCE(parent_id,''), sort, node_id""")
            nodes = [{"node_id": r[0], "parent_id": r[1], "label": r[2], "sublabel": r[3],
                      "icon": r[4], "sort": r[5], "action_intent": r[6],
                      "action_params": r[7] or {}} for r in cur.fetchall()]
        # cc#836 phase B: has_card is derived from the CARDS registry, never stored on the row —
        # a stored copy would go stale the moment a card is added or renamed.
        try:
            from max_native_cards import CARDS as _C
        except Exception:
            _C = {}
        for n in nodes:
            n["has_card"] = bool(n.get("action_intent") in _C)
        return {"nodes": nodes, "count": len(nodes),
                "cards_available": sorted(_C.keys())}
    except Exception as e:
        log.exception("ivr_tree failed")
        # Always JSON (cc#835 doctrine) — the client renders an error card, never a parse exception.
        return {"nodes": [], "count": 0, "error": f"{type(e).__name__}: {str(e)[:200]}"}


class TapReq(BaseModel):
    node_id: str
    session_id: Optional[str] = None


@router.post("/api/max/ivr/tap")
def ivr_tap(body: TapReq):
    """Fire-and-forget telemetry. A failed log must never break navigation, so this always
    returns 200 and reports the outcome in the body."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO max_ivr_taps (node_id, session_id) VALUES (%s,%s)",
                        (body.node_id[:120], (body.session_id or "")[:120] or None))
            conn.commit()
        return {"ok": True}
    except Exception as e:
        log.warning("ivr_tap failed: %s", e)
        return {"ok": False, "error": str(e)[:200]}


@router.get("/api/max/ivr/taps/summary")
def ivr_taps_summary(days: int = 28, limit: int = 30):
    """Feeds the weekly improvisation cycle (13823): most-tapped nodes, and — just as useful — the
    dead branches. Denominator travels with the counts per UNIVERSE_DENOMINATOR_RULE."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("""SELECT node_id, COUNT(*) n FROM max_ivr_taps
                           WHERE ts >= (NOW() AT TIME ZONE 'Asia/Kolkata') - make_interval(days => %s)
                           GROUP BY 1 ORDER BY n DESC LIMIT %s""", (days, limit))
            top = [{"node_id": r[0], "taps": r[1]} for r in cur.fetchall()]
            cur.execute("""SELECT t.node_id, t.label FROM max_ivr_tree t
                           WHERE t.active AND t.parent_id IS NOT NULL
                             AND NOT EXISTS (SELECT 1 FROM max_ivr_taps p
                                             WHERE p.node_id = t.node_id
                                               AND p.ts >= (NOW() AT TIME ZONE 'Asia/Kolkata')
                                                          - make_interval(days => %s))
                           ORDER BY t.parent_id, t.sort""", (days,))
            dead = [{"node_id": r[0], "label": r[1]} for r in cur.fetchall()]
            cur.execute("""SELECT COUNT(*) FROM max_ivr_taps
                           WHERE ts >= (NOW() AT TIME ZONE 'Asia/Kolkata') - make_interval(days => %s)""",
                        (days,))
            total = cur.fetchone()[0]
        return {"window_days": days, "taps_total": total, "top": top,
                "dead_leaves": dead, "dead_count": len(dead)}
    except Exception as e:
        log.exception("ivr_taps_summary failed")
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}
