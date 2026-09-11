# cc#1859 — what "the ATM IV" actually is, before any fair value is built on it

Read-only. Nothing in this file has been written, wired, or shipped.

The card's own build order says: *"Reproduce NATIONALUM 370 = 13.99 to the paisa before going
further."* This file is that reproduction, and what it turned up.

---

## 1 — The price half of the gate passes exactly

Using the repo's own pricer (`deriv_metrics._bs_price`, `R_FREE = 0.07`, calendar days / 365,
q = 0 — the cc#1847 discovery), NATIONALUM spot 373.35, strike 370, 19 DTE:

| σ | CE fair value | vs market 12.80 |
|---|---|---|
| **0.3402** (Fable's hand-check) | **13.9855 → 13.99** | **−8.5%** |

**Matches to the paisa.** The formula, the rate, the day-count and the dividend assumption are all
confirmed. Nothing about Black-Scholes is in doubt.

## 2 — The σ half does not reproduce, and chasing it found something bigger

Fable's stated history for NATIONALUM: 236 sessions, median 0.3402, p25 0.2993, p75 0.3837,
min 0.1738, max 0.6045.

I could not reproduce those under any definition I tried — nearest-strike/nearest-expiry with both
legs, CE only, PE only, the CE/PE pair average, with and without `is_settlement`, and with DTE
floors of 1, 2, 3 and 5. The closest (both legs, nearest expiry, DTE ≥ 1) gives **237 sessions,
median 0.3390, p25 0.2968, p75 0.3843, min 0.1698, max 0.6298**.

**That difference is not worth stopping for.** 0.3390 vs 0.3402 is **4 paise** on the worked strike
— 13.9455 vs 13.9855 — and moves the displayed gap from −8.2% to −8.5%. Same sign, same tag.

**This is worth stopping for**, and it is what the search turned up:

| the same ATM strike, same expiry, same day | σ | fair value | gap vs market |
|---|---|---|---|
| solved from the **call** | 0.3185 | ₹13.26 | −3.5% |
| solved from the **put** | 0.3567 | ₹14.54 | −11.9% |

**₹1.27 apart — 9% of the fair value — on the card's own worked example.** Put-call parity says
those two numbers should be the same. So "the ATM IV" is not one number in this store, and the card
cannot price anything until it is told which one to use.

---

## 3 — How big, how common, and it is not a NATIONALUM quirk

ATM row per symbol per session (nearest strike to spot, nearest expiry, both legs present at that
strike, `iv > 0.005`, DTE ≥ 1), across the whole stored year:

| | |
|---|---|
| ATM pair-rows measured | **50,670** |
| symbols | **235** |
| **rows where the put's IV is above the call's** | **97.7%** |
| median gap (PE − CE) | **+3.51 vol points** |
| rows with a gap above 2 vol points | 89.6% |
| median of the per-symbol medians | +3.49 vol points |

Worst offenders, by per-symbol median gap:

| Symbol | sessions | median CE IV | median PE IV | gap |
|---|---|---|---|---|
| NIFTYNXT50 | 237 | 0.2436 | 0.4164 | **+8.74** |
| **NIFTY** | 197 | **0.0824** | **0.1496** | **+5.81** |
| SUNPHARMA | 237 | 0.1476 | 0.1880 | +4.06 |
| SBILIFE | 237 | 0.1793 | 0.2214 | +4.02 |
| INDIGO | 237 | 0.2282 | 0.2717 | +3.96 |
| NTPC | 237 | 0.1604 | 0.2016 | +3.96 |
| AXISBANK | 237 | 0.1714 | 0.2080 | +3.96 |

On NIFTY the ATM put implies **81% more volatility than the ATM call** at the same strike, same
expiry, same day.

### It grows with maturity

| DTE bucket | n | median gap | as a fraction of the mid IV | % where PE > CE |
|---|---|---|---|---|
| 1–3 | 2,396 | +0.0226 | 8.0% | 82.9% |
| 4–7 | 9,519 | +0.0252 | 10.3% | 96.3% |
| 8–14 | 12,350 | +0.0295 | 10.5% | 98.7% |
| 15–30 | 24,064 | +0.0398 | 14.1% | 99.0% |
| 31+ | 2,341 | +0.0490 | 20.2% | 99.4% |

### It is NOT dividends — tested and refuted

The obvious explanation is the `q = 0` assumption: ignore a dividend and the model's forward is too
high, which makes calls look cheap in IV terms and puts dear. That predicts a **bigger gap on
higher-yielding stocks**. It isn't there.

At a fixed DTE of 15–30, grouping symbols by `screener_raw.dividend_yield`:

| dividend yield band | symbols | avg yield | median per-symbol gap |
|---|---|---|---|
| < 0.25% | 56 | 0.08% | **+0.0393** |
| 0.25–0.75% | 59 | 0.47% | **+0.0396** |
| 0.75–1.5% | 42 | 1.06% | **+0.0396** |
| > 1.5% | 59 | 3.24% | **+0.0398** |

A **40× range in dividend yield** moves the gap by **0.5%**. The dividend hypothesis is dead.

Two candidates survive and I am not going to pick between them on this evidence:

1. **A forward or discount-rate error in the solver** — uniform across symbols, which fits, and
   grows with maturity, which fits. But the magnitude does not: reproducing a 3.96-point gap at
   T = 0.0597 through the rate alone needs `R_FREE` to be overstated by about **13 percentage
   points**, which is not credible.
2. **Real market structure.** Where shorting the cash is hard, the option-implied forward sits
   below the cash forward, and the ATM put reads dear against the ATM call for that reason. That is
   a known feature of Indian single-stock and index options, it is uniform, and it grows with
   maturity — all three fit.

The honest position: cause unresolved, effect measured precisely, and **the effect is what governs
this card either way.**

---

## 4 — What I propose, and why

**Use the average of the ATM call and put IVs — the "ATM mid vol" — as the fair-IV input.**

Three reasons, in order of strength:

1. **It cancels the forward-error component exactly to first order.** A call and a put at the same
   strike and expiry have **identical vega**. Whatever error sits in the synthetic forward shifts
   `C − P` and therefore moves the two solved IVs by *equal and opposite* amounts. Averaging removes
   it; picking either leg keeps all of it.
2. **It is the market convention** for quoting an ATM vol, for exactly that reason.
3. **It uses both observations** instead of throwing half the data away.

For NATIONALUM that gives σ = **0.3357**, fair value **₹13.84**, gap **−7.5%** against market 12.80
— versus −3.5% on the call leg alone and −11.9% on the put leg alone.

### What this does and does not affect

- **The IVP band is robust to it.** The band is a *rank* of today's IV within the symbol's own
  history. A bias that is present on every day of that history cancels out of a rank. This is the
  card's core insight and it survives intact.
- **The fair value in rupees is not robust to it.** That depends on the *level*, and the level moves
  ₹1.27 — 9% — depending on which leg is used. The rupee figure is the half the founder asked to
  add on 10-Sep, so it is the half that needs the ruling.

---

## 5 — Where this leaves the card

The build is not blocked; the *reference* is. I can and will proceed with build-order step 2 (the
bucket-skew coverage report), which counts observations per bucket and does not depend on which leg
defines the fair IV.

What must be ruled before any fair value reaches a screen:

- **which leg** — call, put, or the average (I recommend the average, with the vega argument above);
- **whether Fable's 0.3402 came from a definition I have not found**, which would change nothing
  material (4 paise) but should be reconciled so the stored numbers and the hand-check agree;
- **whether the put/call asymmetry itself gets its own card** — if it is a solver error it is
  contaminating every stored IV in `option_iv_daily`, and if it is real market structure it should
  be documented on the surface rather than averaged away silently.
