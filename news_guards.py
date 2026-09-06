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

──────────────────────────────────────────────────────────────────────────────────────────────
3. DATA GATE — cc#1729 (founder 06-Sep: "last 20 AI editorial not up to the market").

QUALITY_BAR_V3 (session_log 1293) names TWO mandatory post-insert checks for an AI Editorial:
char_check (>= 2000 chars) and data_check (>= 6 distinct hard data points, >= 2 comparative).
Only char_check ever existed in code (the trigger above). Fable's audit of the last 20 rows: every
one passes char_check, 19 of 20 fail data_check, six carry ZERO figures. The length gate was live
and the data gate was a sentence in a spec.

So data_check now sits where char_check sits — in the same BEFORE INSERT trigger, for the same
reason (there is still no polished_news insert path in this repo; rows arrive as raw SQL through
run_sql). ONE counting function, editorial_data_check(body), is the definition:

  hard data point  = a figure carrying a unit or currency (%, crore, lakh, bn, mn, bps, x, ₹, $ ...)
                     counted DISTINCT after whitespace/case normalisation
  comparative      = a sentence that carries a figure AND an explicit comparison word (vs, from-to,
                     up/down from, than, year ago, YoY, QoQ, peer, sector, median, record ...)

Below either floor the INSERT is refused and the exception IS the retry instruction: it names the
deficit, demands real sourced figures (the article + screener_raw / gvm_scores / raw_prices /
universe_technicals), forbids invented numbers outright, and says what to do if the retry still
falls short — downgrade to a Domestic/Global short polish, never ship a thin editorial.
editorial_precheck(raw_news_id, headline, body) runs the same function BEFORE the insert and logs
the attempt, so the writer can test a draft without touching polished_news.

Every check is logged to editorial_gate_log (stage = precheck | insert) with the two counts, so
gate decay is visible in a later audit the way funnel counts are (session_log 5065). A rejected
INSERT cannot log itself — the exception rolls its own row back — which is exactly why the
precheck path exists and is named in the exception text.

FORMAT LOCK. The five-header skeleton (What changed / Why this matters / The India angle / The
honest read / One thing to watch) is not mandatory and was never in the spec. The trigger extracts
the bold headers and compares them with the last five editorials of the same batch window: an
identical header set is flagged skeleton_repeat in the log and raised as a NOTICE, not a reject —
structure is the writer's call per story, repeating one across a batch is the defect.
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


# ── cc#1729: the data gate ────────────────────────────────────────────────────────────────────
# CREATE TABLE IF NOT EXISTS + CREATE OR REPLACE FUNCTION only — no ALTER TABLE (MAINTENANCE_LOCK).
_GATE_LOG_DDL = """
CREATE TABLE IF NOT EXISTS editorial_gate_log (
    id              BIGSERIAL PRIMARY KEY,
    stage           TEXT NOT NULL,              -- 'precheck' | 'insert'
    polished_id     BIGINT,
    raw_news_id     BIGINT,
    headline        TEXT,
    chars           INT,
    data_points     INT NOT NULL,
    comparatives    INT NOT NULL,
    passed          BOOLEAN NOT NULL,
    skeleton_repeat BOOLEAN NOT NULL DEFAULT FALSE,
    headers         TEXT[],
    deficit         TEXT,
    checked_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""

# The ONE definition of a hard data point and a comparative. Everything else calls this.
_DATA_CHECK_FN = r"""
CREATE OR REPLACE FUNCTION editorial_data_check(body TEXT)
RETURNS TABLE(data_points INT, comparatives INT, headers TEXT[], passes BOOLEAN, deficit TEXT) AS $dc$
DECLARE
    txt  TEXT := COALESCE(body, '');
    dp   INT;
    cp   INT;
    hd   TEXT[];
    why  TEXT := '';
BEGIN
    -- distinct figures that carry a unit or a currency; whitespace/case-normalised before DISTINCT
    SELECT COUNT(DISTINCT LOWER(REGEXP_REPLACE(m[1], '\s+', '', 'g'))) INTO dp
    FROM REGEXP_MATCHES(txt,
        '((?:₹|Rs\.?|INR|\$|USD|US\$|€|£)\s*\d[\d,]*(?:\.\d+)?\s*(?:crore|cr|lakh|lakhs|bn|billion|mn|million|trn|trillion|k)?'
        '|\d[\d,]*(?:\.\d+)?\s*(?:%|percent|per cent|percentage points|pp\b|crore|cr\b|lakh|lakhs|bn\b|billion|mn\b|million'
        '|trn|trillion|bps|basis points|x\b|times|tonnes|tonne|MW|GW|GWh|kWh|mmbtu|barrels|bpd|units|kg|km|MT\b|paise|rupees'
        '|dollars|years|months|quarters|days|weeks|sessions|stocks|companies|names|branches|stores|plants|employees|seats'
        '|orders|flights|aircraft|vehicles|EVs|cars))',
        'gi') AS m;

    -- sentences that carry a figure AND an explicit comparison word (peer / sector / historical)
    SELECT COUNT(DISTINCT TRIM(s.sent)) INTO cp
    FROM REGEXP_SPLIT_TO_TABLE(txt, '(?<=[\.\!\?])\s+') AS s(sent)
    WHERE s.sent ~ '\d'
      AND s.sent ~* '(\bvs\.?\b|versus|compared|against|up from|down from|from [^.]{0,40}\d[^.]{0,40} to \d|higher than'
                   '|lower than|more than|less than|above|below|peer|sector|industry average|last year|a year ago'
                   '|year earlier|year-ago|yoy|y/y|qoq|q/q|previous|prior|earlier|median|average|last quarter|since 20'
                   '|record|highest|lowest|fastest|slowest|double|triple|half|twice|outperform|underperform|whereas|than)';

    -- bold headers at paragraph starts: the skeleton, if the piece has one
    SELECT COALESCE(ARRAY_AGG(LOWER(TRIM(TRAILING '.' FROM TRIM(h[1]))) ORDER BY ord), '{}') INTO hd
    FROM REGEXP_MATCHES(txt, '(?:^|\n)\s*\*\*([^*\n]{3,60}?)\*\*', 'g') WITH ORDINALITY AS h(h, ord);

    IF dp < 6 THEN why := why || FORMAT('%s of 6 distinct hard data points; ', dp); END IF;
    IF cp < 2 THEN why := why || FORMAT('%s of 2 comparative sentences; ', cp); END IF;

    RETURN QUERY SELECT dp, cp, hd, (dp >= 6 AND cp >= 2), NULLIF(TRIM(TRAILING '; ' FROM why), '');
END;
$dc$ LANGUAGE plpgsql IMMUTABLE
"""

# The writer's pre-insert test. Logs the attempt (a rejected INSERT cannot log itself) and returns
# the verdict plus the retry instruction, so a draft is checked BEFORE it touches polished_news.
_PRECHECK_FN = """
CREATE OR REPLACE FUNCTION editorial_precheck(p_raw_news_id BIGINT, p_headline TEXT, p_body TEXT)
RETURNS TABLE(data_points INT, comparatives INT, chars INT, passes BOOLEAN, deficit TEXT, instruction TEXT) AS $pc$
DECLARE
    d RECORD;
    n INT := COALESCE(LENGTH(TRIM(p_body)), 0);
BEGIN
    SELECT * INTO d FROM editorial_data_check(p_body);
    INSERT INTO editorial_gate_log (stage, raw_news_id, headline, chars, data_points, comparatives, passed, headers, deficit)
    VALUES ('precheck', p_raw_news_id, LEFT(p_headline, 200), n, d.data_points, d.comparatives,
            (d.passes AND n >= 2000), d.headers, d.deficit);
    RETURN QUERY SELECT d.data_points, d.comparatives, n, (d.passes AND n >= 2000), d.deficit,
        CASE WHEN d.passes AND n >= 2000 THEN 'PASS: insert as AI Editorial.'
             ELSE 'RETRY ONCE with the deficit closed using REAL figures from the underlying article and our own tables '
                  '(screener_raw, gvm_scores, raw_prices, universe_technicals). A hard data point is a figure with a unit or '
                  'an explicit comparison; never invent a number — a fabricated figure is a worse failure than a thin '
                  'editorial. If the retry still falls short, DOWNGRADE to a Domestic/Global short polish. Do not pad.'
        END;
END;
$pc$ LANGUAGE plpgsql
"""


# ── items 4 + 5 ───────────────────────────────────────────────────────────────────────────────
# BEFORE INSERT guard. Fires per row, rejects the row, names it.
_BODY_GUARD_FN = """
CREATE OR REPLACE FUNCTION polished_news_body_guard() RETURNS trigger AS $guard$
DECLARE
    body_len INT := COALESCE(LENGTH(TRIM(NEW.full_summary)), 0);
    hl       TEXT := LEFT(COALESCE(NEW.headline_clean, '(no headline)'), 120);
    d        RECORD;                 -- cc#1729 data-check verdict
    rep      INT := 0;               -- cc#1729 skeleton repeats in the batch window
    skel     BOOLEAN := FALSE;
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

    -- cc#1729: the data gate. Same floor as the spec (QUALITY_BAR_V3): >= 6 distinct hard data
    -- points AND >= 2 comparative sentences, or the editorial does not land as an editorial.
    IF NEW.category = 'AI Editorial' THEN
        SELECT * INTO d FROM editorial_data_check(NEW.full_summary);
        IF NOT d.passes THEN
            RAISE EXCEPTION
              'EDITORIAL_DATA_GATE cc#1729 REJECTED: raw_news_id=% headline=%. Found % distinct hard data points (floor 6) and % comparative sentences (floor 2) — short by: %. RETRY ONCE with the deficit closed using REAL figures from the underlying article and our own tables (screener_raw, gvm_scores, raw_prices, universe_technicals): a hard data point is a figure with a unit or an explicit comparison (%%, crore, x, bps, ratio, a dated prior-period value); a comparative sets it against a peer, the sector or history. NEVER invent a number — a fabricated figure is a worse failure than a thin editorial. If the retry still falls short, DOWNGRADE: insert it as a Domestic/Global short polish, not as AI Editorial. Test a draft first: SELECT * FROM editorial_precheck(raw_news_id, headline, body).',
              COALESCE(NEW.raw_news_id, -1), hl, d.data_points, d.comparatives, d.deficit
              USING ERRCODE = 'check_violation';
        END IF;

        -- format lock check: same bold-header set as the rest of this batch window = a defect,
        -- flagged and logged (NOTICE), not refused — structure is the writer's call per story.
        IF CARDINALITY(d.headers) >= 3 THEN
            SELECT COUNT(*) INTO rep
            FROM (SELECT p.full_summary FROM polished_news p
                  WHERE p.category = 'AI Editorial' AND p.polished_at >= NOW() - INTERVAL '3 hours'
                  ORDER BY p.polished_at DESC LIMIT 5) q,
                 LATERAL editorial_data_check(q.full_summary) x
            WHERE x.headers = d.headers;
            IF rep >= 1 THEN
                skel := TRUE;
                RAISE NOTICE 'EDITORIAL_FORMAT cc#1729: headline=% repeats the header skeleton of % other editorial(s) in this batch window (%). An identical skeleton across a batch is itself a defect — vary the structure by story.',
                  hl, rep, ARRAY_TO_STRING(d.headers, ' / ');
            END IF;
        END IF;

        INSERT INTO editorial_gate_log (stage, polished_id, raw_news_id, headline, chars, data_points, comparatives, passed, skeleton_repeat, headers, deficit)
        VALUES ('insert', NEW.id, NEW.raw_news_id, hl, body_len, d.data_points, d.comparatives, TRUE, skel, d.headers, NULL);
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
        ("editorial_gate", [_GATE_LOG_DDL, _DATA_CHECK_FN, _PRECHECK_FN]),      # cc#1729, before the trigger
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
