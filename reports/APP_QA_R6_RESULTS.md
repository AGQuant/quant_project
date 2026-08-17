# APP_QA_R6 — RESULTS · SPRINT 3 · cc#1085
**Executed by:** CC · 17-Aug-2026 · **Report:** `reports/APP_QA_R6.md` · **Ref:** `design_refs/scorr_gvm_2pager_R1.html`

## Summary

**Nine of ten pushes landed. P8 failed its verify and stopped the 2-Pager chain, as §C P8 instructs.**

The sheet is correct, reproduces the ref number for number, and lands on exactly two A4 pages —
**for symbols in segments of fourteen peers or fewer.** BHARATSE sits in a thirteen-name segment,
one row under the cliff. 1,026 of the 1,791 scored symbols do not, and for those the route
currently renders three sheets. Detail in §P8. Nothing was shrunk to force a fit.

P9 shipped anyway, per §G ("P9 is independent of the 2-Pager chain").

| Push | SHA | Files | Verify | Notes |
|---|---|---|---|---|
| P1 | `564ef83` | `gvm_twopager.py`, `main.py` | 200 / 404 / 503 | Own router, one `include_router()` line |
| P2 | `8879d24` | `gvm_twopager.py`, `test_twopager_css_parity.py` | CSS byte-identical, 3215 B both sides | Parity is a test, not a claim |
| P3 | `cc726cd` | `gvm_twopager.py`, `test_twopager_css_parity.py` | Page 1 matches the live card | Two documented departures from the ref |
| P4 | `6411433` | `gvm_twopager.py`, `test_twopager_page2_clean.py` | Grep: zero word-boundary matches | Grep-as-worded cannot pass — see §P4 |
| P5 | `24a5a49` | `gvm_twopager.py`, `test_twopager_empty_states.py` | Both empty states rendered | Case 2 forced; 0 of 1,791 hit it live |
| P6 | `24a5a49` | `gvm_twopager.py` | `display:flex` screen / `none` print | Print CSS kept OUT of `REF_CSS` |
| P7 | `24a5a49` | `scorr_cio_dashboard.html` | Button reads `2 Pager`, opens new tab | `window.print()` gone from the file |
| P8 | — | — | **FAILED — 3 pages on 57% of symbols** | **Chain stopped. No fix applied.** |
| P9 | `9b51ae9` | `scheduler_master.py`, `test_scheduler_multislot.py` | 6 multi-slot rows parse fully | Card's premise corrected — see §P9 |
| P10 | this file | `reports/APP_QA_R6_RESULTS.md` | — | — |

---

## P3 — page 1, and two places the ref and the data disagree

Page 1 binds from `gvm_company_report()` called **in-process**. No scoring is re-implemented and
there is no HTTP hop to ourselves, so the sheet and the `/cio2` card are one computation consumed
twice.

**Verify (the card's own line):** BHARATSE renders `7.28 / 7.14 / 6.25 / 8.44`, CMP `₹248.05`,
mcap `₹1,558 Cr`, ladder rank `2 of 13`. All four match.

### The ladder market-cap trap is bigger than §E says

§E states nothing user-facing is wrong today because the live page reads `screener_raw`. **That is
true of the header only.** `ladder[].market_cap` in the payload is built from `gvm_scores`
(`gvm_page_extras.py` step 9), so the live GVM card today prints BHARATSE at **₹1,157 Cr inside its
own segment ladder while its header reads ₹1,558 Cr** — the same company, the same page, two
numbers. Twelve of the thirteen rows differ; the spread runs **+49.5%** (SSWL 4,921 vs 3,292) to
**−6.2%** (ASAL 792 vs 844).

`screener_raw` reproduces the ref's ladder **exactly on both mcap and CMP for all 13 names**, which
is how we know the ref is screener-based and the payload's column is the stale one. The 2-Pager
reads `screener_raw` and never the payload column. **Fixing the GVM page itself is out of scope**
(§D: payload shape is read-only) and wants its own card.

### The sheet's CMP and the live card's CMP now disagree

P3 names CMP 248.05. The card no longer shows it: the payload's resolved price is **245.70**,
labelled `Last Tick`, dated 2026-08-14, `is_live` false. `screener_raw.price` says **248.05**, which
is also the 14-Aug daily close in the payload's own volume bars, and is the ref's figure.

One price source is used for the whole sheet — the ladder has no other source for the twelve peers,
and a sheet whose header disagrees with its own row is the defect just fixed on market cap. So the
sheet reproduces the ref and currently differs from the live card by ₹2.35. cc#343's stated rule is
that non-feed symbols show the latest **completed close**, never a partial row; 245.70 does not look
like the completed close. **Open for a ruling.**

### "Median rating" is an average

P3 says median `gvm_score` across the segment family. The ref prints `7.02 / 6.56 / 6.47 / 6.40 /
6.37` — those are the simple **averages**, and they reproduce exactly. The medians are
`7.19 / 6.82 / 6.95 / 6.41 / 6.43` and they **reorder the table** (Drivetrain would jump above Body
& Stampings). The average is computed, so the ref reproduces, and the column header now reads
**"Avg rating"** rather than saying median over a mean.

### Two smaller ref corrections, already handled

- The ref's note says P/E "against its own **10-year** average of 18.9x". The source field is the
  own **5-year** average (`extra_marker` label `own 5y avg`, value 18.88). The label is bound from
  the data.
- The ref's momentum caption says "2nd best in segment on relative strength". BHARATSE is **4th of
  13** on `m_score` (10.00 MUNJALAU, 9.06 MINDACORP, 8.91 SSWL, then 8.44). Pillar captions are now
  computed ranks, so this corrects itself.

The ref's `<div class="note">` lines are hand-written commentary for BHARATSE. Reproducing that
sentence shape for an arbitrary symbol would invent readings the data does not carry, so the notes
are rebuilt from the numbers alone.

---

## P4 — page 2, and a grep that cannot pass as worded

`build_page2()` reads `input_raw.overview` and `screener_raw` and nothing else. It never touches
`rep["scores"]`, `rep["verdict"]` or any benchmark rating — asserted by
`test_twopager_page2_clean.py`.

**The verify grep needs word boundaries.** P4 words it as a case-insensitive grep for `gvm`,
`rating`, `score`. As bare substrings **that check fails on the ref itself**: `rating` sits inside
`ope`**`rating`** and the ref's own page 2 prints "Operating margin" twice — a field P4 explicitly
requires. So `\bword\b` is what runs, with the substring count printed beside it.

Grep output, verbatim, on the **rendered** page-2 div (3,769 chars):

```
grep -io '\bgvm\b'     -> 0 matches
grep -io '\brating\b'  -> 0 matches
     (substring form matches 3, all inside: Operating, operating)
grep -io '\bscore\b'   -> 0 matches
grep -io '\bpillar\b'  -> 0 matches
RESULT: ZERO word-boundary matches for gvm / rating / score / pillar
```

**Nine of 1,791 companies describe themselves with these words** — ICRA, CRISIL and CARERATING are
credit rating agencies; TFCILTD, LICHSGFIN, RECLTD, UGROCAP, TATACAP and ZEEL carry `rating` or
`score` in their business text. Redacting that would make the page wrong about what the company
does, so the ban is enforced on what the module emits, not on the English language. **Zero of 1,791
overviews contain "GVM"**, so that half of the rule is absolute.

**Two ref sections deliberately not reproduced:** "The business model in one line" and "The one
thing to watch". Both are hand-written readings of BHARATSE with no field behind them. Moat and risk
instead split out of the overview's own `Moat:` / `Key risk:` markers, and the block drops when the
text carries neither.

Every ref figure reproduces: revenue ₹2,102 Cr · PAT ₹47.3 Cr · OPM 5.03% · net margin 2.25% ·
ROCE 20.19% · ROE 20.43% · D/E 0.48x · interest cover 7.53x · P/B 6.78x · sales CAGR 22.90/28.93 ·
profit CAGR 26.20/56.67 · promoter 74.66 / FII 0.15 / DII 0.22 / public 24.97 · P/E 32.96x against
own 18.88x · Q1FY27 ₹577.8 Cr vs ₹427.1 Cr = +35.3%, PAT +43.9%, margin −20 bps.

---

## P5 — empty states

Built as an executable test rather than a pasted render: a paste proves the behaviour on the day it
was pasted, and the failure being guarded against is somebody later filling an empty case with a
plausible template sentence. The test also **ratchets** — `build_page2` is allowed exactly two
originated sentences and any third fails.

**Case 1 — AHLWEST, a real symbol whose `input_raw.overview` is genuinely empty**, with its real
`screener_raw` row including three genuinely NULL columns (Return on equity, Debt to equity, Price
to book). Rendered 3,002 chars:

- prints "Business profile not available for this company."
- moat/risk block dropped entirely
- Financial profile and Latest quarter **kept** — the row exists
- the three NULL columns print an em-dash, **not** a fabricated `0.00`

Also covered: `overview` NULL rather than empty, and a 99-character overview (one char under the
threshold) — both take the not-available path, and the 99-char stub is never printed as a profile.

**Case 2 — no `screener_raw` row.** Rendered 548 chars: Financial profile and Latest quarter omitted
entirely, no grid of dashes, masthead and foot intact. **This case is forced, and is labelled forced
in the test: zero of the 1,791 scored symbols lack a screener row today**, so it cannot honestly be
shown on a real one.

---

## P6 / P7

Chromium, computed styles: the `no-print` bar is `display:flex` on screen and `display:none` under
print media; the fallback button reads `Print / Save PDF`. Auto-print fires on `document.fonts.ready`
— printing before the webfont swaps can reflow a sheet whose whole contract is where content falls
on the page. `?print=0` suppresses only the auto-dialog, never the button.

The print CSS lives in **its own `<style>` block, not in `REF_CSS`**, so the byte-identity P2
asserts is untouched. The bar is `position:fixed` so it never enters the flow even before the media
query hides it.

P7 was a str-replace: label `2 Pager`, download arrow kept, opens `/gvm/2pager/{symbol}` in a new
tab. `grep` confirms no `window.print()` call remains anywhere in `scorr_cio_dashboard.html`.

---

## P8 — FAILED. The 2-Pager is a 3-Pager for 57% of the universe.

Rendered in Chromium at A4 with the ref's own 12/12/10/12mm margins. Printable box: **1039px**.

| Symbol | Band | Ladder | Page-1 height | Sheets | Page-1 match | Empty states hit |
|---|---|---|---|---|---|---|
| BHARATSE | micro | 13 | 995px | **2** | YES — 7.28/7.14/6.25/8.44, CMP 248.05, mcap 1,558, rank 2 of 13 | none |
| NETWEB | small | 33 | 1295px | **3** | n/a — sheet broke | none |
| AHLWEST | micro | 6 | 851px | **2** | YES | NULL overview → not-available line, moat/risk dropped |
| JAIBALAJI | mid | 26 | 1201px | **3** | n/a — sheet broke | longest overview in the DB (2,675 chars) — page 2 absorbed it at 453px |

**Root cause is ladder length, not prose.** Sweeping ladder size 10→35 with everything else held
constant:

```
14 rows -> 1011px -> 2 sheets   (last size that holds)
15 rows -> 1027px -> 3 sheets   (under 1039px, but the browser breaks anyway)
16 rows -> 1043px -> 3 sheets   (measurably over)
33 rows -> 1311px -> 3 sheets
```

The sheet holds to a **fourteen-row** segment ladder and no further; each extra peer costs ~15.7px.
Page 2 is not the problem — even the longest overview in the database lands at 453px of 1039px.

**Why nobody saw it:** BHARATSE sits in a 13-name segment. It is **one row under the cliff**. The
ref is not wrong; it is the luckiest possible symbol to have built it on.

**Blast radius, from the DB rather than estimated:** 52 of the 130 segments carry 15+ rated names,
and those segments hold **1,026 of the 1,791 scored symbols**. Largest is IT - Small at 33.

**Not done:** type was not shrunk (forbidden, and the scale is at its floor), rows were not dropped
silently, and P9/P10 were not written as though P8 passed.

**Options, recommendation first:**

- **A — window the ladder** around the subject: at most 14 rows, labelled honestly
  (`showing 14 of 33 peers · ranks 8–21`). Keeps two pages, keeps the denominator visible, keeps the
  product's name true. Cost: the whole segment is no longer on the sheet. **Recommended** — it is
  the only option that preserves both the page count and an honest denominator, and it is the same
  UNIVERSE_DENOMINATOR_RULE pattern already used on the theme cards.
- **B — accept three sheets** on big segments. Cheapest, but then it is not a 2-Pager and the
  founder review compares it against a two-page PDF.
- **C — top-N plus the subject row** with a visible gap marker. Keeps the leaders and the company,
  loses the middle.
- **D — move the ladder to page 2.** **Not viable** — the ladder carries ratings and verdicts, and
  page 2 is founder-locked to carry neither.

The window size is a product decision, not CC's.

---

## P9 — the helper already existed; the parser did not

**The card's premise needs correcting.** The shared exact-minute catch-up helper P9 asks for already
exists: `_bg_catchup_sweep` (cc#841 part_3, `scheduler.py:127`) is registry-derived off
`scheduler_master WHERE active AND category='scheduler_loop'`, bounded to the current period, and
latched on `last_run_at >= period_start` so it cannot double-run. That is option B generalised,
built on 03-Aug. It was not rebuilt.

**What was broken is the parser it depends on.** `classify_cadence()` kept the first `h ==` and the
first `m ==` and discarded the rest. Six active rows declare more than one exact-minute slot:

| Job | Cadence | Old parser saw |
|---|---|---|
| `bg_protocol_one` | 15:40 ; 09:20 | only 15:40 |
| `bg_oi_snapshot` | 09:20 or 15:35 | only 09:20 |
| `bg_yahoo_daily_sync` | 01:00 ; 15:35 | only 01:00 |
| `bg_fetch_universe_reco_news` | 21:10 ; 03:10 | only 21:10 |
| `bg_nse_eod_ingest` | 18:30 ; 19:30 ; 20:30 | only 18:30 |
| `bg_fetch_market_news` | (market loop) ; 05:20 | 05:20 invisible |

**For `bg_protocol_one` this closed the recovery path**, which is a different failure from a deploy
landing on the minute. Its only visible slot was 15:40, so before 15:40 on any day
`expected_last_run()` answered "last Friday 15:40" — always below today's period floor, and the
sweep skips anything whose slot predates the floor. **That job could never be caught up, on any day,
at any hour.**

Pairing rule: each `h ==` binds to the next `m ==` after it, which reads both registry shapes
without parsing boolean structure. An `m ==` with no preceding `h` is deliberately not a slot — that
is an hourly loop with nothing to catch up. `expected_last_run` now returns the latest slot already
passed.

No schema change, no `ALTER`, nothing under `worker/**`. The latch is the existing `last_run_at`
column.

**Scope correction:** the card says "24 other jobs sit on the same exact-minute gates". The real
number is **50** active exact-minute `scheduler_loop` jobs.

**Verification deferred to 18-Aug**, per the card's own instruction.

### Two things P9 could not close

- **Why `bg_ca_daily_note` missed 09:00 today is still unproven, and is not guessed at here.** Its
  registry row is correct (active, `scheduler_loop`, `h == 9 and m == 0`, `last_run_at` 16-Aug 09:00
  IST). Replaying the sweep's exact conditions against that row at today's clock evaluates to
  **would-fire on every one of them**. And there is **not one `SCHEDULER_CATCHUP` row in `ops_log` in
  the last three days**. So the sweep is either not reaching its body on the deployed build or is
  failing inside it and being swallowed by its own outer `except`. Confirming which needs the Railway
  scheduler log. **This deserves its own card — a catch-up sweep that has never once fired is not a
  safety net.**
- **The backfill run was not executed.** Egress to `scorr.in` is blocked from the CC session at the
  proxy (403 on CONNECT) and `ca_watchdog` is not reachable through any available MCP tool. **No
  `session_log` row was written claiming a note that was never generated.** Two clean closes: hit the
  `ca_daily_note` endpoint once, or — if the sweep is in fact alive — the parser fix should let it
  catch up on its own, visible as `scheduler_master.last_run_at` moving to today.

---

## Absorbed cards

- **cc#1084** — P1–P8. **Not closeable:** P8 failed and the chain stopped.
- **cc#1078** — absorbed as P9. Closed against `9b51ae9`.
- **cc#1079** — verification-only (§F). Its 15:40 slot had not fired at the time of writing
  (14:33 IST). Closes on the `session_log` row where `category='protocol_one'`, not before.

## Environment note

Live-URL verification was not possible from this session: the network proxy blocks egress to
`scorr.in` (403 on CONNECT), so every check above is either a DB read through the Scorr MCP server,
execution of the shipped code against live rows, or a Chromium render of the served HTML. §H's live
render of the route remains Fable's step.
