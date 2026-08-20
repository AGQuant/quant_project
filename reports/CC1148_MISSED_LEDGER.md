# cc#1148 — the missed-trade ledger is dead

**DIAGNOSTIC AND READ-ONLY.** No engine write, no schema change, no backfill. Zero rows written
anywhere. Read 20-Aug-2026.

---

## A. The hypothesis is CONFIRMED, with grep

```
grep -n "v8_paper_missed\|_log_missed" v8_signal_writer.py
   -> NO MATCHES
```

**The live entry engine never records a missed trade.** Not once, anywhere in its 161KB.

Every `_log_missed()` call in the repo lives in `v8_paper.py`, inside `paper_tick()`:

| Line | Reason recorded |
|---|---|
| v8_paper.py:496, :503 | conflict_exit_blocked |
| v8_paper.py:794, :820 | has_open |
| v8_paper.py:796, :822 | traded_today |
| v8_paper.py:798, :824 | conflict |
| v8_paper.py:801, :827 | blackout |
| v8_paper.py:803, :829 | slot_full |
| v8_paper.py:538 | the writer itself |

And `paper_tick` is **not the entry engine**. Its own file says so, and cc#1138 confirmed it from
the scheduler: the only paper job scheduled every 5 minutes is `run_paper_exits`, which is
exit-only. `paper_tick` is reachable only through the admin endpoint and the MCP tool.

That is why the table holds **one row, dated 2026-08-05** — the residue of a manual tick — and
nothing since.

---

## B. It is worse than "an analytics gap", and this is the part I did not expect

**The table IS read by a live surface, and its emptiness is silently changing what the app tells
you.**

`v8_endpoints.py:691` — `_load_missed()` reads `v8_paper_missed` for today and feeds
`_signal_reason()`, which resolves the gate chip on every basket tab. That function's priority is:

```
conflict (opposite-side open) > explicit engine missed-reason > slots > cutoff > slots
```

With the table empty, `raw` is always `None`, so the "explicit engine missed-reason" branch can
never fire. Checked today:

| Source | Rows today |
|---|---|
| `v8_paper_missed` where `miss_date = CURRENT_DATE` | **0** |
| `v8_qualified.metrics->>'status' = 'slot_full'` (what `_load_slot_full` reads) | **0** |
| signals qualified today | 9 |

Both evidence sources are empty. So for all nine of today's gated signals, `_signal_reason()` falls
through to its final `return 'slots'`.

**The `-slots` chip is a default, not a measurement.** The comment on that line is honest about it —
*"slots is the dominant gate; a valid signal that didn't enter was slot-gated"* — but it is an
assumption standing in for evidence, and on screen it is indistinguishable from a fact.

**This affects work I shipped an hour ago.** cc#1145 gave that chip the tooltip *"Qualified, but no
free slot on this side."* On today's data that sentence is asserted for every gated row without
anything behind it. The tooltip is not wrong about what `slots` means — it is wrong to state it
with confidence when the reason was inferred. That needs fixing either way, and it is cheap: the
payload can carry whether the reason was *recorded* or *assumed*, and the chip can read
"probably slots" until the ledger is alive.

---

## C. Surfaces reading `v8_paper_missed`

Not "none" — three:

| Location | Use |
|---|---|
| `v8_endpoints.py:691` | **live** — gate chips on all 4 basket tabs |
| `main.py:1878` | a diagnostic API query, last 100 rows |
| `bt7_harness.py:48,53`, `v8_paper_replay.py:51` | backtest scratch/truncate lists |

Because of the first one, this is **not** the low-priority analytics gap the card assumed. It is
feeding a user-visible label today.

---

## D. What the live writer knows but throws away

`_auto_paper_entry` evaluates every gate and returns early on each, recording nothing durable:

| Gate | Where | Currently recorded? |
|---|---|---|
| blackout (earnings) | `guards.blackout`, line ~1361 | no |
| same-side already open | `guards.has_open`, ~1364 | no |
| traded today | `guards.traded_today`, ~1366 | no |
| entry window / 09:30 cool-off | `guards.in_entry_window`, ~1395 | no |
| slot full | ~1428 / ~1431 | **in memory only** — `_record_slot_block()` appends to the
  module-level `_slot_full_blocks` dict for the burst alert, and it dies with the process |

Every one of these is a reason the UI already has a chip for. The information exists at the moment
of the skip; it is simply never written down.

---

## E. Smallest wiring change — proposed, NOT implemented

Per the card, this is a report only. The proposal:

**A new small file, `v8_missed_ledger.py`**, exposing one function:

```
record_miss(conn, d, sym, side, basket, reason, entry=None, target=None, stop=None)
```

It writes to `v8_paper_missed` — the table already exists with the right shape (`v8_paper.py:185`)
— and swallows its own errors, because a logging failure must never block or crash an entry
decision.

`_auto_paper_entry` then gains **one line per early return** — six lines total, no logic moved, no
retyping of the 161KB file. The gates are already sitting on `return`/`continue` statements, so
each is a single call inserted before the existing return.

Two properties worth stating:

- **It cannot change trading behaviour.** Every insertion point is on a path that has already
  decided to skip; the function has no return value the caller reads.
- **It is idempotent per symbol/side/day** if the insert carries an `ON CONFLICT DO NOTHING`,
  which matters because the writer re-evaluates every 5-minute tick and would otherwise write the
  same miss 70 times a day.

**Not in scope here and not done.** No backfill of history — the past is gone and inventing it
would be worse than the gap.

---

## F. Verification

- **A.** Grep result stated above with line references.
- **B.** Yes — confirmed: the live writer does **not** record missed trades.
- **C.** Three surfaces read the table; one is live and user-visible.
- **D.** Zero rows written by this audit. `v8_paper_missed` still holds exactly 1 row, dated
  2026-08-05.

---

## STOPPED — one thing needs your call, one I can just fix

1. **Do I wire the ledger** as proposed in section E? It touches `_auto_paper_entry`, so it is the
   live trading path and I want the go-ahead.
2. **The chip honesty fix is separate and smaller** — making the payload say whether a gate reason
   was recorded or assumed. That does not touch the engine at all. Say the word and it goes in the
   next push.
