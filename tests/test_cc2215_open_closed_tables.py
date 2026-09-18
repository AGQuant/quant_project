"""cc#2215 V8 four bucket tabs -- OPEN/CLOSED table math and the cross-basket side-family pairing.
Pure functions only (no DB, no network): _cc2215_side_family, _cc2215_open_row, _cc2215_closed_row.
The two _table wrappers that hit the database (_cc2215_open_table's caller open_pos and
_cc2215_closed_table's SQL) are exercised through these row-level functions; the DB round trip
itself is the founder's/Fable's live check (no DATABASE_URL in this sandbox)."""
import v8_endpoints as m

ROUND_TRIP = m.BROKERAGE_PER_TRADE * 2   # 1000, same constant the frontend's BROKERAGE_PER_TRADE*2 uses


def test_side_family_buy_and_sell_pair_correctly():
    assert m._cc2215_side_family("buy_momentum") == ("LONG", ("buy_momentum", "buy_reversal"))
    assert m._cc2215_side_family("buy_reversal") == ("LONG", ("buy_momentum", "buy_reversal"))
    assert m._cc2215_side_family("sell_momentum") == ("SHORT", ("sell_momentum", "sell_reversal"))
    assert m._cc2215_side_family("sell_reversal") == ("SHORT", ("sell_momentum", "sell_reversal"))


def test_open_row_long_profit():
    row = m._cc2215_open_row("RELIANCE", {"side": "LONG", "entry_price": 1000, "cmp": 1050, "qty": 10,
                                           "entry_ts": "2026-09-17T11:10:00", "pnl_pct": 5.0})
    assert row["net_pnl"] == 500.0          # (1050-1000)*10
    assert row["net_pnl_pct"] == 5.0
    assert row["side"] == "LONG"
    assert "days" not in row                # frontend derives DAYS via holdDays(entry_ts), not the API


def test_open_row_short_profit():
    # SHORT: price falling is the win -- (entry - cmp) * qty
    row = m._cc2215_open_row("LTM", {"side": "SHORT", "entry_price": 1200, "cmp": 1150, "qty": 20,
                                      "entry_ts": "2026-09-17T11:10:00", "pnl_pct": 4.17})
    assert row["net_pnl"] == 1000.0         # (1200-1150)*20


def test_open_row_long_loss():
    row = m._cc2215_open_row("X", {"side": "LONG", "entry_price": 1000, "cmp": 950, "qty": 10,
                                    "entry_ts": None, "pnl_pct": -5.0})
    assert row["net_pnl"] == -500.0


def test_open_row_missing_cmp_leaves_net_pnl_none_but_keeps_pct():
    row = m._cc2215_open_row("Y", {"side": "LONG", "entry_price": 1000, "cmp": None, "qty": 10,
                                    "entry_ts": None, "pnl_pct": None})
    assert row["net_pnl"] is None
    assert row["net_pnl_pct"] is None


def test_open_table_sorts_by_entry_ts_ascending():
    open_pos = {
        "B": {"side": "LONG", "entry_price": 100, "cmp": 110, "qty": 1, "entry_ts": "2026-09-17T10:00:00"},
        "A": {"side": "LONG", "entry_price": 100, "cmp": 110, "qty": 1, "entry_ts": "2026-09-16T09:00:00"},
    }
    out = m._cc2215_open_table(open_pos)
    assert [r["symbol"] for r in out] == ["A", "B"]


def test_closed_row_target_win_nets_round_trip_brokerage():
    row = m._cc2215_closed_row({"symbol": "X", "side": "LONG", "entry_ts": "2026-08-01T09:20:00",
                                 "entry_price": 1000, "exit_ts": "2026-08-01T14:00:00", "exit_price": 1030,
                                 "qty": 10, "result": "TARGET", "pnl": 300}, ROUND_TRIP)
    assert row["net_pnl"] == 300 - ROUND_TRIP == -700   # a small win still nets negative after Rs.1000 round trip
    assert row["net_pnl_pct"] == round(-700 / (1000 * 10) * 100, 2)
    assert row["result"] == "TARGET"


def test_closed_row_sl_loss():
    row = m._cc2215_closed_row({"symbol": "X", "side": "SHORT", "entry_ts": "2026-08-01T09:20:00",
                                 "entry_price": 1000, "exit_ts": "2026-08-01T10:00:00", "exit_price": 1030,
                                 "qty": 10, "result": "SL", "pnl": -300}, ROUND_TRIP)
    assert row["net_pnl"] == -1300.0


def test_closed_row_missing_pnl_leaves_everything_none():
    row = m._cc2215_closed_row({"symbol": "X", "side": "LONG", "entry_ts": None, "entry_price": 1000,
                                 "exit_ts": None, "exit_price": None, "qty": 10, "result": None,
                                 "pnl": None}, ROUND_TRIP)
    assert row["net_pnl"] is None
    assert row["net_pnl_pct"] is None


def test_closed_row_missing_qty_still_gives_net_pnl_but_not_pct():
    row = m._cc2215_closed_row({"symbol": "X", "side": "LONG", "entry_ts": None, "entry_price": 1000,
                                 "exit_ts": None, "exit_price": None, "qty": None, "result": "TARGET",
                                 "pnl": 2000}, ROUND_TRIP)
    assert row["net_pnl"] == 2000 - ROUND_TRIP
    assert row["net_pnl_pct"] is None
