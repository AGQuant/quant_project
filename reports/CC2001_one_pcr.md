# cc#2001 — ONE PCR: a finding that changes item 1 and item 2, items 3-5 completed

**Headline: R2 and R3's literal instructions do not match the live code. Stopped and reporting
per the card's own gate philosophy, rather than executing a rename/relabel that would fix nothing
real (item 1) or ship a new wrong label (item 2). Items 3, 4, 5 completed as specified.**

## Item 1 — grep first, as instructed. It changed the answer.

R2 says: *"pcr_intraday.pcr (ATM±5 band) — NO column rename... every API payload that emits it
emits the key as pcr_atm5, never pcr."* This describes a column named `pcr` that needs its
payload key corrected to `pcr_atm5`.

**Checked against the live schema before touching anything: `pcr_intraday` has no column called
`pcr`.** `pcr_intraday.py`'s own `CREATE TABLE IF NOT EXISTS` (line 54-61) and its own module
docstring (line 9-10: `"pcr_atm5 : ATM +/- 5 strikes (focused signal)"` / `"pcr_total : full
stored band (standard market PCR)"`) both show the two real columns have always been named
`pcr_atm5` and `pcr_total`. Traced every one of the 8 files in the repo that mention `pcr_atm5`
by name:

| File | What it does |
|---|---|
| `pcr_intraday.py` | The writer. `CREATE TABLE` names the columns `pcr_atm5`/`pcr_total`; every INSERT/UPDATE uses those names throughout. |
| `pcr_endpoints.py` → `get_pcr_intraday()` | `GET /api/pcr/intraday`. `SELECT ts, spot, atm_strike, pcr_atm5, ..., pcr_total, ...` then `dict(zip(cols, r))` — payload keys are the SQL column names **verbatim**. Already correct, nothing to rename. |
| `pcr_endpoints.py` → `get_pcr_intraday_hourly()` | `GET /api/pcr/intraday_hourly`. Selects `pcr_total` **only** (never touches `pcr_atm5`), payload key is the generic `pcr` — not ambiguous in practice since this endpoint is always-and-only the total measure. |
| `protocol_one.py:250` | `COUNT(DISTINCT pcr_atm5)` — an internal freshness/variance check, correctly named, not a payload. |
| `digest_v3.py:1200-1204` | Comment explicitly reasons through the choice: *"the intraday column that matches [pcr_daily.pcr] is pcr_total, NOT pcr_atm5"* — deliberately uses `pcr_total`, correctly names `pcr_atm5` as the one it is NOT using. |
| `mobile/home.html:5455-5458` | Pre-existing comment (cc#1832) already states the Home hero reads `pcr_intraday.pcr_total`, **NOT** `pcr_atm5`, citing the real 1.28-vs-1.80 gap — this exact disambiguation was already done for the Home hero specifically, before this card. |
| `scheduler.py:626-630` | Historical comment about a 2026-vintage bug (cc#121) in an old, since-rewritten INSERT. Not current code. |
| `scorr_v13.html:340-341` | The platform's own metrics dictionary — already lists `pcr_atm5` and `pcr_total` as two separate, correctly-labeled rows with distinct descriptions. |

**Conclusion: no reader anywhere in the live codebase emits the ATM±5 value under a bare `pcr`
key.** R2's premise traces to `reports/CC1986_CLASH_REPORT.md` line 186-187, which says the ATM
measure "is stored in a column called `pcr`" — directly **contradicted by its own table two lines
above it** (line 176: `pcr_intraday.pcr_atm5`), the same class of table-cell/prose mismatch this
session caught in its own earlier work (cc#1996). **No rename performed — there is nothing to
rename.** Verify line 1 ("grep -rn '\"pcr\"' ... no payload key named pcr sources
pcr_intraday.pcr") is satisfied trivially and for a different reason than expected: no such
payload key exists to begin with.

## Item 2 — HELD. R3's two labels would be a new, real mislabel if shipped.

R3 says the tape PCR (`pcr_mood`, "banded") and the card PCR (`oi_structure`, "chain ratio")
should be labeled **"ATM band" / "full chain"**. Read both composers before writing any label:

- `pcr_mood.latest_pcr()` (docstring, line 188-192): *"Never reads pcr_intraday.pcr_total or
  pcr_daily for this value any more"* — as of cc#1846 (08-Sep), it computes PCR **live, directly
  from `option_chain`**, summing PE/CE OI across the **whole near-expiry chain** (`live_pcr()`,
  line 155-185: *"whole chain, not a window"*, explicit in the docstring). "Banded" in R3's own
  wording refers to `pcr_mood`'s **mood bands** (EXTREME FEAR…EXTREME GREED) — a different sense
  of "band" than ATM±5 strikes. Confirmed by reading the CLASH_REPORT's own Section A (line
  190-192), which never calls pcr_mood's reading "ATM band" either — it says the two Home PCRs
  are "different composers," full stop.
- `oi_structure()`'s PCR (`oi_structure.py:176`, `structure_from_rows()`): `tot_pe/tot_ce` summed
  over the same kind of whole-chain rows. Also whole-chain, also independent of `pcr_intraday`.

**Both Home PCRs are whole-chain measures, from two independent composers — the same relationship
R1 already describes for `oi_structure_daily.pcr` vs canonical `pcr_total` ("duplicate writer of
the same measure"), not an ATM-vs-chain relationship at all.** Labeling them "ATM band" / "full
chain" would tell a user one of Home's two PCRs is a narrow-strike number when neither is — a new,
shipped mislabel, on the exact screen this whole card exists to protect. **Not applying R3's
labels.** Holding item 2 rather than inventing different wording on my own authority (the card
specifies exact text; I have evidence that text is wrong, not evidence of what the right two words
are) — Home's two PCRs stay exactly as they render today (unlabeled, as before this card),
pending a corrected ruling.

## Item 3 — DONE. API_REFERENCE.md updated (hand-written sections only, generator untouched)

Added a canonical-PCR callout under the `### PCR` section (mirrors the existing "three TC systems"
warning's placement/tone) stating R1 plainly: `pcr_intraday.pcr_total` canonical for intraday,
`pcr_daily.pcr` canonical for daily-close, `oi_structure_daily.pcr` and `pcr_mood`'s live PCR
named as non-canonical duplicate writers of the same measure, and `pcr_atm5` named as the
genuinely different (not-a-mislabel) narrower measure — folding in the item-1 finding so a future
reader does not re-open R2's mistaken premise. Inline annotations added at `/api/daily/pcr`,
`/api/pcr/intraday`, `/api/pcr/mood`, `/api/pcr/intraday_hourly`, and `/api/oi/structure`.

`python3 tools/gen_api_reference.py --check` confirms the generated route-inventory block (which
I did not touch) is still current — only hand-written prose sections were edited, exactly where
the generator's own docstring says hand edits belong.

## Item 4 — DONE, read-only. Card's evidence file did most of this already; verified, not redone blind.

The card's own `source_of_truth` (cc_task_logs 6255/6248, built from `reports/CC1986_CLASH_REPORT.md`)
already ran this comparison with real numeric deltas. Re-read and verified rather than re-run from
scratch where the report's own work was already rigorous:

| Family | Files | Verdict | Delta on real rows |
|---|---|---|---|
| **RSI** | `invest_check_v2.py:163`, `tc_v4_endpoints.py:90`, `v12_backtest.py:447` | **DIFFERENT.** Two Wilder (seeded SMA + smoothing across the full series), one Cutler (`v12_backtest.py`: last-14-changes-only, no seed, no smoothing) — a genuinely different metric, not a rounding variant. | Report's 120-bar measured comparison: uptrend 69.77 vs 71.48 (gap 1.71); choppy 58.39 vs 47.61 (gap **10.78**); uptrend-then-20-bar-drop 18.71 vs **0.00** (gap **18.71**) — the backtester reads "maximally oversold" where every live scorer reads "oversold but alive." Precedent cited: cc#854, a 5-point RSI gap silently killed a basket; this one reaches 18. **Not fixed here** (read-only, per the card) — needs its own card, and the report is right that the fix is a decision (adopt Wilder in the backtester, or label the divergence), not a patch. |
| **Pivots** | `invest_check_v2.py:257`, `trade_check_v34_endpoints.py`, `tc_v4_endpoints.py`, `v8_intra_backtest.py`, `buy_reversal_simulator.py:154`, `v8_paper.py:219` | **HEALTHY.** Of the six files a grep matched, three are false positives that never derive a level at all (`_pivot_zone` classifies, `_pivot_bar` renders, `_pivot_room_ok` is a predicate). The three real computers (`v8_paper.py`, `invest_check_v2.py`, `buy_reversal_simulator.py`) use the **identical classic floor-pivot formula** — `pp=(h+l+c)/3`, `r1=2pp−l`, `s1=2pp−h`, etc. | Zero — same formula. Window length differs (5-session vs configurable) but each declares its own window explicitly (`v8_paper` persists `window_start`/`window_end`/`base_days`; the others name it in the docstring/function name) — a declared parameter, not a clash. |
| **PCR** | `pcr_endpoints.py` (the `pcr_intraday.py` writer), `pcr_mood.py` | **SAME formula, independent execution.** Both compute `SUM(PE oi) / SUM(CE oi)` over the full near-expiry `option_chain` rows — identical shape. `pcr_mood.py`'s own comment (cc#1846) names the reason for the duplication: the stored writer (`compute_pcr_intraday`'s self-heal loop) has a **confirmed write-race** that can commit a corrupt total mid-write (08-Sep NIFTY 15:30 bar: 0.5047 vs the true 1.2684) — so `pcr_mood` stopped reading the stored value and computes its own, live, off `option_chain` directly, rather than trust a table with a known corruption bug. | CLASH_REPORT's own table: `oi_structure_daily.pcr` 1.303 vs `pcr_intraday.pcr_total` 1.287 at the same 10-Sep tick — agree to within ~1.2%, consistent with "same formula, independent writers, small real divergence" rather than a formula bug. |
| **fall_from_day_high** | `deriv_metrics.py:528`, `v8_signal_writer.py:929` | **SAME formula.** `(cmp_px − hi) / hi * 100` in `deriv_metrics.py` vs `(live − today_high) / today_high * 100` in `v8_signal_writer.py` — identical shape, both `hi`/`today_high` are today's session high from intraday 5-min bars (`deriv_metrics._bars(today)` vs the writer's own bar fetch) — two independent readers of the same underlying intraday-bar data, not a formula divergence. | **Same-moment numeric comparison not meaningfully possible right now, stated plainly rather than faked:** `intraday_prices` shows **zero bars today** for all 5 sampled symbols (RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK — latest bar for every one is still yesterday's 15:35 close), matching the `bg_signal_writer` "no_intraday_bars" alert already flagged on cc#1199 this tick. Both formulas would read off the same stale (yesterday's) or empty input right now — a same-moment delta today would show 0.00 by construction (both reading the identical stale row), which would look like agreement but would actually be measuring nothing. Will re-run this specific comparison once today's feed recovers and both sides have a genuine live tick to disagree or agree on. |

**Gate note:** the card's gate ("Step 4 finding a formula mismatch = STOP and post finding; do not
fix formulas in this card") is satisfied for RSI — a real mismatch, reported, not touched. Pivots,
PCR and fall_from_day_high are not formula mismatches (verdicts: healthy / same-formula-different-writer
/ same-formula-different-writer), so nothing to stop on for those three.

## Item 5 — accepted, per the card

The 601 routes with no resolved read-set stay out of scope for this card, per its own scope item
5 and R12's prior acceptance (log 6255) — not walked here.

## R4 (positions family) — not actionable inside this card's 5 scope items

R4 rules `/api/v8/positions` vs `/api/clients/positions`/`/api/test/positions` REPORT ONLY and
says "Fable will rule after reading your read-set" — but none of this card's five numbered scope
items asks for a positions read-set to be built. Noting this rather than either inventing scope
or silently dropping a ruling that names this card: R4 appears to carry over from the broader
cc#1986 findings this card extends, not from anything cc#2001 itself asks CC to produce.

## Verify

- `grep -rn "\"pcr\""` across `routes/`, `mobile/`, `*.html`: no payload key named `pcr` sources
  `pcr_intraday.pcr_atm5` — true, and true because no such column exists to source from, detailed
  in item 1.
- Founder screenshot of Home showing both labels: **not applicable** — item 2 held, no labels
  shipped to verify.
- Formula log rows: table above, RSI/pivots/PCR/fall_from_day_high all present with numeric
  deltas where real data supports one right now.

Nothing under `worker/**`. No PCR writer touched — `option_chain` feed untouched. Only file
changed: `API_REFERENCE.md` (hand-written sections). Card **NOT** set done — Fable verifies, and
items 1/2 specifically need a re-ruling before anything further ships, not just a verification
pass.
