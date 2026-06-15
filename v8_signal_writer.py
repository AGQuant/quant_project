def _mood_slots(gate_fails: int) -> tuple:
    """Returns (buy_slots, sell_slots) based on mood gate fails.
    Buy slot boost +1 across all tiers (buy_reversal optimisation v1, 15-Jun-2026).
    Strong Bullish: 11/5, Bullish: 9/7, Neutral: 8/8, Bearish: 6/10.
    """
    if gate_fails == 0: return 11, 5
    if gate_fails == 1: return 9, 7
    if gate_fails == 2: return 8, 8
    return 6, 10