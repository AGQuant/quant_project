# cc#1969 — token census (READ-ONLY)

**No code changed under this card. None.** Everything below is measurement and a costed proposal.
The migration is founder-gated and is not authorised here.

Runs after cc#1970 landed (sha `c081538`, on main), against the post-move tree.

Every count states the command that produced it, so the whole thing is re-runnable.

---

## The answer to the founder's question, confirmed by measurement

*"Every page has different colour specs — why not uniform?"*

Because the app carries **three generations of colour vocabulary at once and nothing retired the
older two**, and because **844 places hard-code a literal that wins whenever a token name is
undefined**. It was never a style decision. It is an unfinished migration with a manual bridge in
front of it. Fable's answer on the card is correct; here is the size of it.

---

## Item 2(b) — the literal fallbacks. **844 of them.**

```
grep for  var(--name, <fallback>)  across .js .html .py .css, excluding .git, node_modules,
reports/, design_refs/ ; classify the fallback as a colour literal, another token, or other
```

| | count |
|---|---|
| `var(...)` calls carrying a fallback | **973** |
| …where the fallback is a **literal colour** | **844** |
| …where the fallback is another token | 78 |
| …anything else (a length, a keyword) | 51 |

Top files:

| File | literal fallbacks |
|---|---|
| `pwa_endpoints.py` | 128 |
| `mobile/home.html` | 121 |
| `scorr_news.html` | 65 |
| `v8_dashboard.html` | 59 |
| `scorr_card_common.js` | 54 |
| `scorr_model_portfolio.js` | 40 |
| `scorr_cockpit_card.js` | 27 |
| `mobile/v8.html` | 27 |

### The part that matters: the same name falls back to opposite colours

| Token | fallback | times | what it is |
|---|---|---|---|
| `--txt` | `#E9EEFB` | 64 | near-**white** — a dark theme's text |
| `--txt` | `#1c2536` | 12 | near-**black** — a light theme's text |
| `--panel` | `#121A33` | 19 | dark navy |
| `--panel` | `#0E1526` | 17 | a *different* dark navy |
| `--amber` | `#F5B94A` | 20 | bright amber |
| `--amber` | `#b45309` | 12 | dark amber |
| `--blu` | `#4D7CFE` / `#4d7cfe` | 27 / 15 | the same colour, written two ways |

**A fallback fires exactly when the name is undefined — which is precisely the case the bridge
exists to prevent.** So every one of these 844 is a silent second answer to "what colour is this",
chosen at authoring time to look right on Gold Night, and every one of them is wrong on some other
set. The `--txt` pair is the clearest: the same token name means white in 64 places and black in 12.

---

## Item 3 — the three families, and where one name means two things

First, a correction to how this is usually counted. A raw scan says `--win` has 13 distinct literal
values and `--accent` has 22. **That is not a collision — it is the theme sets.**
`scorr_themes.css` declares all 21 contract tokens once per set, and there are 15 sets. Counting
those as conflicts would make the healthiest file in the tree look like the worst. So
`scorr_themes.css` is excluded and only *other* declarers are counted below.

**1,301 colour-ish custom-property declarations** exist across the tree.

### Contract names re-declared outside their own home

| Token | distinct values | where |
|---|---|---|
| `--accent` | **7** | `scorr_home.html` alone declares five of them (`#7c3aed`, `#0891b2`, `#2563eb`, `#7f77dd`, `#37d3e8`), plus `#db2777` shared with `scorr_mobile.html` and `#3d6bec` in `pwa_endpoints.py` |
| `--panel` | **5** | `#121a33` in 18 files · `#161f33` in 4 · `#17171b` in 4 · **`#ffffff`** in `pwa_endpoints.py` + `scorr_web_tokens.css` · `#121a2f` in 1 |
| `--ink` | **3** | `#0a0f1e` (dark) vs **`#f5f7fb`** (light) — both in `scorr_web_tokens.css` |
| `--field` | 2 | `#0d1322` in 4 files vs `#0a0a0c` in 4 |
| `--gold-hi` | 2 | `#f0d67a` vs `#f0d070` — near-identical, a copy that drifted |

`--panel` is the sharpest: **the same token name is a dark navy on 18 mobile pages and pure white
in the web-token file.** Any component that reads `--panel` and lands in the wrong scope does not
degrade — it inverts.

### The legacy families, and the light/dark inversion running through them

The older `mobile_app.css` family (`--bg --panel --txt --mut --dim --grn --red --blu --cyan`) and
the page-scoped `#gvp/#ckp/#v8p` family (`--well --chalk --mute --gold --up --down`) are both still
declared, and **`scorr_web_tokens.css` and `pwa_endpoints.py` declare LIGHT-mode values for names
the 20 mobile pages declare DARK**:

| Token | dark value, ~20 mobile pages | light value, web tokens / pwa |
|---|---|---|
| `--bg` | `#0a0f1e` | `#f5f7fb` · `#f4f7fe` |
| `--txt` | `#e9eefb` | `#17203a` · `#0e1630` |
| `--panel2` | `#182241` | `#f7f9fd` · `#edf1fa` |
| `--well` | `#131316` | `#eef1f8` |
| `--card` | `#121a33` | `#ffffff` |
| `--line` | `#2a2a31` | `#e4e9f1` |

Also drifting without inverting: `--dim` (8 values), `--amber` (6), `--mut`, `--red`, `--blu`,
`--cyan`, `--bg` (5 each).

**This is the collision the founder can see and nobody can explain from one page.** A page looks
uniform until it is opened on a second theme, because the name resolves against whichever family
won in that scope.

---

## Items 4 and 5 — deferred to cc#1970, and one thing I could not do

The resequencing says items 4 (body-appended surfaces) and 5 (mirror drift) are cc#1970's own
read-only reports and that this card should **cite** them rather than redo them.

**I could not find a cc#1970 report file in `reports/` to cite.** I am saying so rather than
quietly re-deriving it and presenting it as the citation. What I can contribute cheaply, as a
scale check on item 4: **`document.body.appendChild` appears at 56 call sites across 22 files**,
led by `mobile/home.html` (8), `v8_dashboard.html` (6), `pwa_endpoints.py` (5) and
`mobile/gvm.html` (5). Each of those is a surface that lands outside `.screen` and has historically
had to be added to the bridge by hand — cc#1828 `#scorrAnaOv`, cc#1848 `#alWrap`, cc#1941 `#dcOv`
and `#scorrChartOv`, cc#1965 next. 56 is the upper bound on how many more times that can happen.

---

## Item 6 — the proposal, costed per step so each can be approved on its own

**None of this is authorised by this card.** Ordered so that each step is safe on its own and each
makes the next one smaller.

### Step A — stop the bleeding. Ban new literal fallbacks. **Cost: 1 file. Risk: none.**

Add a check (the `theme_validate` tool already exists and already ratchets per file) that fails on
a **new** `var(--x, #literal)`. Nothing existing changes; the count can only fall from 844.
This is the only step with no regression risk at all, and without it every other step is a
treadmill.

### Step B — delete the fallbacks where the name is guaranteed defined. **Cost: ~8 files, ~500 of the 844. Risk: low, and testable per file.**

After cc#1970 the bridge is declared at `body[data-theme]`, so every descendant inherits it. A
fallback on a contract token inside a themed page is now dead code. Take the top files one at a
time — `pwa_endpoints.py` (128), `mobile/home.html` (121), `scorr_news.html` (65),
`v8_dashboard.html` (59), `scorr_card_common.js` (54) — and delete the literal, keeping `var(--x)`.
`theme_validate` measures the result per file, so a regression shows up as a number, not as a
report from a user.

**Do the two `--txt` groups first.** 64 white and 12 black under one name is the single worst
inconsistency found, and it is 76 edits.

### Step C — collapse the legacy families onto the contract. **Cost: ~24 files. Risk: HIGH — this is the one to gate hardest.**

Map `--bg --panel2 --txt --mut --dim --grn --red --blu --cyan --well --chalk --mute --gold --up
--down` onto their contract equivalents and delete the legacy declarations. This touches every app
surface at once and is the largest single-session regression risk on the platform, exactly as the
card's own gate says. **Do not attempt it before Step B**, because B removes the fallbacks that
would otherwise mask a mapping error — with the fallbacks still in place, a wrong mapping looks
fine on Gold Night and wrong everywhere else.

### Step D — collapse the `mobile_endpoints.py` mirror. **Cost: 2 files. Risk: medium.**

Two hand-maintained copies of the bridge that must agree forever is a standing defect; cc#1970's
own report is the input here and should be read first.

### Recommended order and why

**A, then B, then D, then C.** A costs nothing and stops the problem growing. B is measurable per
file and removes the mask. D is small and removes a permanent drift source. C is last because it is
the only irreversible-feeling one, and because doing it first means doing it blind.

---

## Reconciliation against the baseline (item 2(c))

`reports/theme_baseline_v1.json` — `sha`, `note`, `total`, `files`. The live `theme_validate` run
against the current tree reports **raw_total 1,061 against baseline_total 1,237** at
`baseline_sha 55fa94f`, i.e. **176 raw declarations removed since the baseline**, with two files
currently above their own baseline: `scorr_digest_mobile.html` (11 vs 9) and `scorr_v10_signal.html`
(3 vs 2). Those two are pre-existing and are not from cc#1970 or from any card in this drain — they
are named here so they are not attributed to it.
