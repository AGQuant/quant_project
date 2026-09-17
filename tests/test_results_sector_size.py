"""cc#2162 -- the season payload's sector size band comes from the platform's own cap cuts
(investment_check.CAP_LARGE_MIN / CAP_MID_MIN), never from a number retyped in results_app_mobile."""
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
for _m in ("psycopg2", "psycopg2.extras"):      # results_endpoints imports the driver at module level
    try:
        __import__(_m)
    except ImportError:
        sys.modules[_m] = types.ModuleType(_m)
ram = pytest.importorskip("results_app_mobile")
ic = pytest.importorskip("investment_check")


def test_size_bands_follow_the_imported_cuts():
    assert ram.sector_size(ic.CAP_LARGE_MIN) == "Large"
    assert ram.sector_size(ic.CAP_LARGE_MIN - 1) == "Mid"
    assert ram.sector_size(ic.CAP_MID_MIN) == "Mid"
    assert ram.sector_size(ic.CAP_MID_MIN - 1) == "Small"
    assert ram.sector_size("62528") == "Large" and ram.sector_size(3978) == "Small"


def test_no_average_means_no_size():
    assert ram.sector_size(None) is None
    assert ram.sector_size("x") is None


def test_the_cuts_are_not_retyped_in_this_file():
    src = open(os.path.join(ROOT, "results_app_mobile.py"), encoding="utf-8").read()
    assert "20000" not in src and "5000" not in src.replace("50000", "")
