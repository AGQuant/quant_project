import pandas as pd
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# SCREENER LOADER v3 — D5 Final
# Fixes:
# 1. Handles both "Segment" and "HKKR Segment" column names
# 2. OPM Expansion in basis points
# 3. Institutional Holding Absolute = FII + DII
# 4. Institutional Holding Change = FII change + DII change
# 5. Blank FY27 stays blank (score=5), zero=score 0
# 6. IPO fallback to Industry Group
# 7. Potential Upside = FY27 Growth% × PE Multiplier
# 8. NaN NSE code filter before merge (prevents cross-join)
# 9. Deduplicate input on NSE code
# 10. Segment fixes: PFC → Power Finance NBFC, Stylam → Building Materials
# 11. Small segments (<5 stocks) merged into nearest parent segment
# ============================================================

SCREENER_COLUMNS = {
    "Current Price":               "price",
    "Sales growth 5Years":         "sales_growth_5y",
    "Sales growth 3Years":         "sales_growth_3y",
    "Profit growth 5Years":        "profit_growth_5y",
    "Profit growth 3Years":        "profit_growth_3y",
    "YOY Quarterly sales growth":  "qoq_sales_growth",
    "YOY Quarterly profit growth": "qoq_profit_growth",
    "OPM":                         "opm",
    "OPM latest quarter":          "opm_latest_q",
    "OPM preceding year quarter":  "opm_prev_year_q",
    "Fixed Asset Growth":          "fixed_asset_growth",
    "FII holding":                 "fii_holding",
    "DII holding":                 "dii_holding",
    "Change in FII holding":       "fii_change",
    "Change in DII holding":       "dii_change",
    "Return on capital employed":  "roce",
    "Interest Coverage Ratio":     "interest_coverage",
    "Dividend yield":              "dividend_yield",
    "Price to Earning":            "pe",
    "Historical PE 10Years":       "historical_pe",
    "Industry PE":                 "segment_pe",
    "Return over 1year":           "return_1y",
    "Return over 3years":          "return_3y",
    "DMA 50":                      "dma_50",
    "DMA 200":                     "dma_200",
    "52w Index":                   "return_52w_vs_index",
    "Market Capitalization":       "market_cap",
    "Industry Group":              "industry_group",
}

PEER_PARAMS = [
    "sales_growth_5y", "sales_growth_3y", "profit_growth_5y", "profit_growth_3y",
    "qoq_sales_growth", "qoq_profit_growth", "opm", "opm_expansion", "fixed_asset_growth",
    "inst_holding_abs", "inst_holding_change", "roce", "interest_coverage",
    "dividend_yield", "potential_upside", "return_1y", "return_3y", "return_52w_vs_index",
]

BFSI_SEGMENTS = {
    "PSU Banks", "Private Banks", "Small Finance Banks",
    "NBFC - Large", "MSME Finance - Large", "MSME Finance - Mid",
    "MSME Finance - Small", "Microfinance & MSME", "Housing Finance",
    "Life Insurance", "General & Health Insurance", "Capital Markets - Large",
    "Broking & Wealth Management", "Exchanges & Ratings - Mid",
    "REITs", "Holding Companies",
}

# FIX 10: Manual segment corrections
SEGMENT_FIXES = {
    "PFC":      "NBFC - Large",
    "STYLAMIND":"Building Materials - Glass, Ceramics & Ply",
}

# FIX 11: Small segment merge map (<5 stocks → parent segment)
SEGMENT_MERGE = {
    "Advertising":                                  "Business Services",
    "Biotechnology":                                "CDMO & Contract Mfg",
    "Cement Smallcap":                              "Cement - Small",
    "Capital Goods - Electrical Equipment Smallcap":"Capital Goods - Heavy Electrical",
    "Consumer Tech":                                "Entertainment, Content & Digital",
    "Realty - Smallcap":                            "Realty - Small",
    "Plantation & Plantation Products":             "Agro Chemicals - Small",
    "Edible Oil":                                   "FMCG - Foods Small",
    "Shipping and Ship Building services":          "Defence PSU",
    "Roads":                                        "Engineering - EPC Civil Small",
    "Capital Goods-Non Electrical Equipment":       "Capital Goods - Industrial Small",
    "Auto Ancillaries Small":                       "Auto - Body & Stampings",
    "Pharma Smallcap":                              "Pharma - Formulations",
    "Retail Smallcap":                              "Retail - Small",
    "Agro Chemicals":                               "Agro Chemicals - Small",
    "Mining and Minerals":                          "Steel - Mid & Small",
    "Technology - Smallcap":                        "IT - Mid & Small",
    "Non Ferrous Metals":                           "Aluminium & Non Ferrous",
    "Miscellaneous":                                "Business Services",
}


def load_and_merge(input_csv, screener_csv):
    # --- Load input.csv ---
    inp_raw = pd.read_csv(input_csv)

    # Detect segment column
    seg_col  = "Segment" if "Segment" in inp_raw.columns else "HKKR Segment" if "HKKR Segment" in inp_raw.columns else None
    fy27_col = "FY27 EPS Est." if "FY27 EPS Est." in inp_raw.columns else "FY27 Est." if "FY27 Est." in inp_raw.columns else None

    cols = ["Name", "NSE Code"]
    if seg_col:  cols.append(seg_col)
    if fy27_col: cols.append(fy27_col)

    inp = inp_raw[cols].copy()
    inp = inp.rename(columns={
        "NSE Code": "nse_code",
        seg_col:    "gvm_segment" if seg_col else None,
        fy27_col:   "fy27_growth"  if fy27_col else None,
    })
    if "gvm_segment" not in inp.columns: inp["gvm_segment"] = "Unknown"
    if "fy27_growth"  not in inp.columns: inp["fy27_growth"]  = float("nan")

    # FIX 8: Filter NaN NSE codes
    inp = inp[inp["nse_code"].notna()].copy()
    inp["nse_code"] = inp["nse_code"].astype(str).str.strip()
    inp = inp[~inp["nse_code"].isin(["", "nan"])].copy()
    # FIX 9: Deduplicate
    inp = inp.drop_duplicates(subset="nse_code", keep="first").reset_index(drop=True)

    inp["gvm_segment"] = inp["gvm_segment"].astype(str).str.strip()
    inp["fy27_growth"]  = pd.to_numeric(inp["fy27_growth"], errors="coerce")

    # FIX 10: Apply manual segment corrections
    for nse_code, correct_seg in SEGMENT_FIXES.items():
        mask = inp["nse_code"] == nse_code
        if mask.any():
            inp.loc[mask, "gvm_segment"] = correct_seg

    # FIX 11: Merge small segments
    inp["gvm_segment"] = inp["gvm_segment"].replace(SEGMENT_MERGE)

    # --- Load screener.csv ---
    scr = pd.read_csv(screener_csv)
    scr = scr.rename(columns={"NSE Code": "nse_code", "Name": "stock_name"})
    scr = scr[scr["nse_code"].notna()].copy()
    scr["nse_code"] = scr["nse_code"].astype(str).str.strip()
    scr = scr[~scr["nse_code"].isin(["", "nan"])].copy()
    scr = scr.drop_duplicates(subset="nse_code", keep="first").reset_index(drop=True)
    scr = scr.rename(columns=SCREENER_COLUMNS)
    keep = ["nse_code", "stock_name"] + list(SCREENER_COLUMNS.values())
    scr  = scr[[c for c in keep if c in scr.columns]]

    # --- Merge ---
    df = inp.merge(scr, on="nse_code", how="inner")
    df["name"] = df["Name"].fillna(df.get("stock_name", df["Name"]))

    # --- Numeric conversion ---
    skip_cols = {"Name", "stock_name", "name", "nse_code", "gvm_segment", "industry_group"}
    for col in df.columns:
        if col not in skip_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # OPM Expansion in basis points
    if "opm_latest_q" in df.columns and "opm_prev_year_q" in df.columns:
        df["opm_expansion"] = (df["opm_latest_q"] - df["opm_prev_year_q"]) * 100
    else:
        df["opm_expansion"] = float("nan")

    # Institutional Holding Absolute
    if "fii_holding" in df.columns and "dii_holding" in df.columns:
        df["inst_holding_abs"] = df["fii_holding"].fillna(0) + df["dii_holding"].fillna(0)
        df.loc[df["fii_holding"].isna() & df["dii_holding"].isna(), "inst_holding_abs"] = float("nan")
    else:
        df["inst_holding_abs"] = float("nan")

    # Institutional Holding Change
    if "fii_change" in df.columns and "dii_change" in df.columns:
        df["inst_holding_change"] = df["fii_change"].fillna(0) + df["dii_change"].fillna(0)
        df.loc[df["fii_change"].isna() & df["dii_change"].isna(), "inst_holding_change"] = float("nan")
    else:
        df["inst_holding_change"] = float("nan")

    # IPO fallback
    if "industry_group" in df.columns:
        mask = df["gvm_segment"].isin(["nan", "", "None"]) | df["gvm_segment"].isna()
        df.loc[mask, "gvm_segment"] = df.loc[mask, "industry_group"].fillna("Unknown")

    # Potential Upside
    def calc_potential_upside(row):
        fy27    = row.get("fy27_growth")
        pe      = row.get("pe")
        hist_pe = row.get("historical_pe")
        if pd.isna(fy27):  return float("nan")
        if fy27 == 0:      return 0.0
        pe_mult = (pe / hist_pe) if (pd.notna(pe) and pd.notna(hist_pe) and hist_pe > 0) else 1.0
        return round(float(fy27) * pe_mult, 4)

    df["potential_upside"] = df.apply(calc_potential_upside, axis=1)

    print(f"✅ Input:   {len(inp)} stocks")
    print(f"✅ Screener:{len(scr)} stocks")
    print(f"✅ Merged:  {len(df)} stocks | {df['gvm_segment'].nunique()} GVM segments")
    return df


def compute_peer_averages(df):
    peer_avgs = {}
    for segment, group in df.groupby("gvm_segment"):
        avgs = {}
        for param in PEER_PARAMS:
            if param in group.columns:
                vals = group[param].dropna()
                if len(vals) >= 3:
                    q_low, q_high = vals.quantile(0.10), vals.quantile(0.90)
                    trimmed = vals[(vals >= q_low) & (vals <= q_high)]
                    avgs[param] = round(trimmed.mean(), 4) if len(trimmed) > 0 else round(vals.mean(), 4)
                elif len(vals) > 0:
                    avgs[param] = round(vals.mean(), 4)
                else:
                    avgs[param] = None
        peer_avgs[segment] = avgs
    print(f"✅ Peer averages: {len(peer_avgs)} GVM segments")
    return peer_avgs


def build_stock_dict(row, peer_avgs):
    segment = row.get("gvm_segment", "Unknown")
    peers   = peer_avgs.get(segment, {})

    def p(col):
        val = peers.get(col)
        return float(val) if val is not None else None

    def v(col):
        val = row.get(col, None)
        if val is None: return None
        try:
            fval = float(val)
            return None if pd.isna(fval) else fval
        except: return None

    return {
        "name":    row.get("name", "Unknown"),
        "price":   v("price") or 0,
        "segment": segment,
        "is_bfsi": segment in BFSI_SEGMENTS,

        "sales_growth_5y":    v("sales_growth_5y"),    "peer_sales_growth_5y":    p("sales_growth_5y"),
        "sales_growth_3y":    v("sales_growth_3y"),    "peer_sales_growth_3y":    p("sales_growth_3y"),
        "profit_growth_5y":   v("profit_growth_5y"),   "peer_profit_growth_5y":   p("profit_growth_5y"),
        "profit_growth_3y":   v("profit_growth_3y"),   "peer_profit_growth_3y":   p("profit_growth_3y"),
        "qoq_sales_growth":   v("qoq_sales_growth"),   "peer_qoq_sales_growth":   p("qoq_sales_growth"),
        "qoq_profit_growth":  v("qoq_profit_growth"),  "peer_qoq_profit_growth":  p("qoq_profit_growth"),
        "opm":                v("opm"),                "peer_opm":                p("opm"),
        "opm_expansion":      v("opm_expansion"),      "peer_opm_expansion":      p("opm_expansion"),
        "fixed_asset_growth": v("fixed_asset_growth"), "peer_fixed_asset_growth": p("fixed_asset_growth"),
        "inst_holding_abs":   v("inst_holding_abs"),   "peer_inst_holding_abs":   p("inst_holding_abs"),
        "inst_holding_change":v("inst_holding_change"),"peer_inst_holding_change":p("inst_holding_change"),
        "roce":               v("roce"),               "peer_roce":               p("roce"),
        "interest_coverage":  v("interest_coverage"),  "peer_interest_coverage":  p("interest_coverage"),
        "dividend_yield":     v("dividend_yield"),     "peer_dividend_yield":     p("dividend_yield"),
        "pe":                 v("pe"),
        "historical_pe":      v("historical_pe"),
        "segment_pe":         v("segment_pe"),
        "potential_upside":   v("potential_upside"),   "peer_potential_upside":   p("potential_upside"),
        "return_1y":          v("return_1y"),          "peer_return_1y":          p("return_1y"),
        "return_3y":          v("return_3y"),          "peer_return_3y":          p("return_3y"),
        "dma_50":             v("dma_50"),
        "dma_200":            v("dma_200"),
        "return_52w_vs_index":v("return_52w_vs_index"),"peer_return_52w_vs_index":p("return_52w_vs_index"),
    }


def run_all_stocks(input_csv, screener_csv):
    from gvm_engine import api_gvm_score
    df        = load_and_merge(input_csv, screener_csv)
    peer_avgs = compute_peer_averages(df)
    results, errors = [], []

    for _, row in df.iterrows():
        try:
            sd = build_stock_dict(row.to_dict(), peer_avgs)
            r  = api_gvm_score(sd)
            r["segment"]  = row.get("gvm_segment", "Unknown")
            r["nse_code"] = row.get("nse_code", "")
            r["price"]    = row.get("price", 0)
            results.append(r)
        except Exception as e:
            errors.append({"stock": row.get("name", "?"), "error": str(e)})

    results_df = pd.DataFrame([{
        "Rank":      0,
        "Stock":     r["stock"],
        "NSE Code":  r["nse_code"],
        "Segment":   r["segment"],
        "Price":     r["price"],
        "G Score":   r["G_score"],
        "V Score":   r["V_score"],
        "M Score":   r["M_score"],
        "GVM Score": r["GVM_score"],
    } for r in results])

    results_df = results_df.sort_values("GVM Score", ascending=False).reset_index(drop=True)
    results_df["Rank"] = results_df.index + 1
    print(f"\n✅ Scored {len(results)} stocks | ⚠️  {len(errors)} errors")
    return results_df, results, peer_avgs


if __name__ == "__main__":
    import sys
    input_csv    = sys.argv[1] if len(sys.argv) > 1 else "input.csv"
    screener_csv = sys.argv[2] if len(sys.argv) > 2 else "screener.csv"
    print(f"\n{'='*60}\n  GVM SCREENER v3 — D5 Final\n{'='*60}")
    results_df, _, _ = run_all_stocks(input_csv, screener_csv)
    print(f"\n{'='*60}\n  TOP 25 STOCKS BY GVM SCORE\n{'='*60}")
    print(results_df.head(25).to_string(index=False))
    results_df.to_csv("gvm_results.csv", index=False)
    print(f"\n✅ Saved to gvm_results.csv\n{'='*60}\n")