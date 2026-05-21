import pandas as pd
import warnings
warnings.filterwarnings('ignore')
from screener_loader import load_and_merge, compute_peer_averages, build_stock_dict
from gvm_engine import api_gvm_score

# ============================================================
# GVM ANALYTICS — Scoring + Commentary in one pass
# Output: gvm_analytics.csv (single source of truth)
# ============================================================

def label_growth(s):
    if s >= 8.0: return "Excellent"
    elif s >= 6.5: return "Healthy"
    elif s >= 5.0: return "Average"
    else: return "Weak"

def label_value(s):
    if s >= 7.5: return "Attractive"
    elif s >= 6.0: return "Fair"
    elif s >= 4.5: return "Premium"
    else: return "Expensive"

def label_momentum(s):
    if s >= 8.0: return "Strong"
    elif s >= 6.0: return "Positive"
    elif s >= 4.0: return "Neutral"
    else: return "Weak"

def label_gvm(s):
    if s >= 8.0: return "Excellent"
    elif s >= 7.0: return "Good"
    elif s >= 6.0: return "Average"
    elif s >= 5.0: return "Below Average"
    else: return "Poor"

def verdict(s):
    if s >= 8.0: return "Strong Buy"
    elif s >= 7.0: return "Buy"
    elif s >= 6.0: return "Accumulate"
    elif s >= 5.0: return "Wait & Watch"
    else: return "Avoid"

def make_punchline(verd, g_lbl, v_lbl, m_lbl, gvm_lbl):
    action = {
        "Strong Buy":   "It is highly recommended to Buy",
        "Buy":          "It is recommended to Buy",
        "Accumulate":   "It is advisable to Accumulate",
        "Wait & Watch": "It is advisable to Wait & Watch",
        "Avoid":        "It is advisable to Avoid",
    }[verd]
    return (f"{action} for medium to long term perspective, considering its "
            f"{g_lbl} Growth, {v_lbl} Valuation, {m_lbl} Momentum & {gvm_lbl} overall GVM Rating.")


def score_and_label(row, peer_avgs):
    sd  = build_stock_dict(row.to_dict(), peer_avgs)
    gvm = api_gvm_score(sd)

    g, v, m, total = gvm['G_score'], gvm['V_score'], gvm['M_score'], gvm['GVM_score']
    g_lbl   = label_growth(g)
    v_lbl   = label_value(v)
    m_lbl   = label_momentum(m)
    gvm_lbl = label_gvm(total)
    verd    = verdict(total)
    punch   = make_punchline(verd, g_lbl, v_lbl, m_lbl, gvm_lbl)

    return {
        "NSE Code":    row.get("nse_code", ""),
        "Name":        row.get("name", ""),
        "Segment":     row.get("gvm_segment", ""),
        "Price":       row.get("price", 0),
        "G Score":     g,
        "V Score":     v,
        "M Score":     m,
        "GVM Score":   total,
        "Growth":      g_lbl,
        "Value":       v_lbl,
        "Momentum":    m_lbl,
        "GVM Overall": gvm_lbl,
        "Verdict":     verd,
        "Punchline":   punch,
    }


def run_all(input_csv="input.csv", screener_csv="screener.csv"):
    df        = load_and_merge(input_csv, screener_csv)
    peer_avgs = compute_peer_averages(df)

    results, errors = [], []
    for _, row in df.iterrows():
        try:
            results.append(score_and_label(row, peer_avgs))
        except Exception as e:
            errors.append({"stock": row.get("name","?"), "error": str(e)})

    out = pd.DataFrame(results)
    out = out.sort_values("GVM Score", ascending=False).reset_index(drop=True)
    out["Rank"] = out.index + 1
    cols = ["Rank","NSE Code","Name","Segment","Price",
            "G Score","V Score","M Score","GVM Score",
            "Growth","Value","Momentum","GVM Overall","Verdict","Punchline"]
    out = out[cols]
    out.to_csv("gvm_analytics.csv", index=False)
    print(f"\n✅ Scored + labelled {len(out)} stocks | ⚠️  {len(errors)} errors")
    print(f"✅ Saved to gvm_analytics.csv")
    return out


if __name__ == "__main__":
    import sys
    input_csv    = sys.argv[1] if len(sys.argv) > 1 else "input.csv"
    screener_csv = sys.argv[2] if len(sys.argv) > 2 else "screener.csv"

    print(f"\n{'='*60}\n  GVM ANALYTICS — Scoring + Commentary\n{'='*60}")
    out = run_all(input_csv, screener_csv)

    print(f"\n{'='*60}\n  SAMPLE — TOP 10\n{'='*60}")
    for _, r in out.head(10).iterrows():
        print(f"\n{r['Rank']:>2}. {r['Name']} ({r['NSE Code']}) | GVM={r['GVM Score']}")
        print(f"    Verdict: {r['Verdict']}")
        print(f"    {r['Punchline']}")
