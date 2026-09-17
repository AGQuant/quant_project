# cc#2190 — /m/fpc output sheet: Scorr baskets only, and the eight cards the backend already serves

Built 17-Sep-2026 (founder 15:02 IST; Fable rescoped 15:12 IST, log 6997: display only + one constant swap); the landing time is in the task row.

## What shipped

| File | Change |
|---|---|
| `fpc_app_mobile.py` | The one backend edit: `BASKETS` / `BASKET_LABEL` now name the Scorr quant baskets from the live registry, per the card's mapping — Conservative → Large Cap, Model Portfolio, Contra Value; Balanced → Alpha Multicap, Large Cap, Mid Cap; Growth → Mid Cap, Small Cap, 52-Week Breakout. The registry query and `compute()` are untouched. `grep -i finz` on the file: 0 hits. |
| `mobile/fpc.html` | `s3()` (the output sheet) rebuilt to the card's eight items, all from the existing `/api/mobile/fpc/calc` payload: 1 headline as before; 2 FINANCIAL FITNESS = the score /100 with a bar, then four pillar chips Emergency · Debt · Savings · Protection each /100 with its target line (emergency target, "EMIs against take-home", saving % of take-home, cover target); 3 RETIREMENT = Spending then · Corpus needed · What you will have · Gap or Surplus (a zero gap reads "On track", a surplus is labelled Surplus), the from-assets / from-SIP split and the cash-counted note; 4 YOUR SIP = exactly three lines What it takes / What you have / Verdict, then the alternatives as two small cards (Retire later · Age N, Spend less · ₹N /mo) when present; 5 the goal card only when a goal is set; 6 YOUR PROFILE as before with the why line; 7 WHERE TO PUT IT = the Scorr basket rows (`fpc-bk`, 44 px+, holdings · rebalance) each a tap into the basket's page, the suggested-split sentence under them; 8 WHAT WE ASSUMED behind one tap (Show › / Hide ‹). New classes are `fpc-` prefixed. `grep -i finz` on the template: 0 hits. Steps 1 and 2 untouched (cc#2191 / cc#2192). |
| `reports/CC2190_fpc_output.md` | This report. |

**Item 7's link.** The card says `/m/baskets#<basket>`; no such route exists (`app_route_map`, main.py). The live basket detail page is `/m/qb?basket=<name>` (Baskets (mobile), `mobile/qb.html`), so the rows point there — the page the founder already reaches from the Baskets tile.

**Registry check, 17-Sep:** all seven mapped baskets are active with open positions — alpha_multicap 16, large_cap 16, mid_cap 12, model_portfolio 20, small_cap 14, breakout_52w 6, contra_value 1 — so every profile's three rows show holdings and "monthly" / "quarterly" from the registry.

**Web parity (spec item 2 / verify 2).** Fable verified from the code that the mobile endpoint already returns every output the web FPC shows (log 6997 and `fable_verified_backend_1512`); this card renders those fields — same backend call, same numbers by construction. The sandbox cannot open scorr.in, so the side-by-side screenshot is a live check for Fable.

## Harness (Playwright, real Chromium, `scratchpad/cc2190_test.py`) — ALL PASS at 375×812, goldnight + aquawhite

`/api/mobile/fpc/calc` stubbed with a payload in the endpoint's own shape (Balanced profile, fitness 61, gap ₹1.67 Cr, SIP extra ₹9,200 affordable with two alternatives, three Balanced baskets, five assumptions), then a second payload (short by ₹12,000, a goal, a surplus).

- Step 3 reached through the real Next → See my plan taps; no "finz"/"FINZ" anywhere in the rendered text.
- Card order: Financial fitness → Retirement → Your SIP → Your profile → (Where to put it) → What we assumed.
- Fitness: 61 with a 61 % bar; pillars 80 / 100 / 35 / 30 each "/100"; target lines "Target ₹3.00 L set aside", "EMIs against take-home", "Saving 30% of take-home", "Cover target ₹1.20 Cr".
- Retirement: "Gap ₹1.67 Cr"; on the second payload "Surplus ₹12.0 L".
- SIP: exactly three lines — What it takes ₹9,200 more a month / What you have ₹30,000 spare a month / Verdict You can do this; alternatives "Retire later · Age 63" and "Spend less · ₹6,000 /mo"; second payload "Short by ₹12,000 a month" and the goal card "Your goal · education".
- Where to put it: Alpha Multicap, Large Cap, Mid Cap → `/m/qb?basket=…`, "16 holdings · monthly rebalance", rows 47–48 px, no side bar; the suggested-split sentence under them.
- Assumptions: 5 items hidden until "Show ›", visible after the tap; no sideways overflow; no page errors on either theme.

**Screenshots looked at** (`scratchpad/cc2190_*.png`): `375_dark_output` (full page) — the 78 headline, the three hero cells, FINANCIAL FITNESS with the gold bar and the four chips (green 80 and 100, red 35 and 30), RETIREMENT with the red ₹1.67 Cr gap, YOUR SIP with the three lines and the two small cards, YOUR PROFILE, WHERE TO PUT IT with the three basket rows and chevrons, WHAT WE ASSUMED opened. `375_light_output` — the same sheet on Aqua White from the profile card down: white panels, the three basket rows, the opened assumptions.

Observation outside this card: the suggested-split sentence (class `.more`) renders in a monospace face on this page — a shared-CSS bare-selector leak of the cc#2166 family, not touched here.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. `POST https://scorr.in/api/mobile/fpc/calc` with any inputs → `baskets.rows[].basket` are Scorr names (alpha_multicap …), no finz_* anywhere.
2. `https://scorr.in/m/fpc` → step 3 on the phone: the eight cards above; tap a basket row → `/m/qb?basket=…`; compare the corpus / SIP figures with the web FPC for the same inputs (same endpoint).
