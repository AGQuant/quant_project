import sys
import yfinance as yf
import json

SYMBOL_MAP = {
    "NIFTY50-INDEX": "^NSEI",
    "BANKNIFTY-INDEX": "^NSEBANK",
    "SENSEX-INDEX": "^BSESN",
}

def get_yahoo_symbol(symbol):
    if symbol in SYMBOL_MAP:
        return SYMBOL_MAP[symbol]
    base = symbol.replace("-EQ", "").replace("-BE", "").replace("-SM", "")
    return base + ".NS"

def get_quote(symbol):
    yahoo_symbol = get_yahoo_symbol(symbol)
    ticker = yf.Ticker(yahoo_symbol)
    info = ticker.fast_info
    result = {
        "symbol": symbol,
        "yahoo_symbol": yahoo_symbol,
        "last_price": round(info.last_price, 2),
        "prev_close": round(info.previous_close, 2),
        "change": round(info.last_price - info.previous_close, 2),
        "change_pct": round(((info.last_price - info.previous_close) / info.previous_close) * 100, 2),
        "day_high": round(info.day_high, 2),
        "day_low": round(info.day_low, 2),
    }
    return result

if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "NIFTY50-INDEX"
    result = get_quote(symbol)
    print(json.dumps(result))