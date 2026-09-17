"""cc#2168 -- the season payload's upcoming-results rows: size from the ranked universe, GVM, and a bare BSE
scrip code (numeric ticker) shown by company name, never as an NSE symbol."""
import datetime as dt
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
for _m in ("psycopg2", "psycopg2.extras"):
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = types.ModuleType(_m)
ram = pytest.importorskip("results_app_mobile")


def test_window_is_thirty_days():
    assert ram.UPCOMING_DAYS == 30


def test_ranked_row_keeps_its_symbol_size_and_gvm():
    row = ram.upcoming_row(("ORISSAMINE", "The Orissa Minerals Development Company Limited", dt.date(2026, 9, 17), "upcoming", "micro", 5.36))
    assert row["symbol"] == "ORISSAMINE" and row["display"] == "ORISSAMINE" and row["bse_code"] is None
    assert row["size"] == "micro" and row["gvm"] == 5.36 and row["date"] == "2026-09-17"


def test_unranked_row_has_no_size_and_no_gvm():
    row = ram.upcoming_row(("SUPREMEENG", "Supreme Engineering Limited", dt.date(2026, 9, 18), "upcoming", None, None))
    assert row["size"] is None and row["gvm"] is None and row["display"] == "SUPREMEENG"


def test_bse_scrip_code_is_shown_by_company_name():
    row = ram.upcoming_row(("543435", "Clara Industries", dt.date(2026, 9, 17), "upcoming", None, None))
    assert row["display"] == "Clara Industries" and row["bse_code"] == "543435" and row["symbol"] == "543435"
    assert row["size"] is None
