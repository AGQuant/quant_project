import hashlib
import requests

CLIENT_ID  = 'PW51BC0LYU-100'
SECRET_KEY = 'CTU0MVC2VS'
PIN        = '2580'

# Paste latest auth_code here each morning
AUTH_CODE = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBfaWQiOiJQVzUxQkMwTFlVIiwidXVpZCI6ImFhMTQ0NjRmZTk1ZTQ1NTFhOTkzNTYxZGYyYzU1YmEzIiwiaXBBZGRyIjoiIiwibm9uY2UiOiIiLCJzY29wZSI6IiIsImRpc3BsYXlfbmFtZSI6IlhBMzIzMTkiLCJvbXMiOiJLMSIsImhzbV9rZXkiOiI1MWJlZDQyODc0N2YxOTA3Zjk0NmUwMGE2MDIyY2IzMjBkOWFiZWVlYTJiNzk1NDZlZjA2M2M0OCIsImlzRGRwaUVuYWJsZWQiOiJOIiwiaXNNdGZFbmFibGVkIjoiTiIsImF1ZCI6IltcImQ6MVwiLFwiZDoyXCIsXCJ4OjBcIixcIng6MVwiLFwieDoyXCJdIiwiZXhwIjoxNzc5OTAwMjc1LCJpYXQiOjE3Nzk4NzAyNzUsImlzcyI6ImFwaS5sb2dpbi5meWVycy5pbiIsIm5iZiI6MTc3OTg3MDI3NSwic3ViIjoiYXV0aF9jb2RlIn0.nsmpJIkR8hKH8Pf0eWKQKsVCg0RtNJdJVPVAtjyL1oc'

QUOTE_ENDPOINTS = [
    'https://api-t1.fyers.in/api/v3/quotes',
    'https://api.fyers.in/api/v3/quotes',
    'https://api-t1.fyers.in/data-rest/v3/quotes',
    'https://api.fyers.in/data-rest/v3/quotes',
]

def get_token(auth_code):
    h = hashlib.sha256(f'{CLIENT_ID}:{SECRET_KEY}'.encode()).hexdigest()
    r = requests.post(
        'https://api-t1.fyers.in/api/v3/validate-authcode',
        json={'grant_type': 'authorization_code', 'appIdHash': h, 'code': auth_code}
    )
    data = r.json()
    if data.get('code') != 200:
        print('TOKEN ERROR:', data)
        return None
    token = f"{CLIENT_ID}:{data['access_token']}"
    print(f"✅ Token OK")
    return token

def test_quotes(token):
    for url in QUOTE_ENDPOINTS:
        r = requests.get(
            url,
            params={'symbols': 'NSE:RELIANCE-EQ'},
            headers={'Authorization': token},
            timeout=5
        )
        print(f"{r.status_code} | {url} | {r.text[:80]}")

if __name__ == '__main__':
    token = get_token(AUTH_CODE)
    if token:
        test_quotes(token)
