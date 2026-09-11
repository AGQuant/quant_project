# cc#1994 — the separating test: parity-implied forward vs fair forward

Read-only throughout. `option_iv_daily` not touched. No wiring — cc#1859's build proceeds on
the CE/PE average regardless of this card's outcome, per `do_not_touch`.

## The test that actually separates, and the one that does not — corrected mid-analysis

The card's item 2 asks to re-solve both legs against the parity-implied forward F\* and check
whether the call/put IV gap collapses. **It always will, and that is not evidence.** F\* is
*defined* as `K + (C − P)·exp(rT)` — the value that makes put-call parity hold exactly for this
C/P pair — so pricing both legs against `S_eff = F\*·exp(−rT)` is circular: it forces agreement
by construction, regardless of whether the true cause is (A) a solver error or (B) real market
structure. Measured on 140 ATM pairs (NIFTY + RELIANCE, full stored year): the gap collapses from
a median **+0.0396** (put richer) to **exactly 0.0000** on all 140 rows, 0% still positive. That
number is real but it is a tautology, not a finding — flagged here rather than reported as if it
settled anything, which is what a first pass at this script did before the circularity was
caught.

**The test that actually separates is comparing F\* to the textbook cost-of-carry forward**,
`spot·exp(rT)` — not to raw spot (a first draft of this compared F\* to spot directly, which
mixes in the risk-free carry term and understates the real question; corrected before writing
this up).

## F\* vs the fair forward — the real evidence

Full stored year (2025-09-01 → 2026-09-10), five symbols, ATM CE/PE pairs at the nearest expiry:

| Symbol | n | median F\*/fair-forward − 1 | stddev | avg DTE |
|---|---|---|---|---|
| RELIANCE | 237 | **−0.294%** | 0.143% | 15.8 |
| BANKNIFTY | 237 | **−0.282%** | 0.162% | 15.8 |
| PNB | 237 | **−0.259%** | 0.148% | 15.8 |
| NATIONALUM | 237 | **−0.285%** | 0.154% | 15.8 |
| NIFTY | 197 | **−0.242%** | 0.155% | 4.0 |

**Uniform, tight, and — critically — NOT proportional to time.** NIFTY's average tenor (4 days)
is roughly a quarter of the other four (15.8 days), yet its median discount (−0.242%) is the
*same order* as theirs (−0.259% to −0.294%), not a quarter the size. A continuously-compounding
rate/forward error in the solver (hypothesis A — `R_FREE` or the day-count baked into
`deriv_metrics._bs_price` being wrong) would produce a discount that grows roughly linearly with
T; this one does not. That argues against (A) and toward (B) — a market-structure effect that is
closer to a fixed per-trade cost than a per-day carry rate, on this sample.

**Stated honestly: this sample cannot fully close the question.** The DTE range tested is narrow
(1–33 days, capped by weekly/monthly expiry structure) — a genuinely linear-in-T effect that is
small per day can still look roughly flat over a 4-day-to-16-day window. Separating "flat
regardless of tenor" from "linear in T but small" cleanly would need far-dated options (60+ days)
against near-dated ones, which this dataset does not offer in volume.

## Sanity check: same test against raw spot (item 1's literal instruction)

For completeness, the comparison the card's item 1 literally asks for (F\* vs spot, not vs the
fair forward):

| Symbol | median F\*/spot − 1 |
|---|---|
| BANKNIFTY | +0.002% |
| RELIANCE | +0.001% |
| NATIONALUM | +0.020% |
| PNB | +0.023% |
| NIFTY | −0.161% |

Near-zero for four of five. **This comparison is the wrong baseline** — it does not net out the
risk-free carry a fair forward is supposed to include, so near-zero here does not mean "no
anomaly"; it is an artifact of comparing a forward-dated quantity to a spot-dated one without
removing the time value of money. The fair-forward comparison above is the one that answers the
card's actual question.

## Item 4 — which candidate the evidence picks

**Leans toward (B), real market structure, on the DTE-non-scaling pattern above — but does not
close the question outright**, for the stated reason (narrow DTE range in this dataset). The
uniformity across a bond-like name (NATIONALUM), a bank (PNB), a large-cap (RELIANCE) and two
indices, all landing in the same −0.24% to −0.29% band despite very different average tenors, is
the strongest single piece of evidence: an implementation bug in one shared function would be
expected to scale with the one variable that function treats differently per symbol (T), and it
does not.

**DECISION NEEDED (posted to the Fable Room, task 1199):** whether `option_iv_daily` gets
re-solved historically against the forward is a build decision this card does not make; what this
card adds is that the level bias is real, roughly uniform, and does not look rate-proportional —
information for that decision, not the decision itself. The IVP band is confirmed unaffected
either way (a bias present on every day of a symbol's own history cancels out of a rank), so
nothing here changes cc#1859's build, which proceeds on the CE/PE average.
