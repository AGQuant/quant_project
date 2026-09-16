# cc#2121 — Alerts table rebuilt to the founder's nine columns (P1 UI+ENGINE)

## What was built

**Backend, `/api/alerts/list`** (`trade_alerts_endpoints.py`): new `_fill_alert_columns(cur, r, st,
origin, lot_sizes)`, called once per approved row, computing `tag`/`tag_title`, `target`/`stop`/
`level_source` (surfaced from `resolve_close_state`, cc#1781 — not a second precedence chain),
`qty`/`qty_basis`, `cmp`/`cmp_ts`/`cmp_source`/`cmp_live` (instrument-aware per cc#2120 — a
futures-engine row reads `cmp_resolver.resolve_fut_cmp` first, falling back to spot only when no
futures bar exists), `unrealised_pnl`/`unrealised_pnl_pct`/`pnl_basis`, and `potential_left`/
`potential_left_pct`. `lot_size` is batched once (`futures_universe`, all approved symbols in one
query) rather than per row. Every number is computed server-side — the page's own subline promise
("computes nothing of its own") holds.

**Basis convention matched to the Wall of Trades' own locked rule (cc#1763), not invented in
parallel** — read `trade_wall_web.html`'s `pnlCell`/`rsCell`/`pctCell` before writing a line here.
Founder ruling, this card, verbatim: *"Qty 1 lot for Future, P&L data for future show in value
while Equity % (qty 1)."* A FUTURES/OPTIONS row is one lot (`futures_universe.lot_size`) and gets
a rupee figure; EQUITY is percent only, even when a rupee is technically computable. A futures
symbol with no `lot_size` row: rupee blank, percent still prints, labelled `no-lot` — never a
substituted qty of 1 or the engine's own position size.

`potential_left` reuses v8_dashboard.html's own formula **verbatim** (`~3786-3787`):
`(target-cmp)*qty` for LONG, `(cmp-target)*qty` for SHORT; percent divides by **CMP**, not entry —
matched exactly, including that choice of denominator, not approximated.

**Web table** (`trade_alerts_web.html`): rebuilt to `Symbol | Tag | Dir | Entry | CMP | Qty |
Date/Time | TC /100 | Unrealised P&L ₹ (1 lot) | Potential Left ₹ (1 lot)`. STATUS and TRIGGER
columns deleted (every row is APPROVED by construction since cc#2082; a signal-approved row's
TRIGGER was always an em dash). The Approved/Closed badge moves out of its own column: "Approved"
itself said nothing row-to-row so it is dropped, "Closed" survives as a small badge on the SYMBOL
cell so it stays visible even before any horizontal scroll. `tcAlertCell()` (cc#1693) is
repositioned, not reimplemented. `SHOW_ALERT_WHEN`/`SHOW_ALERT_NOTES` (cc#1693's hidden toggles,
both already `false` in production) are retired — Date/Time is now one of the founder's own
permanent columns; Notes stays exactly as before, a title= tooltip on Symbol.

**App card** (`mobile/alerts.html`): the founder's ruling applies here too, but "mirror on the app
screen" does **not** mean forcing a ten-column table onto the phone's own, deliberately different,
founder-approved idea-card UI (`/api/alerts/ideas`, cc#1620 APP_ALERTS_IDEAS_V1 — cards, plan
tiles, a track bar, a sparkline; "no engine or basket code anywhere on glass" is an explicit
founder rule on this exact screen). "Same server fields, same formulas, no second computation" is
honoured at the **data** level: `/api/alerts/ideas` gained the identical instrument-aware CMP fix
(it was calling `cmp_resolver.resolve_cmp_many` — spot only, for every row including futures
ones — a live gap matching the exact bug cc#2120 fixed everywhere else; now overridden per-row via
`resolve_fut_cmp` for a futures-engine signal, same honest-fallback pattern) plus `qty`/
`unrealised_pnl` computed with the exact same `v8_book_canon.unrealised_rupees` call and the same
lot-size batch lookup — not a second derivation. On the card itself, one small addition: a rupee
sub-line under the existing `since_pct` chip, shown only for a FUT/OPT card with a resolvable lot
(a `no-lot` card shows "no lot size" instead, never a fabricated rupee); an EQUITY card is
completely unchanged — still percent only, exactly as before this card.

## Verify

**Syntax**: `ast.parse` + `py_compile` clean on `trade_alerts_endpoints.py`. `node --check` clean
on every changed inline `<script>` block in `trade_alerts_web.html` and `mobile/alerts.html`.

**Hand-check, CAMS end to end** (per the card's own verify instruction), fresh live data this
session: entry 708.60 (futures, per cc#2120's re-stamp), qty 825 = `futures_universe.lot_size`
(confirmed directly, not assumed), target 690 (the sidecar/discretionary level — confirmed to win
over the engine's own 682.2, matching `resolve_close_state`'s documented precedence), CMP 710.55
(fresh `fyers_fut` 5m close, 13:15 IST).
- Unrealised = `(entry − cmp) × qty` for a SHORT = `(708.60 − 710.55) × 825` = **−₹1,608.75**
  (`v8_book_canon.unrealised_rupees`, same sign convention already audited in cc#2120).
- Potential Left = `(cmp − target) × qty` for a SHORT = `(710.55 − 690) × 825` = **+₹16,953.75**.

Both match the rendered cells to the rupee (screenshotted at −₹1,609 / +₹16,954 after normal
rupee rounding).

**Qty source, proven not assumed**: `_fill_alert_columns` reads `lot_sizes.get(r["symbol"])` —
`lot_sizes` comes from one query against `futures_universe`, and `v8_paper_positions.qty` is never
read anywhere in that function (confirmed by re-reading the diff, not by output matching alone).
On today's book the two happen to be identical — CAMS 825/825, DLF 950/950, LTM 150/150, all
checked directly against both tables — stated here exactly because identical outputs are not proof
of the right input on their own; the code path is.

**Equity rows**: `is_equity` routes to `qty=1`, `qty_basis='equity'`, `unrealised_pnl=None` (only
`unrealised_pnl_pct` is set) unconditionally — grepped `trade_alerts_web.html` and
`mobile/alerts.html`: the only rupee formatter call sites are inside the `pnl_basis==='one-lot'`
(web) / `qty_basis !== 'equity'` (app) branches, so an equity row cannot reach a rupee cell by
construction, not by a runtime check alone.

**DLF/LTM Potential Left**: both resolve via `resolve_close_state`'s **engine** tier (no
discretionary sidecar row for either, confirmed) — DLF target 611.59, LTM target 4246.66, both
matching the card's own cited values exactly. Neither renders blank.

**No-lot case**: a futures symbol with no `futures_universe` row renders the rupee cell blank, the
percent cell populated and labelled ("no lot size · %" web, "no lot size" app) — verified in both
harnesses with a constructed `no-lot` fixture, never a substituted qty of 1.

**Rupee header labelling**: `Unrealised P&L ₹ (1 lot)` / `Potential Left ₹ (1 lot)`, title
attribute stating the basis — matches the TC Scanner Closed Book's own header + title
(`v8_dashboard.html ~9974`) verbatim in wording, extended to name the Equity exception explicitly.

**Cross-surface check**: CAMS's Unrealised P&L here (−₹1,608.75 at CMP 710.55) uses the identical
resolver (`resolve_fut_cmp`) and identical math (`unrealised_rupees`) as the Wall's own
`pnl_approved` (cc#2120 Part B) — both now source CMP the same way for the same row, so the two
figures move together by construction rather than by coincidence; they were not compared at the
exact same instant (prices move every 5 minutes) but the FORMULA and INPUT SOURCE are provably
identical by reading both call sites, which is what "must agree" actually requires.

**Real-code Playwright harnesses, both surfaces**, real functions extracted verbatim, real
production values (all queried this session): web — 10-column header order, CAMS row exact
arithmetic, NATIONALUM (real CLOSED row, id=23) showing REALISED P&L (−₹29,250, title says
"realised" not "unrealised") with Potential Left correctly blank, one clearly-labelled synthetic
EQUITY row (RELIANCE — **no real approved-and-visible equity alert exists today**, see below)
confirming percent-only/qty-1. App — CAMS/DLF real rupee lines, RELIANCE equity card unchanged
(one chip only), a synthetic no-lot future confirming the honest fallback. Zero page errors in
either harness.

**VISUAL_VERIFY_GATE_V1 — measured, not eyeballed, at 360/375/390px** (web): only **Symbol / Tag /
Dir / Entry** are reachable without horizontal scroll; **CMP is cut off by ~3px** at all three
widths (the closest column to fitting) and **Qty / Date-Time / TC /100 / Unrealised P&L /
Potential Left all require the scroll**, including the two columns the founder most wanted. The
`SCROLL →` affordance (matching the TC Scanner tables' own wording/class) is present and visible
at all three widths — added this card, since none existed on this page before. This is a real,
stated limitation, not glossed over: nine-plus-symbol columns do not fit a phone width, and the
founder's own two money columns sit at the far end of the scroll. Screenshots taken and looked at
directly: desktop shows all ten columns with correct values/colours; mobile (375px) shows Symbol
through CMP before the cut.

## Real-data gaps, stated plainly
- **No real approved EQUITY row currently renders on the web Alerts page.** The one approved
  equity-instrument alert in `trade_alerts` (id=18, HBLENGINE, `source_engine='qb'`) has
  `kind='rebalance_due'`, which `load()`'s own pre-existing client-side filter drops before it
  would ever reach the table — unrelated to this card, unchanged by it. The equity-column
  behaviour (qty 1, percent only) is verified against a clearly-labelled synthetic row instead,
  stated as such rather than presented as a live figure.
- The app screenshot's design tokens (`mobile_app.css`) are not present as a static file in this
  checkout (server-generated); colours were assembled from the two token files that ARE present
  (`scorr_themes.css`'s real goldnight block + `scorr_theme_r5.css`'s goldnight role-alias
  override) and are a reasonable approximation for anything not directly traceable — this affects
  only cosmetic colour, never the values being verified.

## What did NOT change
`cmp_resolver.SPOT_SOURCES`, `resolve_cmp`, `resolve_cmp_many` — untouched this card (confirmed:
`cmp_resolver.py` does not appear in this push's diff at all). `resolve_close_state()` (cc#1781)
and its precedence — read and reused, not rewritten. `v8_dashboard.html`'s Potential Left formula —
read and reused verbatim, not retyped. The cc#2027 approve flow, the approval window gate, and
`ALERTS_PURE_DISPLAY_V1` (cc#2084) — no Approve/Dismiss/close control exists on either surface;
grepped both files to confirm zero such markup remains. The app's plan tiles / track bar /
sparkline / footer — all untouched, this card only added one line inside `cardHead()`.
