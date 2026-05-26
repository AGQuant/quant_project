"""
V8 Futures Universe Management
- Upload/refresh authoritative F&O stock list
- Replaces using v5_signals as proxy
- Used by Buy/Sell Reversal + Momentum baskets
"""

from fastapi import APIRouter, HTTPException, Request
from typing import Optional, List
import psycopg
import os

router = APIRouter(prefix="/api/v8/futures", tags=["v8-futures"])

def _conn():
    return psycopg.connect(os.getenv("DATABASE_URL"))


@router.get("/list")
def list_futures(active_only: bool = True):
    """Get current futures universe."""
    try:
        with _conn() as conn, conn.cursor() as cur:
            if active_only:
                cur.execute("""
                    SELECT symbol, lot_size, segment, is_active, updated_at
                    FROM futures_universe
                    WHERE is_active = TRUE
                    ORDER BY symbol
                """)
            else:
                cur.execute("""
                    SELECT symbol, lot_size, segment, is_active, updated_at
                    FROM futures_universe
                    ORDER BY symbol
                """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"count": len(rows), "stocks": rows}
    except Exception as e:
        raise HTTPException(500, f"list_futures failed: {e}")


@router.post("/upload")
async def upload_futures(req: Request):
    """Replace entire futures universe with new list."""
    body = await req.json()
    stocks_input = body.get("stocks", [])
    if not stocks_input:
        raise HTTPException(400, "stocks list required")

    stocks = []
    for s in stocks_input:
        if isinstance(s, str):
            stocks.append({"symbol": s.strip().upper(), "lot_size": None})
        elif isinstance(s, dict):
            stocks.append({"symbol": s.get("symbol", "").strip().upper(), "lot_size": s.get("lot_size")})
    stocks = [s for s in stocks if s["symbol"]]
    if not stocks:
        raise HTTPException(400, "no valid symbols")

    try:
        with _conn() as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE futures_universe")
            for s in stocks:
                cur.execute("""
                    INSERT INTO futures_universe (symbol, lot_size, is_active)
                    VALUES (%s, %s, TRUE)
                    ON CONFLICT (symbol) DO UPDATE
                    SET lot_size = EXCLUDED.lot_size, is_active = TRUE, updated_at = NOW()
                """, (s["symbol"], s["lot_size"]))
            conn.commit()
        return {"status": "ok", "action": "replaced", "count": len(stocks), "sample": [s["symbol"] for s in stocks[:5]]}
    except Exception as e:
        raise HTTPException(500, f"upload_futures failed: {e}")


@router.post("/add")
async def add_futures(req: Request):
    """Add stocks to futures universe."""
    body = await req.json()
    stocks = body.get("stocks", [])
    if not stocks:
        raise HTTPException(400, "stocks list required")
    added = 0
    try:
        with _conn() as conn, conn.cursor() as cur:
            for s in stocks:
                sym = s.upper().strip() if isinstance(s, str) else s.get("symbol", "").upper().strip()
                lot = s.get("lot_size") if isinstance(s, dict) else None
                if not sym:
                    continue
                cur.execute("""
                    INSERT INTO futures_universe (symbol, lot_size, is_active)
                    VALUES (%s, %s, TRUE)
                    ON CONFLICT (symbol) DO UPDATE
                    SET is_active = TRUE, updated_at = NOW()
                """, (sym, lot))
                added += 1
            conn.commit()
        return {"status": "ok", "added": added}
    except Exception as e:
        raise HTTPException(500, f"add_futures failed: {e}")


@router.post("/remove")
async def remove_futures(req: Request):
    """Deactivate stocks (soft remove)."""
    body = await req.json()
    stocks = body.get("stocks", [])
    if not stocks:
        raise HTTPException(400, "stocks list required")
    try:
        with _conn() as conn, conn.cursor() as cur:
            for sym in stocks:
                cur.execute("""
                    UPDATE futures_universe SET is_active = FALSE, updated_at = NOW()
                    WHERE symbol = %s
                """, (sym.upper().strip(),))
            conn.commit()
        return {"status": "ok", "removed": len(stocks)}
    except Exception as e:
        raise HTTPException(500, f"remove_futures failed: {e}")
