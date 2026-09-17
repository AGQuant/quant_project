"""cc#2195: the My Alerts payload — triggered newest first, IST stamps, condition text from the registry,
engine rows and QB notices excluded, pending with live text. Pure shaping, no database."""
import trade_alerts_app as m

REG = {"daily_rsi": {"label": "Daily RSI", "unit": "score"}, "day_move_pct": {"label": "Day Move %", "unit": "%"},
       "gvm_score": {"label": "GVM Score", "unit": "score"}}

CUSTOM = [
    {"id": 1, "symbol": "RELIANCE", "label": "cc#2095 first-run proof: daily_rsi below 50", "status": "triggered",
     "created_at": "2026-09-15 13:58:31.552007+00:00", "triggered_at": "2026-09-15 14:00:02.291456+00:00",
     "conditions": [{"position": 1, "metric_key": "daily_rsi", "operator": "below", "threshold": 50.0, "join_operator": None,
                     "last_value": 33.2213, "last_evaluated_at": "2026-09-15 14:00:02+00:00"}]},
    {"id": 2, "symbol": "TCS", "label": None, "status": "active", "created_at": "2026-09-17 09:10:00+00:00", "triggered_at": None,
     "conditions": [{"position": 1, "metric_key": "day_move_pct", "operator": "above", "threshold": 2.5, "join_operator": None, "last_value": None, "last_evaluated_at": None},
                    {"position": 2, "metric_key": "gvm_score", "operator": "above", "threshold": 7, "join_operator": "AND", "last_value": 7.4, "last_evaluated_at": "2026-09-17 10:00:00+00:00"}]},
]
TRADE = [
    {"id": 26, "symbol": "CAMS", "direction": "SELL", "trigger_price": 706.85, "trigger_condition": "N/A", "status": "approved",
     "created_at": "2026-09-16 06:53:31+00:00", "triggered_at": None, "source_engine": "V8", "kind": "entry", "seen": True},
    {"id": 20, "symbol": "WELCORP", "direction": "BUY", "trigger_price": 457174.6, "trigger_condition": "QB REBALANCE DUE", "status": "pending",
     "created_at": "2026-09-06 19:45:39+00:00", "triggered_at": None, "source_engine": "qb", "kind": "rebalance_due", "seen": False},
    {"id": 24, "symbol": "RELIANCE", "direction": "BUY", "trigger_price": 1500, "trigger_condition": "ABOVE", "status": "dismissed",
     "created_at": "2026-09-13 03:46:42+00:00", "triggered_at": None, "source_engine": None, "kind": "entry", "seen": True},
    {"id": 30, "symbol": "INFY", "direction": "BUY", "trigger_price": 1600, "trigger_condition": "ABOVE", "status": "pending",
     "created_at": "2026-09-17 04:00:00+00:00", "triggered_at": None, "source_engine": None, "kind": "entry", "seen": False, "cmp": 1543.2, "cmp_live": True},
    {"id": 31, "symbol": "ITC", "direction": "SELL", "trigger_price": 400, "trigger_condition": "BELOW", "status": "triggered",
     "created_at": "2026-09-16 04:00:00+00:00", "triggered_at": "2026-09-17 04:35:00+00:00", "source_engine": None, "kind": "entry", "seen": False},
    {"id": "c1", "symbol": "RELIANCE", "status": "triggered", "triggered_at": "2026-09-15 14:00:02+00:00", "source_engine": None,
     "alert_type": "custom", "condition_summary": "Daily RSI below 50.0", "seen": True},   # the bell's merged copy — must not double-count
]


def test_ist_stamp():
    assert m.ist("2026-09-15 14:00:02.291456+00:00")[:2] == ("15 Sep · 19:30", "2026-09-15")
    assert m.ist(None) == (None, None, None)


def test_condition_and_price_text():
    assert m.cond_text({"metric_key": "daily_rsi", "operator": "below", "threshold": 50.0}, REG) == "Daily RSI below 50"
    assert m.cond_text({"metric_key": "day_move_pct", "operator": "above", "threshold": 2.5}, REG) == "Day Move % above 2.5%"
    assert m.price_text({"trigger_condition": "ABOVE", "trigger_price": 1500}) == "crosses ≥ ₹1,500"
    assert m.price_text({"trigger_condition": "BELOW", "trigger_price": 400}, fired=True) == "crossed ≤ ₹400"


def test_shape_triggered_newest_first_and_grouped_by_ist_day():
    out = m.shape(TRADE, CUSTOM, REG, today_ist="2026-09-17")
    t = out["triggered"]
    assert [r["key"] for r in t] == ["p31", "c1"]                     # ITC fired 17-Sep 10:05 IST, RELIANCE 15-Sep 19:30
    assert t[1]["symbol"] == "RELIANCE" and t[1]["text"] == "Daily RSI below 50" and t[1]["fired_ist"] == "15 Sep · 19:30" and t[1]["day"] == "2026-09-15"
    assert t[0]["text"] == "crossed ≤ ₹400" and t[0]["outcome"] == "awaiting a decision" and t[0]["today"] is True and t[0]["fired_ist"] == "17 Sep · 10:05"
    assert out["counts"]["triggered"] == 2 and out["counts"]["triggered_today"] == 1


def test_shape_pending_and_exclusions():
    out = m.shape(TRADE, CUSTOM, REG, today_ist="2026-09-17")
    p = out["pending"]
    assert [r["key"] for r in p] == ["c2", "p30"]                     # TCS set 14:40 IST on top, INFY 09:30
    tcs = p[0]
    assert tcs["text"] == "Day Move % above 2.5% AND GVM Score above 7"
    assert [c["join"] for c in tcs["conditions"]] == [None, "AND"] and tcs["conditions"][1]["last_value"] == 7.4
    assert p[1]["text"] == "crosses ≥ ₹1,600" and p[1]["live_text"] == "live ₹1,543.2"
    keys = {r["key"] for r in out["triggered"] + p}
    assert "p26" not in keys and "p20" not in keys and "p24" not in keys   # V8 approval, QB notice, dismissed-never-fired
    assert out["counts"]["pending"] == 2 and out["counts"]["unseen_price"] == 2 and out["max_conditions"] == 5


def test_registry_map_flattens_categories():
    reg = m._registry_map({"categories": [{"category": "MOMENTUM", "metrics": [{"metric_key": "daily_rsi", "label": "Daily RSI", "unit": "score", "cadence": "daily_eod"}]}]})
    assert reg["daily_rsi"]["label"] == "Daily RSI" and reg["daily_rsi"]["category"] == "MOMENTUM"
