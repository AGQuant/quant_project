            # Total 20 slots, mood-adaptive split (15-Jun-2026)
            if fails == 0:   buy_slots, sell_slots, mood = 15, 5,  "Strong Bullish"
            elif fails == 1: buy_slots, sell_slots, mood = 12, 8,  "Bullish"
            elif fails == 2: buy_slots, sell_slots, mood = 10, 10, "Neutral"
            else:            buy_slots, sell_slots, mood = 8,  12, "Bearish"