# cc#1092 — HOME LIVE NEWS · RESULTS
**Executed by:** CC · 17-Aug-2026 · **Ref:** `design_refs/scorr_home_news_R1.html` @ `ec9df19`
**Origin:** founder QA of the live `/m/home` Live News card, 17-Aug 15:33

| Push | SHA | Files | Verify |
|---|---|---|---|
| P1 tabs | `24b9f1c` | `mobile/home.html` | POLISH default + on, both counts live, no new endpoint |
| P2 rows | `24b9f1c` | `mobile/home.html` | `BUTTON`, 44px, chevron on every row, zero headline links |
| P3 sheet | `24b9f1c` | `mobile/home.html` | 213px within the 78vh cap; Escape / backdrop / handle / swipe-down |
| P4 content | `24b9f1c` | `mobile/home.html` | escaped-then-formatted body, chips, honest raw fallback |
| P5 results | this file | `reports/APP_QA_R7_NEWS_RESULTS.md` | counts reconciled to the DB below |

P1–P4 landed as **one commit**, and that is stated rather than dressed up as four: they are one
card and the intermediate states do not run. P2 removes the wrapper anchor P3 needs gone, and P3
without P4 is a sheet with no body.

## Endpoints — both already existed

The card said to check before inventing one. Neither was missing, and **no new endpoint was added**.

| Tab | Endpoint | Table |
|---|---|---|
| RAW | `/api/news/live?hours=48&per_cat=300` | `raw_news` — the same call the web tab makes |
| POLISH | `/api/news/polished?category=all&limit=100` | `v_polished_articles` — the same body Intel renders |

The two feeds are **separate promises**. If polish fails the raw firehose still renders and the tab
reads 0; if raw fails polish still renders. Neither can blank the card, and no count is ever
invented to fill a gap.

## DOM assertions — Chromium, 390×844

```
TABS          : POLISH | RAW          DEFAULT TAB: POLISH
ROWS          : tag BUTTON · min height 44px · chevron on all · HEADLINE LINKS 0
SHEET         : 213px <= 78vh (658px)
HEADLINE      : Lupin gets USFDA nod for generic tablets to treat excessive…
META          : Moneycontrol · 1h ago · BULLISH · HIGH IMPACT
BODY          : <p>Lupin received approval.</p><p>The <b>generic</b> version…</p>   3 paragraphs
XSS ESCAPED   : true   (an <img onerror> in the source body renders as &lt;img)
BOLD KEPT     : true
BODY LOCKED   : true → unlocked on close, reader's scroll position restored
ESCAPE CLOSES : true          BACKDROP CLOSES: true
RAW NO-BODY   : "raw feed item — polished version not yet available."  · zero .nwbd elements
EMPTY POLISH  : both tabs still render
```

## Counts reconciled to the DB

At the time of writing, over the **same 48-hour window** both tabs now use:

| | DB | Tab |
|---|---|---|
| raw headlines, 48h, non-blank | 234 | RAW 234 |
| polished articles, 48h | 30 | POLISH 30 |

## Two bugs this build caught, both CC's own

- **`rnPaint` painted the tabs after its empty-list early return.** A zero-count tab therefore
  rendered no switch at all — POLISH with no articles would have stranded the reader on a blank
  card with no way back to RAW. The switch now outlives its own contents.
- **The tabs would have compared two different windows.** Raw is fetched as `hours=48`; the
  polished endpoint has no hours param and returns the newest `limit` **ever**. Side by side that
  reads `POLISH 100 · RAW 234` as though the two shared a denominator. Polish is now filtered to
  the same 48 hours, so the pair means something. Same UNIVERSE_DENOMINATOR_RULE reflex as the
  theme cards.

## A bug class retired, not just a behaviour changed

The card was one large `<a href="/m/intel">` wrapper. That anchor is what cc#971 (the Next button
navigated away mid-page-turn) and cc#1062 (a swipe navigated away) both had to fight with
event-swallowing hacks. The founder's no-navigation ruling removes the cause rather than adding a
third hack. The header count keeps its link — an explicit *go to the page* is a different intent
from a *peek*, exactly as ruled.

## What CC cannot verify

**The feel.** Sheet slide, swipe-down resistance, the press of a 44px row under a thumb on glass.
Computed styles say nothing about any of it. That is the founder's device check, as the card itself
states — it is not claimed here.
