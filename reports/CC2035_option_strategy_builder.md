# cc#2035 — Option Strategy Builder: sprint summary (cc#2036 → cc#2040)

Five cards, one feature: a payoff builder for NIFTY/BANKNIFTY options, Custom Builder + Readymade
(30 templates from the founder's 2012 Excel + 10 added in 2026), on both web and app. Design ref:
`previews/option_strategy_builder.html` @ ac7cc21 (Fable, founder-approved). DOM/API contract:
`session_log 45192`. Product spec: `session_log 45180`.

## The five shas

| Card | What | Commit (on `main`) |
|---|---|---|
| cc#2036 | `option_strategy_templates` (30 rows) + `option_strategy_engine.py` (payoff math, ported from Fable's own validated reference) + 34 golden tests | `a5133b9` |
| cc#2037 | `/api/options/{meta,chain,templates,resolve,payoff}` | `ad87f33` |
| cc#2038 | Web `/options` page + NAV-COMPLETE | `2a25ab2` |
| cc#2039 | App `/m/options` page + NAV-COMPLETE (shares cc#2038's `static/option_strategy.js`) | `3f56b87` |
| cc#2040 | Home grid tile + web Home link + this report + visual audit | *(this commit)* |

Every sha above is independently confirmed **on `origin/main`** via `git ls-tree`, not just "pushed"
— see each card's own `reports/CC203{6,7,8,9}_*.md` for that card's own landing verification.

## 34-test output (cc#2036)

```
$ pytest -q tests/test_option_strategy_engine.py
..................................                                       [100%]
34 passed in 0.04s
```
4 Excel golden cases (from `reports/CC2035_ref/engine.py`'s own `__main__`) + 30 `golden_30.json`
rows (breakevens, max_profit, max_loss, lot 65), `bounded_by_zero` folded into the same 30 nodes so
the pytest count matches this exact line rather than silently growing.

## Template count query (live DB, re-run for this report)

```sql
SELECT view, count(*) FROM option_strategy_templates WHERE is_active GROUP BY 1 ORDER BY 1;
```
```
 bearish | 10
 bullish | 10
 neutral | 10
```
`SELECT count(*) FROM option_strategy_templates;` → **30**. `... WHERE name ILIKE '%vertical%'` →
**0** (the Excel's "Vertical Spread" sheets are correctly seeded as "Ratio Backspread"/"Ratio
Spread", per `session_log 45180`'s own `validation_findings`).

## Curl-equivalent outputs (cc#2037's own verify list, reproduced here)

No live route to `curl` from this container (no scorr.in access) — these are the real shipped
endpoint functions, called directly with the DB socket stubbed to real rows pulled live via
`run_sql` (cc#2037's own report has the full methodology). Reproduced again here for the record:

- `GET /api/options/meta?underlying=NIFTY` → `lot_size: 65`, `strike_step: 50`,
  `expiries: ["2026-09-29"]` — **exact match** to this card's own stated expectation.
- `GET /api/options/templates?view=neutral` → **10 rows**, `sub_view` populated on all 10.
- `POST /api/options/payoff` with the golden Iron Condor legs (lot 65) →
  `breakevens: [23323.3, 23676.7]`, `max_profit: 4985.5`, `max_loss: 1514.5` — **exact match**.
- `POST /api/options/payoff` with 11 legs → Pydantic `ValidationError` → FastAPI 422.
- `grep include_router main.py | grep option_strategy` → exactly 1 line (`option_strategy_router`);
  the card's own literal `grep option` count is 4 because three OLDER, unrelated routers
  (`option_iv_history_router`, `option_chain_grid_router`, `stock_options_backfill_router`) already
  match that generic substring — stated in cc#2037's own report at the time, not a new finding.

## theme_validate for both files (cc#2038, cc#2039)

`theme_validator.count_raw`, run directly against each file (the MCP tool only reads the deployed
app's filesystem, unreachable pre-push from this container):

- `scorr_options.html` → **0** raw primitives (1 found and fixed during the build — `color:#fff` on
  the CTA button → `var(--on-accent)`, `scorr_web_tokens.css`'s own "text on an accent fill" token).
- `mobile/options.html` → **0** raw primitives (5 found and fixed — four `rgba()` literals reused
  across the stale banner / chip-on / breakeven-row tints, plus the `.ov` sheet backdrop, all
  copied in from the design ref/`trade_wall.html` without a token wrapper — declared as named local
  custom properties instead, e.g. `--ov-backdrop`, `--brand-bg-14`, then referenced via `var()`).

## Visual audit — re-fired on-demand, against the real deployed site, and it found a real bug

Item 4/5 asks to re-fire `visual_audit.py`'s own on-demand queue for `/options` and `/m/options`
and attach the result rows. This container has no route to scorr.in itself, but the on-demand
mechanism (`visual_audit_requests`, drained every ~60s by a separately-running Railway worker —
`scheduler_master.job_name='visual_audit_crawl'`, confirmed `active=true`) reaches the real,
**live, deployed** pages, so this is the one piece of this whole sprint verified against production
rather than a local harness. Four requests were queued (ids 226-229: both routes, both theme
families, each at its own natural viewport) and the worker drained all four within minutes.

**One request was a mistake on this seat's own part, not a page defect.** `/options` (web) was
queued with `theme='goldnight'`/`'aquawhite'` — the **app's** theme vocabulary. Both captures came
back with an **identical `content_hash`** (`c7d6005cf99f75b8`), which looked like a theme-switch
failure until `scorr_theme_boot.js` (the actual web-theme script) was read directly: its own
`ALLOWED` set is `{dark: 1, light: 1}` — "goldnight"/"aquawhite" are meaningless to a web page and
silently fall back to the default (`dark`), so both requests rendered identically **by design**,
not by bug. Re-queued correctly (`theme='light'`, id 230) rather than reported as a false finding.

**One request found a real, confirmed defect, now fixed.** `/m/options` under `aquawhite`
(capture 565) came back `has_check_fail=true` with a genuine near-invisible contrast failure on
`#optSpot` (`1.02:1` measured vs `3.0:1` required — fg `rgb(234,240,250)` on bg `rgb(233,243,247)`,
both very light, virtually unreadable) even though the rest of the same capture correctly showed
the light theme applied everywhere else (other elements' fg/bg pairs all switched correctly). Root
cause, found by reading the shared `mobile_app.css` (embedded in `mobile_endpoints.py` as
`MOBILE_CSS`) directly rather than guessing: it declares a **bare, unscoped `.v{background:
var(--panel2);border:1px solid var(--line2);border-radius:15px;padding:...;cursor:pointer}`** rule
for an unrelated card component, and `mobile/options.html` used the same bare class name (`#optSpot`,
`#optBE`, `#optMaxP`, `#optMaxL` all carried `class="v"`) — colliding with it. This local sandbox's
own stand-in `mobile_app.css` (built for cc#2039's own test, since the real file has no on-disk
copy to load offline) did **not** include this rule, which is exactly why cc#2039's own 19-check
harness never caught it — a real gap in that harness's fidelity, stated here rather than glossed
over. **Fixed**: renamed the class to `.optv` (namespaced like every other id on this page) in all
four places; re-verified `theme_validator.count_raw` still 0 and cc#2039's own 19-check harness
still all-met after the rename. The collision (background/border/padding/cursor) is conclusively
fixed by this rename; the *exact* mechanism behind the specific `rgb(234,240,250)` text-colour
reading is not fully reproduced from this container (the rule that collided sets no `color` of its
own) — re-queued (`/m/options`, both app themes, ids 231-232) against the fix to confirm, and this
is flagged for Fable's own live-page check as this sprint's one open item, not silently marked done.

One of the four original requests (id 228, `/m/options` + `goldnight`) hit a 20s navigation
timeout with no image; its sibling request on the identical route (id 229, `aquawhite`, moments
later) loaded and captured cleanly, and this session's own repeated local loads of the same page
never hung — read as a one-off transient (a cold container, a momentary network hiccup), not a
reproducible defect, consistent with this project's own "one flake, re-check don't chase" doctrine.

Full result rows for every capture id are in `visual_audit_captures`/`visual_audit_results`
(ids 226-232, 233 individual check rows on the first four alone) — a `SELECT * FROM
visual_audit_captures WHERE id IN (562,563,564,565)` (and its `visual_audit_results` counterpart)
reproduces everything summarized above, verbatim, against the real production pages.

## Gate check (this card's own)

"If 2038 or 2039 is not deployed, STOP and report — do not add a dead tile." Both are on `main`
(confirmed via `git ls-tree`) and this repo's own stated architecture is auto-deploy ~90s from every
push (`DEPLOY_GUARD=true`); substantial real time elapsed between those pushes and this card's own
work. Proceeded on that basis, stated here rather than silently assumed — the same "no route to
scorr.in" limitation that gates the screenshots above also means this container cannot
independently confirm live deploy status beyond that.

## Home grid tile + web Home link (this card's own build)

- `mobile/home.html`: `{ href: '/m/options', label: 'Option Strategy', web: false, svg: '...' }`
  added to the `Analytics` group, directly after `/m/check` (per this card's own instruction).
  Grid tile count: **25** (24 + this one, confirmed by count). `HIDDEN_GRID_TILES` untouched.
- `scorr_home.html`: a new `<a class="tile t-options" href="/options">` tile added in the same
  block as, and directly after, the `Check` tile (this card names Wall of Trades as the sibling to
  match; checked first and found WoT has **no** tile in this block at all — Check is the tile that
  actually is there, so its markup pattern is what was followed, stated rather than silently
  assumed). New `.t-options{--accent:#D4AF37}` rule, consistent with every sibling `t-*` rule's own
  literal-hex convention for this custom property (a declaration, not a raw-primitive use — exempt
  from `count_raw` either way).
- `app_route_map`: `/m/options.home_grid_group` set to `'Analytics'`, matching every sibling
  Analytics tile's own real column value (checked via `SELECT home_grid_group FROM app_route_map
  WHERE route IN ('/m/gvm','/m/screeners','/m/tcscan','/m/check','/m/v8')` before writing anything).

## Not done here (explicitly out of scope for the whole sprint)

Black-Scholes/Greeks on this surface (a different card, cc#2034, already ships that on the D-cockpit
chain — out of scope here per every one of the five cards' own text). Weekly-expiry loading.
Re-confirming the `.optv` fix's own capture (ids 231-232) is queued but its result depends on the
worker's next cycle after this commit deploys — a genuine follow-up, not assumed clean.

## The evidence this sprint actually produced

No route to scorr.in from this container for a `curl` or a manual look, throughout the whole
sprint — but this is the one sprint this session has run where the LAST card's own job (the visual
audit) reached the real, live, deployed pages anyway, and it is real evidence, not a local proxy:
**116 local functional checks** (34+25+38+19, cc#2036-2039, against the actual shipped code and,
wherever data was needed, the actual live DB) **plus 233 automated checks against the real
production pages** (contrast, tap-target size, overflow, empty/broken-page, theme-leak — this
sprint's own captures 562/563/565), which is how the one real defect above was actually found.
