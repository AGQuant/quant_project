"""cc#2180 -- the check that would have caught the dead /m/mf: run the V15 screener's real SQL against the real
database on every sort path and expect rows, not an AmbiguousColumn error.

Why: cc#777 joined mf_derived_metrics (which also carries ret_1y / ret_3y) into v15_screener, and the ORDER BY
named ret_1y without a table alias. Postgres saw two output columns called ret_1y and rejected every sort
path -- the default mqs sort tiebreaks on ret_1y too -- so the page had no rows at all.

Runs wherever DATABASE_URL is set (Railway, Fable's seat). Skips, loudly, where it is not.
"""
import os

import pytest

pytest.importorskip("psycopg")

DATABASE_URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- this test needs the real database")


@pytest.mark.parametrize("sort", ["mqs", "1y", "aum"])
def test_v15_screener_every_sort_path_returns_rows(sort):
    import mf_pipeline
    out = mf_pipeline.v15_screener(category="", sort=sort, limit=5)
    assert isinstance(out, dict) and out["count"] > 0, out
    row = out["results"][0]
    for k in ("scheme_code", "name", "category", "mqs", "ret_1y", "aum_cr"):
        assert k in row


def test_v15_screener_one_category():
    import mf_pipeline
    out = mf_pipeline.v15_screener(category="Mid Cap Fund", sort="mqs", limit=5)
    assert out["count"] > 0 and all(r["category"] for r in out["results"])


def test_v15_screener_sort_matches_the_displayed_return():
    import mf_pipeline
    rows = mf_pipeline.v15_screener(category="", sort="1y", limit=20)["results"]
    seen = [r["ret_1y"] for r in rows if r["ret_1y"] is not None]
    assert seen == sorted(seen, reverse=True), "1y sort must follow the official (displayed) ret_1y, DESC"
