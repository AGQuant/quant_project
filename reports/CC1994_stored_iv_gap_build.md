# cc#1994 build — stored put/call IV gap surfaced on the option chain

Per `FOUNDER_RULING_11SEP` (written directly into `cc_tasks.spec` for id=1994, relayed
`cc_task_logs` 6358): the ATM put/call IV asymmetry (cc#1994's own read-only finding, sha
`d2643c7`, `reports/CC1994_forward_separating_test.md`) is accepted as **real market structure**,
not a solver bug. Do not fix the pricer, do not run the far-dated test. Build ask: "show the
put-vs-call IV gap per strike as a plain fact with its own as-of, no interpretation... one push,
chain payload + row label, verify on NIFTY and RELIANCE that the gap shown equals stored put_iv
minus call_iv at that strike."

## Where it comes from, and why not from the chain's own live IV

`/api/deriv/strike-chain/{symbol}` (`deriv_metrics.py`) already computes a live `iv` per CE/PE row
— Black-Scholes-inverted from a live Fyers ltp, intraday. That is a **different number from a
different day's data** than what this card is about: `option_iv_daily` (`option_iv_history.py`) is
the prior trading day's NSE bhavcopy settle, solved the same BS-inversion mechanism nightly.
Blending the two into one "gap" would misstate which day's data produced it. So the stored gap is
its **own field**, `stored_iv_gap`, computed from `option_iv_daily` only, carrying its **own
as-of** (`stored_iv_asof`, the bhavcopy `trade_date`) at the top of the response — never derived
from or mixed with the chain's own live `ce.iv`/`pe.iv`.

New function `_stored_iv_gap_map(cur, sym)` in `deriv_metrics.py`: one query for the latest
`trade_date` that symbol has, one query for that date's `(strike, option_type, iv)` rows, builds
`{strike: {call_iv, put_iv, gap_vol_pts}}` for strikes with a usable pair. Called once (same
cursor, no extra DB connection) inside each of `strike_chain()`'s two existing branches (index /
stock), right where `rv20`/`spot` are already being read — then each chain row gets
`row["stored_iv_gap"] = gap_map.get(row["strike"])` (`None` where there's no stored data or no
usable pair) after `_price_rows()` builds the live rows. `option_iv_daily` is read-only here, never
written (do_not_touch on the original card, unaffected by this build phase).

## The floor-artifact filter — a data-integrity guard, not an interpretation

Checked real data before writing a line of code (NIFTY + RELIANCE, `trade_date=2026-09-10`):
several strikes have one leg's stored `iv` sitting exactly on `0.0001` — `option_iv_history.py`'s
own solver (`_bs_iv_vec`) bisects on `[1e-4, 5.0]`, so a value landing on the floor means the
bhavcopy close price was below what *any* positive vol could produce (typically a deep-ITM leg
priced under intrinsic that day), not a real solved answer. Confirmed on the actual rows: NIFTY
strikes 23000-23300 CE all floor (deep ITM, spot 23484); RELIANCE strike 1370 PE floors the same
way at the other end (spot 1272.5). A "gap" built from a floored leg would be a numeric artifact
wearing the shape of a market fact — exactly the kind of thing this codebase already refuses to do
quietly elsewhere (`gvm_nightly.py`'s `excluded_no_market_cap`, same principle). So `_stored_iv_gap_map`
only returns a strike when **both** legs clear `0.001` — comfortably above the floor. A strike
without a usable pair simply carries no `stored_iv_gap` (`null`), never a manufactured number. This
is a data-integrity filter, not an economic reading of the gap — it says nothing about *why* puts
trade richer, only refuses to report a subtraction where one side of it wasn't really solved.

## The rounding fix the isolated test caught before push

First draft computed `gap_vol_pts` independently from the raw (unrounded) `put_iv - call_iv`
fraction, rounded once. The isolated test (below) caught that this can disagree by 0.1 from the
**displayed** `put_iv`/`call_iv` fields subtracted by hand (e.g. NIFTY ATM: raw gap rounds to 6.0,
but the displayed 12.1 − 6.2 = 5.9) — exactly the discrepancy the card's own verify line would
catch ("the gap shown equals stored put_iv minus call_iv"). Fixed: `gap_vol_pts` is now derived
from the two *already-rounded* display fields (`round(put_pct - call_pct, 1)`), so the identity
holds exactly on the numbers a reader actually sees, by construction, not by coincidence.

## Row label (frontend)

`scorr_cockpit_card.js`'s `dcFetchStrikes` (the ONE place this table renders — `v8_dashboard.html`
loads it as a shared module since cc#805, no second copy to keep in sync) gets:
- a small `ΔIV {+/-}{gap_vol_pts}` label under the strike number, shown only when `stored_iv_gap`
  is present for that row, with a hover title spelling out "Stored (bhavcopy): put IV X% minus
  call IV Y%, as of {date}" — plain fact, sign shown as computed, no directional wording baked in;
- `· stored IV as of {date}` appended to the chain's existing summary line;
- one sentence added to the existing Fair/Tag legend footnote defining ΔIV as "stored put IV minus
  call IV at that strike (nightly bhavcopy capture, not this table's own live IV) — shown only
  where both legs solved cleanly."

`node --check scorr_cockpit_card.js` clean.

## Verify — against the real committed function, real 10-Sep data

`fastapi` / `psycopg` / `requests` aren't installed in this environment, so `deriv_metrics.py` was
imported the same way as cc#1999's guard test this session: the three unavailable/side-effecting
modules stubbed via `sys.modules`, everything else genuinely the just-edited file. A fake DB cursor
replays the **actual rows** read from `option_iv_daily` for NIFTY and RELIANCE (`trade_date`
2026-09-10, fetched via `run_sql` immediately before writing the test) — real data, not synthetic.

- **NIFTY** (spot 23484, ATM strike 23450): `call_iv=6.2`, `put_iv=12.1`, `gap_vol_pts=5.9`,
  `stored_iv_asof=2026-09-10`. 7 floored CE strikes (23000-23300) correctly excluded.
- **RELIANCE** (spot 1272.5, ATM strike 1270): `call_iv=16.3`, `put_iv=20.3`, `gap_vol_pts=4.0`,
  `stored_iv_asof=2026-09-10`. 6 floored strikes (5 deep-ITM CE + the 1370 PE) correctly excluded.
- An independent brute-force recompute from the same raw rows (round-then-subtract, mirroring the
  real function's own formula) matches `gap_map` exactly on every strike for both symbols — 14
  strikes (NIFTY), 15 strikes (RELIANCE).
- `gap_vol_pts == round(put_iv - call_iv, 1)` asserted directly on the returned, already-rounded
  fields for both ATM strikes — the card's own verify wording, checked as a literal equality, not
  approximated.

Both ATM gaps land puts-richer (5.9 and 4.0 vol pts) — consistent with the original finding's
median +3.51 vol pts and the founder's own acceptance of it as real structure, on fresh data one
day newer than the finding itself relied on.

Script: `test_cc1994_stored_gap.py` (scratchpad, not committed — a test harness, not a project
file).

## What was NOT done, stated plainly

- The pricer / solver is untouched — `option_iv_history.py` is not part of this diff.
- `option_iv_daily` is read-only here — no write path added or changed.
- No far-dated (60+ day) test run — the founder ruling explicitly says not to.
- ATM build (`reports/CC1859_atm_iv_definition.md`'s CE/PE average) is unaffected — this is an
  additive field, nothing existing recomputed or replaced.

## Verify checklist

- `ast.parse` + `py_compile` clean on `deriv_metrics.py`.
- `node --check` clean on `scorr_cockpit_card.js`.
- Real-data isolated test: 2/2 symbols pass (gap map matches independent recompute, floored legs
  excluded, ATM gap matches "put_iv minus call_iv" exactly).
- `_price_rows`, `strike_chain`'s existing live ce/pe iv, banding, tag logic — all untouched;
  `stored_iv_gap` is a pure addition to each row's dict, `stored_iv_asof` a pure addition to the
  top-level response.

Nothing under `worker/**`. Files changed: `deriv_metrics.py`, `scorr_cockpit_card.js` (+ this
report). Card **NOT** set done — Fable verifies. Finding rule (`level=finding` on cc#1199) posted
separately.
