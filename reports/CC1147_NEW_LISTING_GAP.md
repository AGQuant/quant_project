# cc#1147 — new listings never reach `input_raw`

**Steps 1–2 are read-only.** No rows written to `input_raw`. Read 20-Aug-2026.

---

## 1. Detection — using a feed already in the pipeline

**`screener_raw` is the detector.** No new data vendor was added; it is already loaded and it
already sees new listings.

The proof is the case that surfaced this card:

| Feed | SHIPROCKET present? |
|---|---|
| `screener_raw` | **YES** — `SHIPROCKET / Shiprocket` |
| `input_raw` | no — only GESHIP, COCHINSHIP |
| `cmp_prices` | no |
| `gvm_scores` | no |

Shiprocket is already sitting in the pipeline. It simply never crosses into `input_raw`.

**Current backlog: 72 symbols** present in `screener_raw` and absent from `input_raw`.

Ten of them, alphabetically (`screener_raw` carries no load timestamp, so "most recent" is not
orderable from this table — stating that rather than implying recency I cannot prove):

ACGL · AEROPLANE · AGL · AMAL · ANAWIL · APSISAERO · ARDEE · ARYAMAN · ASHIKAG · AXTEL

Two adjacent gaps, for context: 29 symbols in `cmp_prices` and 3 in `futures_universe` are also
missing from `input_raw`.

**A caveat on that 72.** `input_raw` (2,008) is *larger* than `screener_raw` (1,866), so the two
are curated differently and some divergence is permanent and correct. The 72 is the right number to
**watch**, not a list of 72 companies that all belong in the universe.

---

## 2. Root cause — answered explicitly

**How a new listing is supposed to enter `input_raw` today: it isn't. There is no job.**

`input_raw` has exactly **one** write path in the entire repo:

```
admin_data.py:72   POST /api/admin/load_input_from_drive
                   -> DELETE FROM input_raw
                   -> INSERT every row of a Google Drive CSV supplied by hand as file_id
```

Three properties of that path:

- **It is not scheduled.** `grep load_input scheduler.py` returns nothing. No cron, no background
  job. It runs only when a human POSTs a `file_id`.
- **It is a full replace**, not an incremental refresh — `DELETE` then reload.
- **It covers new listings only if the CSV the human uploads happens to contain them.**

The other two writers touch existing rows only: `main.py:1439` updates a single field by `id`, and
`result_analysis_gen.py:377` sets `result_analysis`.

**So: does the path cover new listings? No.** Not partially, not on a delay — there is no automated
ingest at all. This is not a broken job. It is an absent one.

---

## 3. Consequence

Every new listing is invisible to `mentioned_symbols` tagging, the R-card news join, GVM coverage
and every screener — and stays invisible until a person notices and re-uploads a CSV. That is the
silent-no-op class from FYERS_INTEGRATION_LEARNINGS: a component quietly stops covering something
and nothing says so.

Shiprocket listed, traded, closed at ₹143.10 with Goldman buying 40.24 lakh shares, and the
polished article had to ship with an **empty** `mentioned_symbols` array — correctly, because
TAGGING_RULE_02AUG forbids force-tagging a listed cousin and data honesty forbids inventing a
symbol. The rules held. The universe did not.

---

## 4. The alarm — implemented, because this card asks for it

Added to `ca_watchdog._master_note()` as section **(e)**, following the same pattern as the
`ops_metrics_pending` probe:

```
lines["universe_gap"] = {"in_screener_not_input": <count>, "sample": [<10 symbols>]}
red_flag when count > 100
```

Three notes on that:

- **The threshold is 100, not zero.** The two feeds are curated differently, so a standing
  divergence is normal. A jump past 100 means listings are piling up unattended.
- **It is read-only.** It counts a gap; it never closes one.
- It uses the file's own `psycopg.connect(DB_URL)` pattern, and its own `try/except` like every
  other probe, so a failure here can never stop the note being written.

---

## 5. Proposed fix — NOT implemented, per the gate

Adding rows to `input_raw` changes every screener and every sector median, and the universe
boundary is governed by `SCRAPE_UNIVERSE_TOP500` (session_log 13546). That is a founder ruling, not
a CC decision, so nothing was inserted.

The smallest honest fix, when you rule on it:

**A scheduled reconciliation that PROPOSES, never inserts.** A daily job that writes the
`screener_raw` − `input_raw` difference into a review queue with market cap attached, so a new
listing appears on a list the morning after it lists instead of whenever someone happens to look.
Admission stays a human decision and the boundary rule stays intact — the job removes the *silence*,
not the gate.

What it does **not** do: bulk-insert, change the universe rule, or decide whether a ₹200cr
micro-cap belongs in a top-500 universe.

---

## 6. Verification

- **A.** Backlog stated: 72, with ten names and an explicit note that recency is not orderable.
- **B.** Root cause names the job — `POST /api/admin/load_input_from_drive`, `admin_data.py:72` —
  and answers **no**, it does not cover new listings, because it is manual and full-replace.
- **C.** No rows written to `input_raw`. Row count unchanged at **2,008**.
- **D.** Watchdog metric added and visible in `MASTER_WATCHDOG_NOTE` under `universe_gap`.
