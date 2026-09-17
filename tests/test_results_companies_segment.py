"""cc#2163 -- /api/mobile/results_app/companies?segment=<name>: only that segment's rows, plus the sector's
medians for the sheet header; without the param the payload is exactly what it was."""
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

RC = {"season": {"quarter": "Q1 FY27"},
      "sectors": [{"sector": "Shipping & Maritime", "reported": 6, "total": 7, "sales_yoy": 40.6, "pat_yoy": 117.3,
                   "avg_mcap": 8015, "n_used": 4, "pat_n_detailed": 4},
                  {"sector": "Retail - Mid", "reported": 25, "total": 25, "sales_yoy": 12.7, "pat_yoy": 50.0,
                   "avg_mcap": 4070, "n_used": 5, "pat_n_detailed": 5}],
      "companies": [{"symbol": "KMEW", "segment": "Shipping & Maritime", "pat_yoy": 472.7, "sales_yoy": 139.6, "basis": "detailed"},
                    {"symbol": "SEAMECLTD", "segment": "Shipping & Maritime", "pat_yoy": None, "sales_yoy": 40.8, "basis": "basic"},
                    {"symbol": "EMIL", "segment": "Retail - Mid", "pat_yoy": 450.0, "sales_yoy": 39.1, "basis": "detailed"}]}


class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a):
        pass

    def fetchall(self):
        return [("KMEW",)]


class _Conn(_Cur):
    def cursor(self):
        return _Cur()


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setattr(ram, "_guard", lambda r: None)
    monkeypatch.setattr(ram, "_rc", lambda: RC)
    monkeypatch.setattr(ram, "_conn", lambda: _Conn())


def test_segment_filters_and_carries_the_sector_medians(stubbed):
    out = ram.mobile_results_companies(None, segment="Shipping & Maritime")
    assert [r["symbol"] for r in out["rows"]] == ["KMEW", "SEAMECLTD"] and out["count"] == 2
    assert out["segment"] == "Shipping & Maritime" and out["size"] == "Mid" and out["total"] == 7 and out["reported"] == 6
    assert out["season_medians"] == {"sales_yoy": 40.6, "pat_yoy": 117.3, "pat_n": 4}
    assert out["rows"][0]["written"] is True and out["rows"][1]["written"] is False


def test_segment_match_is_case_exact_and_unknown_is_empty(stubbed):
    assert ram.mobile_results_companies(None, segment="shipping & maritime")["rows"] == []
    out = ram.mobile_results_companies(None, segment="Nowhere")
    assert out["rows"] == [] and out["count"] == 0 and out["size"] is None and out["season_medians"]["pat_yoy"] is None


def test_no_param_is_the_old_payload(stubbed):
    out = ram.mobile_results_companies(None)
    assert set(out.keys()) == {"quarter", "rows", "count"} and out["count"] == 3
