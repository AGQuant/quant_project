"""cc#1273: CLIENT_BASKET_REBALANCE_MULTIPLIER_V1
Per-client adaptive basket subscriptions and repair decision support.
Three endpoints: /api/adaptive/baskets/available, /subscribe, /repair.
"""

from fastapi import APIRouter, Query, HTTPException, status
from pydantic import BaseModel
import psycopg
import os
import logging

log = logging.getLogger(__name__)
router = APIRouter()


def _conn():
    """Reuse the common connection pattern from app."""
    return psycopg.connect(os.getenv("DATABASE_URL"))


class SubscribeRequest(BaseModel):
    """POST /api/adaptive/baskets/subscribe request body."""
    portfolio_id: int
    basket_name: str
    multiplier: int


class BasketAvailable(BaseModel):
    """Response item for GET /api/adaptive/baskets/available."""
    basket_name: str
    cap_type: str
    n_positions: int
    min_value_1x: float
    current_multiplier: int | None


class RepairRow(BaseModel):
    """One row in the repair table."""
    symbol: str
    target_qty: int
    actual_qty: int
    diff_qty: int
    action: str  # BUY, SELL, HOLD
    cmp: float
    indicative_value: float


def _get_current_price(cur, symbol: str) -> float:
    """cc#1291: was a third ad-hoc price path hitting v8_metrics_live, a table that does not
    exist in this database (confirmed via information_schema — zero rows, every call fell
    through to the try/except and a hand-rolled intraday_prices fallback, silently, since
    cc#1273 shipped). The app already has ONE canonical resolver, built specifically because
    two independent price-lookup paths once caused a real production bug (RAMCOIND showing two
    different prices on two surfaces at once — cc#343/717/811, history is in price_resolver.py).
    This now delegates to it instead of reinventing a third opinion. Takes the caller's own
    cursor (already open at the one call site) rather than a fresh connection."""
    try:
        import price_resolver
        r = price_resolver.resolve_price(cur, symbol)
        return float(r["price"]) if r and r.get("price") is not None else 0.0
    except Exception as e:
        log.warning(f"Failed to fetch CMP for {symbol}: {e}")
        return 0.0


@router.get("/api/adaptive/baskets/available")
async def get_available_baskets(portfolio_id: int = Query(...)):
    """GET /api/adaptive/baskets/available?portfolio_id={id}
    Returns all active quant baskets with live min_value_1x, and current multiplier if subscribed."""
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                # Validate portfolio exists
                cur.execute("SELECT id FROM hr_portfolios WHERE id=%s", (portfolio_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail=f"Portfolio {portfolio_id} not found")

                # Get all active baskets with position counts and live values
                cur.execute("""
                    SELECT
                        qbr.basket_name,
                        qbr.cap_type,
                        COUNT(qpp.id) as n_positions,
                        COALESCE(SUM(qpp.current_price), 0) as min_value_1x
                    FROM quant_basket_registry qbr
                    LEFT JOIN quant_paper_positions qpp
                        ON qbr.basket_name = qpp.basket_name
                        AND qpp.status = 'open'
                        AND qpp.notes NOT ILIKE 'Cash residual%'
                    WHERE qbr.is_active = true
                    GROUP BY qbr.basket_name, qbr.cap_type
                    ORDER BY qbr.basket_name
                """)
                baskets = cur.fetchall()

                # Get current subscriptions for this portfolio
                cur.execute("""
                    SELECT basket_name, multiplier
                    FROM client_basket_subscription
                    WHERE portfolio_id=%s AND status='active'
                """, (portfolio_id,))
                subscriptions = {row[0]: row[1] for row in cur.fetchall()}

                results = []
                for basket_name, cap_type, n_pos, min_val_1x in baskets:
                    results.append(BasketAvailable(
                        basket_name=basket_name,
                        cap_type=cap_type,
                        n_positions=n_pos,
                        min_value_1x=float(min_val_1x) if min_val_1x else 0.0,
                        current_multiplier=subscriptions.get(basket_name)
                    ))
                return results
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"get_available_baskets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/adaptive/baskets/subscribe")
async def subscribe_basket(req: SubscribeRequest):
    """POST /api/adaptive/baskets/subscribe
    Subscribe or update subscription to a basket at a multiplier (upsert).
    """
    try:
        # Validate inputs
        if req.multiplier < 1 or not isinstance(req.multiplier, int):
            raise HTTPException(
                status_code=400,
                detail="multiplier must be a whole number, 1 or more"
            )

        with _conn() as conn:
            with conn.cursor() as cur:
                # Validate portfolio
                cur.execute("SELECT id FROM hr_portfolios WHERE id=%s", (req.portfolio_id,))
                if not cur.fetchone():
                    raise HTTPException(status_code=404, detail=f"Portfolio {req.portfolio_id} not found")

                # Validate basket
                cur.execute(
                    "SELECT is_active FROM quant_basket_registry WHERE basket_name=%s",
                    (req.basket_name,)
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Basket '{req.basket_name}' not found or inactive"
                    )

                # Upsert: try to update existing active subscription
                cur.execute("""
                    UPDATE client_basket_subscription
                    SET multiplier=%s, updated_at=NOW()
                    WHERE portfolio_id=%s AND basket_name=%s AND status='active'
                    RETURNING id, portfolio_id, basket_name, multiplier, status, created_at, updated_at
                """, (req.multiplier, req.portfolio_id, req.basket_name))

                row = cur.fetchone()
                if row:
                    # Updated existing row
                    result = {
                        "id": row[0],
                        "portfolio_id": row[1],
                        "basket_name": row[2],
                        "multiplier": row[3],
                        "status": row[4],
                        "created_at": str(row[5]),
                        "updated_at": str(row[6])
                    }
                else:
                    # Insert new row
                    cur.execute("""
                        INSERT INTO client_basket_subscription (portfolio_id, basket_name, multiplier, status)
                        VALUES (%s, %s, %s, 'active')
                        RETURNING id, portfolio_id, basket_name, multiplier, status, created_at, updated_at
                    """, (req.portfolio_id, req.basket_name, req.multiplier))
                    row = cur.fetchone()
                    result = {
                        "id": row[0],
                        "portfolio_id": row[1],
                        "basket_name": row[2],
                        "multiplier": row[3],
                        "status": row[4],
                        "created_at": str(row[5]),
                        "updated_at": str(row[6])
                    }

                conn.commit()
                return result
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"subscribe_basket: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/adaptive/baskets/repair")
async def get_repair_sheet(portfolio_id: int = Query(...), basket_name: str = Query(...)):
    """GET /api/adaptive/baskets/repair?portfolio_id={id}&basket_name={name}
    Returns repair table with target vs actual holdings, ready for indicative buy/sell/hold decisions.
    """
    try:
        with _conn() as conn:
            with conn.cursor() as cur:
                # Validate portfolio and active subscription
                cur.execute("""
                    SELECT multiplier
                    FROM client_basket_subscription
                    WHERE portfolio_id=%s AND basket_name=%s AND status='active'
                """, (portfolio_id, basket_name))
                sub_row = cur.fetchone()
                if not sub_row:
                    raise HTTPException(
                        status_code=404,
                        detail=f"No active subscription for portfolio {portfolio_id}, basket {basket_name}"
                    )
                multiplier = sub_row[0]

                # Get basket's target list (live open positions, excluding cash residual)
                cur.execute("""
                    SELECT symbol, current_price
                    FROM quant_paper_positions
                    WHERE basket_name=%s AND status='open' AND notes NOT ILIKE 'Cash residual%'
                    ORDER BY symbol
                """, (basket_name,))
                target_positions = cur.fetchall()
                target_symbols = {row[0] for row in target_positions}

                # Get actual holdings from hr_holdings for this portfolio (only for target symbols)
                cur.execute("""
                    SELECT symbol, quantity
                    FROM hr_holdings
                    WHERE portfolio_id=%s AND symbol=ANY(%s)
                    ORDER BY symbol
                """, (portfolio_id, list(target_symbols)))
                actual_holdings = {row[0]: row[1] for row in cur.fetchall()}

                # Build repair table: one row per target symbol
                rows = []
                for symbol, current_price in target_positions:
                    target_qty = multiplier  # 1x = 1 share per symbol
                    actual_qty = actual_holdings.get(symbol, 0)
                    diff_qty = target_qty - actual_qty

                    if diff_qty > 0:
                        action = "BUY"
                    elif diff_qty < 0:
                        action = "SELL"
                    else:
                        action = "HOLD"

                    # Get live CMP (cc#1291: canonical resolver, caller's own cursor)
                    cmp = _get_current_price(cur, symbol)
                    indicative_value = abs(diff_qty) * cmp

                    rows.append({
                        "symbol": symbol,
                        "target_qty": target_qty,
                        "actual_qty": actual_qty,
                        "diff_qty": diff_qty,
                        "action": action,
                        "cmp": round(cmp, 2),
                        "indicative_value": round(indicative_value, 2)
                    })

                    # Insert audit log row
                    cur.execute("""
                        INSERT INTO client_basket_repair_log
                        (portfolio_id, basket_name, symbol, target_qty, actual_qty, diff_qty, action, multiplier_used)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (portfolio_id, basket_name, symbol, target_qty, actual_qty, diff_qty, action, multiplier))

                conn.commit()

                return {
                    "note": "Indicative only — not an order. Reconfirm price before dealing.",
                    "portfolio_id": portfolio_id,
                    "basket_name": basket_name,
                    "multiplier_used": multiplier,
                    "rows": rows
                }
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"get_repair_sheet: {e}")
        raise HTTPException(status_code=500, detail=str(e))
