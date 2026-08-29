"""
segment_pe_fallback.py — cc#1426: screener_raw.segment_pe ("Industry PE") is a dead column. The
Screener.in wide-format CSV export (loaded via load_screener_from_drive) no longer carries an
"Industry PE" column at all — checked the raw header row directly, 44 columns, none of them it.
COUNT(*) / COUNT(segment_pe) on screener_raw returns 1872 / 0: every single row is null, confirmed
on INNOVACAP, CIPLA, SUNPHARMA and PATANJALI, not an isolated symbol. A PATANJALI result_analysis_v2
card frozen on 16-Aug (polished_by=claude_web) still carries "industry 52.1" baked into its stored
text, proving the export DID carry the column once — it vanished from the source sometime after
16-Aug. This is a data-source gap, not a loader bug (screener_loader.py maps "Industry PE" straight
to segment_pe; it has nothing to map from any more).

Step 1 of cc#1426's own scope — flag it to the founder directly, since Arpit owns the Screener.in
export/CSV configuration per this project's role charter — is a conversation, not code, and stays
out of this file.

This module is step 2: compute OUR OWN segment/industry PE, independent of that external export,
so this stops depending on a field that can silently vanish again. ONE_REGISTRY_ONE_DERIVATION_V1:
ONE batched query (segment -> our own trimmed-mean PE, matching screener_loader.compute_peer_averages'
own >=3-values / 10th-90th-percentile-trim / else-plain-mean technique), computed ONCE per caller and
reused across every symbol in that caller's batch — never a per-symbol query in a loop.

Every consumer must state the basis (BASIS_LABEL below) whenever this fallback is shown, so a reader
is never told a number came from Screener.in's Industry PE when it is actually Scorr's own peer-group
computation — the same honesty rule as everywhere else in this codebase.
"""

BASIS_LABEL = "peer avg PE"   # cc#1426: the stated basis whenever this fallback is used


def _trimmed_mean(vals):
    """Same technique as screener_loader.compute_peer_averages: >=3 values -> drop outside the
    10th-90th percentile band, mean the rest; 1-2 values -> plain mean; 0 values -> None."""
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return None
    if n < 3:
        return round(sum(vals) / n, 2)

    def _q(p):
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return vals[lo] + (vals[hi] - vals[lo]) * frac

    q_low, q_high = _q(0.10), _q(0.90)
    trimmed = [v for v in vals if q_low <= v <= q_high]
    use = trimmed if trimmed else vals
    return round(sum(use) / len(use), 2)


def segment_pe_map(cur):
    """ONE batched query across the whole scored universe -> {segment: {"value": x, "n": n}}.
    market_cap > 2000 and pe > 0 (excludes micro-caps and loss-making/negative-PE distortions),
    per cc#1426's own spec. Call this ONCE per request/batch and reuse the returned dict — never
    call it per-symbol."""
    cur.execute("""
        SELECT g.segment, s.pe
        FROM gvm_scores g
        JOIN screener_raw s ON UPPER(s.nse_code) = g.symbol
        WHERE g.score_date = (SELECT MAX(score_date) FROM gvm_scores)
          AND g.market_cap > 2000 AND s.pe IS NOT NULL AND s.pe > 0
    """)
    by_seg = {}
    for seg, pe in cur.fetchall():
        if not seg:
            continue
        by_seg.setdefault(seg, []).append(float(pe))
    out = {}
    for seg, vals in by_seg.items():
        out[seg] = {"value": _trimmed_mean(vals), "n": len(vals)}
    return out


def lookup(pe_map, segment):
    """Convenience accessor: (value, basis_label_with_n) for one segment, or (None, None) when the
    segment has no coverage. basis_label is e.g. 'peer avg PE (n=8)' — always state this alongside
    the number, per the honesty rule in this module's docstring."""
    if not segment:
        return None, None
    entry = pe_map.get(segment)
    if not entry or entry["value"] is None:
        return None, None
    return entry["value"], f"{BASIS_LABEL} (n={entry['n']})"
