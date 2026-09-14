# cc#2078 (high) — Custom Screen Builder: OI Δ% and TC /100 both empty (two separate no-fallback date bugs)

Founder screenshot: on the V8 Dashboard's Index Intel pane, the Custom Screen Builder's OI Δ% and
TC /100 columns showed a dash for all 207 matched rows.

This card arrived with both root causes already precisely diagnosed (exact SQL, exact numbers) —
my Step 1 gate was confirming that diagnosis by reading the code directly, not re-deriving it.

## Root cause A — OI Δ% (read-only backend flag, never surfaced)

`v10_endpoints.py`'s `v10_buildup()` — the `o` CTE computes the since-open OI baseline only from
bars inside the 09:15–09:30 opening window (cc#1509's own explicit ruling: a baseline measured
from a late first bar "would be a fabricated number"). The 11-Sep session's first bar arrived at
10:05, so **zero** symbols got a baseline that day — universe-wide, not per-symbol. The endpoint
already detects and reports this exact case (`opening_data_missing`, `first_bar_time`,
`symbols_with_open` — cc#1844, 08-Sep) but repo-wide search confirmed **zero** matches for
`opening_data_missing` in `v8_dashboard.html`: the flag has existed in the payload for six days
and was never wired to any display. cc#1834's own closing note flagged exactly this gap and
promised a follow-up card that was never filed — this is that follow-up.

**Found the fix already built once**: `scorr_v10_signal.html`'s `buCard()` (the OI Build Ups card
on that same page) already reads `bd.opening_data_missing` and renders a founder-ruled message —
"Opening data is missing for today — the futures feed started at `<time>` IST, after the market
opened. \[X\] needs the session's first bar to measure open-interest change from, so it has
nothing to show right now." That wording is reused **verbatim** here (adapted for "OI Δ%"), not
reinvented, per the same one-fact-one-sentence discipline this codebase applies elsewhere.

**Fix, in two places**: `v8_dashboard.html`'s `rCustomScreen()` (the surface the founder's
screenshot actually shows) and — found to have the identical, never-wired gap while checking —
`scorr_v10_signal.html`'s own `csbSection()` (my cc#2068 port of the same card, sharing the same
`D.build` payload). Both gained the same `oiNotice`, rendered above the table whenever
`D.build.opening_data_missing` is true, empty string otherwise. No backend change for this half —
the flag already existed; only the display was missing.

## Root cause B — TC /100 (backend: no session fallback)

`v10_endpoints.py`'s `v10_tc_screen()` (`GET /api/v10/tc_screen`) built `start_utc` as midnight IST
of **today** and queried `tc_universe_ticks WHERE ts >= start_utc` with no fallback. `tc_universe_ticks`
itself is healthy and complete for 11-Sep (verified: 207 symbols, 828 rows = 207 × 4 buckets, last
tick 15:20 IST) — but the endpoint only ever looks forward from today's midnight, so on any day
before today's first tick lands (today, 14-Sep, is a market holiday — zero ticks) it returns an
empty `scores` object regardless of how recent or complete the last real session was.

**Fix**: mirrors `v10_buildup`'s own `sess` CTE session-resolution pattern exactly, per the card's
own instruction — `WITH sess AS (SELECT MAX(ts::date) AS d FROM tc_universe_ticks) ... WHERE
ts::date = sess.d` instead of a hardcoded "today" cutoff. Falls back to the latest real session's
close-of-session scores. Docstring updated to say "latest tick of the most recent session with
data" rather than "today", since that's now factually what it does. The now-dead `datetime`/
`timezone` imports and the `now_ist`/`start_utc` computation were removed; the `as_of` conversion
now reuses the already-built `ist` ZoneInfo instead of constructing a second one redundantly.
Both `v8_dashboard.html` and `scorr_v10_signal.html` read this same endpoint — one backend fix
resolves the TC /100 column on both surfaces, no frontend change needed for this half.

**do_not_touch respected**: the ABSENT-vs-FAILED / never-fabricate conventions are untouched on
both bugs — an individual symbol genuinely missing a score still renders a dash; this card only
ever widened which SESSION is looked at, never invented a number for a symbol that has none.

## Verify

`py_compile` clean on `v10_endpoints.py`. `node --check` clean on both HTML files.

**Backend — real production Postgres, not a mock.** Re-ran the card's own diagnostic query
pattern directly: `MAX(ts::date)` resolves to 2026-09-11, 207 distinct symbols, 61,272 rows, last
tick 09:50:08 UTC (15:20 IST) — matching the card's own stated numbers exactly. The full
`DISTINCT ON (symbol, bucket)` query my fix actually runs was executed for real: **828 rows, 207
symbols, all 4 buckets** — a complete, healthy session, exactly what `v10_tc_screen()` will now
return instead of an empty object.

**Frontend — real headless Chromium**, the new `oiNotice` ternary from both files extracted
verbatim — **22/22 checks pass**: renders the correct message (with the real `first_bar_time`,
truncated to HH:MM) when `opening_data_missing` is true; renders nothing on a healthy day; renders
nothing (and doesn't throw) when `D.build` itself hasn't loaded yet; falls back to honest text
("a later time") when `first_bar_time` itself is missing rather than blank/NaN; `first_bar_time` is
HTML-escaped, not injected raw (checked and fixed one over-strict test assertion of my own along
the way — the code's `.slice(0,5)` truncation runs *before* `esc()`, matching `buCard()`'s own
established order, so only the first 5 raw characters are ever escaped; the test now checks the
property that actually matters, zero unescaped `<` anywhere in the output). Source-level checks
confirm the notice is spliced into both render chains in the right position, and the wording is
byte-identical between both files.

Re-ran cc#2068's own full original build-and-test suite against the current file afterward —
**26/26 still pass**, zero regression on the Custom Screen Builder's core filtering/sorting/
persistence behaviour.

**FOUNDER-ONLY, not done here** (this container is network-blocked from scorr.in): confirming
on-glass that the live Custom Screen Builder now shows real OI Δ% and TC /100 values (today being
a market holiday, TC /100 should show 11-Sep's real scores; OI Δ% will show the honest notice again
today specifically once the feed does start, if it starts after 09:30 — that direction of the bug
is now a stated fact rather than a silent blank, not eliminated, since a fabricated baseline is
exactly what cc#1509 already ruled out).
