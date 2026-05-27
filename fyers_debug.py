import hashlib, requests
from datetime import datetime, timedelta
import pytz

FYERS_CLIENT_ID = '1A4STS8ZGD-100'
FYERS_SECRET    = 'YXTIR2MN9V'
AUTH_CODE = input("Auth code: ").strip()

h = hashlib.sha256(f'{FYERS_CLIENT_ID}:{FYERS_SECRET}'.encode()).hexdigest()
r = requests.post('https://api-t1.fyers.in/api/v3/validate-authcode',
    json={'grant_type':'authorization_code','appIdHash':h,'code':AUTH_CODE})
token = r.json()['access_token']
print("Token OK")

IST = pytz.timezone('Asia/Kolkata')
now = datetime.now(IST)

# Test 3 symbols quickly
for sym in ['360ONE', 'ABB', 'ABCAPITAL']:
    fyers_sym = f'NSE:{sym}-EQ'
    print(f"Testing {fyers_sym}...")
    try:
        r2 = requests.get('https://api-t1.fyers.in/data/history',
            params={
                'symbol': fyers_sym,
                'resolution': '1',
                'date_format': '1',
                'range_from': (now - timedelta(days=1)).strftime('%Y-%m-%d'),
                'range_to': now.strftime('%Y-%m-%d'),
                'cont_flag': '1',
            },
            headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'},
            timeout=5)
        d = r2.json()
        candles = d.get('candles', [])
        print(f"  {sym}: {len(candles)} candles | {r2.status_code}")
    except Exception as e:
        print(f"  {sym}: ERROR {e}")
