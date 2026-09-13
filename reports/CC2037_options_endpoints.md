# cc#2037 — /api/options/* endpoints (OPT sprint 2/5)

Backend wiring wrapping cc#2036's just-landed `option_strategy_engine.py` + `option_strategy_templates`
table: `option_strategy_endpoints.py`, one `APIRouter`, one import + one `include_router` line in
main.py (wiring only, per this card's own `do_not_touch`). Five routes exactly as
`session_log 45192`'s own API contract names them, plus the two one-line placeholder pages
(`/options`, `/m/options`) that let cc#2038/cc#2039 build real pages against a router that already
answers.

## Reused, not invented — the card's own instruction

- **Spot mapping**: `deriv_metrics._INDEX_OPT_ROOT` / `_INDEX_OPT_SPOT` — the exact NIFTY/BANKNIFTY
  → `option_chain.underlying` / `cmp_prices.symbol` maps `strike_chain()` (cc#1576) already
  established. Checked directly against the live tables before writing anything: `option_chain`
  really does store `underlying='NIFTY'/'BANKNIFTY'`, and `cmp_prices.symbol='NIFTY'` is a
  **different, long-abandoned key** (last updated 10-Jul) from `'NIFTY50'` (fresh, 11-Sep) — using
  the wrong one would have silently served a two-month-stale number as live. `_INDEX_OPT_SPOT`
  already points at the right one; this file imports it rather than re-deriving the mapping.
- **Staleness clock**: the same naive-IST "now" construction (`utcnow()+5:30`) `gvm_market_endpoints._ist_now()`
  already uses — `option_chain.ts` is naive IST, and comparing it against a tz-aware or raw-UTC
  `now` is the exact "phantom 330 minutes" bug class this codebase has hit and documented before
  (`mobile_home2.py`). Stale is gated to the 09:15–15:30 IST session window only; a quiet chain
  outside it is expected, not a defect.
- **Payoff math**: 100% `option_strategy_engine.py` (cc#2036) — this file does zero pricing of its
  own, only DB reads + request/response shaping.
- **Auth**: checked `trade_wall_endpoints.py` as instructed — its `/api/tradewall/*` routes carry no
  bespoke auth of their own (this app's login gate is a page-level middleware keyed off `PROTECTED`,
  not an API-level check). Matched exactly: no auth added to `/api/options/*`.

## The five routes

- **GET /api/options/meta** — `lot_size` from `futures_universe` (is_active), `strike_step` (fixed
  50/100, session_log 45180's own numbers, not derived from any table), `expiries` from
  `option_chain` DISTINCT, `spot` from cmp_prices via the mapping above (**no raw_prices fallback**
  — session_log 45192 asks for null-on-unavailable here, a stricter rule than `strike_chain()`'s own
  dual-fallback), `atm` = the strike minimizing `|ce.ltp − pe.ltp|` at the latest tick (put-call
  parity's convergence point — a different, more precise notion than "nearest to spot").
- **GET /api/options/chain** — latest tick per strike for the given (or nearest) expiry, `stale`
  gated to the session window as above.
- **GET /api/options/templates?view=** — straight read of cc#2036's table, `is_active`, ordered by
  `view, sort_order`.
- **POST /api/options/resolve** — a template's abstract `{kind,side,offset_steps,qty}` legs resolved
  around the live ATM into real strikes (`atm + offset_steps*strike_step`), premium filled from the
  same chain tick or left `null` (never fabricated). A FUT leg's "strike" (= entry price per
  session_log 45192) defaults to this same request's live spot when the template doesn't supply one
  — a real, already-fetched, standard small-basis proxy, stated here rather than silently assumed;
  premium forced to `0` for FUT regardless of input, per spec.
- **POST /api/options/payoff** — Pydantic-validated (`<=10` legs, `kind∈{CE,PE,FUT}`,
  `side∈{BUY,SELL}`, `qty 1..50`, strike/premium required for CE/PE, FUT strike defaults to the
  request's own `spot`, FUT premium forced to 0) then handed straight to
  `option_strategy_engine.price_strategy()`. Any violation raises a Pydantic `ValidationError`,
  which FastAPI turns into a 422 automatically — no bespoke error-handling code needed for that
  contract line.

## Verify — the real functions, real live data, not reimplemented

`ast.parse` clean on both files. No DATABASE_URL is reachable from this sandbox (only the `run_sql`
MCP tool reaches the live Railway DB), so end-to-end verification used the actual shipped functions
from `option_strategy_endpoints.py`, imported directly, with only the DB **socket** stubbed — a fake
cursor that plays back **real rows pulled from the live DB via `run_sql` moments earlier** (the
actual NIFTY chain tick, `cmp_prices`, `futures_universe`, and the Iron Condor template), not
synthetic fixtures. Every query string, branch and response line executed is the real shipped code.

**25/25 checks pass**, including three independent real-data cross-checks:
- `meta.atm.strike` computed from the live chain (`min|ce−pe|`) lands on **23500** — the exact same
  ATM `golden_30.json` (cc#2036's own fixture) states, computed a completely different way here.
- `resolve` on the Iron Condor template reproduces strikes **[23300, 23400, 23600, 23700]** with
  premiums **[156.25, 191.55, 171.95, 130.55]** pulled live from the chain — **identical** to
  `golden_30.json`'s own Iron Condor legs, confirming that fixture really was built from this same
  chain tick (`as_of: "... 2026-09-11 15:35 IST"`), and that resolve's offset arithmetic is correct.
- Feeding those real resolved legs into `/payoff` reproduces **breakevens [23323.3, 23676.7],
  max_profit 4985.5, max_loss 1514.5** — this card's own stated verify line, now proven via the
  actual resolve→payoff composition on live data, not just the engine in isolation (cc#2036 already
  proved the engine alone).
- `GET /api/options/templates?view=neutral` checked directly against the live table (identical SQL):
  **10 rows, `sub_view` populated on all 10** (`range`/`breakout`) — matches this card's verify line
  exactly.
- All eight validation-rejection cases (11 legs, bad kind, bad side, qty 0, qty 51, missing CE
  strike) raise `ValidationError` → FastAPI 422, and the FUT-defaults-to-spot behaviour is confirmed
  in isolation.

`main.py`: exactly one new `include_router(option_strategy_router)` line (confirmed via
`git diff --stat main.py` → `+2` lines total: one import, one include_router — nothing else
touched). Note on the card's own verify command (`grep include_router main.py | grep option -> exactly
1 line`): three OTHER pre-existing routers already match that same generic `option` substring
(`option_iv_history_router`, `option_chain_grid_router`, `stock_options_backfill_router`), so the
literal command returns 4, not 1, regardless of what this card does — the actual, meaningful check
(this router's own line appears exactly once, nothing else in main.py changed) is confirmed above.

## Small incidental fix (cc#2036's own seed script, not this card's scope)

While researching this card's own jsonb writes, noticed `option_strategy_templates_seed.py`
(cc#2036) passed `json.dumps(legs)` — a plain string — into a `jsonb` column via a parameterized
placeholder with no explicit cast, relying on Postgres's implicit text→jsonb assignment cast rather
than the explicit `Json(...)` wrapper this codebase already uses everywhere else for this exact
purpose (`scheduler.py`, five other files). The live table's data is unaffected (the actual seeding
ran via hand-generated literal SQL with explicit `::jsonb` casts, already verified against the real
table in cc#2036's own report) — this only hardens the **checked-in, re-runnable script** for a
future run. Changed to `from psycopg.types.json import Json` / `Json(legs)`, matching the
established convention exactly; re-verified `build_rows()` still produces 30 correct rows.

## Not done here (explicitly deferred)

The real `/options` and `/m/options` page templates (cc#2038 web, cc#2039 app) — this card ships a
one-line placeholder only, per its own scope. Black-Scholes/Greeks on this surface (out of scope;
cc#2034 already owns that, a different surface). A live `curl` against scorr.in — no route to prod
from this container; the real-function-plus-real-data harness above is the evidence available here.
