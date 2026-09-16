# cc#2132 — hide QB Universe from nav; one "+ Build a Basket" flow on the QB page

## Gate
cc#2129 has `finished_at` (2026-09-16 09:41) and `commit_sha 9773302` — the four rule engines are
wired into `v12_backtest.py` and a first real backtest ran. Met. cc#2135 shipped first (`0ede955`)
so its Pool + Filters UI is **reused by embedding the real page**, not rebuilt (spec item 7).

## What shipped
- **NAV** (`pwa_endpoints.py` NAV array): the `QB Universe` stop is removed. `/v12` was never in
  the NAV array — cc#557 already folded it into the QB page ("QB-page button (removed from top
  nav)", NAV_REGISTRY kind `tab`). This card gives `/qb/universe2` the identical treatment:
  `NAV_REGISTRY` kind `nav` → `tab` (`main.py`), route kept alive, still `PROTECTED` and still in
  `_PWA_INJECT_PATHS` — exactly what cc#557's precedent did for `/v12`. Stated deviation from the
  spec's literal "remove from `_PWA_INJECT_PATHS`/`PROTECTED`": removing from PROTECTED would
  un-gate a logged-in page, and the injected chrome is what makes the page render themed when
  reached directly; the precedent kept both, so this card keeps both.
- **`quant_basket.html`**: the link out to `/v12` is replaced by one **`+ Build a Basket`** button.
  It opens an in-place panel with the stepper — exactly the founder's six, in his order:
  **Universe · Entry · Exit · Risk Management · Backtest · Deploy** (current step blue, prior steps
  green-ticked, matching the design ref). Step 1 embeds the REAL Universe builder
  (`/qb/universe2?embed=1` — cc#2123 + cc#2135, live pools and live pass-counts); steps 2–6 embed
  the REAL V12 builder (`/v12?embed=1&step=N`) at the matching panel, driven by `postMessage`
  (moving between steps 2–6 never reloads the frame). The child pages report their height so the
  frame fits the content. Both pages strip their own chrome under `?embed=1` via `main.py`'s
  existing `_is_embedded()` (the cc#1282 mechanism) — no new server code.
- **`scorr_v12.html`** (additive; presets untouched and re-verified):
  - Its own stepper is now the same six steps. **Risk management is a real step (p3)** carrying
    the two fields cc#2128 built and cc#2129 wired into the engine: **Basket cap (max stocks)** →
    `entry.max_stocks` (the same single input, moved out of Entry to where the founder named it)
    and **Sector cap (max weight %)** → the new top-level `risk.sector_cap_pct` that
    `v12_endpoints._validate_basket_def` now accepts. Residual split (cash/GOLDBEES/SILVERBEES)
    exists in `qb_risk_rules.py` but the backtest definition has no field for it — said on the
    panel, not shown as a dead control.
  - Exit gains **Hard stop % below entry** → `exit.hard_stop_pct` (cc#2127 → cc#2129) — a real,
    validated engine field the page never exposed.
  - Blank fields are omitted from the definition (the validator rejects nulls).
  - `applyPreset` also fills the new fields when a preset carries them (none does today).
  - Embed mode: header + own stepper hidden, `?step=N` opens on that panel, parent messages switch
    panels, height + current step posted back.
  - **Deploy = honest STOP.** The spec: wire to whatever `qb_endpoints.py` already uses to create a
    `scheduler_master` row for a new basket, and if none exists, STOP and report. Checked directly:
    `qb_endpoints.py` has no route that creates a basket or a `scheduler_master` row (its routes
    are `eod_check*`, `rebalance_*`, `nav/rebuild`, `seed/run`, `fix_allocations*`). The Deploy
    panel says so, names the missing endpoint, and points back to the backtest as the deliverable.
    Logged as a STOP on the card; nothing invented.
- **`scorr_qb_universe.html`**: embed height reporting only.

## Honest scope statement
The Universe step's embedded pool/filter explorer (cc#2135) and the V12 backtest's own universe
input (`universe_ref.filters`, the 12-field grid on V12's step 1, hidden inside this flow behind
the real Universe page) are **not yet joined**: the backtest still takes V12's filter grid (which
presets pre-fill), not the pool selected on the cc#2135 page. Joining them needs a `universe_ref`
that can reference a pool + CAT_1 filter set — an engine change, out of this nav + composition
card (do_not_touch: computation logic). Named here so it is a known gap, not a hidden one; it is
the natural next card.

## Verify
- `node --check` clean on every inline script in the three pages **and on the `PWA_JS` string**
  (the NAV array lives inside it) — the string no longer contains `'/qb/universe2'`, still
  contains `'/quant-basket'`. `ast.parse` clean on `main.py`, `pwa_endpoints.py`. Zero new
  `var(--x, #literal)` fallbacks in any line this card added (the ratchet's three regressions are
  the same pre-existing files — `mobile/v8.html`, `pwa_endpoints.py` (+2, from before this card;
  this card only removed lines there), `scorr_bell.js`).
- **Playwright, real pages + the real `V12_PRESETS` constant read from `v12_endpoints.py` by
  `ast`** (31 checks, all pass): V12 standalone — six steps in order, `e_max` gone from Entry and
  present in Risk with `r_seccap`, `x_hard` in Exit, Deploy names the missing endpoint; the real
  Alpha Multicap preset renders, "Use this preset" pre-fills `top_x=12` and `max_stocks=12` (now
  in the Risk panel) and lands on step 0 — **preset loading not regressed**; `buildDefinition()`
  carries `exit.hard_stop_pct=10`, `risk.sector_cap_pct=25` (top-level, per the validator) and
  `entry.max_stocks=12`, and omits them when blank; Run backtest POSTs `/api/v12/backtest` with
  the definition. V12 embed — header/stepper hidden, `?step=3` opens Risk, a parent
  `{v12step:2}` message switches to Exit, no page errors. QB page — old `/v12` link gone, button
  present, panel hidden until clicked then opens in place, six steps in order, step 1 frame src
  `/qb/universe2?embed=1`, step 4 → `/v12?embed=1&step=3` with 1–3 ticked done, step 6 keeps the
  same frame (message, no reload), a child height message resizes the frame. 375px: no
  horizontal overflow with the builder open.
- **VISUAL_VERIFY_GATE_V1** — three screenshots looked at: QB page with the panel open (button,
  title, six-step bar with green done ticks and the blue active step; the frame area is empty in
  the harness because `file://` cannot load the embedded routes — those are verified in embed
  mode separately above), the V12 page at step 5 with the preset card intact and the new
  "Next: Deploy" hand-off, and the QB page at 375px (stepper scrolls sideways, nothing clipped).

## Not touched
`qb_universe_builder.py` / `qb_entry_rules.py` / `qb_exit_rules.py` / `qb_risk_rules.py` /
`v12_backtest.py` / `v12_endpoints.py` logic; stored presets and their locked stats
(session_log 6086); every other nav entry; the six COMING categories.
