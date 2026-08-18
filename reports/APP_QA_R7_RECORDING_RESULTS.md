# APP QA R7 — founder recording, 18-Aug-2026

cc#1096. Four defects, each raised from a frame of `rec_4.mp4` (19:55–19:59 IST, 3m51s, Scorr app
on a 720×1560 phone) and reviewed frame by frame by Fable. Three were fixed; the fourth is a
diagnostic and changed no code, as the card required.

*Filename note: `APP_QA_R7_RESULTS.md` was already taken by cc#1090 Sprint 4, which the card named
that way. This file is the recording round, so it carries its own name rather than overwriting one.*

---

| Defect | Push | SHA | Files | Outcome |
|---|---|---|---|---|
| D1 nav differs by page | 1 | `ac7b190` | scorr_theme_r5.css | colour half FIXED · slot half not reproducible, evidence below |
| D2 pivot ladder unreadable | 2 | `5efec6d` | scorr_digest_mobile.html | FIXED |
| D3 BSE codes leaking | 3 | `8662a2b` | digest_v3.py · scorr_digest_mobile.html | FIXED |
| D4 three verdicts | 4 | — | none, by design | DIAGNOSED · logged as a QUESTION |
| results file | 5 | this file | reports/APP_QA_R7_RECORDING_RESULTS.md | — |

---

## D1 — the other nav

**Owning file, which V1 asks for.** The Digest nav *markup* is `scorr_digest_mobile.html` lines
255–261. Its *look* comes from `scorr_appshell.css` `.as-nav` / `.as-bn`. It is not the `mobile/`
template set, which is why an earlier code read of those templates found nothing wrong.

**The app has two nav implementations, not one:**

| Implementation | Files |
|---|---|
| `.bnav` / `.bn` | the 16 templates under `mobile/` |
| `.as-nav` / `.as-bn` | scorr_digest_mobile.html · scorr_gvm_fightcard.html · scorr_v10_signal.html |

**Cause — cc#1071's own cause, missed once more.** That block in `scorr_theme_r5.css` states it
outright: nothing paints these purple. Rule 5 is `a{color:var(--r5-pulse)}` at (0,3,1), a nav tab
is an `<a>`, so the one interactive rule swallows the whole navigation. cc#1071 took `.bn` and
`.tool` back out of it at (0,4,0) and did not know `.as-bn` existed.

**Measured at the founder's 720px width:**

| | before | after |
|---|---|---|
| Home | Home gold 212,175,55 · four aqua 53,224,255 · bar 10,16,30 | unchanged |
| Digest | all five purple 124,92,255 · bar 19,19,22 | five aqua 53,224,255 · bar 10,16,30 |
| Digest, active slot | — | gold 212,175,55 + four aqua · bar 10,16,30 |

**The slot half is not reproducible at HEAD, and there is evidence for why rather than a denial.**
Both navs carry the identical five slots — Home · GVM · Check · Intel · Models — verified in the
rendered DOM, not by reading. A V8 slot *did* exist: `mobile/home.html` carried
`<a class="bn" href="/m/v8">V8</a>` in slot 5 until commit `2a2c1fe` (cc#886, 07-Aug-2026 10:20
UTC) replaced it with Models. Nothing in the repo has rendered a V8 nav slot since. The recorded
Home frame shows a page the current code cannot produce, which points at a stale cached shell on
the device — the app is a PWA with a service worker, and `/m/*` paths are deliberately outside
`_PWA_INJECT_PATHS`. A cache is not a CSS problem, so it is logged as a question rather than fixed
on my own judgement.

---

## D2 — the pivot ladder

**Payload values**, from `v8_paper_pivots` at `pivot_date` 2026-08-18, which V2 asks for:

| | S2 | S1 | PP | R1 | R2 |
|---|---|---|---|---|---|
| BANKNIFTY | 56,697 | 57,094 | 57,555 | 57,952 | 58,413 |
| NIFTY50 | 24,063 | 24,214 | 24,418 | 24,569 | 24,773 |

They match the frame exactly. The frame shows four because R2 was the one pushed past the edge.

**Three things stacked, not one.** `gap:0` meant there was no column separation at all — five
numbers shoulder to shoulder, kept apart only by slack inside each cell. `flex:1` with the default
`min-width:auto` means a cell can never shrink below its own text, so the moment a value is wider
than its share the *row* grows past the card instead of the columns giving way. And nothing capped
the overflow, so the spill landed under `.card`'s clip-path and lost digits.

**Honest limit on the repro:** at default text scale on a 360px viewport the values measure 28px in
a 60px cell and the row looks fine. The layout is one text-width step from breaking and Chrome for
Android's text scaling is exactly that step — stated as the hypothesis it is. The fix does not
depend on it being right.

**Fixed with a declared grid**: five tracks of `minmax(max-content, 1fr)`, a real 6px gutter, and
`overflow-x:auto` so that if content genuinely cannot fit, the row scrolls rather than hiding a
digit.

```
default   gaps 6,6,6,6 · five columns · nothing past the card edge
boosted   zero colliding pairs (tracks grow to max-content) · the row scrolls
before    gaps 0,0,0,0 at every width
```

Layout only — 22 lines of CSS and comment, no JavaScript line changed, so `ladder()`'s
`num(r.value)` renders exactly what it rendered before. **V5 holds: no engine value moved.**

---

## D3 — the BSE codes

**Resolution path**, which V3 asks me to name: `earnings_calendar.ticker` is rendered straight to
screen. That column holds a BSE scrip code for BSE-only companies, and the payload LEFT JOINs
`screener_raw` on `nse_code`, which cannot match one — so the join fails silently, `market_cap`
comes back NULL, and the raw code reaches the chip.

**A real resolution was checked first**, because a symbol always beats a name. `screener_raw`
carries a `"BSE Code"` column, so a mapping looked possible:

```
earnings_calendar rows                                   2,470
rows whose ticker is all digits                            163
of those 163, resolvable to an nse_code via "BSE Code"       0
```

Zero — `screener_raw` **is** the NSE-listed universe (1,816 rows) and these companies are not on the
NSE at all. There is no symbol to resolve to, and inventing one is forbidden.

**So the name**, and the evidence leaves no gap: `company_name` is populated on 2,470 of 2,470 rows,
zero nulls. Resolved server-side in `digest_v3._label` so both surfaces and any future consumer get
one answer, with `is_symbol:false` carried alongside. The ticker is **kept** in the payload
untouched — the news match in `_yesterday_results` keys on it against `mentioned_symbols`.

Before → after, run through the shipped function against real rows:

| ticker | rendered before | rendered after |
|---|---|---|
| 540027 | 540027 | Prabhat Tech. |
| 521137 | 521137 | Eureka Industrie |
| 512379 | 512379 | Cressanda Railwa |
| 531515 | 531515 | Mahan Industries |
| 539175 | 539175 | Starbeam Ventures |
| TVVISION · ANNAPURNA · BAGFILMS · INDOMIM · SURAJEST · SILVERLINE | unchanged | unchanged |

**An unresolvable symbol renders as the code itself** — not a blank and not an em-dash. If a numeric
code ever arrives with no name the reader can at least look it up, and hiding it would hide that a
company reported at all. Only a row with neither ticker nor name shows the em-dash.

---

## D4 — three verdicts, one minute apart

**Three different measures, not one disagreeing. Not a P1.** No code was changed; the finding is
logged as a `QUESTION:` in the room, as the card requires.

| Surface | Word | Payload field | What it actually measures |
|---|---|---|---|
| Home hero | BEARISH · 4 of 4 checks failed | `hero.mood` / `hero.fails` from `/api/mobile/home2` | `v8_endpoints.market_mood()` — the **V8 signal gate**. Four domestic checks: ADR ≥ 1, Nifty Day ≥ 0, Week ≥ 0, Month ≥ 0. The word comes from the FAIL COUNT (0 Strong Bullish, 1 Bullish, 2 Neutral, 3+ Bearish). Its real job is setting `buy_slots`/`sell_slots`. |
| Digest | MARKET READ · CAUTIOUS | `market_read.bias` from `/api/digest/v3` | ADR **alone** on a three-band scale: >1.2 Bullish continuation, 0–0.8 Cautious, else Range. The tape count and breadth number printed beneath are `market_read.support` — display only, neither feeds the bias. |
| Market mood card | CAUTION | client `pcrMood()` over `hero.pcr` | PCR **alone**, contrarian five-band PCR_MOOD_MAPPING_V1 (session_log 18024, bands_final <0.5 / 0.5–0.8 / 0.8–1.0 / 1.0–1.4 / >1.4). |

Live at the time of writing: ADR 0.774, Nifty Day −0.87, Week −1.74, Month −0.74, fails 4 → Bearish;
ADR 0.774 → Cautious; PCR 0.95 → Caution. All three agree on **direction**, which is why nothing
looks numerically wrong. Three inputs, three vocabularies, three words.

**One thing worth not leaving unsaid.** ADR feeds two of the three with **different thresholds** —
pass/fail at ≥ 1.0 in the gate, a 0.8/1.2 three-band in the digest. Today 0.774 clears neither, so
they agree. Between 0.80 and 1.00 they diverge by construction: the gate counts a FAIL, pushing the
word toward Bearish, while the digest says "Range". That is not a wording problem — it is two
thresholds for one measure.

*Evidence note: the `market_mood` figures are live from the endpoint. The digest bias was derived by
running the shipped rule on that live ADR rather than fetching `/api/digest/v3`, because this
container cannot reach scorr.in. Said plainly rather than presenting a derivation as a fetch.*

---

## Open questions in the room

1. **D1 cache** — the Home frame shows a nav no file has produced since 07-Aug. Does the app shell
   want cache/version discipline (a build-stamped service-worker precache, or a version check on
   boot)? That would also explain any other "fixed weeks ago but still on screen" report.
2. **D4 labels** — do the three verdicts get scope names (engine gate · breadth read · options
   mood), and in what words?
3. **D4 thresholds** — does the ADR 0.80–1.00 window get reconciled, or is one threshold right for a
   gate and the other right for a read?
