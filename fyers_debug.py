import hashlib, requests
from datetime import datetime, timedelta
import pytz

FYERS_CLIENT_ID = '1A4STS8ZGD-100'
FYERS_SECRET    = 'YXTIR2MN9V'
AUTH_CODE = input("Auth code: ").strip()

h = hashlib.sha256(f'{FYERS_CLIENT_ID}:{FYERS_SECRET}'.encode()).hexdigest()
r = requests.post('https://api-t1.fyers.in/api/v3/validate-authcode',
    json={'grant_type':'authorization_code','appIdHash':h,'code':AUTH_CODE})
d = r.json()
token = d['access_token']
print(f"Token OK")

IST = pytz.timezone('Asia/Kolkata')
now = datetime.now(IST)
range_from = int((now - timedelta(days=2)).timestamp())
range_to   = int(now.timestamp())

# Test multiple endpoint variants
endpoints = [
    'https://api-t1.fyers.in/data/history',
    'https://api.fyers.in/data-rest/v3/history',
    'https://api-t1.fyers.in/api/v3/data/history',
]

params = {
    'symbol': 'NSE:RELIANCE-EQ',
    'resolution': '5',
    'date_format': '1',
    'range_from': range_from,
    'range_to': range_to,
    'cont_flag': '1',
}

for url in endpoints:
    r2 = requests.get(url, params=params,
        headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'}, timeout=10)
    print(f"\n{url}")
    print(f"Status: {r2.status_code} | {r2.text[:150]}")
