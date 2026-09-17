"""cc#2176 -- /api/mobile/tcscan: open.by_side (per-side unrealised split from the SAME per-row pnl_rs the
rows carry) and record.ALL (both sides, from the same _record query rolled up)."""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
for _m in ("psycopg", "psycopg2", "psycopg2.extras"):
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = types.ModuleType(_m)
sam = pytest.importorskip("scanners_app_mobile")


def _row(sym, side, entry, cmp, lot):
    pnl_pct = None if cmp is None else round((cmp - entry) / entry * 100 * (1 if side == "BUY" else -1), 2)
    pnl_rs = None if (cmp is None or lot is None) else round((cmp - entry) * lot * (1 if side == "BUY" else -1), 2)
    return {"symbol": sym, "side": side, "entry_price": entry, "cmp": cmp, "lot_size": lot,
            "pnl_pct": pnl_pct, "pnl_rs": pnl_rs, "score": 85, "entry_ts": "2026-09-17 09:15:00",
            "target": None, "sl": None, "exit_price": None, "exit_ts": None, "exit_reason": "OPEN"}


BUY = [_row("A", "BUY", 90.0, 100.0, 250), _row("B", "BUY", 50.0, None, 100)]          # 2 open, 1 priced (B has no cmp)
SELL = [_row("C", "SELL", 200.0, 190.0, 10), _row("D", "SELL", 300.0, 310.0, None)]     # 2 open, 1 priced (D has no lot)
BOOK = {"n": 4, "priced": 2, "rows_without_lot": 1, "rows_without_cmp": 1, "unrealised_rs_one_lot": 2600.0}
HOLDS = {"date": "2026-09-17", "as_of": "2026-09-17 14:20:00", "last_closure_date": "2026-09-17",
         "open_all": {"buy": BUY, "sell": SELL}, "open_all_count": 4, "open_all_book": BOOK,
         "closed_by_exit": {"buy": [], "sell": []}, "closed_by_exit_stats": {}, "closed_by_exit_book": {}, "lot_rule": "x"}
RECORD_ROWS = [("ALL", 36, 16, 15.89, "2026-09-07", "2026-09-17", 36, 43980.25),
               ("BUY", 7, 1, -13.78, "2026-09-07", "2026-09-15", 7, -102222.75),
               ("SELL", 29, 15, 29.68, "2026-09-07", "2026-09-17", 29, 146203.0)]


class _Cur:
    executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        self.sql = sql
        _Cur.executed.append(sql)

    def fetchall(self):
        if "ROLLUP" in self.sql:
            return RECORD_ROWS
        return [("2026-09-17",), ("2026-09-16",)]

    def cursor(self):
        return _Cur()


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(sam, "_guard", lambda r: None)
    monkeypatch.setattr(sam, "tc_scanner_holds", lambda d: HOLDS)
    monkeypatch.setattr(sam, "tc_scanner_spec", lambda: {})
    monkeypatch.setattr(sam, "_conn", lambda: _Cur())
    _Cur.executed = []


def test_by_side_sums_the_same_per_row_rupee_and_counts_the_rest(stubbed):
    out = sam.mobile_tcscan(None)
    bs = out["open"]["by_side"]
    assert bs["BUY"] == {"count": 2, "priced": 1, "rows_without_lot": 0, "rows_without_cmp": 1,
                         "unrealised_rs": 2500.0, "unrealised_pts_pct": 11.11}
    assert bs["SELL"] == {"count": 2, "priced": 1, "rows_without_lot": 1, "rows_without_cmp": 0,
                          "unrealised_rs": 100.0, "unrealised_pts_pct": round(5.0 + (-3.33), 2)}
    # one formula: the two sides add up to the book total the page header already prints
    assert bs["BUY"]["unrealised_rs"] + bs["SELL"]["unrealised_rs"] == out["open"]["book"]["unrealised_rs_one_lot"]
    assert "one lot" in out["open"]["by_side_basis"]


def test_a_side_with_nothing_open_is_none_never_zero(stubbed):
    assert sam._by_side({"buy": [], "sell": SELL})["BUY"] == {"count": 0, "priced": 0, "rows_without_lot": 0,
                                                              "rows_without_cmp": 0, "unrealised_rs": None,
                                                              "unrealised_pts_pct": None}
    only_unpriced = sam._by_side({"buy": [_row("B", "BUY", 50.0, None, None)], "sell": []})["BUY"]
    assert only_unpriced["count"] == 1 and only_unpriced["unrealised_rs"] is None and only_unpriced["unrealised_pts_pct"] is None


def test_record_all_comes_from_the_same_rolled_up_query(stubbed):
    out = sam.mobile_tcscan(None)
    rec = out["record"]
    assert set(rec) == {"ALL", "BUY", "SELL"}
    assert rec["ALL"] == {"closed": 36, "wins": 16, "wr_pct": 44.4, "net_pts_pct": 15.89, "since": "2026-09-07",
                          "last": "2026-09-17", "net_rs_one_lot": 43980.25, "rs_rows": 36}
    assert rec["BUY"]["wr_pct"] == 14.3 and rec["SELL"]["wr_pct"] == 51.7
    sql = [q for q in _Cur.executed if "tc_scanner_holds h" in q][0]
    assert "GROUP BY ROLLUP (h.side)" in sql and "GROUPING(h.side)" in sql


def test_old_keys_untouched(stubbed):
    out = sam.mobile_tcscan(None)
    assert set(out["open"]) == {"buy", "sell", "count", "book", "by_side", "by_side_basis"}
    assert out["open"]["count"] == 4 and out["open"]["book"] is BOOK
    assert [r["symbol"] for r in out["open"]["buy"]] == ["A", "B"] and out["open"]["buy"][0]["pnl_rs"] == 2500.0
