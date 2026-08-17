# APP_QA_R4 — EXECUTION RESULTS

**Package:** `reports/APP_QA_R4.md` @ `528a7e1` · **Ticket:** cc#1077 · **Executed:** 17-Aug-2026, production mode
**Refs built to:** `design_refs/scorr_home_themes_R1.html` @ `80da8c6` · `design_refs/scorr_appshell_R1.html` @ `a20ae0d`

All 12 pushes landed, strictly P1→P12, one commit each. Nothing in §D was touched: no engine, no
`worker/**`, no `/api/*` payload, no data wiring inside the three R5 pages, no `/m/intel` or its
nav label, no DECIDE-gated colour from cc#1072. cc#1076 stayed a separate claim and is done at
`a9ee34e`.

Every sha below is a claim, not a verification — Fable verifies diff + DB + founder device.

## The 12 pushes

| Push | SHA | Files | Verify output | Notes |
|---|---|---|---|---|
| P1 | `a4ccc03` | `scorr_themes.css`, `pwa_endpoints.py` | 3 token blocks compared block-for-block against the ref → **byte-identical**; route serves under `/static/` | Pattern answer: repo root + a route, same as `scorr_theme_r5.css`. There is no `static/` directory — `/static/` is a URL namespace. No page consumes it at P1. |
| P2 | `5cabce0` | `mobile/home.html`, `main.py` | All three themes resolve the ref's values **inside `.screen`**; choice survives reload with the pressed state matching; no page errors | Carries the bridge (see Findings 1). Dark moves to the ref's dark — deltas listed below. |
| P3 | `52bbc5b` | `scorr_appshell.css`, `scorr_v10_signal.html`, `pwa_endpoints.py`, `main.py` | Wordmark → `/m/home`; nav renders, nothing highlighted; diff is header + nav + 2 link tags | Shell shipped as a **shared** asset, not three copies. Class-prefix deviation, see Findings 2. |
| P4 | `acc2d2f` | `scorr_digest_mobile.html` | Same as P3 | `#dd-date` / `#dd-state` moved into the shell slot with ids intact, so every loader still finds them. |
| P5 | `b978d9b` | `scorr_gvm_fightcard.html`, `v10_page_endpoints.py`, `scheduler_health_endpoints.py` | Exactly **one** `GET /m/gvm2`, now in the page router; `scheduler_health` diff is deletion-only (17 removed, 4 added = pointer comment) | Variant B, GVM tab volt. Status slot left empty rather than invented. |
| P6 | `958fc50` | `mobile_endpoints.py` | Exactly **one** `GET /m/digest`, in `v10_page_endpoints.py` | Grep finding verbatim below. |
| P7 | `58b5947` | `mobile/home.html` | `grep "V10 VIEW"` across `mobile/` → **0** | Nav labels untouched; Intel rename parked. |
| P8 | `14beacd` | `mobile/home.html` | `/m/v10` tile present; 16 tiles, every href resolves to a live route | Web tile kept and relabelled so two tiles never claim the same words. |
| P9 | `7c923f7` | `scorr_theme_r5.css` | Linked and standalone titles both compute `rgb(234,240,250)`; eyebrows `rgb(53,224,255)`; chevrons keep pulse | cc#1074 absorbed. Cause was inheritance, not a purple rule — see Findings 3. |
| P10 | `1e116ca` | `mobile/home.html` | `grep -c "#FF9F45"` → **3** (0 before); `.irow .inm` keeps `flex:1`; diff is that file only, 3 lines | cc#1075 absorbed, exactly as specced. |
| P11 | `915308f` | `pwa_endpoints.py`, `main.py` | `/m/v10` in NAV array + NAV_REGISTRY; both routes already in `PROTECTED` (main.py:252, :254) | `_PWA_INJECT_PATHS` deliberately not touched — open question below. |
| P12 | this file | `reports/APP_QA_R4_RESULTS.md` | — | The artifact Fable verifies against. |

## P6 — the grep finding, verbatim

Two routes existed:

```
v10_page_endpoints.py:79   @router.get("/m/digest")  -> scorr_digest_mobile.html
mobile_endpoints.py:998    @router.get("/m/digest")  -> _page("digest")
```

**The old one did not win.** `main.py` includes `v10_page_router` at line 527 and `mobile_router`
at line 589, and FastAPI matches in registration order — so `v10_page_endpoints` has been serving
`/m/digest` since Fable added it. P6's condition ("if it exists **and** wins registration order")
was therefore not met, and no behaviour changed.

Removed anyway, deliberately: P6's verify asks for exactly one `GET /m/digest` in the codebase, and
a shadowed duplicate is a live hazard — any future change to include order hands the URL silently
back to the old page. `mobile/digest.html` stays in the repo, unreferenced by any route.

## Findings that changed how a push was built

**1 · The theme layer could never reach the app's content, and that is the cc#1066 symptom.**
`mobile_app.css` declares the palette on `.screen,.bnav` (framework 15913, "never bare `:root`").
A custom property set on a nearer ancestor wins, so anything declared on `:root` or `body` was
shadowed for every element inside `.screen`. Measured in Chromium on the live sheets: at `:root`
`--panel` was `#161F33` and `--grn` `#C8F542`, while inside `.screen` those same names still read
`#121A33` and `#2FD48B`. A switch on `<body>` alone would have moved nothing but the header. P2
adds a bridge re-declaring the legacy names on `.screen,.bnav`, sourced from the theme layer.

**2 · The ref's nav class names collide on a live page.** The ref names its nav `.bnav` / `.bn`.
`main.py` injects `/static/mobile.css` into every protected page and that sheet already defines
both — and the injection lands at `</head>`, *after* the page's own links, so on equal specificity
it wins. Ref-named classes would have silently taken the web sheet's styling instead of the ref's.
Every shell class is therefore prefixed `as-`. The visual spec is byte-faithful; only the names
differ, and they differ so the ref actually renders.

**3 · Nothing was painting titles purple.** cc#1074 reads as "remove purple from headings", but no
rule set a purple title. The theme paints every `<a>` pulse and the app wraps whole cards in
`<a class="card-link">`, so a title *inside* a linked card inherited pulse while the identical
title in an unlinked card rendered light — same class, two colours, decided only by whether a
parent happened to be a link. Fixed with one rule at the shared level. This also answers cc#1074
item 4 ("list which files carried the purple"): **none did.** A per-page sweep would have found
nothing to change.

## P2 — dark deltas, stated because the package asked for two things that conflict

P2 asks to "map the themed sections' colors to the new vars" **and** for "dark theme
byte-identical rendering to today". Those cannot both hold: the ref's dark values and home's
current in-scope values are different. Built to the **ref**, which is the palette every other
surface already shows. Measured inside `.screen`:

| token | before | after |
|---|---|---|
| `--bg` | `#0A0F1E` | `#0D1322` |
| `--panel` | `#121A33` | `#161F33` |
| `--panel2` | `#182241` | `#1C2740` |
| `--line` | `rgba(148,166,210,.14)` | `#26334F` |
| `--txt` | `#E9EEFB` | `#EAF0FA` |
| `--mut` | `#8C99BD` | `#8A97B0` |
| `--dim` | `#5E6B8F` | `#8A97B0` |
| `--grn` | `#2FD48B` | `#C8F542` |
| `--red` | `#FF5C6C` | `#FF4D6D` |
| `--blu` | `#4D7CFE` | `#7C5CFF` |
| `--cyan` | `#37D3E8` | `#35E0FF` |

`--mut`/`--dim` and `--line`/`--line2` converge because the ref carries one `--muted` and one
`--edge`. That is the ref's hierarchy, not a flattening introduced here. If dark was meant to stay
frozen and the switch only affect the two gold themes, say so and the dark block is re-pointed in
one push.

## Open question for Fable

**P11 · `_PWA_INJECT_PATHS`.** The push asks for `/m/digest` and `/m/v10` in that set. Not done,
and not silently: cc#874 keeps every `/m/*` path out of it on purpose, because `pwa.js` injects the
**desktop** navbar into `#scorr-nav` while these screens carry their own 5-slot bottom nav — the
one P3/P4/P5 just added. Adding them puts two navigations on one screen, which the comment at
`main.py:246` states in as many words that it is avoiding. If the intent was auth or asset-injection
coverage, `PROTECTED` already provides it. Confirm and it lands in a follow-up push.

## Carried DECIDEs (unchanged by this package)

Intel tab fate · web-tile keep or prune · theme count final (3 shipped, founder may cut).
