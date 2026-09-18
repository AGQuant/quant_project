# cc#2211 — /m/v8 chart sheet: unrealised history capped at the last 100 bars; explanatory notes removed

Spec: session_log 48587 (V8_APP_UNREAL_CHART_100_BARS_AND_NOTE_CLEANUP_V1, founder ask 18-Sep 06:25 IST).
One file: `mobile/v8.html`. Client-side only — the series still come from `/api/v8/unrealised_daily/series`
and `/api/v8/daylog/series` (ONE SOURCE rule); nothing server-side changed.

## What changed

| item | where | change |
|---|---|---|
| 1 | `dlChartSheetHtml` window pills | unrealised sheet: `100 BARS` pill (key `100b`) in place of `SINCE START`; 1W / 1M / 3M unchanged. Realised sheet keeps `SINCE START` / 1W / 1M / 3M. |
| 1 | `dlChartOpen` | unrealised opens on `100b` (was `1w`, cc#2097 item 4); realised / net still open on `all`. |
| 1–2 | `dlWindowSlice` | `win === '100b'` → `pts.slice(-100)`, then the SAME rebase every other window gets (subtract the first in-window point's `net_cum` / `gross_cum`). Fewer than 100 points → all of them, nothing padded. |
| 3 | sub-line | unchanged code path: a non-`all` window already prints `first – last · N trading days · marked to market by date`; the `Since …` wording is only produced for `all`, which the unrealised sheet no longer offers. |
| 4 | `dlChartSheetHtml` | `brokNote` (the span beside Net/Gross) and `liveNote` (the "○ live = …" line) removed for BOTH kinds. `dlChartSvg` untouched — the hollow live end-point stays on the chart. |
| 5 | unrealised foot | `foot = ''` — no `cagr_note` sentence. Realised foot (Return / Annualised CAGR + its note on `all`; "Change over this window" on 1W/1M/3M) byte-identical. |
| 6 | `kwInfoLines` / infoFoot | the `note` field deleted for unrealised, realised and net; the note `<div>` no longer rendered. Titles and the three rows per key are verbatim. |

`node --check` on both inline script blocks: OK. Grep after the edit: `brokNote` 0, `liveNote` 0, `info.note` 0, `note:` 0.

## Harness — Playwright 375×812, the real page with mocked APIs, sheets opened by clicking the KPI well's chart button — 21 checks, PASS

Mocked book: unrealised ₹12,345.5, gross ₹7,000, brokerage ₹2,000 (4 closed trades). Unrealised series 150 points (then 60), realised 150 points with return 12.34 % / CAGR 30.1 %.

| # | check | result |
|---|---|---|
| U1–U2 | unrealised pills `100 BARS · 1W · 1M · 3M`, 100 BARS lit by default | ok |
| U3 | sub-line `1 May – 17 Sep · 100 trading days · marked to market by date` = points 51..150 of 150 | ok |
| U4–U6 | no `Since` wording, no SINCE START, no CAGR foot, none of the eleven removed sentences, no 9.5 px note element left | ok |
| U7 | info title + exactly three rows (`P&L on open positions ₹12,346` · `Brokerage charged so far ₹0` · `Net ₹12,346`) | ok |
| U8 | one SVG with the live end-point circle | ok |
| U9–U10 | 1W → 5 points (11–17 Sep), back to 100 BARS → 100 | ok |
| U11 | Long slice: 100 BARS default, no info block, no notes | ok |
| U12 | 60-point series → `26 Jun – 17 Sep · 60 trading days`, 100 BARS still lit, nothing padded | ok |
| R1–R2 | realised pills `SINCE START · 1W · 1M · 3M`, SINCE START lit, `Since <date> · 150 trading days · cumulative by exit date` | ok |
| R3–R4 | Return / Annualised (CAGR) foot + its cagr_note kept; exactly one 9.5 px element left (that foot) | ok |
| R5–R6 | shared notes gone; realised info title + three rows (`Brokerage (4 closed trades × ₹500) −₹2,000`), no note | ok |
| R7 | 1W → `Change over this window`, no CAGR | ok |
| N1 | net info title + three rows, no note; SINCE START pills | ok |
| E1 | no page errors while driving the sheets | ok |

Screenshots looked at: `cc2211_375_unreal.png` (100 BARS lit, chart, three rows, no sentences), `cc2211_375_realised_1w.png`.

## Live check (the sandbox cannot reach scorr.in)

/m/v8 → Unrealised well → chart button: the sheet shows `100 BARS` lit with 1W / 1M / 3M beside it, the sub-line
names the first and last of the last 100 sessions, and there is no sentence under the pills, under the chart or
under the HOW THIS IS WORKED OUT rows. Realised and Net Total sheets: unchanged apart from the same two lines gone.
