# cc#2083 (normal) — Trade Check V4 dual-style: left tiles + verdict blank despite a real computed score

Founder screenshot (14-Sep-2026), symbol PNB: all four style tiles (BUY-MOM/BUY-REV/SELL-MOM/
SELL-REV) show a dash for score and verdict, and the headline result reads a dash — while the
right-side rule breakdown for the selected BUY-MOM tile renders a full, real 20-rule table summing
to 52.50/100.

## Root cause — confirmed by reading the code, not the card's own guess

The card suspected a frontend field-name mismatch between the tile renderer and the (working)
rules table. That is **not** what is happening — both read the same payload correctly. The real
cause is a genuine, deliberate backend design (`tc_v4_dual.py`, cc#1909 TICK OVERLAY,
TC_LIVE_INTRADAY_CANON_V1, do_not_touch) whose corresponding frontend honesty step was never built:

- Every card is first scored **live**, straight from its own `rules` (credit/weight/max) — this is
  always correct and always populated, which is exactly why the right-side rules table (which
  recomputes its own total directly from `rules`, never from a score field) works.
- A second pass then **overwrites** `score10`/`verdict10`/`score100` on every card with **today's
  stored tick** from `tc_universe_ticks`, in place — by explicit, commented design: *"never a
  silent fall-back to the live ratio."* When no tick exists for today, that pass sets
  `score10`/`verdict10`/`score100` to `None` **and** `no_score_today = True`, discarding the
  perfectly good live numbers on purpose.
- `scorr_check.html` never read `no_score_today` anywhere — the hero verdict word and every tile
  just saw `null` and rendered a bare, unexplained `—`, on top of a rules table that (correctly,
  by a completely different code path) still had real numbers to show.

This is the same class of gap this session has already fixed twice this week (cc#2078's
`opening_data_missing`, cc#2080's `market_open`): a backend correctly refuses to fabricate a
number and already flags exactly why — the flag was simply never wired to a display. **do_not_touch
respected**: `tc_v4_dual.py`'s tick-overlay logic, and the rules table's own live computation, are
both completely untouched. This fix only explains the blank; it never changes what number (or lack
of one) is shown.

## Item 4 — does this reproduce only on a no-tick day, or also on a normal trading day?

**Confirmed against real production Postgres**: `tc_universe_ticks` has **zero rows for PNB today**
(14-Sep, an NSE holiday) — exactly the reported case. On the last real trading day (11-Sep), the
very first tick of the entire universe landed at **09:15:02 IST**, seconds after the market opened.
So in practice this is overwhelmingly a **non-trading-day** (holiday/weekend) symptom; on an
ordinary trading day the "no tick yet" window is only the first couple of seconds after 09:15, not
a meaningfully recurring daily gap. The fix handles both cases identically and correctly either way
— it does not special-case "is today a holiday", it reads the same `no_score_today` flag the
backend already sets for exactly this condition, whatever causes it.

## Fix — `scorr_check.html` only

Three places in the same render chain (`_tcrHero`, `tcrRead`), each reading `no_score_today` per
card (a tile is its own bucket's tick lookup, so one bucket can carry a tick while a sibling does
not — confirmed handled independently, see Verify):

- **Hero verdict word**: shows "No tick yet today" instead of a bare `—` when `best.no_score_today`.
  The headline **score** itself stays `—` — still no fabricated number, only the missing-reason
  label changes.
- **Each tile's badge**: shows "no tick yet" instead of a bare `—` when that card's own
  `no_score_today` is set — independent per tile, not borrowed from the hero.
- **The (i) info line and its tap-to-expand sheet** (`tcrRead`): both explain the same fact in a
  full sentence, and state plainly that the rule breakdown on the right is still live — so a reader
  is never left wondering why the two sides disagree.

**Wording reused, not invented**: "no tick yet today" is this exact file's own **pre-existing**
phrase, already used on the CMP line (`_tcrScoredLabel`) for the identical "no tick for today"
condition. The new copy matches it rather than introducing a second phrase for the same fact.

**Scope note — `mobile/check.html` not touched, a real open question flagged, not guessed at**:
mobile/check.html also renders BUY-MOM/BUY-REV/SELL-MOM/SELL-REV tiles with the same bare-`—`-on-
null pattern and no visible `no_score_today` handling either — but it calls a **different**
endpoint entirely (`/api/mobile/check/tc`, confirmed by reading its own fetch call — not
`/api/trade-check/v4/detail`), and that page carries its own actively-evolving history around this
exact score/tick distinction (cc#1987/1988/1991/1995, including one item already "flagged for a
ruling"). The founder's own screenshot evidence (a two-column "Right side"/"Left card" layout) is
unambiguously the web page. Confirming whether the mobile endpoint's payload even carries
`no_score_today`, and how it should be surfaced given that page's own recent redesign history, is
a genuinely separate investigation — stated here as open, not guessed into a page I have not fully
traced.

## Verify

`node --check` clean on both non-empty inline `<script>` blocks in `scorr_check.html`.

**14/14 checks, real headless Chromium**, `_tcrHero` and `tcrRead` extracted verbatim
(balance-checked) across four realistic payload shapes:
1. **No tick today, PNB's real shape** (all 4 cards `no_score_today=true`): hero verdict says "No
   tick yet today", hero score stays an honest `—`, **all four tiles** independently show "no tick
   yet" (not a bare dash), the (i) line names the tick engine and states the rule breakdown is
   still live, and the tap-to-expand sheet explains the same fact rather than silently rendering
   nothing.
2. **A normal day with real tick data** (regression): the hero shows the real verdict ("Valid") and
   the real score (52.5) — zero "no tick" text anywhere, confirming no regression on the working
   path.
3. **The pre-existing uncalibrated case** (regression): still shows "Uncalibrated", not overridden
   by the new no-tick branch — confirmed the two states stay mutually exclusive (a card only ever
   reaches `uncal` when `score10` is non-null, and the tick overlay always nulls `score10` together
   with `no_score_today`, so they can never fire on the same card).
4. **Mixed cards** (one bucket ticked, one not): each tile states its own, independently correct
   reason — the ticked bucket shows its real verdict, the un-ticked sibling shows "no tick yet".

Two test-harness issues were caught and fixed along the way, both test-script artifacts, not
product bugs: a missing `TC_RULE_DEFS` stub (the real page defines this large rule-metadata
constant globally; `_tcrName()` already degrades gracefully when a specific key is absent, but
needs the object itself to exist) and a tile-count assertion that double-counted the pre-existing
CMP line's own, unrelated use of the same "no tick yet" substring — fixed to match the tiles'
exact markup instead of a bare substring search.

**FOUNDER GLASS CHECK, not done here** (this container is network-blocked from scorr.in): re-run
PNB on the live Trade Check page and confirm the tiles now say "no tick yet" / "No tick yet today"
today, and spot-check a second symbol on the next live trading day to confirm the tiles show real
numbers exactly as before, matching the detail-panel figures.
