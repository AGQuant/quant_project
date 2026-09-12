# Security fix — 4 unauthenticated write routes gated

Follows up on cc#1986's clash-hunt audit (log 6267, carried into a finding on cc#1199 at log
6385) — flagged, not yet fixed. Confirmed independently before touching anything, then fixed the
four clearest cases.

## Confirmed unauthenticated (before this push)

Read each function body directly — none called `_check_admin`, checked `ADMIN_TOKEN`, or gated
in any other way:

- `POST /api/position/open` (`scorr_endpoints.py:946`) — opens a Client/Test book position.
- `POST /api/position/close` (`scorr_endpoints.py:1000`) — closes a position, books P&L.
- `POST /api/v8/futures/add` (`v8_futures.py:85`) — adds symbols to the futures universe.
- `POST /api/v8/futures/remove` (`v8_futures.py:131`) — deactivates symbols from it.

All four are real writes to live tables (`client_positions`/`test_positions`/`*_closed`/
`futures_universe`) reachable by anyone who can reach the deployed app, with nothing else in the
route checking who is calling.

## Confirmed safe to gate — no UI depends on the open door

Repo-wide grep for the four literal paths across every `.html`/`.js` file: **zero matches**
outside `scorr_endpoints.py`/`v8_futures.py` themselves and reference docs. Nothing in the live
web app calls any of these four routes client-side, so adding an admin-token gate cannot break a
UI flow — these are internal/tool-call routes (the file's own comment on `position_open`/
`position_close`: "ONE tested code path for what Claude web used to hand-compute in freehand
SQL"), not public-facing ones.

## The fix

- `scorr_endpoints.py`: added a local `_check_admin(token)` — same logic as `main.py`'s own
  (`if not ADMIN_TOKEN: return True; if token != ADMIN_TOKEN: raise 403`), duplicated rather than
  imported to avoid a circular import (`main.py` mounts this file's router). Wired into
  `position_open` and `position_close` as their first statement, matching the convention used
  everywhere else in `main.py`.
- `v8_futures.py`: `add_futures`/`remove_futures` gated with the exact same check `sync_lots`
  already uses in this same file (`if x_admin_token != os.getenv("ADMIN_TOKEN"): raise
  HTTPException(401, ...)`) — file-local consistency, not a new pattern.

Both patterns already exist elsewhere in the codebase; this push applies the established
convention to four routes that had been missed, not a new auth mechanism.

## Not done in this push, stated plainly

- The `source_tag` routes (`scorr_endpoints.py:723,750,896`) are a DIFFERENT class — the file's
  own comment marks them "UI-only writes," and unlike the four above they only edit a label on an
  *existing* position, not create/destroy P&L rows. Left alone pending the broader sweep below —
  gating them without checking whether some other part of the app calls them without a token could
  break a real feature, and grep alone did not rule that out for these lower-severity, `Body`-only
  routes the way it did for the four above.
- A fuller, systematic sweep of every `POST`/`PUT`/`DELETE` route in the repo (not just the ones
  named in the original finding) is running separately to confirm whether the "23 orphaned write
  routes" figure from log 6267 names anything else this severe. This push covers the four already
  independently confirmed; more may follow from that sweep as a fast follow-up, each on its own
  merits (checked for a live caller first, same as here).
- Whether an edge/network layer (reverse proxy, Railway network policy) already covered any of
  this is unknown from inside this container, same limitation the original finder noted — this
  fix does not depend on that answer either way; it closes the application-level gap regardless.

## Verify

- `ast.parse` clean on both files.
- Grep-confirmed no `.html`/`.js` caller for any of the four routes, repo-wide.
- Fix mirrors an existing, already-used pattern in each file (`main.py`'s `_check_admin` shape for
  `scorr_endpoints.py`; `v8_futures.py`'s own `sync_lots` for the futures routes) — not a new
  mechanism to review from scratch.

Nothing under `worker/**`. Card not filed as a numbered cc_task (surfaced as a direct finding/fix
per the severity); logged on cc#1199 instead. Verification is still Fable's/the founder's per
standing practice — this is a claim until read back.
