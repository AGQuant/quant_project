"""cc#2184: category averages for the /m/mf list are computed server-side over the scored
direct-growth population, keyed by the category the row displays; funds without a figure are
counted in n but not in the mean. No DB: _conn is replaced."""
import mf_app_mobile as mm


class _Cur:
    def __init__(self, rows): self._rows = rows
    def execute(self, sql, params=None): pass
    def fetchall(self): return self._rows
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Conn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _Cur(self._rows)
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_cat_avgs_mean_and_counts(monkeypatch):
    rows = [  # name, category, ret_1y, ret_3y, expense_ratio
        ("A Large Cap Fund - Direct Plan - Growth", "Large Cap Fund", 1.0, 10.0, 1.0),
        ("B Large Cap Fund - Direct Plan - Growth", "Large Cap Fund", 3.0, 12.0, 0.5),
        ("C Large Cap Fund - Direct Plan - Growth", "Large Cap Fund", None, None, None),   # counted, not averaged
        ("D Small Cap Fund - Direct Growth", None, 7.0, 20.0, 0.8),                        # category derived from the name
    ]
    monkeypatch.setattr(mm, "_conn", lambda: _Conn(rows))
    out = mm._cat_avgs()
    assert out["Large Cap Fund"] == {"n": 3, "n_1y": 2, "avg_1y": 2.0, "avg_3y": 11.0, "avg_er": 0.75}
    k = mm._derive_cat("D Small Cap Fund - Direct Growth", None)      # the screener's own name -> category mapping
    assert k and k != "Large Cap Fund"
    assert out[k] == {"n": 1, "n_1y": 1, "avg_1y": 7.0, "avg_3y": 20.0, "avg_er": 0.8}


def test_cat_avgs_survive_a_db_failure(monkeypatch):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(mm, "_conn", boom)
    assert mm._cat_avgs() == {}
