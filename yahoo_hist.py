import sys
import json
import yfinance as yf
import datetime

def get_symbol(symbol):
    symbol = symbol.upper()
    if symbol == "NIFTY50-INDEX": return "^NSEI"
    if symbol == "NIFTYBANK-INDEX": return "^NSEBANK"
    if symbol == "SENSEX-INDEX": return "^BSESN"
    return symbol.replace("-EQ", ".NS").replace("-BE", ".BO")

def get_history(symbol, days, resolution="D"):
    yahoo_sym = get_symbol(symbol)
    ticker = yf.Ticker(yahoo_sym)

    # Map resolution to yfinance interval
    interval_map = {
        "D": "1d",
        "W": "1wk",
        "5": "5m",
        "15": "15m",
        "60": "1h"
    }
    interval = interval_map.get(resolution, "1d")

    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)

    df = ticker.history(start=str(start), end=str(end), interval=interval)

    candles = []
    for date, row in df.iterrows():
        candles.append({
            "date": date.strftime('%d-%b-%Y'),
            "open": round(row["Open"], 2),
            "high": round(row["High"], 2),
            "low": round(row["Low"], 2),
            "close": round(row["Close"], 2),
            "volume": int(row["Volume"])
        })

    return {
        "symbol": symbol,
        "yahoo_symbol": yahoo_sym,
        "candles": candles,
        "total": len(candles)
    }

if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "RELIANCE-EQ"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 365
    resolution = sys.argv[3] if len(sys.argv) > 3 else "D"
    print(json.dumps(get_history(symbol, days, resolution)))