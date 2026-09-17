"""cc#2174 -- the test cc#2158's fake-cursor harness lacked: run the custom screener's real SQL against the real
database and look at ONE ROW OBJECT, not a count.

Why: run_sql once wrapped the row subquery as `json_agg(m) FROM (... ROUND(m_score) AS m ...) m` and Postgres
bound the bare `m` to the COLUMN, so every row came back as a bare M-score and the app drew dashes under a
correct count. Counts cannot catch that; a real row can.

Runs wherever DATABASE_URL is set (Railway, Fable's seat). Skips, loudly, where it is not.
"""
import json
import os

import pytest

psycopg = pytest.importorskip("psycopg")

DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- this test needs the real database")

ROW_KEYS = {"symbol", "company_name", "segment", "cap", "gvm", "verdict", "g", "v", "m",
            "invest_score", "invest_band", "price", "year_return", "w52"}


def _rows(selection, limit):
    import custom_screener_app as csa
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute(csa.run_sql(selection, limit=limit))
        row = dict(zip([d[0] for d in cur.description], cur.fetchone()))
    rows = row["rows"]
    if isinstance(rows, str):
        rows = json.loads(rows)
    return row, rows


def test_run_rows_are_objects_with_the_full_key_set():
    row, rows = _rows({"size": ["large"]}, 2)
    assert isinstance(rows, list) and len(rows) == 2, rows
    first = rows[0]
    assert isinstance(first, dict), "rows must be JSON objects, not bare numbers (cc#2174)"
    assert ROW_KEYS <= set(first.keys()), sorted(set(first.keys()) ^ ROW_KEYS)
    for k in ("symbol", "gvm", "g", "v", "m", "invest_score"):
        assert k in first
    assert isinstance(first["symbol"], str) and first["symbol"]
    assert first["m"] is None or isinstance(first["m"], (int, float))
    assert int(row["total"]) >= 2 and int(row["universe"]) >= int(row["total"])


def test_run_rows_follow_the_sort():
    _, rows = _rows({"gvm": ["good"]}, 25)
    scores = [r["invest_score"] for r in rows if r["invest_score"] is not None]
    assert scores == sorted(scores, reverse=True), "invest_score DESC NULLS LAST"


def test_empty_selection_returns_no_rows_but_the_universe():
    row, rows = _rows({}, 300)
    assert rows == [] and int(row["total"]) == int(row["universe"]) > 0
