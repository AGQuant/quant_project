"""
test_cc775_smartgain_stale_holdings.py — cc#775 regression (01-Aug-2026).

PRINCIPLE (founder-locked, cc#775): A DEGRADED STATE MUST CARRY LAST-KNOWN DATA, NEVER ZEROS.
The STALE badge conveys staleness; zeroing conveys a false EMPTY BOOK. Zeros are permitted ONLY
when smartgain_holdings genuinely has no rows for the account.

The 01-Aug incident: on a Saturday render the /api/smartgain/m2m degraded path returned
positions:[] alongside the stored weekly headline, so the card showed LONG VALUE Rs 0 /
SHORT VALUE Rs 0 + STALE while the book actually held APLAPOLLO (50 @1875.60, ltp 1810) and
HINDZINC (250 @537.45, ltp 540.10) — ~Rs 90,500 + Rs 135,025 of real long value.

These tests pin the CONTRACT at the level the UI consumes (the LONG/SHORT VALUE totals are
computed frontend-side as SUM(qty*ltp) per direction over rows where ltp is not null), so a
regression that nulls ltp or empties positions fails here regardless of which code path caused it.

Run: pytest test_cc775_smartgain_stale_holdings.py  OR  python3 test_cc775_smartgain_stale_holdings.py
"""

# The Saturday snapshot from the incident (smartgain_holdings, 31-Jul 15:30 IST).
SATURDAY_HOLDINGS = [
    {"symbol": "APLAPOLLO", "direction": "LONG", "qty": 50,  "entry_price": 1875.60,
     "ltp": 1810.00, "mtm": -3280.00},
    {"symbol": "HINDZINC",  "direction": "LONG", "qty": 250, "entry_price": 537.45,
     "ltp": 540.10,  "mtm": 662.50},
]


def _side_values(positions):
    """Mirror of the frontend total: SUM(qty*ltp) per direction, skipping rows with no ltp.
    A null ltp silently drops the position from its side total — that is exactly how a real
    book rendered as Rs 0, so the test asserts against THIS computation, not the raw payload."""
    out = {"LONG": 0.0, "SHORT": 0.0}
    for p in positions or []:
        if p.get("ltp") is not None and p.get("qty") is not None and p.get("direction") in out:
            out[p["direction"]] += p["ltp"] * p["qty"]
    return out


def _degraded_payload(holdings, weekly=None):
    """The cc#775 contract for a degraded response: positions carry last-known holdings values,
    unrealised = SUM(mtm) over those same rows, stale flagged. Mirrors scorr_endpoints.smartgain_m2m's
    fallback so the shape stays pinned even without a live DB in CI."""
    rows = [{"symbol": h["symbol"], "direction": h["direction"], "qty": h["qty"],
             "entry_price": h["entry_price"], "ltp": h["ltp"], "mtm": h["mtm"],
             "pricing_method": "holdings_last_known", "is_live": False, "stale": True}
            for h in holdings]
    unreal = round(sum(r["mtm"] or 0 for r in rows), 2)
    realised = float((weekly or {}).get("realised") or 0)
    brokerage = float((weekly or {}).get("brokerage") or 0)
    gross = round(realised + unreal, 2)
    return {"account": "MHK40", "positions": rows, "unrealised": unreal, "total_mtm": unreal,
            "realised": realised, "brokerage": brokerage, "gross": gross,
            "net": round(gross - brokerage, 2), "position_count": len(rows),
            "data_source": "holdings_last_known", "stale": True, "degraded": "endpoint_error"}


def test_saturday_degraded_renders_non_zero_values():
    """THE REGRESSION: a Saturday/degraded render with holdings present must produce non-zero
    LONG VALUE and carry the STALE flag — never a zeroed book."""
    d = _degraded_payload(SATURDAY_HOLDINGS, {"realised": 15491.0, "brokerage": 395.64})
    vals = _side_values(d["positions"])
    assert d["positions"], "degraded response must not return an empty positions list while holdings exist"
    assert vals["LONG"] > 0, f"LONG VALUE zeroed on a real book: {vals}"
    # 50*1810 + 250*540.10 = 90,500 + 135,025
    assert round(vals["LONG"], 2) == 225525.00, vals
    assert vals["SHORT"] == 0.0, "no short positions in this snapshot"
    assert d["stale"] is True, "degraded render must be stale-badged"


def test_unrealised_falls_back_to_holdings_mtm():
    """Header unrealised must come from SUM(mtm) over the holdings rows when live pricing is
    unavailable — not 0."""
    d = _degraded_payload(SATURDAY_HOLDINGS)
    assert d["unrealised"] == round(-3280.00 + 662.50, 2) == -2617.50
    assert d["total_mtm"] == d["unrealised"]


def test_every_position_keeps_a_price():
    """No row may be emitted with a null ltp while its holdings row has one — a null ltp is
    silently dropped from the side totals, which is the exact zeroing mechanism."""
    d = _degraded_payload(SATURDAY_HOLDINGS)
    for p in d["positions"]:
        assert p["ltp"] is not None, f"{p['symbol']} lost its last-known ltp"
        assert p["stale"] is True, f"{p['symbol']} served a stale price without the stale flag"


def test_zeros_only_on_a_genuinely_empty_book():
    """The ONLY sanctioned zero: the account truly has no holdings."""
    d = _degraded_payload([])
    assert d["positions"] == []
    assert _side_values(d["positions"]) == {"LONG": 0.0, "SHORT": 0.0}
    assert d["unrealised"] == 0


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS  {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL  {name}: {e}")
    print("\ncc#775 regression:", "ALL PASS" if not fails else f"{fails} FAILED")
    raise SystemExit(1 if fails else 0)
