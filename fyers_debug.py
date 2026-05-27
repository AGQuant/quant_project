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
range_from = (now - timedelta(days=2)).strftime('%Y-%m-%d')
range_to   = now.strftime('%Y-%m-%d')

r2 = requests.get('https://api-t1.fyers.in/data/history',
    params={
        'symbol': 'NSE:RELIANCE-EQ',
        'resolution': '5',
        'date_format': '1',
        'range_from': range_from,
        'range_to': range_to,
        'cont_flag': '1',
    },
    headers={'Authorization': f'{FYERS_CLIENT_ID}:{token}'}, timeout=10)
print(f"Status: {r2.status_code}")
print(f"Raw: {r2.text[:500]}")
