"""cc#2196: the watchlist store's pure shaping, validation, and the add-time capture query (stub cursor)."""
import watchlist_app_mobile as m


def test_constants_and_identity_gate():
    assert m.MAX_LISTS == 5 and m.USER_ID == "founder" and m.NAME_MAX == 40
    assert any("user_watchlists" in d for d in m.DDL) and any("user_watchlist_items" in d for d in m.DDL)
    assert all(not d.strip().upper().startswith(("ALTER", "DROP")) for d in m.DDL)   # CREATE-only


def test_validate_name_and_symbol():
    assert m.validate_name("  Trading   watchlist ") == ("Trading watchlist", None)
    assert m.validate_name("")[1].startswith("Give the watchlist")
    assert m.validate_name("x" * 41)[1].startswith("Keep the name")
    assert m.validate_symbol("reliance") == ("RELIANCE", None)
    assert m.validate_symbol("M&M") == ("M&M", None) and m.validate_symbol("BAJAJ-AUTO") == ("BAJAJ-AUTO", None)
    assert m.validate_symbol("bad symbol!")[1] is not None


def test_shape_lists_counts_avg_and_top_mover():
    lists = [(1, "Trading", 1, "2026-09-17 12:00:00+00:00"), (2, "Investment", 2, "2026-09-17 12:01:00+00:00")]
    items = [(1, "RELIANCE"), (1, "TCS"), (1, "NOPRICE"), (2, "INFY")]
    out = m.shape_lists(lists, items, {"RELIANCE": 1.5, "TCS": -0.5, "INFY": 2.25})
    assert out[0]["count"] == 3 and out[0]["priced"] == 2 and out[0]["avg_day_pct"] == 0.5 and out[0]["top_mover"] == {"symbol": "RELIANCE", "day_pct": 1.5}
    assert out[1]["symbols"] == ["INFY"] and out[1]["avg_day_pct"] == 2.25
    empty = m.shape_lists([(3, "Sell", 3, None)], [], {})
    assert empty[0]["count"] == 0 and empty[0]["avg_day_pct"] is None and empty[0]["top_mover"] is None


def test_shape_items_sorted_by_day_pct_with_since_add_and_gvm_delta():
    rows = [("TCS", "2026-09-17 09:00:00+00:00", "gvm", 5.41, 2200.0), ("RELIANCE", "2026-09-17 09:05:00+00:00", "tcscan", 4.5, 1200.0), ("NOPX", None, None, None, None)]
    prices = {"TCS": (2190.0, "2026-09-17 15:33"), "RELIANCE": (1243.9, "2026-09-17 15:33")}
    prev = {"TCS": 2210.0, "RELIANCE": 1230.0}
    ratings = {"TCS": {"gvm": 5.41, "verdict": "Weak", "segment": "IT - Large", "score_date": "2026-09-16"},
               "RELIANCE": {"gvm": 4.77, "verdict": "Weak", "segment": "Refineries", "score_date": "2026-09-16"}}
    out = m.shape_items(rows, prices, prev, ratings, {"TCS": "Tata Consultancy Services Ltd"})
    assert [r["symbol"] for r in out] == ["RELIANCE", "TCS", "NOPX"]           # +1.13 %, −0.9 %, unpriced last
    rel = out[0]
    assert rel["day_pct"] == 1.13 and rel["since_add_pct"] == 3.66 and rel["gvm_delta"] == 0.27 and rel["added_on"] == "17 Sep 26"
    assert out[1]["company_name"].startswith("Tata") and out[1]["gvm_delta"] == 0.0
    assert out[2]["cmp"] is None and out[2]["day_pct"] is None and out[2]["gvm"] is None


class _Cur:
    """Stub cursor: answers the three capture queries in order, records the SQL."""
    def __init__(self, gvm, cmp, close):
        self.calls, self._q = [], [(gvm,), (cmp,), (close,)]
    def execute(self, sql, params=None):
        self.calls.append((sql, params)); self._last = self._q.pop(0) if self._q else None
    def fetchone(self):
        v = self._last; return None if (v is None or v[0] is None) else v


def test_capture_at_add_reads_latest_gvm_and_live_cmp_with_close_fallback():
    cur = _Cur(6.3, 1243.9, None)
    assert m.capture_at_add(cur, "RELIANCE") == (6.3, 1243.9)
    assert "gvm_history" in cur.calls[0][0] and "ORDER BY score_date DESC" in cur.calls[0][0] and "cmp_prices" in cur.calls[1][0]
    cur2 = _Cur(None, None, 987.5)
    assert m.capture_at_add(cur2, "XYZ") == (None, 987.5) and "raw_prices" in cur2.calls[2][0]
