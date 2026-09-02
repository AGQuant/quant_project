#!/usr/bin/env python3
"""cc#1164 — regenerate SPEC_REGISTRY_INDEX.md from session_log.

WHY THIS EXISTS AS A SCRIPT
    The index was last generated 05-Aug-2026 and by 21-Aug it was missing 171 doctrine entries —
    including ROLE_CHARTER_V4, PUSH_MODES_V2 and every founder lock from the four-bucket Trade
    Check night. It went stale because regenerating meant hand-transcribing rows out of the
    database, and hand work does not get done twice. Now it is one command, so the index can be
    current whenever anyone wants it to be.

    The file is an INDEX. It carries id, category and title, and never the spec body. The body
    lives in the DB and the DB is the authority.

CURATION LIVES IN A SIDECAR, NOT IN THIS SCRIPT
    tools/spec_index_sections.json maps id -> topical section, seeded mechanically from the 233
    entries a human had already sorted in the 05-Aug file. Nothing curated was thrown away. An id
    with no mapping lands in the dated recent section rather than being guessed into a topic —
    the index says where a thing sits or admits it has not been filed, and never invents.

USAGE
    python3 tools/gen_spec_index.py                    # reads DATABASE_URL
    python3 tools/gen_spec_index.py --rows rows.tsv    # offline: 'id :: category :: title' lines
    python3 tools/gen_spec_index.py --out /tmp/x.md --check
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DEFAULT = os.path.join(ROOT, "SPEC_REGISTRY_INDEX.md")
SECTIONS_JSON = os.path.join(ROOT, "tools", "spec_index_sections.json")

# The categories that carry DOCTRINE. archived_superseded is excluded on purpose: those entries are
# retired and must not read as current.
#
# `spec` and `locked_spec` are in this list because of a real miss found on 21-Aug: TC_SCORE_V1
# (27957), the parent spec the whole four-bucket weight lock set hangs off, was filed under `spec`
# rather than `spec_locked` and was therefore invisible to the previous sweep. A one-character
# category difference should not be able to hide a founder-directed spec.
#
# `spec_registry` and `debug_learnings` are here because the 05-Aug curated file already listed
# id 150 and id 207 by hand. A sweep that dropped them would have quietly lost two entries a human
# had deliberately filed — the regression a regenerator is most likely to cause and least likely
# to notice. The generator prints filed/unfiled counts so that loss cannot happen silently again.
DOCTRINE = ("spec_locked", "canonical_spec", "decision", "framework", "trading_learnings",
            "ruling", "standing_rule", "mobile_framework", "protocol_one", "architecture",
            "locked_spec", "spec", "memory_rules", "propagation_map",
            "spec_registry", "debug_learnings",
            # `doctrine` holds the founder rules themselves (28971 CC_QUEUE_DRAIN_RULE_V1, 29164
            # PRODUCTION_MODE_V3, 36144 MODEL_EFFORT_CACHE_RULE_V1 …). Missing here until cc#1563:
            # a plain DB-backed regen silently dropped all of them (found on cc#1560).
            "doctrine",
            # Day and week logs are DATED RECORDS, not specs. They are indexed because the founder
            # asks for entries by id and a day log is a legitimate thing to look up, but they are
            # rendered in their own clearly-labelled section at the end so nobody mistakes one for
            # doctrine. Note the DB carries both `day_log` and `daylog` — the same thing under two
            # spellings, which is itself worth someone's attention one day.
            "day_log", "daylog", "week_log")

LOG_CATS = ("day_log", "daylog", "week_log")

IST = timezone(timedelta(hours=5, minutes=30))

QUERY = """
    SELECT id, category, replace(title, chr(10), ' ')
    FROM session_log
    WHERE category = ANY(%s)
    ORDER BY id
"""

HEADER = """# Scorr — Spec Registry Index

Generated {stamp} from the live `session_log` table by `tools/gen_spec_index.py`
({n} doctrine entries across {cats}).

**How to use this.** This is an INDEX, not the specs themselves. The full text of every
entry lives in the Railway DB and is the authority. To read one:

```sql
SELECT id, title, details FROM session_log WHERE id = <id>;
```

To find one by topic:

```sql
SELECT id, category, title FROM session_log
WHERE title ILIKE '%<keyword>%'
  AND category NOT IN ('archived_superseded')
ORDER BY id DESC;
```

`archived_superseded` is deliberately excluded — those are retired and must not be
treated as current. If a spec below says "supersedes id=X", X is archived.

**To regenerate:** `python3 tools/gen_spec_index.py`. Topical curation lives in
`tools/spec_index_sections.json` (id -> section). An entry with no mapping lands in the
dated recent section rather than being guessed into a topic.

---
"""

TRAIL = """
## Supersession trail — what is NO LONGER current

These are named here because an index that simply drops a retired entry leaves a reader
unable to tell "retired" from "never existed", and a rule that reads as absent is a rule
someone re-adopts by accident.

| retired id | was | now live |
|---|---|---|
| 17868 | ROLE_CHARTER_V3 — Fable owns all app tasks, CC benched | **27934** ROLE_CHARTER_V4 / EXECUTION_MODEL_PHASE_3 |
| 22301, 22318, 22324, 22338 | V8 table specs, four rounds | **22342** (+ base 22321), per the 22344 lineage pointer |
| 22405 | SEGMENT_TAXONOMY_V1 | **22406** SEGMENT_TAXONOMY_V2 |
| 5650 gates | BUY_MOMENTUM V3 gate set | **23197** BUY_MOMENTUM_V5 (rule source 23186) |
| 324 | investment_check v1.0 | **27979** INVESTMENT_CHECK_V2 — *on V2 go-live; v1 still serving* |
| 22310 | CC_PROGRESS_REPORT_FORMAT_V1 | **27944** REPORT_FORMAT_V2 (extends, does not retire) |
"""


def _rows_from_db():
    import psycopg
    url = os.getenv("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set — pass --rows for an offline run instead of guessing.")
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(QUERY, (list(DOCTRINE),))
        return [(int(i), c, (t or "").strip()) for i, c, t in cur.fetchall()]


def _rows_from_file(path):
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        if not line.strip():
            continue
        parts = line.split(" :: ", 2)
        if len(parts) != 3:
            sys.exit("bad row (want 'id :: category :: title'): %s" % line[:80])
        out.append((int(parts[0]), parts[1].strip(), parts[2].strip()))
    return sorted(out)


def build(rows, sections):
    cats = sorted({c for _, c, _ in rows})
    stamp = datetime.now(IST).strftime("%d-%b-%Y")
    doc = [HEADER.format(stamp=stamp, n=len(rows), cats=", ".join("`%s`" % c for c in cats))]

    # Curated sections keep their original order and their original members.
    order, seen = [], set()
    for sec in sections.values():
        if sec not in seen:
            seen.add(sec)
            order.append(sec)
    order.sort(key=lambda s: int(s.split("·")[0].strip()) if s.split("·")[0].strip().isdigit() else 99)

    by_id = {i: (c, t) for i, c, t in rows}
    placed = set()
    for sec in order:
        ids = [i for i, s in sections.items() if s == sec and i in by_id]
        if not ids:
            continue
        doc.append("\n## %s\n" % sec)
        doc.append("| id | Title |")
        doc.append("|---|---|")
        for i in sorted(ids, key=lambda x: -x):
            doc.append("| %d | %s |" % (i, by_id[i][1].replace("|", "\\|")))
            placed.add(i)
        doc.append("")

    # Everything the curated map has not filed, newest first, grouped by category so a reader can
    # see WHAT KIND of entry it is even though nobody has assigned it a topic yet.
    rest = [r for r in rows if r[0] not in placed and r[1] not in LOG_CATS]
    if rest:
        doc.append("\n## Not yet filed by topic — newest first\n")
        doc.append("Entries added since the last curated pass. They are indexed and readable; "
                   "they simply have no topical home yet. Add one to "
                   "`tools/spec_index_sections.json` to file it.\n")
        for cat in sorted({c for _, c, _ in rest}):
            group = sorted([r for r in rest if r[1] == cat], key=lambda x: -x[0])
            doc.append("\n### `%s` — %d\n" % (cat, len(group)))
            doc.append("| id | Title |")
            doc.append("|---|---|")
            for i, _c, t in group:
                doc.append("| %d | %s |" % (i, t.replace("|", "\\|")))
            doc.append("")

    # Dated records last, and labelled as such. They are lookups, not doctrine.
    logs = sorted([r for r in rows if r[1] in LOG_CATS and r[0] not in placed], key=lambda x: -x[0])
    if logs:
        doc.append("\n## Day and week logs — dated records, NOT specs\n")
        doc.append("Indexed so an id can be looked up, kept apart so none of them is ever read as "
                   "a rule. The DB carries both `day_log` and `daylog` spellings.\n")
        doc.append("| id | Title |")
        doc.append("|---|---|")
        for i, _c, t in logs:
            doc.append("| %d | %s |" % (i, t.replace("|", "\\|")))
        doc.append("")

    doc.append(TRAIL)
    return "\n".join(doc).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", help="offline rows file: 'id :: category :: title' per line")
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--check", action="store_true",
                    help="print counts and exit non-zero if nothing would be written")
    a = ap.parse_args()

    rows = _rows_from_file(a.rows) if a.rows else _rows_from_db()
    sections = {int(k): v for k, v in json.load(open(SECTIONS_JSON, encoding="utf-8")).items()}
    text = build(rows, sections)

    filed = sum(1 for i, _c, _t in rows if i in sections)
    print("rows=%d filed_by_topic=%d unfiled=%d bytes=%d"
          % (len(rows), filed, len(rows) - filed, len(text.encode())))
    if a.check and not rows:
        sys.exit("refusing to write an empty index")
    open(a.out, "w", encoding="utf-8").write(text)
    print("wrote", a.out)


if __name__ == "__main__":
    main()
