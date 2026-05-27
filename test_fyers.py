import hashlib
import requests

# ── STEP 1: Get fresh token ──────────────────────────────────────────
CLIENT_ID  = 'PW51BC0LYU-100'
SECRET_KEY = 'CTU0MVC2VS'

# Paste your auth_code from Google redirect URL here
AUTH_CODE = 'PASTE_AUTH_CODE_HERE'

def get_token(auth_code):
    h = hashlib.sha256(f'{CLIENT_ID}:{SECRET_KEY}'.encode()).hexdigest()
    r = requests.post(
        'https://api-t1.fyers.in/api/v3/validate-authcode',
        json={'grant_type': 'authorization_code', 'appIdHash': h, 'code': auth_code}
    )
    data = r.json()
    token = f"{CLIENT_ID}:{data['access_token']}"
    print(f"Token OK: {token[:60]}...")
    return token

# ── STEP 2: Fetch CMP ───────────────────────────────────────────────
def get_cmp(token, symbols):
    sym_str = ','.join(symbols)
    r = requests.get(
        f'https://api-t1.fyers.in/api/v3/quotes?symbols={sym_str}',
        headers={'Authorization': token}
    )
    print(f"Status: {r.status_code}")
    print(r.text[:500])

if __name__ == '__main__':
    token = get_token(AUTH_CODE)
    get_cmp(token, ['NSE:RELIANCE-EQ', 'NSE:TCS-EQ', 'NSE:INFY-EQ'])
