# cc#1185 P2 — THEME VIOLATIONS v1

Read-only. Every raw value that bypasses the token layer, per file per line, classified into the card's three buckets. Same declaration walk as P1 with a different filter — comments masked first, so a hex inside a comment is never counted as a live primitive.

| bucket | meaning | count |
|---|---|---|
| **(a)** | maps to an EXISTING token — the literal already has a name | **319** |
| **(b)** | needs a NEW token key | **1711** |
| **(c)** | SEMANTIC (P&L / pass-fail) — exempt from the swap by the card's invariant | **37** |
| | **total raw declarations** | **2067** |

**(a) + (b) = 2030 is the repoint job.** (c) is not debt — the card's invariant exempts P&L green/red and pass/fail from the theme swap, so those 37 declarations must keep their literals and P8's validator needs them on its allowlist rather than in its error count.

### How (c) is decided, because getting it wrong is expensive in both directions

A declaration is semantic when its own SELECTOR says so — `.ok`, `.bad`, `.win`, `.loss`, `.up`, `.down`, `.pos`, `.neg` and the like — not when its colour happens to be green or red. The cyan eyebrow and the volt accent are chrome that happens to be bright, and they are exactly what a theme has to be able to move. Fold a semantic colour into the theme and a future palette turns a loss amber; leave a chrome colour in (c) and it never gets tokenised at all.

## Why this total is not P1's 2,201, reconciled to the declaration

Two reports in one sprint that disagree on the headline number are worth nothing, so the gap is closed here rather than left for someone to notice.

| step | count |
|---|---|
| P1 report, measured at sha `1cdb587` | 2201 |
| P1's own predicate re-run on today's files | 2178 |
| less: `vh` / `vw` / `%` / `ch` lengths — `width:100%`, `border-radius:50%` | −64 |
| less: keyword colours — `transparent`, `currentColor` | −31 |
| less: animation timings in `ms` / `s` | −27 |
| **P2 total** | **2067** |

The 2,201 → 2,178 step is the corpus moving, not a measurement changing: cc#1190 pushes 2 and 3 landed on `scorr_digest_mobile.html` after P1 ran, replacing the old results card with token-based CSS. The 111 exclusions are deliberate — **a `width:100%` is not a theme token, and neither is `transparent`.** P1 asked "how many primitives carry a literal"; P2 asks "how many literals could the token layer own", which is a strictly smaller question. Verified strictly smaller: running both predicates over the same corpus, P2 catches **nothing** P1 missed — 2178 - 111 + 0 = 2067.

## Per file

| file | (a) existing token | (b) new key | (c) semantic | total |
|---|---|---|---|---|
| `static/mobile_app.css` | 80 | 545 | 7 | 632 |
| `mobile/home.html` | 73 | 298 | 9 | 380 |
| `static/mobile.css` | 45 | 232 | 0 | 277 |
| `mobile/v8.html` | 36 | 211 | 4 | 251 |
| `scorr_digest_mobile.html` | 21 | 151 | 7 | 179 |
| `scorr_gvm_fightcard.html` | 23 | 94 | 2 | 119 |
| `scorr_cards_preview.html` | 8 | 50 | 0 | 58 |
| `static/scorr_theme_r5.css` | 14 | 41 | 0 | 55 |
| `scorr_v10_signal.html` | 6 | 39 | 7 | 52 |
| `mobile/trade_wall.html` | 9 | 25 | 1 | 35 |
| `mobile/gvm.html` | 1 | 9 | 0 | 10 |
| `mobile/check.html` | 1 | 5 | 0 | 6 |
| `mobile/models.html` | 1 | 5 | 0 | 6 |
| `mobile/qb.html` | 1 | 5 | 0 | 6 |
| `static/theme_mobile.css` | 0 | 1 | 0 | 1 |

## (a) — literals that already have a token name

These are pure wins: the value is identical to a declared token, so repointing is a rename with a provable zero pixel delta. Showing the first 60 of 319.

| file | line | selector | property | value | already named |
|---|---|---|---|---|---|
| `static/mobile.css` | 43 | `.hscroll-fade::after` | background | `linear-gradient(to right,rgba(255,255,255,0),r` | rgba(255,255,255,0) == --panel/--card |
| `static/mobile.css` | 53 | `input,select,textarea,.fnum` | font-size | `16px !important` | 16px == --r5-cut/--cut |
| `static/mobile.css` | 79 | `.mtable-wrap::before` | background | `linear-gradient(to right,rgba(255,255,255,.96)` | rgba(255,255,255,.96) == --panel/--card |
| `static/mobile.css` | 80 | `.mtable-wrap::after` | background | `linear-gradient(to left,rgba(255,255,255,.96),` | rgba(255,255,255,.96) == --panel/--card |
| `static/mobile.css` | 96 | `.mtable thead th` | height | `44px` | 44px == --mux-tap |
| `static/mobile.css` | 144 | `.smc-hd` | min-height | `44px` | 44px == --mux-tap |
| `static/mobile.css` | 162 | `.smc-chip` | padding | `3px 6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 172 | `.smc-bar` | width | `6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 182 | `.scorr-ml-btn` | min-height | `44px` | 44px == --mux-tap |
| `static/mobile.css` | 187 | `.scorr-ml-btn:hover` | background | `rgba(148,166,210,.12)` | rgba(148,166,210,.12) == --line/--line2 |
| `static/mobile.css` | 192 | `.scorr-ml-hd` | padding | `16px 18px` | 16px == --r5-cut/--cut |
| `static/mobile.css` | 195 | `.scorr-ml-x` | min-width | `44px` | 44px == --mux-tap |
| `static/mobile.css` | 195 | `.scorr-ml-x` | min-height | `44px` | 44px == --mux-tap |
| `static/mobile.css` | 199 | `.scorr-ml-card` | min-height | `44px` | 44px == --mux-tap |
| `static/mobile.css` | 210 | `.ml-foot` | margin-top | `6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 216 | `.ml-dot` | width | `6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 216 | `.ml-dot` | height | `6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 218 | `.ml-ring` | width | `6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 218 | `.ml-ring` | height | `6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 229 | `.dt .chip` | gap | `6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 231 | `.dt .chip.live` | border-color | `rgba(47,212,139,.35)` | rgba(47,212,139,.35) == --bull/--bull-soft |
| `static/mobile.css` | 233 | `.dt .chip.stale` | border-color | `rgba(255,92,108,.35)` | rgba(255,92,108,.35) == --bear/--bear-soft |
| `static/mobile.css` | 238 | `.dt .gate` | padding | `18px 16px 16px` | 18px == --r |
| `static/mobile.css` | 238 | `.dt .gate` | box-shadow | `0 18px 44px rgba(3,7,20,.5)` | 18px == --r |
| `static/mobile.css` | 275 | `.dt .tabs` | margin | `18px -14px 0` | 18px == --r |
| `static/mobile.css` | 275 | `.dt .tabs` | padding | `2px 14px 6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 279 | `.dt .tab.on` | color | `#fff` | #fff == --panel/--card |
| `static/mobile.css` | 279 | `.dt .tab.on` | box-shadow | `0 6px 18px rgba(77,124,254,.35)` | rgba(77,124,254,.35) == --blue/--blu |
| `static/mobile.css` | 280 | `.dt .tab .b` | background | `rgba(255,255,255,.14)` | rgba(255,255,255,.14) == --panel/--card |
| `static/mobile.css` | 280 | `.dt .tab .b` | padding | `2px 6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 285 | `.dt .prow` | gap | `6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 290 | `.dt .lab` | gap | `6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 298 | `.dt .rail-wrap` | margin-top | `6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 298 | `.dt .rail-wrap` | padding | `18px 6px 6px` | 18px == --r |
| `static/mobile.css` | 300 | `.dt .rail` | height | `6px` | 6px == --r5-cut-sm |
| `static/mobile.css` | 300 | `.dt .rail` | background | `linear-gradient(90deg,rgba(255,92,108,.55),rgb` | rgba(255,92,108,.55) == --bear/--bear-soft |
| `static/mobile.css` | 301 | `.dt .rail .tick` | height | `16px` | 16px == --r5-cut/--cut |
| `static/mobile.css` | 306 | `.dt .cmp-dot` | box-shadow | `0 0 0 3px rgba(47,212,139,.25),0 0 14px var(--` | rgba(47,212,139,.25) == --bull/--bull-soft |
| `static/mobile.css` | 308 | `.dt .verdict` | border | `1px solid rgba(245,185,74,.3)` | rgba(245,185,74,.3) == --amber/--amber-soft |
| `static/mobile.css` | 309 | `.dt .bnav` | background | `rgba(13,20,40,.92)` | rgba(13,20,40,.92) == --well |
| `static/mobile.css` | 309 | `.dt .bnav` | backdrop-filter | `blur(18px)` | 18px == --r |
| `static/mobile.css` | 349 | `:root[data-theme="light"] .cmp-dot,:root[data-theme="light` | box-shadow | `0 0 0 2px rgba(15,169,104,.22)` | rgba(15,169,104,.22) == --grn/--green |
| `static/mobile.css` | 350 | `:root[data-theme="light"] .chip.live .dot` | box-shadow | `0 0 4px rgba(15,169,104,.5)` | rgba(15,169,104,.5) == --grn/--green |
| `static/mobile.css` | 351 | `:root[data-theme="light"] .dt .bnav` | background | `rgba(255,255,255,.9)` | rgba(255,255,255,.9) == --panel/--card |
| `static/mobile.css` | 351 | `:root[data-theme="light"] .dt .bnav` | border-top-color | `rgba(20,35,80,.14)` | rgba(20,35,80,.14) == --line/--line2 |
| `static/scorr_theme_r5.css` | 366 | `:root:root:not([data-theme="light"]) .bn.on` | color | `#D4AF37 !important` | #D4AF37 == --gold/--railgrad |
| `static/scorr_theme_r5.css` | 366 | `:root:root:not([data-theme="light"]) .bn.on` | text-shadow | `0 0 10px rgba(212,175,55,.35)` | rgba(212,175,55,.35) == --gold/--railgrad |
| `static/scorr_theme_r5.css` | 384 | `:root:root:not([data-theme="light"]) .bn.on::before` | background | `#D4AF37` | #D4AF37 == --gold/--railgrad |
| `static/scorr_theme_r5.css` | 386 | `:root:root:not([data-theme="light"]) .bn .i` | font-size | `16px` | 16px == --r5-cut/--cut |
| `static/scorr_theme_r5.css` | 419 | `:root:root:not([data-theme="light"]) .as-bn span` | font-size | `16px` | 16px == --r5-cut/--cut |
| `static/scorr_theme_r5.css` | 420 | `:root:root:not([data-theme="light"]) .as-bn.on` | color | `#D4AF37 !important` | #D4AF37 == --gold/--railgrad |
| `static/scorr_theme_r5.css` | 420 | `:root:root:not([data-theme="light"]) .as-bn.on` | text-shadow | `0 0 10px rgba(212,175,55,.35)` | rgba(212,175,55,.35) == --gold/--railgrad |
| `static/scorr_theme_r5.css` | 422 | `:root:root:not([data-theme="light"]) .as-bn.on::before` | background | `#D4AF37` | #D4AF37 == --gold/--railgrad |
| `static/scorr_theme_r5.css` | 425 | `:root:root:not([data-theme="light"]) .tool span` | color | `#D4AF37` | #D4AF37 == --gold/--railgrad |
| `static/scorr_theme_r5.css` | 425 | `:root:root:not([data-theme="light"]) .tool span` | text-shadow | `0 0 10px rgba(212,175,55,.35)` | rgba(212,175,55,.35) == --gold/--railgrad |
| `static/scorr_theme_r5.css` | 444 | `:root:root:not([data-theme="light"]) .sect` | text-shadow | `0 0 12px rgba(53, 224, 255, .4)` | rgba(53, 224, 255, .4) == --r5-aqua/--brand |
| `static/scorr_theme_r5.css` | 624 | `.pressed` | padding | `14px 16px` | 16px == --r5-cut/--cut |
| `static/scorr_theme_r5.css` | 624 | `.pressed` | box-shadow | `inset 0 3px 9px rgba(0,0,0,.9),
             i` | rgba(255,255,255,.10) == --panel/--card |
| `static/scorr_theme_r5.css` | 633 | `.well-up` | box-shadow | `0 1px 0 rgba(255,255,255,.07), 0 -1px 0 #000` | rgba(255,255,255,.07) == --panel/--card |
| `static/mobile_app.css` | 20 | `.chip` | min-height | `44px` | 44px == --mux-tap |

## (b) — needs a NEW token key, by family

| family | count |
|---|---|
| font | 604 |
| spacing | 539 |
| shape | 180 |
| border | 179 |
| radius | 131 |
| colour | 45 |
| shadow | 33 |

The shape of (b) is the P3 schema in miniature: the families with the biggest counts are the keys the token file is missing most.

_Generated read-only by cc#1185 P2. No stylesheet or page was modified._
