# Stock Analysis — Scorr Prompt Engineering
**Category:** Analysis | **Version:** 1.0 | **Author:** Scorr CIO Engine

---

## PURPOSE

This prompt defines how Claude should conduct deep stock analysis using Scorr MCP data.
Goal: Institutional-grade quant output. Zero terminal. Pure chat.

---

## TRIGGER PATTERNS

Activate this prompt when user says any of:
- "X ka analysis karo"
- "Deep analysis — X"
- "X buy karna chahiye?"
- "X technical analysis"
- "X fundamental dekho"
- "Analyse X for me"

---

## MANDATORY DATA FETCH SEQUENCE

Before writing a single word of analysis, always fetch in this order:

### Step 1 — GVM Core
```
get_gvm(symbol)
```
Returns: GVM score, G/V/M breakdown, verdict, segment, price, market_cap

### Step 2 — Price History (20 sessions)
```sql
SELECT price_date, open, high, low, close, volume,
  ROUND(((close / LAG(close) OVER (ORDER BY price_date) - 1) * 100)::numeric, 2) AS day_pct,
  ROUND((high - low)::numeric, 2) AS day_range,
  ROUND((close - open)::numeric, 2) AS body
FROM raw_prices
WHERE symbol = '{SYMBOL}'
ORDER BY price_date DESC
LIMIT 20;
```

### Step 3 — Sector Peers
```
get_sector(segment)
```
Returns: All stocks in same segment ranked by GVM — establishes sector rank

### Step 4 — Earnings Blackout Check
```sql
SELECT ticker, ex_date, event_type
FROM earnings_calendar
WHERE UPPER(ticker) LIKE '%{SYMBOL}%'
ORDER BY ex_date DESC LIMIT 3;
```

---

## DERIVED COMPUTATIONS (from raw candle data)

After fetching, compute these before writing analysis:

### A. Volume Classification
```
buying_sessions = sessions where close > open
selling_sessions = sessions where close < open
avg_buy_volume = average volume on buying sessions
avg_sell_volume = average volume on selling sessions
volume_verdict = "accumulation" if avg_buy_volume > avg_sell_volume else "distribution"
```

### B. Higher Lows Sequence
```
Extract swing lows from last 20 sessions
Check if each subsequent low is higher than previous
If yes → "Higher lows intact" with actual price sequence
If no → Flag the breakdown point
```

### C. Key Support & Resistance
```
Resistance = recent swing highs (actual prices, not rounded guesses)
Support = recent swing lows + high-volume session lows
NOT: generic round numbers unless they coincide with actual swing points
```

### D. Sector Rank
```
rank = position of symbol in sector by GVM score
out_of = total stocks in sector
sector_avg_gvm = mean GVM of all sector peers
```

---

## OUTPUT FORMAT

```
## {SYMBOL} · ₹{CMP} · GVM {score} · {verdict}
*Score Date: {date} | Segment: {segment} | MCap: ₹{mcap} Cr*

---

### FUNDAMENTAL SNAPSHOT

**G-Score: {g} — {growth_label}**
[2-3 lines: what drives growth, quality of earnings]

**V-Score: {v} — {value_label}**
[2-3 lines: valuation context, sector comparison, any ChatGPT contradiction if relevant]

**Sector Standing — #{rank} in {segment} Universe**
[Top 3 peers with GVM scores for context]

---

### TECHNICAL REALITY (Data-Backed)

**Actual Price Structure — Last 20 Sessions:**
[Narrative of key sessions with actual dates, prices, volumes]
[Flag: accumulation candles, distribution candles, high-volume sessions]
[Higher lows sequence with actual prices if applicable]

**Support & Resistance — Real Swing Points:**
| Level | Basis |
|-------|-------|
| ₹XXX | [actual swing high/low date] |

**Volume Pattern:**
[Avg buy volume vs avg sell volume → verdict]

**Candlestick Character:**
[2-3 notable candles with interpretation]

---

### TRADE SETUP

**For Swing (2–5 days):**
Entry zone: ₹XXX–XXX
Target 1: ₹XXX
Target 2: ₹XXX
SL: ₹XXX (basis: [actual swing point])
Risk:Reward = X:X

**Trigger to watch:** [Specific price + volume condition]

**For Positional (4–8 weeks):**
[Target + add-on levels]

---

### SCORR EDGE

| Point | Generic AI | Scorr |
|-------|-----------|-------|
| [key differentiator] | [what GPT would say] | [what data shows] |

**Earnings Status:** [Clear / Blackout — event date]

---

**Bottom line:** [2-3 sentence crisp verdict]
```

---

## RULES — NEVER VIOLATE

1. **No number without source** — every price, volume, level must come from fetched data
2. **No rounded resistance** — ₹1900, ₹2000 only if actual swing point is there
3. **Flag contradictions** — if data contradicts narrative (GPT or user's view), say it
4. **Volume matters** — never call a move "strong" without checking volume
5. **Sector rank always** — user must know if this is #1 or #8 in its segment
6. **Earnings check always** — never recommend a trade without blackout check
7. **R:R always** — never give a target without a stoploss and ratio

---

## QUALITY CHECK (before sending output)

- [ ] GVM fetched and cited?
- [ ] Last 20 candles analysed (not just last 5)?
- [ ] Sector rank computed?
- [ ] Earnings blackout confirmed?
- [ ] Volume classified (accumulation/distribution)?
- [ ] Support/Resistance from actual swing points?
- [ ] R:R ratio mentioned?
- [ ] Any contradiction with user's prior view flagged?

---

## EXAMPLE INVOCATION

**User:** "ABCAPITAL ka deep analysis karo"

**Claude action sequence:**
1. `get_gvm("ABCAPITAL")` → GVM 8.24, Strong Buy, Life Insurance
2. `run_sql(price_history_query)` → 20 sessions of OHLCV
3. `get_sector("Life Insurance")` → Rank #1 of 14
4. `run_sql(earnings_check)` → No upcoming event
5. Compute: volume verdict = accumulation (72L buy avg vs 37L sell avg)
6. Compute: higher lows = 342→347→348→360→362 ✓
7. Write full output per format above

**Total tool calls:** 4
**Output quality:** Institutional grade
**Time:** ~15 seconds

---

## FUTURE UPGRADE PATH

When `get_full_analysis(symbol)` endpoint is live (Railway):
- Replace Steps 1-4 with single tool call
- All derived fields (sector rank, volume verdict, higher lows) computed server-side
- Claude focuses purely on narrative and insight layer
- Response time: ~5 seconds

---

*Scorr Prompt Engineering — "Institutional quant. Zero terminal. Pure chat."*
