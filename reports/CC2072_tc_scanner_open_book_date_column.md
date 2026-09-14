# cc#2072 — TC Scanner Open Book: last column shows open date, not days held

Founder ask: the Open Book table's last column showed an elapsed-days count ("7d", "today").
Wants the actual open/entry date instead.

## Step 1 gate

Confirmed the card's own citation directly: `TC13_OPEN_COLS`'s last pair was
`["_daysN","Days Held"]`, and `tc13OpenRow(r)`'s last cell called `daysHeld(r.entry_ts)` — the
elapsed-days formatter (`"7d"` / `"today"`). `entry_ts` (full timestamp) is already on every
`open_all` row per the card's own evidence — confirmed, no backend change needed.

Found the right formatter already on this page rather than writing a new one: `dShort(ts)`
(`"D MMM"`, e.g. `"12 Sep"`) is the exact function the Closed Book's own Date column already
calls on `exit_ts` — reusing it for `entry_ts` here means the Open Book and Closed Book now show
dates in the identical format, one function, not a second near-duplicate.

## The fix

- `v8_dashboard.html`, `tc13OpenRow(r)`: last `<td>` — `daysHeld(r.entry_ts)` → `dShort(r.entry_ts)`.
- `TC13_OPEN_COLS`: the last column's header label — `"Days Held"` → `"Open Date"` (the header has
  to match what the column now shows; leaving it as "Days Held" over a rendered date would be its
  own bug).
- **Sort key intentionally left alone**: `_daysN` (`daysHeldNum(entry_ts)`, cc#1884's own numeric
  twin) still drives the column's sort — it's a monotonic inverse of the entry date, so sorting by
  it still sorts the column chronologically. Recomputing a second date-based sort key for the same
  ordering would be pure churn for zero behaviour change, so left untouched.
- `daysHeld()`/`daysHeldNum()` themselves are untouched and still defined — `daysHeldNum` still
  feeds the sort key; `daysHeld` (the display string) is simply no longer called from this cell.

**Do not touch, respected**: the Closed Book's own Date column (`dShort(r.exit_ts)`) — confirmed
byte-identical, this card only ever touched the Open Book's last column.

## Verify

`node --check` clean (all 8 inline `<script>` blocks). Real headless Chromium, the actual
`TC13_OPEN_COLS` / `tc13OpenRow` / `openTable` extracted verbatim from the committed file —
**17/17 checks pass**:

- Source: new label present, old label gone, `dShort(entry_ts)` call confirmed, both day-count
  helpers still defined, the sort-key computation and the Closed Book's own date cell both
  confirmed byte-unchanged.
- A 3-row fixture (entered 08-Sep, 12-Sep, and today) run through the real `openTable()`: header
  reads "Open Date"; each row shows its own formatted date (`"8 Sep"`, `"12 Sep"`, today's date) —
  never the old `"Nd"`/`"today"` elapsed text.
- Sort key still computes correctly and stays numeric: the older (08-Sep) entry has a higher
  `_daysN` than the newer (12-Sep) one, and today's entry is 0 — chronological order preserved.
- Zero page errors.

**FOUNDER-ONLY, not done here** (this container is network-blocked from scorr.in): confirming
on-glass that the live Open Book's last column now reads as a date.
