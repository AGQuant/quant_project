"""cc#2212 V8 UNREALISED 5-MIN SERIES: bar boundaries floor to the IST 5-min grid, the snapshot stores
book_canon's figures verbatim under that bar (upsert, and a canon error is raised rather than stored),
and the series builder / endpoint serve the last N real bars ascending with N capped at 100 server-side.
No DB, no network -- a fake connection stands in for psycopg."""
from datetime import datetime, timedelta, timezone

import pytest

import v8_unrealised_daily as m

IST = m.IST


class _Cur:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows=None):
        self.cur = _Cur(rows)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_bar_ts_floors_to_the_ist_5min_grid():
    assert m.bar_ts(datetime(2026, 9, 18, 9, 17, 43)).isoformat() == "2026-09-18T09:15:00+05:30"
    assert m.bar_ts(datetime(2026, 9, 18, 15, 30, 43)).isoformat() == "2026-09-18T15:30:00+05:30"
    assert m.bar_ts(datetime(2026, 9, 18, 15, 34, 59)).isoformat() == "2026-09-18T15:30:00+05:30"
    # an aware UTC clock is converted first: 10:00:30Z is 15:30:30 IST
    assert m.bar_ts(datetime(2026, 9, 18, 10, 0, 30, tzinfo=timezone.utc)).isoformat() == "2026-09-18T15:30:00+05:30"


def test_snapshot_stores_the_canon_figures_under_the_bar(monkeypatch):
    monkeypatch.setattr(m, "book_canon", lambda conn: {"unrealised": 12345.5, "open": 3,
                                                        "long": {"unrealised": 8000.0}, "short": {"unrealised": 4345.5}})
    conn = _Conn()
    res = m.snapshot_unrealised_5m(conn, datetime(2026, 9, 18, 9, 22, 7))
    assert res["ts"] == "2026-09-18T09:20:00+05:30" and res["unrealised"] == 12345.5 and res["open_n"] == 3
    ddl, ins = conn.cur.calls
    assert ddl[0].startswith("CREATE TABLE IF NOT EXISTS v8_unrealised_5m")
    assert "INSERT INTO v8_unrealised_5m" in ins[0] and "ON CONFLICT (ts) DO UPDATE" in ins[0]
    assert ins[1] == (datetime(2026, 9, 18, 9, 20, tzinfo=IST), 12345.5, 8000.0, 4345.5, 3)
    assert conn.commits == 1


def test_snapshot_raises_on_a_canon_error_and_writes_nothing(monkeypatch):
    monkeypatch.setattr(m, "book_canon", lambda conn: {"error": "db down", "canon": "V8_PNL_CANON_V1"})
    conn = _Conn()
    with pytest.raises(RuntimeError):
        m.snapshot_unrealised_5m(conn, datetime(2026, 9, 18, 9, 20))
    assert conn.cur.calls == [] and conn.commits == 0


def test_clamp_n():
    assert m.clamp_n(None) == 100 and m.clamp_n("x") == 100
    assert m.clamp_n(500) == 100 and m.clamp_n(0) == 1 and m.clamp_n(-3) == 1
    assert m.clamp_n("7") == 7 and m.clamp_n(100) == 100


def _rows(n, start=datetime(2026, 9, 17, 9, 15, tzinfo=IST)):
    return [(start + timedelta(minutes=5 * i), 1000.0 + i) for i in range(n)]


def test_build_series_serves_the_last_100_ascending_and_skips_nulls():
    rows = _rows(130)
    rows[129] = (rows[129][0], None)          # the newest bar has no figure -> not a point
    rows[10] = (rows[10][0], None)
    out = m.build_series_5m(list(reversed(rows)), "all")   # any input order
    assert out["n"] == 100 and out["n_max"] == 100 and out["kind"] == "unrealised_5m" and out["bar"] == "5m"
    ts = [p["ts"] for p in out["points"]]
    assert ts == sorted(ts) and all(t.endswith("+05:30") for t in ts)
    assert out["last_ts"] == rows[128][0].isoformat() and out["points"][-1]["value"] == 1128.0
    assert out["first_ts"] == rows[29][0].isoformat()   # 100 real bars back from bar 128
    assert out["points"][0]["value"] == 1029.0


def test_build_series_with_fewer_bars_serves_them_all_and_empty_is_honest():
    out = m.build_series_5m(_rows(7), "long", n=100)
    assert out["n"] == 7 and out["slice"] == "long" and len(out["points"]) == 7
    empty = m.build_series_5m([], "short")
    assert empty["n"] == 0 and empty["points"] == [] and empty["first_ts"] is None and empty["last_ts"] is None


def test_endpoint_picks_the_column_and_caps_n(monkeypatch):
    conn = _Conn(rows=list(reversed(_rows(5))))
    monkeypatch.setattr(m, "_conn", lambda: conn)
    out = m.v8_unrealised_5m_series(slice="LONG", n=500)
    sql, params = conn.cur.calls[-1]
    assert "SELECT ts, long_unrealised FROM v8_unrealised_5m WHERE long_unrealised IS NOT NULL ORDER BY ts DESC LIMIT %s" == sql
    assert params == (100,) and out["n_max"] == 100 and out["n"] == 5 and out["slice"] == "long"
    out2 = m.v8_unrealised_5m_series(slice="nonsense", n=3)
    assert conn.cur.calls[-1][0].startswith("SELECT ts, unrealised FROM") and conn.cur.calls[-1][1] == (3,)
    assert out2["slice"] == "all"
