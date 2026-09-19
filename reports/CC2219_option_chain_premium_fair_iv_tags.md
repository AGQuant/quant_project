# cc#2219 -- option chain strike-detail panel: Premium/Fair/IV tag rewiring

Frontend-only rewiring in `scorr_cockpit_card.js`'s `_dcChainDetailHtml`. No calculation change --
`deriv_metrics.py` and `option_ivp.py` are both untouched, confirmed by diff.

## What changed

- **Premium** tag now reads `o.tag` (deriv_metrics's own Black-Scholes premium/fair ratio verdict)
  via a new `_dcBsTagWord()` word map (Cheap / Expensive / **Reasonable** -- a distinct vocabulary
  from ivp's Cheap/Fair value/Expensive, kept in its own function per the card's explicit warning
  not to blur the two). Was: `ivp.tag` via `_dcTagWord()`.
- **Fair value** number now reads `o.fair` (Black-Scholes/RV20, tag-free). Was: `ivp.fair_value`.
  Null fallback rewritten to name the real condition (RV20 not computable) instead of reusing the
  ivp sentence, which described a different gate (60-session floor).
- **IV** gets a new small tag from `ivp.tag` (unchanged `_dcTagWord()` vocabulary). When `ivp.tag`
  is null, the "Not enough history yet to rate this strike" sentence now renders here -- the line
  it actually describes -- instead of where Fair value used to show it.
- Nothing else touched: Greeks, OI, DTE/as-of footer, wall/max-pain colours+legend, the (i) panel,
  market-closed notice -- confirmed untouched by diff.

## Verified against the real payload shape

Read `deriv_metrics.py._price_rows()` (lines ~1704-1733) directly: `tag = "EXPENSIVE" if prem>25
else ("CHEAP" if prem<0 else "REASONABLE")`, `row[key] = {..., "fair": ..., "tag": tag, ...}` --
confirms `o.tag`'s exact vocabulary and that `o.fair` is populated independently of the ATM+-5
eligibility gate that nulls `o.tag` (`fair = round(fair,2) if fair else None` is set unconditionally
per leg; `tag`/`prem_pct`/`ratio` are the only ones gated on `eligible`). This is why `fairLine`
checks `o.fair!=null` on its own, separate from `premiumLine`'s `o.tag!=null` check.

## Validation done in this sandbox

- `node --check scorr_cockpit_card.js` -- clean.
- Read `deriv_metrics.py` source directly to confirm `o.tag`/`o.fair`/`o.ivp.tag` field names and
  null-gating exactly match what the new JS reads (above).

## What could not be verified from this sandbox

No running server / no browser -- the founder's own screenshot (Claude-web cannot browse scorr.in)
is the live visual check, per the card's own verify list. DB spot-check of a strike where `o.fair`
and `o.ivp.fair_value` differ is also a live-page check, not something this sandbox can render.
