"""cc#2175 -- saved custom screens (custom_screens table, POST /save, GET /saved, DELETE /saved/{id}).

No-DB tests pin the validation and the SQL text; the real-DB test (skips without DATABASE_URL) saves a screen,
reads it back with a live count equal to /run's count for the same selection, and deletes it.
"""
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

csa = pytest.importorskip("custom_screener_app")
DATABASE_URL = os.getenv("DATABASE_URL", "")


def test_schema_is_create_if_not_exists_with_owner_name_unique_and_no_alter():
    s = csa.SCREENS_SCHEMA_SQL
    assert s.startswith("CREATE TABLE IF NOT EXISTS custom_screens")
    assert "UNIQUE (owner, name)" in s and "selection   jsonb NOT NULL" in s
    assert "ALTER" not in s.upper()
    assert "ON CONFLICT (owner, name) DO UPDATE" in csa.UPSERT_SQL and "RETURNING id" in csa.UPSERT_SQL


def test_clean_name_rules():
    assert csa.clean_name("  Large / Mid   GVM  ") == ("Large / Mid GVM", None)
    assert csa.clean_name("")[1] == {"error": "name required"}
    assert csa.clean_name(None)[1] == {"error": "name required"}
    assert "too long" in csa.clean_name("x" * (csa.SCREEN_NAME_MAX + 1))[1]["error"]


def test_selection_from_body_validates_through_the_registry():
    sel, err = csa.selection_from_body({"selection": {"gvm": ["good"], "size": "large,mid"}})
    assert err is None and sel == {"size": ["large", "mid"], "gvm": ["good"]}, "registry order, comma lists accepted"
    assert csa.selection_from_body({"selection": {"size": ["huge"]}})[1]["error"].startswith("unknown button")
    assert csa.selection_from_body({"selection": {"colour": ["red"]}})[1]["error"].startswith("unknown filter")
    assert csa.selection_from_body({"selection": {}})[1]["error"] == "pick at least one filter before saving"
    assert "must be an object" in csa.selection_from_body({"selection": ["size"]})[1]["error"]
    assert csa.words({"size": ["large", "mid"], "gvm": ["good"]}) == "Size: Large / Mid · GVM rating: Good"


def test_save_rejects_bad_input_before_touching_the_database(monkeypatch):
    def no_db():
        raise AssertionError("the database must not be touched for a rejected save")
    monkeypatch.setattr(csa, "_conn", no_db)
    monkeypatch.setattr(csa, "_guard", lambda r: None)
    assert csa.custom_screener_save(None, {"name": "", "selection": {"size": ["large"]}}) == {"error": "name required"}
    out = csa.custom_screener_save(None, {"name": "x", "selection": {"size": ["huge"]}})
    assert out["error"].startswith("unknown button")
    out = csa.custom_screener_save(None, {"name": "x", "selection": {}})
    assert out["error"] == "pick at least one filter before saving"


@pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- this test needs the real database")
def test_save_read_back_with_live_count_then_delete():
    psycopg = pytest.importorskip("psycopg")
    name = "pytest cc2175 probe"
    selection = {"size": ["large", "mid"]}
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        csa.ensure_screens_table(conn)
        cur.execute("DELETE FROM custom_screens WHERE owner = %s AND name = %s", (csa.APP_OWNER, name))
        cur.execute(csa.UPSERT_SQL, (csa.APP_OWNER, name, json.dumps(selection)))
        sid = cur.fetchone()[0]
        cur.execute("SELECT selection FROM custom_screens WHERE id = %s", (sid,))
        stored = cur.fetchone()[0]
        stored = stored if isinstance(stored, dict) else json.loads(stored)
        assert stored == selection
        live = csa._count(cur, selection)
        cur.execute(csa.run_sql(selection, limit=0))
        assert live == int(csa._one(cur)["total"] or 0)
        cur.execute("DELETE FROM custom_screens WHERE id = %s RETURNING id", (sid,))
        assert cur.fetchone()[0] == sid
        conn.commit()
