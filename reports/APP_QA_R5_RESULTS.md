# APP_QA_R5 — EXECUTION RESULTS · SPRINT 2

**Package:** `reports/APP_QA_R5.md` @ `1fb9aac` · **Ticket:** cc#1082 · **Executed:** 17-Aug-2026, production mode
**Refs built to:** `scorr_sprint2_goldnight_R1.html` @ `cd75e2c` · `scorr_digest_mobile_R2.html` @ `c77cae9` · `scorr_appshell_R1.html` @ `a20ae0d`

All 11 pushes complete, strictly P1→P11. Section D honoured: no engine, no `worker/**`, no
`/api` payload key changed (P4 turned out to need none), no P&L hue touched, no `scorr_themes.css`
token **value** edited, `/m/intel` label still parked, goldday/dark token blocks hidden not deleted.

Every sha is a claim until Fable verifies diff + DB + founder device.

## The 11 pushes

| Push | SHA | Files | Verify output | Notes |
|---|---|---|---|---|
| P1 | `8cc9436` | `mobile/home.html` | Fresh load = goldnight; a device carrying `scorr_theme='dark'` also renders goldnight **and still has `dark` in localStorage**; switch not displayed | Stored key read, never deleted. Switch hidden, not removed — one `display:none`. |
| P2 | `d153d7a` | `mobile/home.html` | Stat key 10.5px `rgb(140,122,63)` = gold-deep `#8C7A3F`; value 18px; tool 12/19px; nav 11px active `rgb(212,175,55)`. No size below 9.5px | Page-scoped deliberately — see Decisions 1. |
| P3 | `86e0bea` | `mobile/home.html` | `ok` class unreachable on the vix branch (grep → 0); chrome flat in both states | cc#1081 absorbed. Paste-before-change + item-4 answer below. |
| P4 | — **no commit** | none | 16 of 16 tape names carry a week value, 0 nulls, −7.01% to +4.61% | `week_chg_pct` already existed. See Decisions 2. |
| P5 | `ac13f78` | `scorr_digest_mobile.html` | DAY `-0.33/+0.32/+0.10`; WEEK `-1.18/+1.35/—`; back to DAY restores; switch measures 52×30; page floor now 9.5px | cc#1080 half B. Null week = em-dash, never 0. |
| P6 | `b504cc7` | `scorr_v10_signal.html`, `scorr_digest_mobile.html`, `scorr_gvm_fightcard.html` | All three resolve goldnight on a device carrying `dark`: `--field #0A0A0C`, `--panel #131316`, `--chalk #F5F2EA`, `--volt #3DD68C`; `dark` still stored | Default line only, as specified. |
| P7 | `bdcafb4` | `mobile/gvm.html` | goldnight resolves **inside `.screen`**; wordmark 59×44; GVM tab `rgb(212,175,55)`; `#sub` id intact | Scope issue present → bridge. Zero legacy hex already. |
| P8 | `859673f` | `mobile/check.html` | goldnight inside `.screen`; wordmark 59×44; Check tab gold | Variant B. |
| P9 | `619b212` | `mobile/models.html`, `mobile/qb.html` | models = active tab `rgb(212,175,55)`; qb = **no lit tab**, verified not assumed | Variant B / variant A split as specified. |
| P10 | `21355ba` | `scorr_theme_r5.css` | An **unmigrated** page and migrated home both render nav active `rgb(212,175,55)` at 11px, gold notch, tools 12/19px gold | One injected rule, not nine page edits — see Decisions 3. |
| P11 | this file | `reports/APP_QA_R5_RESULTS.md` | — | The artifact Fable verifies against. |

## P3 — paste-before-change, and the item-4 answer

The VIX chip logic exactly as it was:

```
class="hc tap" + (vix == null ? '' : (vix <= 14 ? ' ok' : (vix >= 17 ? ' bad' : '')))
```

`VIX <= 14` took the `ok` class — the app's green. That is the fear gauge celebrating while the
scoreboard read 4-of-4 failed. Now red at ≥17, neutral otherwise, `ok` unreachable in every state.

**Item 4, read-only:** VIX and PCR are **not** counted in the mood gate. The checks are exactly
ADR, Nifty Day, Nifty Week, Nifty Month (`v8_endpoints.py:755-760`) and `fails` counts only those,
so both chips are context-only. The card says drop the W/L prefix in that case — **there was
nothing to drop.** Probed in Chromium: `::before` content on `.hc.ok` and `.hc.bad` is `none`,
while on `.chip.g` it is `"W"`. The `W` in the founder's screenshot belonged to a neighbouring
**check** chip, not the VIX chip. Fable ratified this and voided the prefix clause.

**Open limitation:** the payload carries no VIX *change* field, only a level, so the
confirming-fear half of the rule is not live. Shipped red-on-≥17 alone; no change value invented
client-side. Completion is Fable's **cc#1083** — the P3 branch flips on automatically once
`vix_chg` reaches the hero payload.

## Decisions worth the reader's time

**1 · The raised scale is page-scoped, not in the shared sheet.** `mobile_app.css` sizes `.st`,
`.tool` and `.bn` for the whole app, and the goldnight ref is a *home* ref. Raising them in the
shared sheet would have silently re-scaled every unmigrated screen — the shared-vs-per-page
mistake in the other direction from the one Sprint 1 documented. P7–P9 migrate the other pages
deliberately, one at a time.

**2 · P4 needed no code, and that is the finding.** The card's premise — "no week column exists,
payload computes it" — is right about the column and wrong about the work. `global_heatstrip.py`
already does exactly what P4 specifies: it fetches `WEEK_SESSIONS + 1 = 6` daily rows per symbol,
takes `rows[5]` as the base, and emits `week_chg_pct`, `week_band`, `week_base_date` and
`week_sessions` on every tile (lines 226-230, 237). The null rule matches too — line 227 guards
`if len(rows) >= WEEK_SESSIONS + 1`, so under six sessions yields `None`, never 0 and never a
different window. `digest_v3._global_tape` passes it straight through. A second calculation in
`digest_v3.py` would have duplicated a live one and let the two drift.

Sample row, as cc#1080's verify asks:
`{"symbol":"BTC-USD","name":"Bitcoin","price":62827.93,"day_chg_pct":-0.33,"week_chg_pct":-1.18,"week_base_date":"2026-08-12"}`
— plus Brent day `+0.32` week `+1.35` base `2026-08-11`, WTI day `+0.08` week `+0.48` base `2026-08-11`.
Real values, no zeros-for-missing.

**3 · P10 is one injected rule, not nine page edits.** Applying the theme layer and a bridge to
every remaining nav-bearing template would be a full *migration*, not a polish sweep — it would
turn nine screens goldnight that are not in this sprint's founder review list, when P7–P9 name
four pages deliberately. One rule in `scorr_theme_r5.css` reaches every page through main.py's
existing injection, changes nav and tools only, and leaves those palettes where the sprint left
them. Migrated pages are unaffected: their page-scoped rule sets the same gold from `--t-brand`.

**4 · The `.screen` scope bridge, carried forward from Sprint 1.** Every migrated page needed it.
`mobile_app.css` declares the palette on `.screen,.bnav` (framework 15913), and a custom property
on a nearer ancestor wins — so tokens set on `body` are shadowed for everything inside `.screen`.
Without the bridge, forcing goldnight repaints the header strip and nothing else.

## Housekeeping done during the sprint

- cc#1081 marked **done** against P3 (`86e0bea`) — it had landed but was never closed.
- cc#1078 and cc#1082 were **not actually claimed**: the earlier `UPDATE` ran in a batch that
  errored on an unrelated column, so the status never changed. Re-claimed and corrected.

## Still open

- **cc#1078** — `STOPPED:`, awaiting Fable `RECO:`. `bg_ca_daily_note` missed 09:00 because a
  deploy landed on the tick (push at 03:29:42 UTC, ~90s deploy, job fires 03:30:00). The gate is
  correct; the fix is structural, three options proposed. **24 other jobs sit on the same
  exact-minute gates.** Today's note is not backfilled and will not be without a ruling.
- **cc#1083** — Fable's, completes the VIX rule.
- Carried DECIDEs: Intel tab · aqua-theme return timing · the founder's pending green comment
  (24214), now concrete since P2 moved home's money green `#2FD48B` → volt `#C8F542`.
