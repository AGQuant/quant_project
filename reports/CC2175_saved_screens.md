# cc#2175 — Custom screener: Save a screen by name, and My screens

Founder review 17-Sep-2026 12:21 IST ("Show names doesn't make sense — it should be Save or Clear. For Save there should be an option to save by name, and it will appear under another button."). Built 17-Sep-2026, 13:08–13:35 IST.

## What shipped

| File | Change |
|---|---|
| `custom_screener_app.py` | Appended block: the `custom_screens` table (created from the router's startup hook, retried lazily on the first save / list), `POST /api/mobile/custom_screener/save`, `GET /api/mobile/custom_screener/saved` (live count per screen, `?counts=0` for the number only), `GET /api/mobile/custom_screener/saved/{id}`, `DELETE /api/mobile/custom_screener/saved/{id}`. `/run`, `/meta` and the registry are untouched. |
| `mobile/screeners.html` | Footer is now Clear / Save / Show names; Save opens an inline name prompt (44 px input, prefilled with the applied words shortened, Save / Cancel); a duplicate name asks before overwriting; a toast confirms "Saved as <name>"; the header and view title read "My screen: <name>" while the chips still show that screen; a My screens card under Build your own with the count; tapping it opens the shared sheet listing each saved screen (name, applied words, live count, Delete with a Keep / Delete confirm, also reachable by a long-press); tapping a row opens `/m/screeners?view=custom&saved=<id>` pre-filled and pre-counted. Share moved from the footer to a link on the count line. |
| `tests/test_custom_screens.py` | Four no-DB tests (schema text, name rules, selection validation through the registry, a rejected save never touches the database) and one real-DB test that saves, reads back with a live count equal to `/run`'s, and deletes (skips without `DATABASE_URL`). |
| `reports/CC2175_saved_screens.md` | This report. |

## Decisions stated on the card

- **Owner.** The app has one password and no user table (`scorr_auth`: `SCORR_AUTH_PASSWORD`, a session token per login, no identity behind it). Every saved screen belongs to the one app identity `APP_OWNER = "scorr_app"`. The `owner` column exists so a per-user login later needs no migration; only `_owner()` changes.
- **Table.** `CREATE TABLE IF NOT EXISTS custom_screens (id serial PK, owner text NOT NULL, name text NOT NULL, selection jsonb NOT NULL, created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(), UNIQUE (owner, name))`. It runs from `@router.on_event("startup")` (the cc#879 pattern: after the app is up, never per request, never at import where the DB is often unreachable on a cold boot) and again lazily from the first save / list if the hook failed. No `ALTER`, ever. No Railway console step.
- **Validation.** A saved selection goes through `parse_selection` on the way in (unknown key or button → `{error}`, nothing written; nothing selected → error) and again on the way out, so a button retired from the registry later can never break `/run`. The live count is `run_sql(selection, limit=0)`, the same statement `/run` uses, so a saved screen's number is the custom view's number for the same chips.
- **Duplicate name.** `POST /save` without `overwrite` on a taken name returns `{exists: true, id, name}` and writes nothing; the app asks "A screen named X already exists. Overwrite it with these chips?" and re-posts with `overwrite: true` (an upsert on `(owner, name)`).
- **Show names stays.** It is the way to see the results (not "show more"); the footer is Clear / Save / Show names as the spec asked. Share (cc#2160) was in that footer; it is now a link at the right of the count line, shown once a chip is on, so nothing from cc#2160 is lost.
- **Delete.** A visible Delete button on each row (discoverable) that turns the row into "Delete <name>? Keep / Delete"; a 550 ms long-press on the row does the same. A swipe gesture was not built: on a bottom sheet a swipe fights the sheet's own scroll.
- **URL state.** `?size=…` (cc#2160) still works and wins over `?saved=` when both are present. After a save, or when a screen is reopened, the URL carries `saved=<id>` plus the filter keys; changing any chip (or Clear) drops the "My screen" marker and the `saved=` id, so the header never claims a screen the chips no longer show. An unknown or deleted id says "That saved screen is gone." and falls back to the plain custom view.
- **Empty state.** No saved screens → the card line reads "No saved screens yet. Save one from the custom screener." and the sheet says so too; never a placeholder.

## Harness (Playwright, real Chromium, `scratchpad/cc2175_test.py`) — ALL PASS at 375×812, goldnight + aquawhite

Steps checked on both themes, with an in-memory stub of the four endpoints (the stub records every POST body and DELETE id):

1. Custom view: footer buttons `Clear / Save / Show names`; Save disabled and Share hidden until a chip is on; Share lives on the count line.
2. Large + Mid → count 247, Save enabled, Share shown.
3. Save → the prompt opens right above the footer (prompt bottom = footer top = 693 px, footer above the nav at 754 px), input 44 px, prefilled `Large/Mid`, focused.
4. Name "Big names" → POST body `{name: "Big names", selection: {size: [large, mid]}, overwrite: false}`; toast "Saved as Big names"; header "My screen: Big names", view title "Big names"; URL `?view=custom&saved=1&size=large,mid`.
5. Same name again → the overwrite ask; Overwrite → POST with `overwrite: true`, toast again.
6. A chip change → header back to "Custom screener", `saved=` gone from the URL.
7. List view: the My screens card reads "1 saved screen"; tap → the shared sheet: "My screens · 1 saved screen · live count each · tap to open", row "Big names / Size: Large / Mid / 247 names / Delete".
8. Tap the row → `?view=custom&saved=1`: Large + Mid on, 247, header "My screen: Big names".
9. `?view=custom&saved=1&gvm=good` → GVM Good only, count 366, header "Custom screener" (filters win).
10. `?view=custom&saved=99` → toast "That saved screen is gone.", plain custom view.
11. Delete → "Delete Big names? Keep / Delete"; Delete → `DELETE /saved/1` sent, row gone, sheet says "No saved screens yet…", card line "No saved screens yet. Save one from the custom screener.".
12. No sideways scroll, no page or console errors on either theme.

**Screenshots looked at** (`scratchpad/cc2175_*.png`, dark and light): `prompt` — the "NAME THIS SCREEN" box with `Large/Mid` selected in a gold-edged input, Cancel / Save, sitting on the Clear / Save / Show names footer. `toast` — "Saved as Big names" pill, header "MY SCREEN: BIG NAMES", title "Big names", subtitle "Your saved screen. Tap a chip to change it.", Large + Mid lit. `overwrite` (light) — "NAME IN USE — A screen named Big names already exists. Overwrite it with these chips?" with Cancel / Overwrite. `my` — the list view dimmed under the sheet: "My screens", "1 saved screen · live count each · tap to open", the row with 247 names and a Delete button, the footer note about live counts; the My screens card visible above with "1 saved screen". `reopen` — the custom view with the saved chips and the name in the header. `delete` — the row turned into "Delete Big names?" with Keep and a red Delete. After the first run the toast sat over the footer buttons; it now sits above the footer.

## Unit tests

`tests/test_custom_screens.py`: 4 passed, 1 skipped (the real-DB one, no `DATABASE_URL` in the sandbox). `tests/` as a whole: green apart from the DB-gated skips.

## Live checks (Fable — the sandbox cannot reach scorr.in)

1. After the deploy: `SELECT column_name FROM information_schema.columns WHERE table_name = 'custom_screens'` → id, owner, name, selection, created_at, updated_at (the startup hook created it; the app log line is "cc#2175: custom_screens ensured at startup").
2. On the phone: `/m/screeners?view=custom`, tap Large + Mid, Save, keep the name → toast; then `SELECT id, owner, name, selection, updated_at FROM custom_screens` → one row, owner `scorr_app`, selection `{"size": ["large", "mid"]}`.
3. `https://scorr.in/api/mobile/custom_screener/saved` → that screen with `count` equal to `https://scorr.in/api/mobile/custom_screener/run?size=large,mid` → `count`.
4. `/m/screeners` → My screens card "1 saved screen" → sheet → tap → the custom view pre-filled with the same count → Delete → the row is gone and the table is empty again.
5. `POST /api/mobile/custom_screener/save` with `{"name":"x","selection":{"size":["huge"]}}` → `{"error":"unknown button 'huge' for 'size'"}` and no row.

## Out of scope (by spec)

Sharing saved screens between users; alerts on a saved screen. `/run`, `/meta` and the registry are unchanged.
