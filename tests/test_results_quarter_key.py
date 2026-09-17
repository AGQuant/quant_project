"""cc#2167 -- /m/results looked up result_analysis_v2 with the DISPLAY label ('Q1 FY27', from result_corner's
_fq_label) while the table stores 'Q1FY27', so every mover read 'no write-up' and the Written up block was
empty under a header counting 774 analyses. The one normalisation lives in results_app_mobile._qkey.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# results_endpoints imports psycopg2 at module level; a seat without it (this sandbox) still runs the no-DB
# tests -- the stub is only installed when the real driver is absent, and no test below touches it.
import types
for _m in ("psycopg2", "psycopg2.extras"):
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = types.ModuleType(_m)
ram = pytest.importorskip("results_app_mobile")
DATABASE_URL = os.getenv("DATABASE_URL", "")


class _Cur:
    def __init__(self):
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return [("CHENNPETRO",), ("APOLLOTYRE",)]


def test_qkey_strips_the_space_and_keeps_the_stored_spelling():
    assert ram._qkey("Q1 FY27") == "Q1FY27"
    assert ram._qkey("Q1FY27") == "Q1FY27"
    assert ram._qkey(None) == "" and ram._qkey("") == ""


def test_written_set_queries_with_the_stored_spelling():
    cur = _Cur()
    got = ram._written_set(cur, "Q1 FY27")
    assert cur.calls and cur.calls[0][1] == ("Q1FY27",), cur.calls
    assert got == {"CHENNPETRO", "APOLLOTYRE"}


@pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- this test needs the real database")
def test_written_set_on_production_rows_contains_chennpetro():
    psycopg = pytest.importorskip("psycopg")
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        written = ram._written_set(cur, "Q1 FY27")
    assert "CHENNPETRO" in written and len(written) > 700
