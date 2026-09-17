"""cc#2194: the snapshot payload is the shelf row passed through — nulls stay nulls, nothing re-derived."""
import portfolio_app_mobile as m


def _row(**kw):
    base = {"id": 165, "name": "Akshay Gupta", "category": "PMS", "active": True, "n_holdings": 25,
            "invested": 2768000.0, "current": 3100000.0, "cash": 276000.0, "total_portfolio": 3376000.0,
            "pnl": 608000.0, "pnl_pct": 21.97, "basis": "meta", "needs_invested_amount": False,
            "realised_pnl": 0.0, "unrealised_pnl": 400000.0, "holdings_cost": 2700000.0,
            "start_date": "2025-05-15", "start_source": "alpha", "nifty_pct": 4.1, "alpha_pct": 17.9,
            "xirr": None, "nifty_xirr": None, "alpha_xirr": None, "xirr_reason": "no_ledger",
            "ledger_rows": 0, "payin_total": None, "payout_total": None, "net_payin": None,
            "ledger_first_date": None, "ledger_last_date": None}
    base.update(kw)
    return base


def test_default_pid_is_the_reference_portfolio():
    assert m.DEFAULT_PID == 165


def test_pick_row_finds_the_pid_or_none():
    rows = [_row(id=4, name="Vishal Bhosale"), _row()]
    assert m.pick_row(rows, 165)["name"] == "Akshay Gupta"
    assert m.pick_row(rows, 999) is None
    assert m.pick_row(None, 165) is None


def test_shape_passes_the_row_through_and_keeps_nulls():
    out = m.shape(_row())
    s = out["snapshot"]
    assert s["total_portfolio"] == 3376000.0 and s["pnl_pct"] == 21.97 and s["alpha_pct"] == 17.9
    assert s["xirr"] is None and out["xirr_note"].startswith("No ledger on record")
    cf = out["cash_flows"]
    assert cf["has_ledger"] is False and cf["payin_total"] is None and cf["net_payin"] is None and cf["period"] is None
    assert cf["invested_set_manually"] is False
    assert "same row the web Adaptive Dashboard" in out["source"]


def test_shape_with_a_ledger_carries_the_period_and_the_manual_marker():
    out = m.shape(_row(xirr=18.2, nifty_xirr=6.1, alpha_xirr=12.1, xirr_reason=None,
                       ledger_rows=12, payin_total=2500000.0, payout_total=100000.0, net_payin=2400000.0,
                       ledger_first_date="2025-05-15", ledger_last_date="2026-09-10"))
    assert out["xirr_note"] is None and out["snapshot"]["xirr"] == 18.2
    cf = out["cash_flows"]
    assert cf["has_ledger"] and cf["period"] == "since 2025-05-15" and cf["net_payin"] == 2400000.0
    assert cf["invested_set_manually"] is True   # 2,400,000 vs 2,768,000 invested


def test_shape_without_an_invested_amount_says_so():
    out = m.shape(_row(invested=None, pnl=None, pnl_pct=None, basis="unknown_cost", needs_invested_amount=True, alpha_pct=None))
    assert out["basis_note"].startswith("Invested amount not set")
    assert out["snapshot"]["pnl_pct"] is None and out["snapshot"]["alpha_pct"] is None
