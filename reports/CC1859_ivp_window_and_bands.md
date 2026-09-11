# cc#1859 items 2, 3 and 4 — the lookback window, the bands, and the hard stop

Read-only. No code, no wiring, nothing deployed. This is the evidence the card requires **before**
the tag ships.

Unblocked by Fable's RULED 3 (room log 6301): the ATM fair IV is the **average of the ATM call and
put**. Everything below uses that definition — ATM = nearest strike to spot, nearest expiry,
`iv > 0.005`, DTE ≥ 1, the two legs averaged.

Clock: `server_now`, 11-Sep-2026 08:1x IST. The container clock is ignored, permanently.

---

## The unlock condition, re-measured independently

| | mine | the card's |
|---|---|---|
| ATM rows | 51,033 | — |
| symbols | 235 | 235 |
| symbols with **60+** sessions | **228** | 228 |
| symbols with 120+ sessions | 211 | 213 |
| symbols under 60 (no tag, ever) | **7** | 7 |
| date range | 2025-09-01 → 2026-09-10 | to 09-Sep |

228 of 235 confirmed at the floor, from my own query. The 120+ figure differs by two because a
further trading day has landed since the card was written.

---

## Item 4 — THE HARD STOP. It fires on one test and passes on the other, and the difference is the whole point.

The card says: *"if any single band exceeds roughly 50% of rows, STOP and report — the same failure
that blocked cc#1847 would simply have moved to a new baseline."*

### Test A — today's cross-section, all 228 symbols. **FAILS every band scheme.**

| window | bands | CHEAP | FAIR | EXPENSIVE |
|---|---|---|---|---|
| all history (~237) | <25 / 25–75 / >75 | **51.3%** | 41.7% | 7.0% |
| trailing 120 | <25 / 25–75 / >75 | **85.5%** | 7.9% | 6.6% |
| all history | terciles 33/67 | **66.7%** | 25.9% | 7.5% |
| all history | <15 / 15–85 / >85 | 26.8% | **66.7%** | 6.6% |
| trailing 120 | <15 / 15–85 / >85 | **69.3%** | 24.1% | 6.6% |

Today's median IVP is **24.7** on full history and **10.0** on a trailing 120. The market's implied
volatility is broadly compressed right now.

### Test B — the replay the card actually asked for. 24 symbols, every day, 4,208 tagged rows. **PASSES everywhere.**

| window | CHEAP <25 | FAIR 25–75 | EXPENSIVE >75 | largest band |
|---|---|---|---|---|
| expanding (all prior days) | 14.4% | 44.9% | 40.7% | 44.9% ✅ |
| **trailing 120** | **22.9%** | **41.6%** | **35.5%** | **41.6% ✅** |
| trailing 60 | 32.5% | 40.5% | 27.0% | 40.5% ✅ |

Terciles also pass on both trailing windows (120: 28.7 / 28.7 / 42.6 · 60: 39.5 / 25.6 / 34.9).

### Why A failing is not a reason to stop, and B passing is the answer

These two tests measure different things and only one of them is the card's:

- **Test A asks "how does the market look today".** It looks cheap, because it *is* cheap — IV is
  compressed across the board. A tag that can say "almost everything is cheap right now" **when
  almost everything is cheap** is working, not broken.
- **Test B asks "does this tag say the same thing every day regardless of the data".** That was
  the RV20 defect: 91% of clean strikes read EXPENSIVE **by construction, every day, forever**,
  because option premia carry a volatility risk premium over realised vol. A percentile cannot do
  that — it is a rank against the symbol's own history, so over that history it spreads out. The
  replay confirms it: 22.9 / 41.6 / 35.5, no band near 50%.

**So the hard stop does not fire.** If it had fired on the replay I would have stopped, and the
card would be dead. It fired only on a one-day snapshot that the card does not ask for, and that
snapshot is a market reading rather than a defect. I ran both rather than only the one that
passes.

One honest caveat on Test B: the replay skews slightly EXPENSIVE (35.5% against an ideal 25%) on a
trailing 120. That is the same fact as Test A seen from the other end — IV was *higher* through
most of the stored year and has fallen recently, so historical days rank high against their own
trailing window. It is a property of this particular year, not of the method, and it is within the
stop threshold by a wide margin.

---

## Item 2 — the lookback window. **Proposed: trailing 120 sessions.**

The card asks for one window with evidence and states the trade-off: longer is stabler, shorter
reflects a regime change faster.

**The evidence for 120:**

1. **It gives the most balanced replay of the three** — 22.9 / 41.6 / 35.5 against an ideal
   25 / 50 / 25. The expanding window over-weights EXPENSIVE (40.7%) because early days have few
   priors; trailing 60 over-weights CHEAP (32.5%) because it is noisy.
2. **211 of 228 eligible symbols already have 120+ sessions**, so it costs almost nothing in
   coverage against the 60 floor.
3. **The window choice is not a detail — it moves the number a long way.** Between full history and
   trailing 120, today's IVP moves by a mean of **12.2 points**, a maximum of **38.9**, and **56 of
   228 symbols move 20 points or more**. The card's own NATIONALUM note saw this (IVP 43 on full
   history, 24 on ~120). **Whichever window is chosen must be STATED ON SCREEN**, because the same
   option is "typical" or "cheap" depending on it.

**Minimum stays 60 sessions.** Seven symbols are below it today and must show no tag and no fair
value, with the D-button saying why in plain words — never a weak percentile.

---

## Item 3 — the bands. **Proposed: CHEAP < 25 · FAIR 25–75 · EXPENSIVE > 75.**

Proposed **from this data**, not copied from the RV20 thresholds, as the card requires:

- On the recommended trailing-120 replay it gives **22.9 / 41.6 / 35.5** — the closest of every
  combination tested to a sane spread, and no band within 8 points of the stop threshold.
- Tightening to <15 / >85 pushes FAIR to 62% and makes the tag say less.
- Terciles (33/67) push the high band to 42.6% and leave only 28.7% reading FAIR, which reads as a
  market permanently at an extreme.

These are quartile cuts on a rank, which is also the plainest thing to explain on the sheet: *"this
strike is trading richer than 81% of the days we have data for"* is literally what the number is.

---

## What is still blocked, and it is not this

Item 5 (per-strike fair value across ATM±5) needs the **second** anchor ruling — what a bucket skew
is measured against. Measured within its own leg the 1%-OTM NIFTY call is +0.0194; measured against
a blended ATM the same strike reads −0.0241. The sign flips on the anchor alone, and the ATM bucket
itself reads exactly 0.0000 within-leg (as it must) but −0.0266 / +0.0301 against a blend. Level
from the blend, shape from the leg — see `reports/CC1859_bucket_coverage.md` (sha `76e8835`).

The ATM half — fair IV, fair value, IVP, band — is now fully specified and waits only on Fable
ruling this window and these bands.
