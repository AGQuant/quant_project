# APP_QA_R6 — RESULTS · SPRINT 3 · cc#1085
**Executed by:** CC · 17-Aug-2026 · **Report:** `reports/APP_QA_R6.md` · **Ref:** `design_refs/scorr_gvm_2pager_R1.html`

## Summary

**All ten pushes landed.** P8 failed on its first attempt, stopped the chain as §C P8 instructs,
and was rebuilt against `design_refs/scorr_gvm_2pager_R2.html` after Fable's Option A ruling. The
sheet now lands on exactly two A4 pages for every ladder shape in the database.

The original failure is kept in full below rather than edited out — the measurement is the reason
the ruling exists, and a results file that quietly reads as though P8 passed first time would lose
the one finding that mattered most in this sprint.

| Push | SHA | Files | Verify | Notes |
|---|---|---|---|---|
| P1 | `564ef83` | `gvm_twopager.py`, `main.py` | 200 / 404 / 503 | Own router, one `include_router()` line |
| P2 | `8879d24` | `gvm_twopager.py`, `test_twopager_css_parity.py` | CSS byte-identical, 3215 B both sides | Parity is a test, not a claim |
| P3 | `cc726cd` | `gvm_twopager.py`, `test_twopager_css_parity.py` | Page 1 matches the live card | Two documented departures from the ref |
| P4 | `6411433` | `gvm_twopager.py`, `test_twopager_page2_clean.py` | Grep: zero word-boundary matches | Grep-as-worded cannot pass — see §P4 |
| P5 | `24a5a49` | `gvm_twopager.py`, `test_twopager_empty_states.py` | Both empty states rendered | Case 2 forced; 0 of 1,791 hit it live |
| P6 | `24a5a49` | `gvm_twopager.py` | `display:flex` screen / `none` print | Print CSS kept OUT of `REF_CSS` |
| P7 | `24a5a49` | `scorr_cio_dashboard.html` | Button reads `2 Pager`, opens new tab | `window.print()` gone from the file |
| P8 | `237e2a1` | `gvm_twopager.py`, `test_twopager_css_parity.py` | Six symbols, all 2 sheets | Failed first, rebuilt to R2 — see §P8 |
| P9 | `9b51ae9` `4083a68` `c9d266a` | `scheduler_master.py`, `test_scheduler_multislot.py` | 6 multi-slot rows parse fully | Card's premise corrected — see §P9 |
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

### P8 REBUILT — passed, against R2

Fable's ruling: **Option A, twelve visible rows, not fourteen.** His correction, quoted because it
is the load-bearing part: the ref was tuned to land two pages *for one symbol*, and that was called
a property of the format. Fourteen rows leave 28px of 1039px, which one two-line company name eats.
Twelve leaves real headroom. **A format verified on a single instance is not verified.**

Built to `design_refs/scorr_gvm_2pager_R2.html` @ `f3e9a95`. R1 stays in the repo as history;
`REF_CSS` stays byte-identical to it. R2 is a ladder-only fragment contributing exactly two rules
(`.winlbl`, `tr.gap td`), extracted from its style block and served in their own tag. The parity
test now checks **both** refs and asserts R2's rules did not bleed into `REF_CSS`.

Rules implemented: twelve data rows; **rank 1 always kept** (a window centred on the subject can
drop the segment leader, and "second of thirteen" means nothing without knowing who is first);
where the window already reaches rank 1 it is simply the top twelve, contiguous; **every omitted
range prints a visible gap row** naming how many peers and which ranks are missing; the denominator
is always on the sheet.

| Symbol | Segment | N | Self | Page-1 | Sheets | Window |
|---|---|---|---|---|---|---|
| BHARATSE | Auto - Body & Stampings | 13 | 2 | 1011px | **2** | ranks 1–12 |
| NETWEB | IT - Small | 33 | 1 | 995px | **2** | ranks 1–12 |
| HGS | IT - Small | 33 | 33 | 995px | **2** | rank 1 and ranks 23–33 |
| AHLWEST | Hotels - Small | 19 | 1 | 977px | **2** | ranks 1–12 |
| JAIBALAJI | Steel - Mid & Small | 26 | 21 | 977px | **2** | rank 1 and ranks 16–26 |
| MAANALU | Aluminium & Non Ferrous | 15 | 15 | 977px | **2** | rank 1 and ranks 5–15 |

Twelve data rows each, subject always present, rank 1 always present, gap rows accounting for every
omitted rank exactly once — asserted across eleven ladder shapes, not only the six rendered.

**A second bug this rebuild caught, in P3's own work.** The segment-family query matched `fam` and
`fam - %` only, so it returned **four** Auto segments where the ref shows five: the fifth is
`Auto OEM`, which carries no dash. P3's verification missed it because the harness fed a hardcoded
five-row family instead of running the query. Fixed by also matching `fam %`.

**Residual, measured and stated rather than left to be discovered.** The ladder is capped but page 1
still has a variable block: the segment-family table runs 1 to 6 rows (Pharma and Engineering are
the sixes). At 6 siblings plus a windowed ladder with two gap rows the worst case measures
**1014px — 25px of headroom**, not the ~59px the twelve-row cap was expected to buy. It holds on
every shape in the database today. If more margin is wanted, capping the family table is the next
lever; that is a design call, so it is reported, not taken.

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

**Verified LIVE the same afternoon, not deferred to 18-Aug** — because fixing the parser exposed two
deeper faults, and all three are now proven on real data:

- **15:00:12 IST** — `ops_log` `SCHEDULER_CATCHUP`: `{"fired":["bg_protocol_one","bg_ca_daily_note"],"examined":88}`.
  The catch-up sweep's **first successful dispatch in the fourteen days since it was built.**
- **15:00:14 IST** — `CA_DAILY_NOTE` written with live content ("1 upcoming ex-date · 0 restated
  overnight · 0 genuine-crash flags", KIRLPNU face-value split ex-date 18-Aug). That is cc#1078's
  backfill, produced by the mechanism rather than by hand, and **honestly late** — 15:00, not 09:00,
  with `ops_log` and `last_run_at` both carrying the true time.
- **15:40:00 IST** — `session_log` id **24585**, `category='protocol_one'`. cc#1079's first-run
  evidence, written by the scheduler on its own gate.

### Why it took three fixes, and the doctrine that came out of it

**part_2 (`4083a68`) — the sweep could not fail visibly.** It wrapped its body in
`except Exception → log → return None`, and `_run_recorded` stamps `last_status='ok'` on any normal
return. So "ok" never meant the body ran; it meant the function returned, which it does even when it
dies on its first statement. Three days of "ok" with an empty `ops_log` is exactly what a totally
dead sweep produces. A failure now writes its own `SCHEDULER_CATCHUP_ERROR` row with the traceback.

**part_3 (`c9d266a`) — the sweep had never fired once.** Ten minutes after part_2 shipped, it said so:

```
File "/app/scheduler.py", line 175, in _bg_catchup_sweep
    _spawn(fn)
File "/app/scheduler.py", line 89, in _spawn
    loop = asyncio.get_running_loop()
RuntimeError: no running event loop        examined: 88
```

`asyncio.get_running_loop()` only answers on the event-loop thread. The sweep is dispatched **into**
the pool, so it runs on a worker thread and raised on the first job it wanted to fire. It read all 88
rows, evaluated every gate correctly, and fell over at dispatch — every time, since 03-Aug. **The
answer to "how many jobs was it capable of dispatching" is zero.** Fixed by submitting straight to
the same `_EXECUTOR` through the same `_run_recorded` wrapper when there is no loop.

**cc#1079 (`73ee656`) — protocol_one crashed the same silent way.** Its clock is naive IST, but
`scheduler_master.last_run_at` is `timestamptz`, so comparing an aware `last_run` to the naive
due-slot raised `TypeError` on the first row — swallowed, stamped ok. A quieter twin was fixed
alongside: `raw_news.fetched_at` is also `timestamptz`, and a naive bound against it slid the
six-hour news window by the IST offset.

**SWALLOWED_EXCEPTION_RULE_V1** (Fable, session_log): no scheduler job, sweep or reporter may wrap
its whole body in `except Exception → log → return None`. A body-level failure must re-raise into
`_run_recorded` so `last_status` carries `error`, or write its own loud alert row. `ok` must mean the
body completed its work, never merely that the function returned. **Three components were dead while
reporting healthy today, all by the same construction.**

### Two findings the sentinel surfaced on its first run — reported, not fixed

Both were checked before assuming, per Fable's instruction, and **neither is a calendar defect**:

- **`bg_v8_eod` expected slot printed as 16-Aug 15:45, a Saturday.** The expectation calendar is
  gate-aware; this cadence has no gate to be aware of. It is registered as `h == 15 and m == 45` with
  **no weekday and no trading-day gate**, so an EOD engine job legitimately resolves to Saturday and
  will read late every Monday. The defect is in the registration, not the reader.
- **`bg_heal_intraday` three days stale.** Not stale. Its cadence is weekday-gated, Friday was its
  last trading slot, and it ran normally today at 15:43 IST. It read late only because the report
  executed at 15:40:00.4 — the same minute the job was due, before it had finished. A job inside its
  own due minute should arguably not be called late; that is a third, smaller finding.

### The one thing P9 could not do by hand

The backfill note was never executed manually, and did not need to be. Egress to `scorr.in` is
blocked from the CC session at the proxy (403 on CONNECT) and `ca_watchdog` is not reachable through
any available MCP tool, so **no `session_log` row was written claiming a note that was never
generated.** Once `c9d266a` restored the sweep, the mechanism produced the note itself at 15:00 —
which is the better outcome, because it proves the recovery path rather than papering over it.
---

## Absorbed cards

- **cc#1084** — P1–P8. Closeable now that P8 passes against R2.
- **cc#1078** — absorbed as P9. Closed against `9b51ae9`, with the backfill produced by the restored
  sweep at 15:00 rather than by hand.
- **cc#1079** — closed on `session_log` id **24585**, written on its own 15:40 slot with six-domain
  content. ENGINE_LIVENESS_RULE satisfied by the row, not by the registration.
- **cc#1091** — opened by Fable off this work and closed the same afternoon: the catch-up sweep had
  dispatched zero jobs in fourteen days.

## Environment note

Live-URL verification was not possible from this session: the network proxy blocks egress to
`scorr.in` (403 on CONNECT), so every check above is either a DB read through the Scorr MCP server,
execution of the shipped code against live rows, or a Chromium render of the served HTML. §H's live
render of the route remains Fable's step.

One consequence worth recording. For roughly ninety minutes, eight commits sat code-complete on
`claude/status-1017-32m92b` while `origin/main` did not contain them, so every "pushed" line written
in that window meant pushed-to-branch, not deployed. The working copy was on the feature branch when
this session resumed after a context compaction — no `git checkout` was run by CC, and the branch is
the one this session's operating instructions designate — so the most likely cause is the environment
placing the working copy there on resume. It is stated as the likely cause, not a proven one.
