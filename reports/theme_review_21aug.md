# cc#1185 P10 — THEME REVIEW: the evening walkthrough

The card names this file `theme_review_21aug.md`. The sprint actually ran on 22 and 23 August; the filename is kept as specified rather than quietly corrected, and the date is stated here instead. Every number below is measured, not estimated.

## How to look at a page

Open it normally — that is GOLD NIGHT and it is the only theme anyone can select. Then add `?theme=aqua_white` to the address to see the same page in the new set. The preview lasts for that one page load. It is never saved, it never appears in any menu, and reloading without the parameter puts you straight back on Gold Night. There is no write path to storage at all, so it cannot stick by accident.

**What you are checking is that nothing changed.** This sprint moved plumbing, not appearance. Gold Night should look exactly as it did yesterday. If a spacing looks off or a size looks wrong, that is a finding and it is worth stopping on.

## The pages, in the order worth walking them

| page | open at | repointed | raw left | what to look at |
|---|---|---|---|---|
| `mobile/home.html` | `/m/home` | 328 | 41 | The book, the stat trio, the news card and the theme switch row. Home carries the most repointed rules of any page (328), so if the sprint broke anything it shows here first. Check the news rows are still a fixed height and the chips have not moved. |
| `scorr_digest_mobile.html` | `/m/digest` | 151 | 9 | The biggest repoint after Home (151). Walk the results deck left and right, open the sheet, page the analysis list. The deck slide pitch and the sheet padding are both token-driven now. |
| `scorr_gvm_fightcard.html` | `/m/gvm2` | 108 | 3 | The fight card layout — 108 swaps, second-densest page. Look at the two-column split, the bar fills and the header strip. |
| `scorr_v10_signal.html` | `/m/v10` | 45 | 2 | Index intel desk — the ladder, the tape, the mood strip. 45 swaps, mostly spacing. |
| `mobile/gvm.html` | `/m/gvm` | 8 | 1 | The report card: verdict band, pillar bars, the punch line, the 3px accent rail. Only 8 swaps here — cc#897 already moved this page's tokens into the shared sheet, so most of what you see is coming from mobile_app.css, which is NOT repointed yet. |
| `mobile/check.html` | `/m/check` | 4 | 2 | Bucket cards and the score row. 4 swaps, all font sizes. |
| `mobile/models.html` | `/m/models` | 4 | 2 | Model list rows. 4 swaps, all font sizes. |
| `mobile/qb.html` | `/m/qb` | 4 | 2 | Basket rows. 4 swaps, all font sizes. |

## Pages NOT touched by this sprint, and why

`/m/v8`, `/m/intel`, `/m/trades` and every page fed only by the shared sheets are unchanged. They do not link the token file, so repointing them would delete the declaration rather than rename it — a `var()` that resolves to nothing takes the whole line down. They need one decision first: whether `scorr_themes.css` gets injected on every page the way `mobile.css` already is. That is **1,009 of the 1,237 raw declarations still outstanding**, so it is most of what is left.

## What was proven, and how

| claim | evidence |
|---|---|
| Gold Night is unchanged on every repointed page | 557 (selector, property) probes across the eight pages, each built as a real descendant chain so the rule applies. All resolved to a real value. **Zero computed-style differences.** |
| Every token resolves to the literal it replaced | 201 distinct (key, literal) pairs re-read in each page's own cascade. Zero mismatches. No key ever bound to two different literals. |
| Nothing shifted at any phone width | 48 full-page screenshot pairs — eight pages x 360/390/430 x 1.0/1.3 — byte-identical in all 48. |
| The preview actually re-skins | All eight pages switch to `aquawhite` under the parameter, `--field` moves from `#0A0A0C` to `#F4F9FB`, and all eight render a different picture. |
| The preview never sticks | With `scorr_theme` set to `dark` in storage, the preview renders Aqua White **and storage still reads `dark`** afterwards. |
| An unknown theme name is ignored | `?theme=goldday` renders Gold Night. A stored value outside the allowed list renders Gold Night. |
| A page without the theme layer is untouched | With `data-theme` removed from `<body>`, the resolver adds nothing. This is what keeps it off the web dashboards. |

The screenshot passes are corroboration, not the main evidence, and it is worth saying why: the harness strips the shared scripts, so those pages render their static skeleton rather than their built sections. The 557 probes are what actually cover the rules.

## Where the sprint got to

| | before | now |
|---|---|---|
| `mobile/home.html` | 380 raw | 41 |
| `scorr_digest_mobile.html` | 179 raw | 9 |
| `scorr_gvm_fightcard.html` | 119 raw | 3 |
| `scorr_v10_signal.html` | 52 raw | 2 |
| **schema keys** | 17 theme keys | **17 + 152 structural** |
| **theme sets** | 3 | **4** |

## The two things a validator will now stop

`theme_validator.py` runs on every push through `github_push`.

1. **A file that adds raw values.** Each themed file has a recorded count and may only go down or stay level. The refusal names the file and the numbers.
2. **A theme set missing a key.** A missing key does not render blank — it falls through to another theme's value and looks correct on whichever theme you are testing.

Current state of check 2, reported and deliberately not fixed because `DO_NOT_TOUCH` covers the legacy sets:

| set | keys | missing |
|---|---|---|
| `aquawhite` | 17 | — none |
| `dark` | 14 | `--gold-hi`, `--gold-lo`, `--sunk` |
| `goldday` | 14 | `--gold-hi`, `--gold-lo`, `--sunk` |
| `goldnight` | 17 | — none |

## Open decisions for you

1. **Inject `scorr_themes.css` site-wide?** It unblocks 1,009 of the 1,237 remaining raw declarations. Measured as inert: the file's theme blocks only bind to `body[data-theme]`, and exactly the eight pages that already link it are the only ones in the repo that set that attribute. The cost is one more stylesheet request per page. Not done on my own reading — that injection point is every page at once.
2. **Are `--state-live` / `--state-stale` / `--state-off` theme colours or semantic?** I keyed them as theme colours, since live-versus-stale is not money truth. If they should be frozen across themes it is a one-line move.
3. **The type ramp is 30 steps in half-pixel increments.** That is an accretion, not a design. Collapsing it into a real scale moves pixels, so it needs to be its own card and your call.

_Generated for cc#1185 P10. Every figure re-measured at this sha._
