# cc#2144 — QB Universe builder: per-filter STEP count beside the ALONE count (funnel)

Founder, 17-Sep: with Nifty 500, GVM ≥ 7 and G ≥ 7 the rows read "123 of 490 pass" and "230 of
490 pass" (both ALONE counts, cc#2135) while the universe read 97 — the second row must also show
the stepped-down number, and both must be visible. Step 2 of the QB Universe sprint, additive.

## Backend (`qb_universe_builder.py`, `/api/qb/universe2/preview`)
- New `step_counts {filter_key: n}`: for the k-th applied row, the count of scored-pool rows
  passing the prefix expression `((f1 OP2 f2) … OPk fk)` — built by the **same `_combine`** over
  the same prefix of `_FILTER_ORDER`, run against the **same CTE** as `count`
  (`scored_count_sql`). One real query per applied row; nothing computed on the page. The last
  step is the full expression, so it equals `count` by construction. Under AND each step can only
  fall; under OR it can rise — returned exactly as the query says, never clamped or reordered.
- `per_filter_counts` and `binding_filter` are byte-identical before/after (unit-tested with a
  fake DB: same dicts, same `{"filter": "gvm", "cut_to": 123, "cut_count": 367}` under AND and OR).
  `_combine` / `_parse_ops` / `_CAT1_SQL` untouched (do_not_touch).

## Page (`scorr_qb_universe.html`, `passCountHtml`)
Every applied row now shows both numbers, labelled:
`230 of 490 pass on its own | 97 remain after this step (AND with the rows above)` — the alone
figure in the existing green, the step figure in blue. The first applied row has nothing above it,
so its step equals its alone count and carries no combinator note. No other UI change.

## Verify (the card's own cases, on real numbers)
- Live DB, Nifty 500 scored pool 490, the exact CTE: GVM ≥ 7 alone **123**, G ≥ 7 alone **230**,
  `(gvm AND g)` **97**, `(gvm OR g)` **256** (and a third-row sanity: `((gvm AND g) OR v ≥ 8)` = 131,
  v ≥ 8 alone 53 — a step that rises above the row before it under OR).
- Fake-DB unit test of the endpoint: `step_counts == {"gvm": 123, "g": 97}` with `count == 97`
  under AND; `{"gvm": 123, "g": 256}` with `count == 256` when G is OR (≥ 123, proving the step
  follows the real expression); the step SQL is the prefix expression verbatim; no filters →
  `step_counts == {}`.
- Playwright on the real page (real theme boot + web tokens; `/preview` stubbed with the numbers
  above): GVM row "123 of 490 pass on its own | 123 remain after this step"; G row "230 of 490
  pass on its own | 97 remain after this step (AND with the rows above)"; universe 97. Flip G to
  OR → request carries `ops=gvm:AND,g:OR`, G row "… | 256 remain after this step (OR with the rows
  above)", universe 256. Alone and step colours distinct; no page errors; `node --check` clean;
  0 literal fallbacks. Screenshots looked at (VISUAL_VERIFY_GATE_V1).
- Founder on glass: `/qb/universe2`, Nifty 500, GVM ≥ 7 + G ≥ 7 → the G row shows 230 and 97.

## Not touched
How filters combine, `binding_filter`, `_CAT1_SQL`, the pool section (cc#2143).
