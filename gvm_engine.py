# ============================================================
# GVM SCORING ENGINE v3 — D5 Final
# Fixes applied:
# 1. Blank rule: missing data → score = 5 (neutral)
# 2. Zero rule: zero = scored per band
# 3. OPM Expansion: F2 = score_relative vs peer
# 4. Institutional Holding Absolute: bands >50=10, 25-50=7.5, 10-25=5, <10=2.5
# 5. Interest Coverage: BFSI = SKIP | Non-BFSI blank = 5 | zero = band
# 6. Potential Upside: blank=5, zero=0, filled = scored normally
# 7. FIXED: Negative peer avg in score_relative (opposite sign logic)
# ============================================================

import math

BLANK_SCORE = 5.0
SKIP_SCORE  = None


def _is_blank(val):
    if val is None: return True
    try: return math.isnan(float(val))
    except: return True


def score_absolute(value):
    if value > 15:   return 10.0
    elif value > 5:  return 7.5
    elif value >= 0: return 5.0
    else:            return 2.5


def score_relative(stock_val, peer_avg):
    if peer_avg is None or _is_blank(peer_avg) or peer_avg == 0:
        return BLANK_SCORE

    stock_val = float(stock_val)
    peer_avg  = float(peer_avg)

    # FIX: opposite sign — division breaks when peer is negative
    if stock_val > 0 and peer_avg < 0:
        return 10.0   # stock positive, peer negative → clearly better
    if stock_val < 0 and peer_avg > 0:
        return 2.5    # stock negative, peer positive → clearly worse
    if stock_val < 0 and peer_avg < 0:
        # Both negative — less negative = better → flip ratio
        ratio = (peer_avg / stock_val) * 100
        if ratio > 125:   return 10.0
        elif ratio > 100: return 7.5
        elif ratio > 75:  return 5.0
        else:             return 2.5

    ratio = (stock_val / peer_avg) * 100
    if ratio > 125:   return 10.0
    elif ratio > 100: return 7.5
    elif ratio > 75:  return 5.0
    else:             return 2.5


def score_relative_inverse(stock_val, peer_median):
    """cc#506: lower-is-better relative scorer for report-only metrics with no absolute-band
    spec (Forward PE, Price/Book, and the same _peer_block-driven EV/EBITDA + Annual Upside
    blocks in gvm_report_endpoints.py). F1 is a neutral 5.0 placeholder (no absolute band spec
    exists for these); F2 bands on (peer_median/stock_val)*100 -- mirrors score_pe's pe_factor
    inverse cutoffs exactly (smaller stock_val vs peer scores higher)."""
    if _is_blank(stock_val):
        return BLANK_SCORE
    stock_val = float(stock_val)
    f1 = 5.0
    if peer_median is None or _is_blank(peer_median) or float(peer_median) == 0 or stock_val == 0:
        f2 = BLANK_SCORE
    else:
        ratio = (float(peer_median) / stock_val) * 100
        if ratio > 125:   f2 = 10.0
        elif ratio > 100: f2 = 7.5
        elif ratio > 75:  f2 = 5.0
        else:             f2 = 2.5
    return round((f1 + f2) / 2, 2)


def param_score(stock_val, peer_avg):
    if _is_blank(stock_val): return BLANK_SCORE
    stock_val = float(stock_val)
    f1 = score_absolute(stock_val)
    f2 = score_relative(stock_val, peer_avg)
    return round((f1 + f2) / 2, 2)


def score_dma(price, dma):
    if _is_blank(dma) or float(dma) == 0:   return 5.0
    if _is_blank(price) or float(price) == 0: return 5.0
    deviation = ((float(price) - float(dma)) / float(dma)) * 100
    if deviation > 10:   return 10.0
    elif deviation > 5:  return 7.5
    elif deviation < -5: return 2.5
    else:                return 5.0


def score_pe(pe, historical_pe, segment_pe):
    if _is_blank(pe): return BLANK_SCORE

    def pe_factor(stock_pe, benchmark_pe):
        if _is_blank(benchmark_pe) or float(benchmark_pe) == 0: return BLANK_SCORE
        ratio = (float(stock_pe) / float(benchmark_pe)) * 100
        if ratio < 75:    return 10.0
        elif ratio < 100: return 7.5
        elif ratio < 125: return 5.0
        else:             return 2.5

    f1 = pe_factor(pe, historical_pe)
    f2 = pe_factor(pe, segment_pe)
    return round((f1 + f2) / 2, 2)


def score_inst_holding_abs(value):
    if _is_blank(value): return BLANK_SCORE
    val = float(value)
    if val > 50:   return 10.0
    elif val > 25: return 7.5
    elif val > 10: return 5.0
    else:          return 2.5


def score_interest_coverage(stock_val, peer_avg, is_bfsi=False):
    if is_bfsi:           return SKIP_SCORE
    if _is_blank(stock_val): return BLANK_SCORE
    return param_score(stock_val, peer_avg)


def score_opm_expansion(stock_val, peer_avg):
    if _is_blank(stock_val): return BLANK_SCORE
    stock_val = float(stock_val)
    if stock_val > 100:   f1 = 10.0
    elif stock_val > 50:  f1 = 7.5
    elif stock_val >= 0:  f1 = 5.0
    else:                 f1 = 2.5
    f2 = score_relative(stock_val, peer_avg)
    return round((f1 + f2) / 2, 2)


def score_potential_upside(stock_val, peer_avg):
    if _is_blank(stock_val): return BLANK_SCORE
    stock_val = float(stock_val)
    if stock_val == 0: return 0.0
    f1 = score_absolute(stock_val)
    f2 = score_relative(stock_val, peer_avg)
    return round((f1 + f2) / 2, 2)


# ── INDIVIDUAL PARAMETER APIs ────────────────────────────────

def api_sales_growth_5y(sv, pa):   return {"parameter": "Sales Growth 5Y",   "score": param_score(sv, pa)}
def api_sales_growth_3y(sv, pa):   return {"parameter": "Sales Growth 3Y",   "score": param_score(sv, pa)}
def api_profit_growth_5y(sv, pa):  return {"parameter": "Profit Growth 5Y",  "score": param_score(sv, pa)}
def api_profit_growth_3y(sv, pa):  return {"parameter": "Profit Growth 3Y",  "score": param_score(sv, pa)}
def api_qoq_sales_growth(sv, pa):  return {"parameter": "QoQ Sales Growth",  "score": param_score(sv, pa)}
def api_qoq_profit_growth(sv, pa): return {"parameter": "QoQ Profit Growth", "score": param_score(sv, pa)}
def api_opm(sv, pa):               return {"parameter": "OPM",               "score": param_score(sv, pa)}
def api_opm_expansion(sv, pa):     return {"parameter": "OPM Expansion",     "score": score_opm_expansion(sv, pa)}
def api_fixed_asset_growth(sv, pa):return {"parameter": "Fixed Asset Growth","score": param_score(sv, pa)}
def api_inst_holding_abs(sv, pa):  return {"parameter": "Inst Holding Abs",  "score": score_inst_holding_abs(sv)}
def api_inst_holding_change(sv, pa):return{"parameter": "Inst Holding Change","score": param_score(sv, pa)}
def api_roce(sv, pa):              return {"parameter": "ROCE",              "score": param_score(sv, pa)}
def api_interest_coverage(sv, pa, is_bfsi=False): return {"parameter": "Interest Coverage", "score": score_interest_coverage(sv, pa, is_bfsi), "skipped": is_bfsi}
def api_dividend_yield(sv, pa):    return {"parameter": "Dividend Yield",    "score": param_score(sv, pa)}
def api_pe_ratio(pe, hist_pe, seg_pe): return {"parameter": "PE Ratio",      "score": score_pe(pe, hist_pe, seg_pe)}
def api_potential_upside(sv, pa):  return {"parameter": "Potential Upside",  "score": score_potential_upside(sv, pa)}
def api_return_1y(sv, pa):         return {"parameter": "1Y Return",         "score": param_score(sv, pa)}
def api_return_3y(sv, pa):         return {"parameter": "3Y Return",         "score": param_score(sv, pa)}
def api_dma50(price, dma):         return {"parameter": "DMA 50",            "score": score_dma(price, dma)}
def api_dma200(price, dma):        return {"parameter": "DMA 200",           "score": score_dma(price, dma)}
def api_return_52w_vs_index(sv, pa):return{"parameter": "52W vs Index",      "score": param_score(sv, pa)}


# ── COMPOSITE SCORES ─────────────────────────────────────────

def api_g_score(s):
    is_bfsi = s.get("is_bfsi", False)
    ic_score = score_interest_coverage(s.get("interest_coverage"), s.get("peer_interest_coverage"), is_bfsi)

    scores = {
        "Sales Growth 5Y":    param_score(s.get("sales_growth_5y"),    s.get("peer_sales_growth_5y")),
        "Sales Growth 3Y":    param_score(s.get("sales_growth_3y"),    s.get("peer_sales_growth_3y")),
        "Profit Growth 5Y":   param_score(s.get("profit_growth_5y"),   s.get("peer_profit_growth_5y")),
        "Profit Growth 3Y":   param_score(s.get("profit_growth_3y"),   s.get("peer_profit_growth_3y")),
        "QoQ Sales Growth":   param_score(s.get("qoq_sales_growth"),   s.get("peer_qoq_sales_growth")),
        "QoQ Profit Growth":  param_score(s.get("qoq_profit_growth"),  s.get("peer_qoq_profit_growth")),
        "OPM":                param_score(s.get("opm"),                s.get("peer_opm")),
        "OPM Expansion":      score_opm_expansion(s.get("opm_expansion"), s.get("peer_opm_expansion")),
        "Fixed Asset Growth": param_score(s.get("fixed_asset_growth"), s.get("peer_fixed_asset_growth")),
        "Inst Holding Abs":   score_inst_holding_abs(s.get("inst_holding_abs")),
        "Inst Holding Change":param_score(s.get("inst_holding_change"),s.get("peer_inst_holding_change")),
        "ROCE":               param_score(s.get("roce"),               s.get("peer_roce")),
        "Dividend Yield":     param_score(s.get("dividend_yield"),     s.get("peer_dividend_yield")),
    }
    if ic_score is not None:
        scores["Interest Coverage"] = ic_score

    g = round(sum(scores.values()) / len(scores), 2)
    return {"parameter": "G Score", "score": g, "breakdown": scores}


def api_v_score(s):
    scores = {
        "PE Ratio":         score_pe(s.get("pe"), s.get("historical_pe"), s.get("segment_pe")),
        "Potential Upside": score_potential_upside(s.get("potential_upside"), s.get("peer_potential_upside")),
    }
    return {"parameter": "V Score", "score": round(sum(scores.values()) / len(scores), 2), "breakdown": scores}


def api_m_score(s):
    scores = {
        "1Y Return":    param_score(s.get("return_1y"),           s.get("peer_return_1y")),
        "3Y Return":    param_score(s.get("return_3y"),           s.get("peer_return_3y")),
        "DMA 50":       score_dma(s.get("price"), s.get("dma_50")),
        "DMA 200":      score_dma(s.get("price"), s.get("dma_200")),
        "52W vs Index": param_score(s.get("return_52w_vs_index"), s.get("peer_return_52w_vs_index")),
    }
    return {"parameter": "M Score", "score": round(sum(scores.values()) / len(scores), 2), "breakdown": scores}


def api_gvm_score(s):
    g = api_g_score(s)
    v = api_v_score(s)
    m = api_m_score(s)
    gvm = round((g["score"] + v["score"] + m["score"]) / 3, 2)
    return {
        "stock":       s.get("name", "Unknown"),
        "GVM_score":   gvm,
        "G_score":     g["score"],
        "V_score":     v["score"],
        "M_score":     m["score"],
        "G_breakdown": g["breakdown"],
        "V_breakdown": v["breakdown"],
        "M_breakdown": m["breakdown"],
    }