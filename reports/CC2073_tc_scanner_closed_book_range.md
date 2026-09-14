# cc#2073 — TC Scanner Closed Book: full history by default + date-range selector

Founder ask: the Closed Book only ever showed trades closed on ONE selected date. Wants it to
show everything closed to date by default, with an option to pick a date RANGE instead of one day.

## Step 1 gate — confirmed the bug on real data before writing a line

`tc_scanner_endpoints.py` (`/api/scanners/tc/holds`) matched the card's own citation exactly:
`WHERE h.exit_reason <> 'OPEN' AND h.exit_ts::date = %s`, one `date_` param defaulting to
`str(date.today())`. Ran the real predicate against the live table before touching anything:

| query (real production data, 2026-09-14) | result |
|---|---|
| total closed rows (`exit_reason <> 'OPEN'`) | **21** |
| closed span | 07-Sep to 11-Sep (1, 3, 9, 2, 6 per day) |
| OLD default — `exit_ts::date = CURRENT_DATE` (today, 14-Sep) | **0** |

Today is a market holiday with zero closures — the old default silently showed an empty table
against 21 real closed positions. Confirmed the exact bug, not assumed it.

## The fix

**Backend** (`tc_scanner_endpoints.py`, `/api/scanners/tc/holds`):
- New optional params `from_`, `to_` alongside the existing `date_` (both use the file's own
  trailing-underscore convention — `date`/`from`/`to` all shadow builtins/keywords). Resolution:
  `from_`/`to_` given → use them (`to_` defaults to today if only `from_` given); else a lone
  `date_` → collapses to a one-day range (`from=to=date_`, byte-identical to the old behaviour);
  else (no params at all, the boot call) → `from=None` (unbounded), `to=today`.
- The `closed_by_exit` query now runs `exit_ts::date BETWEEN %s AND %s` when a lower bound is in
  effect, or `exit_ts::date <= %s` (unbounded) when it is not — one extra branch, same table, same
  `exit_ts::date` basis cc#1599 already established.
- `closed_by_exit_stats` and `closed_by_exit_book` need no separate change — they already compute
  over `by_exit` (built from the query's own rows), so widening the query widens them for free.
- Response gained `"closed_range": {"from": ..., "to": ...}` — the range actually applied, so the
  frontend never has to guess or keep its own copy of what was requested.
- **Untouched, confirmed by diff**: the `buy`/`sell` (scan_date-keyed) legacy query, the `"date"`
  response field, and the entire `open_all` block (cc#1744 — the Open Book has no date predicate
  at all and this card doesn't touch it).

**Frontend** (`v8_dashboard.html`, TC13 Closed Book):
- `closedTable(rows, dateStr, lastClosure)` → `closedTable(rows, range, lastClosure)`. `range` is
  the endpoint's own `closed_range`, not separate UI state — the two date inputs and the header
  badge always show exactly what was queried, never a stale guess (`range=range||{from:null,
  to:null}` guards a direct call with nothing passed).
- Single `#tc13Date` picker → two inputs, `#tc13From` / `#tc13To`, each firing `load()` with both
  current values on `change` (editing one preserves the other).
- `load(dateStr)` → `load(fromStr, toStr)`, building `?from_=&to_=` (either or both omitted when
  blank). `tc13Boot` now calls `load(null,null)` — sends **no query params at all**, so the very
  first load hits the server's own new default (full history to date), not a client-guessed date.
- Empty-state message and header badge both describe the range in plain words ("through 14-Sep" /
  "08-Sep → 10-Sep") instead of a single day.
- **Deploy-window safety**: `render()` reads `d.closed_range || {from:null, to:d.date}` — a
  payload from an old server (mid-deploy) that has no `closed_range` key still renders, matching
  this file's own established fallback convention for exactly this situation (cc#1744/cc#1599
  used the same pattern for their own new keys).
- **Untouched, confirmed by diff**: Open Book (`openTable`, `prox`, `sortTbl` call), the Long/Short
  side filters, `closedKpis`/`bookRs` (Realised/Accuracy/Avg Profit math itself — only the markup
  wrapping them changed).

## Verify

`ast.parse` clean (backend). `node --check` clean on all 8 inline `<script>` blocks (frontend).

**Backend — real production data, not a fixture** (`mcp__Scorr__run_sql` against the live table):

| predicate tested | expected | got |
|---|---|---|
| new default: `exit_ts::date <= CURRENT_DATE` | 21 (all closed) | **21** |
| new range 08-Sep..10-Sep: `BETWEEN` | 3+9+2=14 | **14** |
| new single-day collapse (09-Sep, from=to) | matches old exact-match | **9 = 9** |
| old exact-match on 09-Sep (regression control) | 9 | **9** |
| old default on today (the bug, for the record) | 0 | **0** |

The single-day-collapsed range returns the byte-identical count to the old exact-match query —
zero regression for the "pick one day" case the card asked to keep.

**Frontend — real headless Chromium**, `closedTable()`/`load()`/`render()` extracted verbatim from
the committed file (brace-balance self-checked on extraction) — **38/38 checks pass**:
- Source-level: both files carry the new signatures, `#tc13Date` is fully gone, Open Book's own
  render call site is untouched.
- `closedTable()` in isolation: unbounded-from range → From blank / badge "through `<date>`";
  bounded range → both inputs + badge show the real dates; single-day collapse → both inputs equal;
  `undefined` range doesn't throw; empty-range message reads correctly for both shapes; a 2-row
  fixture's Realised/Accuracy capsules compute correctly through the new markup (regression check
  on code this card didn't touch).
- `load()`: all four argument combinations build the exact expected URL, including the boot case
  (`null,null` → no query string).
- `render()` end-to-end on a realistic payload: `#tc13From`/`#tc13To` wired to the payload's own
  `closed_range`; editing `#tc13To` fires one fetch carrying the still-blank `#tc13From`'s value
  correctly (not silently dropped); Open Book renders unaffected; a payload with no `closed_range`
  key (deploy window) still renders via the fallback, no throw.
- Zero page errors across every case.

**FOUNDER-ONLY, not done here** (this container is network-blocked from scorr.in): confirming
on-glass that the live Closed Book now shows the 21 real trades by default and that the From/To
pickers behave the same way against the live endpoint.
