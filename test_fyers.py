import hashlib
import requests

CLIENT_ID  = 'PW51BC0LYU-100'
SECRET_KEY = 'CTU0MVC2VS'
PIN        = '2580'

AUTH_CODE = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBfaWQiOiJQVzUxQkMwTFlVIiwidXVpZCI6ImFhMTQ0NjRmZTk1ZTQ1NTFhOTkzNTYxZGYyYzU1YmEzIiwiaXBBZGRyIjoiIiwibm9uY2UiOiIiLCJzY29wZSI6IiIsImRpc3BsYXlfbmFtZSI6IlhBMzIzMTkiLCJvbXMiOiJLMSIsImhzbV9rZXkiOiI1MWJlZDQyODc0N2YxOTA3Zjk0NmUwMGE2MDIyY2IzMjBkOWFiZWVlYTJiNzk1NDZlZjA2M2M0OCIsImlzRGRwaUVuYWJsZWQiOiJOIiwiaXNNdGZFbmFibGVkIjoiTiIsImF1ZCI6IltcImQ6MVwiLFwiZDoyXCIsXCJ4OjBcIixcIng6MVwiLFwieDoyXCJdIiwiZXhwIjoxNzc5OTAwMjc1LCJpYXQiOjE3Nzk4NzAyNzUsImlzcyI6ImFwaS5sb2dpbi5meWVycy5pbiIsIm5iZiI6MTc3OTg3MDI3NSwic3ViIjoiYXV0aF9jb2RlIn0.nsmpJIkR8hKH8Pf0eWKQKsVCg0RtNJdJVPVAtjyL1oc'

def get_token(auth_code):
    h = hashlib.sha256(f'{CLIENT_ID}:{SECRET_KEY}'.encode()).hexdigest()
    r = requests.post(
        'https://api-t1.fyers.in/api/v3/validate-authcode',
        json={'grant_type': 'authorization_code', 'appIdHash': h, 'code': auth_code}
    )
    data = r.json()
    if data.get('code') != 200:
        print('ERROR:', data)
        return None, None
    access_token  = f"{CLIENT_ID}:{data['access_token']}"
    refresh_token = data['refresh_token']
    print(f"\n✅ ACCESS TOKEN:  {access_token[:60]}...")
    print(f"✅ REFRESH TOKEN: {refresh_token[:60]}...")
    return access_token, refresh_token

def refresh_access_token(refresh_token):
    h = hashlib.sha256(f'{CLIENT_ID}:{SECRET_KEY}'.encode()).hexdigest()
    r = requests.post(
        'https://api-t1.fyers.in/api/v3/validate-refresh-token',
        json={'grant_type': 'refresh_token', 'appIdHash': h, 'refresh_token': refresh_token, 'pin': PIN}
    )
    data = r.json()
    print('\nRefresh response:', data)
    return data

def get_cmp(token, symbols):
    sym_str = ','.join(symbols)
    r = requests.get(
        f'https://api-t1.fyers.in/api/v3/quotes?symbols={sym_str}',
        headers={'Authorization': token}
    )
    print(f"\nQuotes status: {r.status_code}")
    print(r.text[:500])

if __name__ == '__main__':
    access_token, refresh_token = get_token(AUTH_CODE)
    if access_token:
        get_cmp(access_token, ['NSE:RELIANCE-EQ', 'NSE:TCS-EQ'])
        print('\n--- Testing refresh token ---')
        refresh_access_token(refresh_token)
