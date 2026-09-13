"""cc#2036 P1 · seed option_strategy_templates from reports/CC2035_ref/golden_30.json.

Idempotent (ON CONFLICT (name) DO UPDATE) — safe to re-run. Reads the one already-committed,
Fable-validated JSON fixture as its single source of truth for legs/risk_profile/view/name; this
script only APPLIES the exact derivation rules cc#2036 item 2 states, it does not invent template
data of its own. Run once against the real DB: `python3 option_strategy_templates_seed.py`.

DERIVATION RULES (verbatim from cc#2036 item 2 / session_log 45192 table_ddl):
  - legs: {kind, side, offset_steps, qty} per leg. offset_steps = (strike-23500)/50 for CE/PE legs
    (golden_30.json's own atm=23500); FUT legs always offset_steps=0. The template stores no
    strike/premium of its own (those are resolved live, cc#2037's POST /resolve) -- confirmed all
    30 templates' CE/PE offsets divide evenly by 50 (checked below, not assumed).
  - name: the [range]/[breakout] suffix is stripped and becomes sub_view; the bare name (including
    any existing parenthetical, e.g. "Long Combo (Risk Reversal)") is kept as-is.
  - sort_order: position in golden_30.json's own templates array (0-indexed).
  - risk_profile: golden_30.json's own risk_profile field, copied verbatim.
  - description: ONLY for the strategies session_log 45180's own bullish/bearish lists already
    spell out a leg-composition parenthetical for (e.g. "Covered Put (short FUT + short OTM PE)")
    -- reusing text the founder/Fable already wrote, never inventing new marketing copy for the
    dozen-plus vanilla strategies (Long Call, Bull Call Spread, ...) that 45180 gives no such text
    for; those stay NULL, an honest gap rather than a fabricated blurb.
  - source: 'added_2026' for the 10 strategies cc#2036 item 2 names explicitly (Iron Condor, Iron
    Butterfly, Long Put Butterfly, Short Put, Short Call, Long Combo, Short Combo, Synthetic Long
    Future, Synthetic Short Future, Bear Collar -- "Synthetic Long/Short Future" in the card's own
    text is read as BOTH synthetic strategies, not one; see the report's own note on this), else
    'excel_2012'.

    NOTE on a genuine discrepancy, stated rather than silently resolved: cc#2036 item 2 also says
    "excel_2012 for the founder 21", i.e. implies 21 of 30 should be excel_2012. The 10 explicitly
    named added_2026 strategies leave 20, not 21 -- one short of that aside. Two names already
    changed since the original 2012 Excel per session_log 45180's own validation_findings (the
    Excel's "Vertical Spread" sheets are renamed "Ratio Backspread"/"Ratio Spread" here, same
    underlying strategy) -- a plausible source of the same kind of one-off drift, not a new one.
    This script uses the card's own EXPLICIT name list (unambiguous) over its arithmetic aside
    (which could easily be an approximate recollection of 45180's "21 readymade [Excel] sheets"
    figure, a different count for a different purpose) -- flagged here and in the task report
    rather than silently picked, since it is cosmetic (a provenance/attribution tag, not a priced
    or traded number) but still worth a human eye.
"""
import json
import os

import psycopg
from psycopg.types.json import Json

DATABASE_URL = os.getenv("DATABASE_URL", "")
GOLDEN_PATH = os.path.join(os.path.dirname(__file__), "reports", "CC2035_ref", "golden_30.json")
ATM_REF = 23500  # golden_30.json's own "atm" field -- the reference ATM these strikes were built around

ADDED_2026 = (
    "Iron Condor", "Iron Butterfly", "Long Put Butterfly", "Short Put", "Short Call",
    "Long Combo", "Short Combo", "Synthetic Long Future", "Synthetic Short Future", "Bear Collar",
)

# session_log 45180's own leg-composition parentheticals, reused verbatim -- not invented here.
DESCRIPTIONS = {
    "Covered Call": "long FUT + short OTM CE",
    "Protective Put": "long FUT + long PE",
    "Collar": "long FUT + long PE + short CE",
    "Long Combo": "long OTM CE + short OTM PE",
    "Synthetic Long Future": "long ATM CE + short ATM PE",
    "Call Ratio Backspread": "short 1 ITM CE + long 2 OTM CE",
    "Covered Put": "short FUT + short OTM PE",
    "Protective Call": "short FUT + long CE",
    "Bear Collar": "short FUT + long CE + short PE",
    "Short Combo": "short OTM CE + long OTM PE",
    "Synthetic Short Future": "long ATM PE + short ATM CE",
    "Put Ratio Backspread": "short 1 ITM PE + long 2 OTM PE",
    "Call Ratio Spread": "long 1 ITM CE + short 2 OTM CE",
    "Put Ratio Spread": "long 1 ITM PE + short 2 OTM PE",
}

UPSERT_SQL = """
INSERT INTO option_strategy_templates
    (name, view, sub_view, legs, risk_profile, description, source, sort_order, is_active)
VALUES (%(name)s, %(view)s, %(sub_view)s, %(legs)s, %(risk_profile)s, %(description)s,
        %(source)s, %(sort_order)s, true)
ON CONFLICT (name) DO UPDATE SET
    view = EXCLUDED.view, sub_view = EXCLUDED.sub_view, legs = EXCLUDED.legs,
    risk_profile = EXCLUDED.risk_profile, description = EXCLUDED.description,
    source = EXCLUDED.source, sort_order = EXCLUDED.sort_order, is_active = true
"""


def strip_suffix(name: str):
    """"Iron Condor [range]" -> ("Iron Condor", "range"); a name with no bracket -> (name, None)."""
    if name.endswith(" [range]"):
        return name[: -len(" [range]")], "range"
    if name.endswith(" [breakout]"):
        return name[: -len(" [breakout]")], "breakout"
    return name, None


def build_rows():
    with open(GOLDEN_PATH) as f:
        golden = json.load(f)
    rows = []
    for i, t in enumerate(golden["templates"]):
        base_name, sub_view = strip_suffix(t["name"])
        legs = []
        for l in t["legs"]:
            if l["kind"] == "FUT":
                offset = 0
            else:
                raw = l["strike"] - ATM_REF
                assert raw % 50 == 0, f"{t['name']}: strike {l['strike']} not a clean 50-multiple of ATM {ATM_REF}"
                offset = raw // 50
            legs.append({"kind": l["kind"], "side": l["side"], "offset_steps": offset, "qty": l["qty"]})
        source = "added_2026" if any(base_name.startswith(n) for n in ADDED_2026) else "excel_2012"
        description = next((v for k, v in DESCRIPTIONS.items() if base_name.startswith(k)), None)
        rows.append({
            "name": base_name, "view": t["view"], "sub_view": sub_view,
            "legs": Json(legs), "risk_profile": t.get("risk_profile"),
            "description": description, "source": source, "sort_order": i,
        })
    return rows


def main():
    rows = build_rows()
    assert len(rows) == 30, f"expected 30 templates, got {len(rows)}"
    n_added = sum(1 for r in rows if r["source"] == "added_2026")
    print(f"seeding {len(rows)} templates ({n_added} added_2026, {len(rows) - n_added} excel_2012)")
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS option_strategy_templates (
                    id           serial PRIMARY KEY,
                    name         text UNIQUE NOT NULL,
                    view         text NOT NULL CHECK (view IN ('bullish','bearish','neutral')),
                    sub_view     text CHECK (sub_view IN ('range','breakout') OR sub_view IS NULL),
                    legs         jsonb NOT NULL,
                    risk_profile text,
                    description  text,
                    sketch       text,
                    source       text NOT NULL DEFAULT 'excel_2012',
                    sort_order   int NOT NULL DEFAULT 0,
                    is_active    bool NOT NULL DEFAULT true,
                    created_at   timestamptz DEFAULT now()
                )
            """)
            for r in rows:
                cur.execute(UPSERT_SQL, r)
        conn.commit()
    print("done")


if __name__ == "__main__":
    main()
