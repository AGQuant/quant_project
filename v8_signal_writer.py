def _mood_slots(gate_fails: int) -> tuple:
    """Returns (buy_slots, sell_slots) based on mood gate fails.
    Total slots = 20, mood-adaptive split (15-Jun-2026).
    Strong Bullish (0 fails): 15B / 5S
    Bullish        (1 fail):  12B / 8S
    Neutral        (2 fails): 10B / 10S
    Bearish        (3+ fails): 8B / 12S
    """
    if gate_fails == 0: return 15, 5
    if gate_fails == 1: return 12, 8
    if gate_fails == 2: return 10, 10
    return 8, 12