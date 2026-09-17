# cc#2142 — `mobile_tables.js` observed `document.body` at `<head>` time

## The bug
`pwa_endpoints.MOBILE_TABLES_JS` (served at `/mobile_tables.js`, injected site-wide by `main.py`
`_MOBILE_HEAD` at `</head>`) ended with
`new MutationObserver(scanSoon).observe(document.body, …)` executed at script time. In `<head>`
`document.body` is still `null`, so every `/m/*` page load threw
`Failed to execute 'observe' on 'MutationObserver': parameter 1 is not of type 'Node'`. The initial
`scan()` was already DOMContentLoaded-guarded, so tables present at load were wired; the observer
never attached, so a `.mtable` added after load never got `initWrap` / `initTable`.

## The fix (one place, `MOBILE_TABLES_JS` only)
The observer is attached through the same guard `scan()` uses: `observe()` runs on
`DOMContentLoaded` while `readyState === 'loading'`, else immediately; it also no-ops when
`MutationObserver` or `document.body` is missing. Nothing else in the string changed; injection order
in `main.py` untouched.

## Verify
- `node --check` clean on the extracted string, before and after; `ast` clean on `pwa_endpoints.py`.
- Playwright on the real `mobile/intel.html` in **production injection order** (the script tag at
  `</head>`), a static `.mtable` in the body, and a second `.mtable-wrap` + table appended after load:

| | page errors | static table wired at load | dynamic table wired after inject | row tap opens detail |
|---|---|---|---|---|
| before | the TypeError above | 1 row (scan guard) | **0 of 2 rows** | — |
| after | **none** | 1 row | **2 of 2 rows, chevrons added** | yes (1 detail row) |

## Not touched
Any other injected script; `main.py` injection order; the rest of `MOBILE_TABLES_JS`.
