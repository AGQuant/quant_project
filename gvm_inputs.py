"""
gvm_inputs.py — cc#1714 (founder 05-Sep-2026, OPTION 2): ONE helper that hands every reader the
raw input and the segment-median peer behind each of the 21 GVM parameters.

Why: gvm_scores has *_raw / *_peer columns that nobody batch-writes. They were filled lazily by
gvm_company_report on a page open (8 of 1,787 names) and wiped by the nightly DELETE, so PE showed
blank for 23 of 24 shortlisted names while screener_raw carried PE for 1,727 of 1,875 rows. The
founder chose NOT to fill those columns from the nightly; instead readers take raw inputs from the
source and gvm_scores stops pretending to carry them. Those columns are now deprecated (see the
comment above the INSERT in gvm_nightly.recompute_gvm).

What this returns is exactly what scored: raw values come through gvm_nightly._load_merged_df
(screener_raw joined to input_raw, with the nightly's own derived columns — YoY growth per
cc#1005, opm_expansion in bps per cc#1094, net FII+DII holdings, potential_upside) and are shaped
by gvm_nightly._stock_dict; peers are gvm_nightly._peer_averages (segment MEDIAN, cc#506). Nothing
is forked or re-derived here. M's five inputs come from momentum_scores (the values api_m_score
actually used — never screener_raw's dead return_1y/3y/52w columns, cc#1494), with the segment
median computed the same way over the same merged frame.

    get_inputs(["IPCALAB"]) -> {"IPCALAB": {"pe": {"raw": 36.3, "peer": 31.9}, "roce": {...}, ...}}

Keys are the gvm_scores column prefixes: sales_5y sales_3y profit_5y profit_3y qoq_sales qoq_profit
opm opm_exp fa_growth inst_abs inst_change roce int_cov div_yield pe upside ret_1y ret_3y dma_50
dma_200 ret_52w_idx. Values are floats rounded to 2dp or None (never NaN, never 0 for missing).

The merged frame is ~1,800 rows and is rebuilt at most every _TTL seconds per process; a report
page open costs one cached dict lookup after the first call.
"""
import logging
import threading
import time
from datetime import date
from typing import Dict, List, Optional

import pandas as pd

import gvm_nightly as _gn

log = logging.getLogger("gvm_inputs")

# (prefix, _stock_dict key). raw = sd[key]; peer = sd["peer_"+key], except PE whose peer is the
# live segment-median pe that score_pe uses (sd["segment_pe"], cc#506).
GV_PARAMS = [
    ("sales_5y", "sales_growth_5y"), ("sales_3y", "sales_growth_3y"),
    ("profit_5y", "profit_growth_5y"), ("profit_3y", "profit_growth_3y"),
    ("qoq_sales", "qoq_sales_growth"), ("qoq_profit", "qoq_profit_growth"),
    ("opm", "opm"), ("opm_exp", "opm_expansion"), ("fa_growth", "fixed_asset_growth"),
    ("inst_abs", "inst_holding_abs"), ("inst_change", "inst_holding_change"),
    ("roce", "roce"), ("int_cov", "interest_coverage"), ("div_yield", "dividend_yield"),
    ("pe", "pe"), ("upside", "potential_upside"),
]
# (prefix, momentum_scores column)
M_PARAMS = [("ret_1y", "ret_1y"), ("ret_3y", "ret_3y"), ("dma_50", "dma_50"),
            ("dma_200", "dma_200"), ("ret_52w_idx", "ret_52w_vs_index")]
PARAM_KEYS = [p for p, _ in GV_PARAMS] + [p for p, _ in M_PARAMS]

_TTL = 300.0
_cache = {"ts": 0.0, "data": None}
_lock = threading.Lock()


def _val(x) -> Optional[float]:
    """float rounded 2dp, or None — never NaN into a payload (same rule as gvm_nightly._rating_val)."""
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(xf) else round(xf, 2)


def _load_momentum(target_date: date) -> pd.DataFrame:
    cols = ", ".join(f"{c} AS m_{p}" for p, c in M_PARAMS)
    with _gn._conn() as conn:
        mom = pd.read_sql_query(
            f"SELECT DISTINCT ON (symbol) symbol, {cols} FROM momentum_scores "
            "WHERE score_date <= %s ORDER BY symbol, score_date DESC",
            conn, params=(target_date,),
        )
    mom["symbol"] = mom["symbol"].astype(str).str.strip()
    return mom.rename(columns={"symbol": "nse_code"})


def _segment_medians(df: pd.DataFrame, cols: List[str]) -> Dict:
    """Segment MEDIAN per column — the M-side twin of gvm_nightly._peer_averages (which only knows
    PEER_PARAMS). Same grouping key, same median, same rounding."""
    out = {}
    cols = [c for c in cols if c in df.columns]
    for seg, grp in df.groupby("gvm_segment"):
        avgs = {}
        for c in cols:
            vals = pd.to_numeric(grp[c], errors="coerce").dropna()
            avgs[c] = round(vals.median(), 4) if len(vals) else None
        out[seg] = avgs
    return out


def _build(target_date: Optional[date] = None) -> Dict[str, Dict[str, Dict[str, Optional[float]]]]:
    target_date = target_date or date.today()
    df = _gn._load_merged_df(target_date)
    if df.empty:
        return {}
    try:
        mom = _load_momentum(target_date)
        df = df.merge(mom, on="nse_code", how="left")
    except Exception as e:   # M inputs missing must not blank the G/V inputs
        log.warning("gvm_inputs: momentum_scores unavailable (%s) — M raw/peer will be None", e)
    peer_avgs = _gn._peer_averages(df)
    m_cols = [f"m_{p}" for p, _ in M_PARAMS]
    m_peers = _segment_medians(df, m_cols)

    out: Dict[str, Dict[str, Dict[str, Optional[float]]]] = {}
    for _, row in df.iterrows():
        sym = str(row.get("nse_code", "")).strip()
        if not sym:
            continue
        sd = _gn._stock_dict(row, peer_avgs)
        rec: Dict[str, Dict[str, Optional[float]]] = {}
        for p, key in GV_PARAMS:
            peer = sd.get("segment_pe") if p == "pe" else sd.get("peer_" + key)
            rec[p] = {"raw": _val(sd.get(key)), "peer": _val(peer)}
        seg_m = m_peers.get(sd.get("segment"), {})
        for p, _c in M_PARAMS:
            rec[p] = {"raw": _val(row.get(f"m_{p}")), "peer": _val(seg_m.get(f"m_{p}"))}
        out[sym] = rec
    return out


def get_all(refresh: bool = False) -> Dict[str, Dict[str, Dict[str, Optional[float]]]]:
    """{symbol: {param: {raw, peer}}} for every scored name; rebuilt at most every _TTL seconds."""
    now = time.time()
    with _lock:
        if refresh or _cache["data"] is None or now - _cache["ts"] > _TTL:
            _cache["data"] = _build()
            _cache["ts"] = now
        return _cache["data"]


def get_inputs(symbols: Optional[List[str]] = None, refresh: bool = False) -> Dict[str, Dict[str, Dict[str, Optional[float]]]]:
    """Raw input + segment-median peer for each of the 21 GVM parameters, per symbol.
    symbols=None returns every scored name. Unknown symbols are simply absent."""
    data = get_all(refresh=refresh)
    if symbols is None:
        return data
    want = {str(s or "").strip().upper() for s in symbols}
    return {s: data[s] for s in want if s in data}
