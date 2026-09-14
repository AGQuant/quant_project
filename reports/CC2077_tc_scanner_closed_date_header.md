# cc#2077 — TC Scanner Closed Book: last column header "Date" → "Closed Date"

Founder ask: the Closed Book's last column reads "Date" — ambiguous since the table mixes rows
entered on different days. Relabel to "Closed Date".

## Step 1 gate

Confirmed via this window's own earlier reads (cc#2072/2076): the cell under this header renders
`dShort(r.exit_ts)` — the exit/closing date, matching cc#1599's exit-date basis exactly. Per the
card's own scope_note, only the label changes; the data source is untouched.

## The fix

`v8_dashboard.html`, `closedTable()`'s hand-built `<thead>`: `<th>Date</th>` → `<th>Closed Date</th>`.

## Verify

`node --check` clean — caught and fixed one thing on the way: my first pass placed the new
comment as a JSX-style `{/* ... */}` inline in the middle of the string-concatenation chain, which
is not valid there in plain JS (that position needs a `+` continuation, not a block). Corrected to
a plain `//` comment on its own lines before the term; `node --check` then passed clean, exactly
the kind of mistake this step exists to catch before it ships.

Real headless Chromium, `closedTable()` extracted verbatim: the rendered header row is exactly the
expected 7 columns, last one now reading "Closed Date" — 4/4 checks pass.

**FOUNDER-ONLY, not done here** (this container is network-blocked from scorr.in): confirming
on-glass that the live Closed Book's last header now reads Closed Date.
