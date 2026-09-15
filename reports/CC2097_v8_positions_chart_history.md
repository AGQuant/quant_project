# cc#2097 — V8 Positions deck: chart button icon swap, Unrealised history, window toggle

## What shipped

### Item 1-2: icon swap + folded-in breakdown
The three pane-leading boxes (UNREALISED, REALISED, NET TOTAL) had a small `(i)` disclosure
button opening a separate popover (`kwInfoOpen`, cc#2005). That popover and its own click branch
are retired — `kpiWell()`'s corner button is now a chart-glyph (`opts.chartBtn`), wired to the
same `dlChartOpen()` the box's whole-area tap already opened (`opts.chart`, unchanged). The
number breakdown that used to live behind `(i)` (`kwInfoLines(key)` — reused, not deleted) now
renders as a footer inside the chart sheet itself (`opts.chartInfo`, threaded through as
`data-kw-chart-info`) — one visible affordance per box, nothing lost.

### Item 3: v8_unrealised_daily — the first-ever stored unrealised history
New table + endpoint (`v8_unrealised_daily.py`) + scheduled job (`bg_v8_unrealised_snapshot`,
15:35 IST, 5 min after close, registered in `scheduler_master`). Reads `v8_book_canon.book_canon()`
directly — the exact function `/api/mobile/v8book` itself calls (rule 13 V8_PNL_CANON_V1) — and
stores `unrealised` / `long.unrealised` / `short.unrealised` / `open` verbatim. Never recomputes
from raw positions. **No backfill** (item 4's own instruction): there is no historical
mark-to-market record anywhere in this schema, so history starts accumulating from this feature's
first real snapshot — the client says so plainly when the series is still empty.

`/api/v8/unrealised_daily/series` serves the same `points`/`window_start`/`trading_days` shape
`v8_daylog_extras.build_series` (cc#1561) already established, so the client's existing chart
drawing code needs zero new drawing logic — only the *meaning* of `net_cum`/`gross_cum` differs
(a daily mark-to-market **level**, not a running sum), which the client now threads through
explicitly via a `kind` parameter rather than silently treating a level series as a cumulative flow.

### Items 4/6: Unrealised pane routes to the new series
All three `open-kpi` boxes (UNREALISED/LONG/SHORT) now open `kind='unreal'` — a real, separate
change in *what* the chart shows, not just how it's launched (previously all three opened the
REALISED cumulative chart, with live unrealised tacked on as one end-point). Opens to a 1-week
default window, per the founder's explicit ask.

### Item 5/6: window toggle (Since start / 1W / 1M / 3M)
Client-side slice of the already-fetched `points` array — no new backend endpoint, no second
formula. A window means "P&L change since **that window's own start**", symmetric with what
"Since start" already means for the full era — not a truncated view of the era-long cumulative
curve sitting at whatever value it happened to reach by then. The live end-point's math had to
become kind-aware to stay correct after rebasing:

- **`kind='realised'`** (unchanged formula): `liveVal = rebased_last_point + live_unrealised`.
  This is baseline-invariant — rebasing shifts every historical point by a constant, and
  `live_unrealised` is always an *additive increment* on top, so the same formula is correct
  whether or not a window rebased the series.
- **`kind='unreal'`** (new): the historical points are **levels**, not a flow, so adding
  `live_unrealised` to the last point would double-count. Its live point is
  `live_unrealised − rebase_amount` instead — landing on the same rebased scale as the line it
  extends.

Verified this distinction explicitly (see below) with a test that would have caught exactly this
double-counting bug if the formula were wrong.

The window toggle applies uniformly to both kinds (item 6: "no extra code needed") — a kind/window
combination with fewer real days than the window asks for just returns everything that exists,
never fabricates the rest.

**Return/CAGR footer, deliberately NOT recomputed for a window**: the era-level Return/CAGR
figures are server-provided and only ever shown for the unwindowed "Since start" view. A windowed
view shows the window's own net P&L change instead — the rebased last point re-displayed as plain
currency, which is not a new formula, just the same subtraction the chart line itself already
performs.

## Verification

**Backend** (`v8_unrealised_daily.py`): syntax-validated (`ast.parse`/`py_compile`); `scheduler_master`
row registered, `active=true`; the gated boot-time job will produce real first-run evidence on
the next deploy + trading-day close (15:35 IST) — no real rows exist yet since this is a brand-new
table, stated honestly rather than claimed.

**Frontend** (`mobile/v8.html`): `node --check` clean on the full extracted script. The pure
window-slice/live-endpoint math (`dlWindowSlice` + the kind-aware `liveVal` logic) was copied
verbatim from the committed file and exercised in Node against constructed test fixtures (no real
`v8_unrealised_daily` rows exist yet to test against — this verifies the *logic*, not real
production numbers):

| Test | Result |
|---|---|
| "Since start" window returns points unchanged, rebase 0 | PASS |
| "1W" window slices to exactly the last 7 calendar days AND rebases the first point to 0 | PASS |
| `kind='realised'` live-endpoint formula is baseline-invariant (correct with or without rebasing) | PASS |
| `kind='unreal'` live-endpoint correctly **subtracts** rebase (a level, not a flow) | PASS |
| Explicit check that the unreal formula does NOT match the (wrong) additive realised-style formula — proves no double-counting | PASS |
| A window wider than the real history available degrades to "everything that exists", never fabricates extra days | PASS |

## What did NOT change

The whole-box tap-for-chart behaviour (`data-kw-chart`) — unchanged, the new button is additive.
`dlChartSvg`/`dlChartOpen`/`dlSeriesLoad`'s existing Net/Gross pill and live-endpoint logic for
Realised and Net Total — unchanged in the default "Since start" case (verified above). `/api/v8/daylog/series`
and `v8_daylog_extras.py` — read-only use, no schema or formula change. `book_canon`'s own
canon numbers — the new snapshot job reads them, never recomputes.

## Live checks still needed (Arpit)

Per the card's own verify list: confirm all three panes show the chart-glyph button where `(i)`
used to be; confirm the window pill row appears and re-slices without a new network request per
click; confirm — after the first few trading days post-ship — the Unrealised chart shows one real
point per day plus today's live end-point, with no fabricated pre-ship history.
