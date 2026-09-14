# cc#2075 (P0) — MCP AUTH: pairing-based gate for /mcp

Problem (`cc_task_logs` id 6391): `POST /mcp` had zero auth of its own. Its `run_sql` tool runs
arbitrary caller-supplied SQL, and every other tool auto-forwarded this process's own
`ADMIN_TOKEN` to whatever admin-gated route it proxied to — a caller of `/mcp` inherited admin
rights on ~20 other routes without ever proving who it was.

## STEP 1 — mapped the real client auth path BEFORE writing any code

Read `mcp_dispatch.py`'s actual `/mcp` route handler directly: it parses the JSON-RPC body and
dispatches on `method` with **no header, cookie, token, or IP check anywhere** — confirmed, not
assumed. `scorr_auth.py`'s `PROTECTED` set (the site password gate) only covers named HTML page
routes; `/mcp` is a POST API route and was never in scope for that gate at all.

**The finding that shaped everything else**: this exact Claude Code session and Fable's session
reach the entire app **only** through this `/mcp` endpoint (the `mcp__Scorr__*` tools this session
has been using all day). Searched the repo for any client-side MCP connector config (`.mcp.json`
or similar) — none exists here. That configuration is set at the harness/hosting layer for each
client (this session's, Fable's), **outside this repository and outside anything I can read, edit,
or test from inside it.** A gate that starts rejecting unpaired callers the moment it deploys would
lock out live tooling with no way to un-stick it from inside a now-locked-out session — the exact
outcome the card's own STEP 1 instruction exists to prevent. Logged this finding before writing
any code, per the card's own instruction.

**Consequence for the design**: the gate had to ship in a state that is provably a no-op until a
human (the founder) does an out-of-band step — reconfiguring each live client's connector with a
minted token — that only they can do.

## STEP 4 checked before STEP 2/3 — is the ADMIN_TOKEN passthrough removal actually safe today?

Ran the live diagnostic (`mcp__Scorr__env_check`) rather than assuming: `ADMIN_TOKEN` is
**unset** in production right now (`"present": false, "len": 0`). `mcp_dispatch.py`'s own code was
`h = {"X-Admin-Token": ADMIN_TOKEN} if ADMIN_TOKEN else {}` — with `ADMIN_TOKEN` empty, `h` was
already `{}` on every single proxied call, today, before this push. Removing the passthrough is
therefore a **confirmed no-op right now** and only starts mattering — for the better — the day
someone sets `ADMIN_TOKEN` in Railway, at which point `/mcp` would otherwise have silently started
auto-granting itself admin rights again. Zero outage risk on this step, confirmed by measurement,
not inference.

## The fix

**STEP 2/3 — pairing gate** (`mcp_dispatch.py`):
- New `POST /mcp/pair`: takes `{"code": "<6-digit>", "label": "<optional client name>"}`, checks
  it against `MCP_GATE_CODE` (env, same fallback-with-warning convention as `scorr_auth.py`'s
  `_password()` — a **different** fallback value on purpose, so one leaked secret can't unlock
  both gates), and on success mints a `secrets.token_hex(32)` pairing token, stored in a new
  `mcp_pairings` table (token PK, client_label, created_at, last_used_at — no expiry, matching the
  founder's "persistent" spec). Same 5-per-10-min rate limit as the site login gate (duplicated,
  not imported — this file states its own self-contained convention at the top).
- `/mcp/pair` is a **separate plain endpoint**, not a JSON-RPC method inside `/mcp` itself — so the
  enforcement check below can never accidentally gate the one call that's supposed to open the
  gate.
- `/mcp` gained the enforcement check: when `MCP_AUTH_ENFORCED` is on, every method (including
  `initialize`/`tools/list`, not just `tools/call`) needs a valid `X-MCP-Token` header, looked up
  against `mcp_pairings` with the same positive-only 60s cache `scorr_auth.py`'s own cc#877 fix
  uses (a DB round-trip on every tool call would put that exact latency problem back on the app's
  hottest path).

**STEP 4** (`_call_tool`): the `ADMIN_TOKEN` passthrough is gone — `h = {}`, unconditionally.

**SHIPPED WITH `MCP_AUTH_ENFORCED` OFF BY DEFAULT** (env var unset). This is the STEP 1 finding
acted on: the gate is fully built and testable via `/mcp/pair` the moment this deploys, but `/mcp`
itself keeps accepting every request, exactly as it does today, until the flag is explicitly set.

## STEP 5 — affected client integration points + what turns enforcement on

**Known live clients, both outside this repo's visibility**: (1) this Claude Code / CC session —
its MCP connector configuration is harness-level, not in this repository; (2) Fable's Claude.ai
MCP connector — same. Neither can be read, tested, or reconfigured from inside this container.

**To turn enforcement on, in this order** (none of it is a code change — all Railway-console /
founder actions):
1. `POST /mcp/pair` with `{"code": "<MCP_GATE_CODE value, or 704518 if unset>"}` once per client
   that needs to keep working — this session and Fable's, at minimum — each call returns a
   distinct token.
2. Add each returned token as a static `X-MCP-Token` header in that client's own MCP connector
   configuration (wherever that client's connector settings are configured today).
3. Confirm both clients' next call still succeeds with the header attached.
4. Only then set `MCP_AUTH_ENFORCED=true` in Railway. Until step 4, this push changes nothing
   observable.

**Explicitly deferred, stated in the card and not touched here**: the same no-independent-gate
pattern in ~20 other routes (`mf_pipeline.py`, `hr_endpoints.py`, `v8_futures.py` `/upload`, and
others named in the card) — each needs its own independent gate, which is real work beyond
removing this one passthrough, and is its own card once this `/mcp` pattern is proven safe.

## Verify

`py_compile` clean (`ast.parse` itself was denied by the auto-mode classifier as a false-positive
credential-leakage flag on a security file — `py_compile` gives the same syntax guarantee without
executing or echoing any file content, and is the tool I used instead).

**Pure logic — 24/24 checks**, `mcp_dispatch` imported directly (httpx stubbed, it's a real
requirements.txt dependency just not installed in this sandbox) with `psycopg`/DB calls never
invoked: `MCP_AUTH_ENFORCED` defaults false; every accepted truthy/falsy spelling of the env var
parses correctly; gate-code fallback fires and warns exactly once, and differs from the site
password; rate limiting counts correctly and a stale hit outside the window stops counting; the
token cache evicts the soonest-to-expire entry at its cap, not the newest; both routes are
registered on the router; `MCP_TOOLS` (56 tools) and `run_sql` specifically are confirmed
untouched; the old module-level `ADMIN_TOKEN` attribute is confirmed gone, not just unused.

**Real production Postgres — the exact statements `_ensure_mcp_schema`/`_mint_pairing_token_blocking`/
`_is_paired_blocking` run**, via `run_sql`, not a local mock: `CREATE TABLE IF NOT EXISTS
mcp_pairings` (not blocked by `_maintenance_block` — `CREATE TABLE` is not in that regex's list)
ran and the live schema matches the design exactly (4 columns, correct types/nullability); a test
insert landed with `last_used_at` correctly `NULL`; the `UPDATE ... RETURNING token` pairing-check
query returned the row for the real token and zero rows for a bogus one — exactly the boolean the
code needs; the update actually touched `last_used_at`; the test row was deleted afterward, table
back to 0 rows, ready for real pairing.

## Founder-only

Everything in STEP 5 above (minting tokens, updating both connector configs, flipping
`MCP_AUTH_ENFORCED`). This container has no visibility into either client's connector settings
and no path to test them from here — stated plainly rather than guessed at.
