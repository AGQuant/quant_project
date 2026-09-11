"""cc#1985 item 2 GATE -- what GVM peer benchmarking does with a 2-member segment.
Runs the REAL gvm_engine functions. Peer medians came straight out of Postgres
(PERCENTILE_CONT(0.5), the same median gvm_nightly._peer_averages takes with pandas).
Only the PEER SET changes between the two scenarios; each company's own values are identical,
so any difference is the n=2 effect and nothing else."""
import sys; sys.path.insert(0, '/home/user/quant_project')
import gvm_engine as E

N25 = {"sales_growth_5y":25.48,"sales_growth_3y":20.47,"profit_growth_5y":43.535,"profit_growth_3y":26.91,
"qoq_sales_growth":26.6165413533835,"qoq_profit_growth":21.75995949285985,"opm":13.53,
"opm_expansion":-55.99999999999996,"fixed_asset_growth":17.89,"inst_holding_abs":17.740000000000002,
"inst_holding_change":0.049999999999999996,"roce":17.08,"interest_coverage":12.33,"dividend_yield":0.15,
"potential_upside":30.720528252868586,"pe":54.045}
N2 = {"sales_growth_5y":11.61,"sales_growth_3y":-1.085,"profit_growth_5y":48.79,"profit_growth_3y":-10.675,
"qoq_sales_growth":18.85757058856875,"qoq_profit_growth":25.465548721675653,"opm":6.470000000000001,
"opm_expansion":775.0000000000002,"fixed_asset_growth":2.445,"inst_holding_abs":17.005000000000003,
"inst_holding_change":-1.02,"roce":6.465,"interest_coverage":12.26,"dividend_yield":1.155,
"potential_upside":45.52079525156054,"pe":40.075}
HEG = {"nse_code":"HEG","sales_growth_5y":15.41,"sales_growth_3y":1.41,"profit_growth_5y":46.19,
"profit_growth_3y":-13.73,"qoq_sales_growth":11.098599823754,"qoq_profit_growth":22.5728884881274,
"opm":9.97,"opm_expansion":487.0000000000001,"fixed_asset_growth":0.15,"inst_holding_abs":17.740000000000002,
"inst_holding_change":-1.12,"roce":8.35,"interest_coverage":12.33,"dividend_yield":1.35,"pe":14.22,
"historical_pe":7.03,"potential_upside":36.1871692745377}
GRA = {"nse_code":"GRAPHITE","sales_growth_5y":7.81,"sales_growth_3y":-3.58,"profit_growth_5y":51.39,
"profit_growth_3y":-7.62,"qoq_sales_growth":26.6165413533835,"qoq_profit_growth":28.3582089552239,
"opm":2.97,"opm_expansion":1063.0000000000002,"fixed_asset_growth":4.74,"inst_holding_abs":16.27,
"inst_holding_change":-0.92,"roce":4.58,"interest_coverage":12.19,"dividend_yield":0.96,"pe":65.93,
"historical_pe":23.93,"potential_upside":54.85442122858338}
PP = [k for k in N25]

def sdict(r, peers):
    d = {"name": r["nse_code"], "segment": "x", "is_bfsi": False,
         "pe": r["pe"], "historical_pe": r["historical_pe"], "segment_pe": peers["pe"]}
    for p in PP:
        if p == "pe": continue
        d[p] = r.get(p); d["peer_"+p] = peers.get(p)
    return d

print("="*100)
print("cc#1985 GATE -- 2-MEMBER SEGMENT, REAL gvm_engine, HEG + GRAPHITE")
print("="*100)
res = {}
for tag, peers, n in (("A n=25 (today, Electronics - Consumer & Smart)", N25, 25),
                      ("B n=2  (proposed, Graphite Electrodes)", N2, 2)):
    print("\n%s" % tag)
    for r in (HEG, GRA):
        d = sdict(r, peers); g = E.api_g_score(d); v = E.api_v_score(d)
        res[(r["nse_code"], n)] = (g, v)
        print("   %-9s  G = %5.2f   V = %5.2f" % (r["nse_code"], g["score"], v["score"]))

print("\nDELTA (n=2 minus n=25) -- the peer set is the ONLY thing that changed")
for s in ("HEG","GRAPHITE"):
    g25,v25 = res[(s,25)]; g2,v2 = res[(s,2)]
    print("   %-9s  G %+5.2f   V %+5.2f" % (s, g2["score"]-g25["score"], v2["score"]-v25["score"]))

print("\n" + "="*100)
print("WHY -- the relative half (score_relative) of every peer parameter")
print("="*100)
print("%-21s | %-19s | %-19s | %s" % ("parameter","n=25  HEG / GRAPH","n=2   HEG / GRAPH","n=2 pair sum"))
print("-"*100)
t25 = t2 = 0; sums = []
for p in PP:
    if p == "pe": continue
    out = []
    for peers in (N25, N2):
        out.append([E.score_relative(r[p], peers[p]) for r in (HEG, GRA)])
    if out[0][0] == out[0][1]: t25 += 1
    if out[1][0] == out[1][1]: t2 += 1
    sums.append(out[1][0] + out[1][1])
    fm = lambda c: "%5.1f / %-5.1f" % tuple(c)
    print("%-21s | %-19s | %-19s | %6.1f" % (p, fm(out[0]), fm(out[1]), out[1][0]+out[1][1]))
print("-"*100)
print("same relative score for BOTH members: n=25 -> %d of %d params   n=2 -> %d of %d params"
      % (t25, len(PP)-1, t2, len(PP)-1))
print("n=2 pair sums, distinct values seen: %s" % sorted(set(sums)))
print("n=2 pair-sum mean: %.4f  (12.5/2 = 6.25 per member, on EVERY parameter)" % (sum(sums)/len(sums)))
