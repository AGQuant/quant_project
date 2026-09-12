# cc#2002 — Check tab rule sheet, 2-line plain-English descriptions

## What this lands

- **`mobile/check.html`** — a new `RULE_DESC` map (id → `[what it checks, why it matters]`),
  keyed by `r.rule` (the same clean id `NAMES`/`TPL`/`RULEKEYS`/the dormant `DEFS` already key by —
  no new lookup mechanism needed). Wired into `ruleSheet(c, r)` via a new `descHtml(r)` helper,
  inserted right after the header (`head`) in **both** branches — the Volume rules (R5/R5V, which
  skip the FULL/HALF/NONE grid entirely) and the standard rules — matching where the equivalent
  block already sits on the web Check page (`scorr_check.html`'s `defHtml` also renders right after
  its own header, before the rest of the body).
- **39 rule ids covered — every id the engine currently emits, zero gaps**:
  - **11 SELL-REV entries copied verbatim** from the web page's own `TC_WEB_RULE_LINES`
    (`scorr_check.html`, cc#1808) — the same rule now reads identically on both surfaces instead of
    two independently-drifting descriptions of one condition.
  - **28 new entries**, written directly from `tc_v4_dual.py`'s own `required` strings: SELL-MOM's
    7 own ids, all 18 ids shared between BUY-MOM/BUY-REV (one description per id, matching how
    `NAMES` already keys by id only, not id+bucket — the two variants test the same underlying idea
    with only the exact threshold differing), R8 (BUY-REV only), and R21/R22 (BUY-MOM only).
  - Rule-by-rule coverage was checked against a **live enumeration** of `card_maxes()` — BUY-MOM=20
    rules, BUY-REV=19, SELL-MOM=9, SELL-REV=11 — not the stale rule-count comments elsewhere in
    `tc_v4_dual.py` (its own docstrings say "SELL-MOM 12 rules" in two places; the live count is 9).
- **Missing-id handling** (item 4): `descHtml(r)` returns `''` for an unmapped id — no placeholder,
  no raw condition text — and `ruleDescMissing(r.rule)` logs a one-time `console.warn` per id per
  page session, so a future new rule with no description yet is visible, not silently blank.
- **CSS**: a new `.c-desc` rule in `mobile_endpoints.py`'s `MOBILE_CSS` (the "CHECK v2" section,
  same file check.html's own comment names as the styles' real home) — a muted, left-bordered text
  block, deliberately distinct from `.c-bx`'s bordered condition boxes ("own token style, not styled
  as a condition," per the card).

## Verify

- `node --check` clean on both script blocks.
- **Playwright structural check** against the real, just-edited file (headless Chromium, the
  pre-installed one — `/api/mobile/check/tc` intercepted with a synthetic-but-realistic payload
  built from the exact `_R()` shape confirmed against `tc_v4_dual.py`; 2 cards, BUY-MOM and
  SELL-REV, 4 and 3 rules respectively including one deliberately unmapped id, `R99_FAKE`):
  - Tapped **7 real rules across the 2 buckets** (R1, R5, R21, R99_FAKE on BUY-MOM; LOCK_MOOD, R5V,
    LOCK_FALL_FRESHNESS on SELL-REV) — each of the 6 mapped rules shows its **own, correct, distinct**
    2-line description (hand-checked against the map: R5's BUY-flavoured volume text differs
    correctly from R5V's SELL-flavoured one; LOCK_MOOD's description differs correctly from R1's;
    none repeated or generic).
  - `R99_FAKE` shows **no** `.c-desc` block at all (`hasDescBlock: false`) and the missing-id warning
    fired with the right id — confirming "show nothing, log it" rather than a placeholder or crash.
  - Zero unexpected console errors (only the expected 404s for external stylesheets/fonts this
    minimal test server doesn't serve, unrelated to this change).
- This is a smaller-scope, lower-risk change than cc#2005 (text only, no new data flow, no new
  interactive mechanism), so the verification is scaled to match — a targeted Playwright pass on
  the actual `ruleSheet()` output rather than the fuller structural sweep cc#2005's swipe deck
  needed, plus `node --check`.

## What this does NOT change

The FULL/HALF/NONE boxes, the credit/weight numbers, the volume rows, cc#1987/1988/1989/1990's
layout, and the dormant `DEFS` map (a different, not-yet-wired feature — left completely untouched,
not repurposed for this card). No rule logic or threshold changed anywhere — text only, per the
card's own explicit out-of-scope note.

Card done from this side. No founder glass-check gate here (unlike cc#2005) since nothing here is
a number or a live-data question — Fable verifies the diff + the description text against the
engine's own conditions.
