"""
cc#308: futures_universe.lot_size sync from the Fyers NSE_FO master.

Root-cause fix. The /add and Monday sync upserts used ON CONFLICT (symbol) DO UPDATE
SET is_active/updated_at ONLY — so once a symbol existed its lot_size was frozen
forever, and an NSE lot revision at expiry (e.g. NESTLEIND 40 -> 500) was never picked
up. This mis-sizes P&L everywhere futures_universe.lot_size is used (SmartGain qty,
client_positions real accounts, V8 paper 1-lot sizing, quant basket, trade cards).

This module fetches the authoritative per-underlying F&O lot from the Fyers public
NSE_FO master and (a) audits futures_universe, (b) corrects mismatched ACTIVE symbols,
(c) logs the diff + client-position blast radius to ops_log. It is called by the Monday
sync, the /api/v8/backfill/sync_universe endpoint, and /api/v8/futures/sync_lots.
"""

import json
import logging

import requests

log = logging.getLogger("lot_sync")

FYERS_NSE_FO_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"

# Fyers NSE_FO CSV (no header): col 3 = LOT, col 9 = ticker (NSE:...), col 13 = underlying.
_COL_LOT, _COL_TICKER, _COL_UNDERLYING = 3, 9, 13


def fetch_fyers_lots(timeout: int = 40) -> dict:
    """Return {underlying_symbol: lot_size} parsed from the Fyers NSE_FO master.

    Lot is per-underlying; we read it off the FUT rows (ticker ends with 'FUT'). Raises
    if the parse is implausibly small so a bad download can never wipe good lot data."""
    r = requests.get(FYERS_NSE_FO_URL, timeout=timeout)
    r.raise_for_status()
    lots, rows = {}, 0
    for line in r.text.splitlines():
        parts = line.split(",")
        if len(parts) <= _COL_UNDERLYING:
            continue
        ticker = parts[_COL_TICKER].strip().upper()
        if not ticker.startswith("NSE:") or not ticker.endswith("FUT"):
            continue
        und = parts[_COL_UNDERLYING].strip().upper()
        try:
            lot = int(float(parts[_COL_LOT]))
        except (ValueError, TypeError):
            continue
        if und and lot > 0:
            lots[und] = lot          # per-underlying; every FUT row of it carries the same lot
        rows += 1
    if len(lots) < 50:
        raise RuntimeError(f"Fyers master parse yielded only {len(lots)} lots (rows={rows}) — refusing to apply")
    log.info(f"Fyers NSE_FO master: {rows} FUT rows -> {len(lots)} underlyings")
    return lots


def audit_and_fix_lots(conn, apply: bool = True) -> dict:
    """Diff futures_universe.lot_size vs the Fyers master; optionally correct ACTIVE
    mismatches. Writes one ops_log(lot_size_audit) entry and flags client_positions
    real-account rows whose symbol lot changed (does NOT rewrite them — Claude web owns
    client data). Idempotent: a second run right after finds 0 further changes."""
    fyers = fetch_fyers_lots()
    with conn.cursor() as cur:
        cur.execute("SELECT symbol, lot_size, is_active FROM futures_universe")
        current = cur.fetchall()

        changes, missing_from_fyers, null_lots = [], [], []
        for symbol, lot_size, is_active in current:
            fy = fyers.get((symbol or "").upper())
            old = int(lot_size) if lot_size is not None else None
            if fy is None:
                missing_from_fyers.append(symbol)      # delisted/renamed — never touched, only logged
                continue
            if old is None:
                null_lots.append(symbol)
            if old != fy:
                changes.append({"symbol": symbol, "old": old, "new": fy, "active": bool(is_active)})

        applied = 0
        if apply:
            for ch in changes:
                if ch["active"]:                        # only correct active symbols
                    cur.execute("UPDATE futures_universe SET lot_size=%s, updated_at=NOW() WHERE symbol=%s",
                                (ch["new"], ch["symbol"]))
                    applied += 1

        # blast radius: real-account client positions whose symbol lot changed. qty was
        # stored as lots*lot_size at insert, so Claude web must re-derive it (we never
        # auto-rewrite client_positions).
        changed_active = [c["symbol"] for c in changes if c["active"]]
        client_flags = []
        if changed_active:
            cur.execute("""SELECT client, symbol, direction, lots, qty FROM client_positions
                           WHERE status='OPEN' AND is_dabba IS NOT TRUE AND symbol = ANY(%s)""",
                        (changed_active,))
            client_flags = [{"client": r[0], "symbol": r[1], "direction": r[2], "lots": r[3], "qty": r[4]}
                            for r in cur.fetchall()]

        report = {
            "fyers_underlyings": len(fyers),
            "universe_symbols": len(current),
            "changed_count": len(changes),
            "changes": changes,
            "applied": applied,
            "apply": apply,
            "missing_from_fyers": missing_from_fyers,
            "missing_count": len(missing_from_fyers),
            "null_lots_before": null_lots,
            "client_positions_to_rederive": client_flags,
        }
        cur.execute("""INSERT INTO ops_log (session_date, session_ts, category, title, details)
                       VALUES (CURRENT_DATE, NOW(), 'lot_size_audit', %s, %s::jsonb)""",
                    (f"{len(changes)} stale lots, {applied} corrected, "
                     f"{len(missing_from_fyers)} absent from Fyers", json.dumps(report)))
        conn.commit()
    log.info(f"lot_size_audit: {len(changes)} stale, {applied} corrected, {len(missing_from_fyers)} absent")
    return report
