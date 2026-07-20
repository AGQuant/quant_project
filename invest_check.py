"""
invest_check.py — ARCHIVED / SUPERSEDED (cc#588, 2026-07-20).

The v2.0 archetype count-model (COMPOUNDER / VALUE-QUALITY / BLEND) is RETIRED and replaced by the
unified Investment Check v3.0 Gate + Conviction engine in `investment_check.py`
(spec session_log id=6632). The 3 hard gates replace what archetype-gated thresholds used to do.

This thin re-export remains ONLY so any lingering `from invest_check import compute_invest_check`
import keeps working; it forwards to the v3.0 engine. No v2.0 logic runs anymore.
"""

from investment_check import compute_invest_check  # noqa: F401  (v3.0 engine, single source)


def native_invest_check(query, use_api=False):
    """Legacy /ask markdown wrapper — now renders the v3.0 gate+conviction result."""
    d = compute_invest_check(query, use_api=use_api)
    if not d.get("ok"):
        return f"**Investment Check v3.0**\n{d.get('error', 'error')}"
    lines = [f"**Investment Check v3.0 — {d.get('company')} ({d.get('symbol')})**",
             f"_{d.get('segment')} · GVM {d.get('gvm')} · {d.get('verdict_display')} "
             f"({d.get('verdict')})_", ""]
    lines.append("**Gates (3 hard):**")
    for g in d.get("gates", []):
        mark = "PASS" if g["pass"] is True else ("FAIL" if g["pass"] is False else "NO DATA")
        lines.append(f"| {g['gate']} {g['name']} | {g['condition']} | {g['value']} | {mark} |")
    lines.append("")
    lines.append(f"**Conviction: {d.get('conviction')}/{d.get('max_conviction')}**")
    for fl in d.get("filters", []):
        lines.append(f"| {fl['code']} {fl['name']} | {fl['points']}/{fl['max']} | {fl['value']} |")
    lines.append(f"\n---\n**Verdict: {d.get('emoji')} {d.get('verdict_display')} ({d.get('verdict')})**")
    return "\n".join(lines)
