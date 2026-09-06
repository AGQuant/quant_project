"""yoy_basis.py — cc#1759 · YOY_BASIS_RULE_V1 (session_log 39560, founder 06-Sep-2026:
"all data points should be yoy not qoq").

THE COLUMNS ARE YoY DESPITE THE qoq_ PREFIX. screener_raw.qoq_sales_growth and
screener_raw.qoq_profit_growth hold the LATEST QUARTER vs the SAME QUARTER A YEAR AGO — the
Screener export's "YOY Quarterly sales / profit growth" header (screener_loader.py maps it), the
founder's basis change of 11-Aug-2026 (cc#1005), and Fable's 06-Sep cross-check against Finkhoz
(IPCALAB 20.8/72.3, MAHABANK 13.9/34.5, KARURVYSYA 18.7/44.9 — exact matches). The NAME is the
misnomer; the VALUE is right. Renaming the column is an ALTER TABLE and off the table
(MAINTENANCE_LOCK_RULE, cc#351), so this module is the one place the honest name lives: readers
import these accessors and stop typing qoq_* themselves, which is how cc#1758's blank columns
happened (a renderer looking for a field called sales_yoy found nothing and printed a dash).

NEVER RECOMPUTE THESE AS SEQUENTIAL. A "fix" that re-derived them as quarter-vs-preceding-quarter
would silently corrupt every surface that reads them, including research documents already sent
out. These accessors READ; they do not derive.

MARGIN: a margin comparison is opm minus opm_prev_year_q, in percentage POINTS with a sign
(10.14 -> 11.44 is -1.30 pts, never -11.4%). Same rule as cc#1758. The "now" column is `opm` per
that card; gvm_nightly.opm_expansion pairs opm_latest_q with the same year-ago quarter — that is
a scoring input and is not changed by this module (flagged on cc#1758 / cc#1759 for a ruling).
"""

# The physical columns, so a SQL fragment can be built from one place: f"SELECT {SALES_GROWTH_YOY_COL} ..."
SALES_GROWTH_YOY_COL = "qoq_sales_growth"     # YoY despite the name — see the module docstring
PROFIT_GROWTH_YOY_COL = "qoq_profit_growth"   # YoY despite the name — see the module docstring
MARGIN_NOW_COL = "opm"
MARGIN_LY_COL = "opm_prev_year_q"

# Reader-facing captions, written once. Plain-language surfaces use the long form
# (AI_EDITORIAL_STYLE_ADDENDUM_09JUL); tables use the short one.
SALES_GROWTH_YOY_LABEL = "Sales YoY"
PROFIT_GROWTH_YOY_LABEL = "PAT YoY"
MARGIN_CHANGE_LABEL = "Margin vs LY (pts)"
YOY_PLAIN = "vs the same quarter last year"


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _get(row, *keys):
    """First present key among the physical column name and the short keys readers already use."""
    if not row:
        return None
    for k in keys:
        try:
            if k in row and row[k] is not None:
                return row[k]
        except TypeError:
            return None
    return None


def sales_growth_yoy(row, ndigits=2):
    """Latest-quarter sales growth vs the same quarter last year, %. None when the source is NULL."""
    v = _num(_get(row, SALES_GROWTH_YOY_COL, "qoq_sales", "sales_growth_yoy", "sales_yoy"))
    return None if v is None else round(v, ndigits)


def profit_growth_yoy(row, ndigits=2):
    """Latest-quarter profit growth vs the same quarter last year, %. None when the source is NULL."""
    v = _num(_get(row, PROFIT_GROWTH_YOY_COL, "qoq_profit", "profit_growth_yoy", "pat_yoy"))
    return None if v is None else round(v, ndigits)


def margin_change_pts(row, ndigits=2):
    """opm minus opm_prev_year_q, percentage points, signed. None when either margin is NULL —
    never 0, never a percent change."""
    now = _num(_get(row, MARGIN_NOW_COL, "margin_now", "opm_now"))
    ly = _num(_get(row, MARGIN_LY_COL, "margin_ly", "opm_ly"))
    return None if (now is None or ly is None) else round(now - ly, ndigits)
