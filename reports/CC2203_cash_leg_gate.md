# cc#2203 — The cash-leg outage gate closes at 15:15, when continuous cash trading ends

Filed and built 17-Sep-2026 from the cc#2201 diagnostic; the landing time is in the task row. One file: `yahoo_live_quote.py`. `feed_guardian.py`, `worker/**` and the cc#2198 cache are untouched.

## Evidence

| Slot (IST) | fyers_eq rows | fyers_eq_auction rows | fyers_fut rows |
|---|---|---|---|
| 17-Sep 15:10 | 211 | 0 | 207 |
| 17-Sep 15:15 | 12 | 211 | 207 |
| 17-Sep 15:20 | 0 | 211 | 207 |
| 17-Sep 15:25 | 0 | 211 | 207 |
| 16-Sep 15:15 | 7 | 211 | 207 |
| 16-Sep 15:20 | 0 | 211 | 207 |

`intraday_prices`, 5-minute bars per source. The cash leg does not stall at 15:15 — it changes source. SEBI's closing auction (CAS, live since 03-Aug-2026) ends continuous cash trading at 15:15; `scheduler._is_cash_continuous()` is 09:15–15:15 for that reason, the feed worker writes the cash leg as `fyers_eq_auction` from 15:15, and the guardian tick stops at 15:15 by design (last ticks 15:15:13 on 16-Sep, 15:15:18 on 17-Sep, 15:20:58 on 15-Sep).

`yahoo_live_quote.fyers_eq_outage()` did not know this: it gated to `MARKET_CLOSE` 15:30 and measured `feed_guardian._leg_ages()` on the `fyers_eq` source only. From about 15:20 on every trading day the `fyers_eq` age passes `STALE_MIN` (10 min) and the gate declares an outage while the leg is alive under the auction source. On 17-Sep that false outage ran the 28-second Yahoo sweep inside every `/api/mobile/home2` call (cc#2198); since 62e4d77 it would instead have shown "Live cash feed paused" with Yahoo or last-known values on Home for the last ten minutes of every session.

## The change

`CASH_CONTINUOUS_END = 15:15`. After it, `fyers_eq_outage()` returns `(False, age)` — never an outage in the auction window, the measured age still returned for logging. Before it, unchanged: a `fyers_eq` age over `STALE_MIN` inside the continuous session is still an outage. Weekends and after 15:30 unchanged (`(False, None)`).

Feed guardian needs no matching change: its tick already stops at 15:15 (`_is_cash_continuous`), so it never measured the auction window.

## Tests (`tests/test_fyers_eq_outage_gate.py`, 5 passed; the cc#2198 budget tests still pass, 8 in all)

- 14:50 with age 12 → outage. 15:10 with age 5 → none. 15:15:00 with age 11 → outage (still continuous). 15:22 with age 12 and 15:29 with age 18 → none (the 17-Sep case). Sunday and 15:45 → `(False, None)`.

## Live check (Fable — the sandbox cannot reach scorr.in)

Tomorrow 15:20–15:30 IST: `https://scorr.in/api/mobile/home2` → `live_fallback` is `null`, no "Yahoo fallback" warning in the app log, no "Live cash feed paused" line on Home.
