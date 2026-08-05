"""
news_guards.py — cc#870: SUPPRESSION FLAG + POLISH BODY GUARD (founder 05-Aug-2026, P1).

Two defects on the 05-Aug morning batch, both fixed here, both fixed at the level where they
actually happen rather than where they are visible.

──────────────────────────────────────────────────────────────────────────────────────────────
1. SUPPRESSION — a bad item can be HIDDEN without being DELETED.

Deleting a polished_news row is not a cull, it is a loop. The polish candidate query is
`raw_news WHERE NOT EXISTS (polished_news child)`, so deleting the polished row hands the raw
article straight back to the candidate pool and the same rejected story becomes eligible on the
next run. 503 raw rows / 286 currently unpolished at 48h — a deleted row joins that 286.

So suppression is a FLAG, not a delete. news_suppressed carries the raw_news_id, and it is read
in TWO places, which is the part that makes it stick:

  * v_polished_articles      — the story stops being displayed          (the symptom)
  * the unpolished selection — the story stops being re-picked          (the cause)

Item 2 alone would hide it for one day and hand it back the next.

──────────────────────────────────────────────────────────────────────────────────────────────
2. BODY GUARD — a short can never land with an empty body.

167 of 172 rows in the 05-Aug batch were inserted with full_summary NULL, so they render as bare
headlines. Spec 733 requires a short polish to carry a 3-4 sentence paragraph; 8188 states a short
is 3-4 lines and NOT a 1-2 line compression; QUALITY_BAR_V3 (1293) puts an AI Editorial at 2000
chars minimum, and all five editorials that morning were 1173-1331.

WHY A TRIGGER AND NOT A PYTHON CHECK. There is NO polished_news insert path in this repo. Grep is
the evidence: `INSERT INTO polished_news` returns zero hits across every .py file. Polish rows are
written by Claude.ai as raw SQL through the run_sql MCP tool. A guard in a Python insert path
would therefore guard nothing at all — it would be a check on a code path that does not exist,
which reads as coverage and is not. The guard has to sit where the write really lands, so it sits
on the table.

WHY A TRIGGER AND NOT A CHECK CONSTRAINT. The card forbids a constraint and the live data says why:
755 existing rows would violate it (Domestic 471, Global 253, IPO 31 empty bodies out of 3,321
total). A CHECK would have to be added with ALTER TABLE and would fail validation on those rows.
A BEFORE INSERT trigger is created with CREATE TRIGGER, never ALTER TABLE, and only ever sees NEW
rows — every existing row is untouched and keeps rendering exactly as it does today.

The failure is LOUD and NAMED: the exception states the category, the raw_news_id, the headline and
the measured length, so the row that was refused is identifiable from the response alone. run_sql
surfaces that message verbatim, which is the whole point — the writer finds out immediately, at
insert time, instead of the founder finding out on the live feed.

Stock Views is deliberately NOT guarded. The card names four canonical categories (Domestic,
Global, AI Editorial, IPO) and adding a fifth to the guard would be inventing a rule nobody wrote.
"""

import logging

log = logging.getLogger("scorr.news_guards")

# ── item 1 ────────────────────────────────────────────────────────────────────────────────────
# CREATE TABLE only. No ALTER TABLE anywhere in this module (cc#351 MAINTENANCE_LOCK_RULE).
# Idempotent, matching the repo pattern used by v8_pivot_star_log and news_fetcher.ensure_tables().
#
# raw_news_id is the PRIMARY KEY, not polished_id, and that is the load-bearing choice: the raw
# article is what returns to the candidate pool, so the raw id is what has to be remembered. A
# story suppressed before it was ever polished is still recorded, with polished_id NULL.
NEWS_SUPPRESSED_DDL = """
CREATE TABLE IF NOT EXISTS news_suppressed (
    raw_news_id   BIGINT PRIMARY KEY,
    polished_id   BIGINT,
    reason        TEXT NOT NULL,
    suppressed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

# The exclusion fragment. ONE definition, used by the view AND by the candidate query, so the two
# halves of suppression can never drift apart. `raw_id_expr` is the caller's own raw-id column —
# `p.raw_news_id` in the view, `r.id` in the candidate query. It is a code-supplied identifier, not
# user input, so it is interpolated; every value the query actually filters on stays parameterised.
def suppressed_exclude(raw_id_expr: str) -> str:
    return (f" NOT EXISTS (SELECT 1 FROM news_suppressed ns "
            f"WHERE ns.raw_news_id = {raw_id_expr}) ")


# ── items 4 + 5 ───────────────────────────────────────────────────────────────────────────────
# BEFORE INSERT guard. Fires per row, rejects the row, names it.
_BODY_GUARD_FN = """
CREATE OR REPLACE FUNCTION polished_news_body_guard() RETURNS trigger AS $guard$
DECLARE
    body_len INT := COALESCE(LENGTH(TRIM(NEW.full_summary)), 0);
    hl       TEXT := LEFT(COALESCE(NEW.headline_clean, '(no headline)'), 120);
BEGIN
    IF NEW.category IN ('Domestic', 'Global', 'IPO') AND body_len = 0 THEN
        RAISE EXCEPTION
          'POLISH_BODY_GUARD cc#870 REJECTED: category=% raw_news_id=% headline=%. full_summary is empty. A short polish must carry a 3-4 sentence paragraph (spec 733); a short is 3-4 lines, NOT a 1-2 line compression (spec 8188). Write the body, then insert.',
          NEW.category, COALESCE(NEW.raw_news_id, -1), hl
          USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.category = 'AI Editorial' AND body_len < 2000 THEN
        RAISE EXCEPTION
          'POLISH_BODY_GUARD cc#870 REJECTED: category=AI Editorial raw_news_id=% headline=%. full_summary is % chars, below the 2000 minimum (QUALITY_BAR_V3, session_log 1293).',
          COALESCE(NEW.raw_news_id, -1), hl, body_len
          USING ERRCODE = 'check_violation';
    END IF;

    RETURN NEW;
END;
$guard$ LANGUAGE plpgsql
"""

_BODY_GUARD_DROP = "DROP TRIGGER IF EXISTS trg_polished_news_body_guard ON polished_news"

# BEFORE INSERT only, per the card. An UPDATE that later fills the body is the intended repair
# path — Claude.ai owns polish text — so guarding UPDATE would block the fix as well as the fault.
_BODY_GUARD_TRIGGER = """
CREATE TRIGGER trg_polished_news_body_guard
    BEFORE INSERT ON polished_news
    FOR EACH ROW EXECUTE FUNCTION polished_news_body_guard()
"""


def ensure_news_guards(conn) -> dict:
    """Create the suppression table and the body-guard trigger. Idempotent, safe to call at import.

    Each statement is committed on its own so one failure cannot take the others down with it —
    the suppression table in particular MUST exist before v_polished_articles is (re)created,
    because the view now references it.
    """
    out = {}
    for key, stmts in (
        ("news_suppressed", [NEWS_SUPPRESSED_DDL]),
        ("body_guard", [_BODY_GUARD_FN, _BODY_GUARD_DROP, _BODY_GUARD_TRIGGER]),
    ):
        try:
            with conn.cursor() as cur:
                for s in stmts:
                    cur.execute(s)
            conn.commit()
            out[key] = "ok"
        except Exception as e:
            try:
                conn.rollback()
            except Exception:
                pass
            out[key] = f"{type(e).__name__}: {str(e)[:160]}"
            log.warning("cc#870 ensure_news_guards(%s) failed: %s", key, e)
    return out
