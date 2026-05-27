import hashlib
import requests

CLIENT_ID  = 'PW51BC0LYU-100'
SECRET_KEY = 'CTU0MVC2VS'

AUTH_CODE = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBfaWQiOiJQVzUxQkMwTFlVIiwidXVpZCI6ImFhMTQ0NjRmZTk1ZTQ1NTFhOTkzNTYxZGYyYzU1YmEzIiwiaXBBZGRyIjoiIiwibm9uY2UiOiIiLCJzY29wZSI6IiIsImRpc3BsYXlfbmFtZSI6IlhBMzIzMTkiLCJvbXMiOiJLMSIsImhzbV9rZXkiOiI1MWJlZDQyODc0N2YxOTA3Zjk0NmUwMGE2MDIyY2IzMjBkOWFiZWVlYTJiNzk1NDZlZjA2M2M0OCIsImlzRGRwaUVuYWJsZWQiOiJOIiwiaXNNdGZFbmFibGVkIjoiTiIsImF1ZCI6IltcImQ6MVwiLFwiZDoyXCIsXCJ4OjBcIixcIng6MVwiLFwieDoyXCJdIiwiZXhwIjoxNzc5OTAwMjc1LCJpYXQiOjE3Nzk4NzAyNzUsImlzcyI6ImFwaS5sb2dpbi5meWVycy5pbiIsIm5iZiI6MTc3OTg3MDI3NSwic3ViIjoiYXV0aF9jb2RlIn0.nsmpJIkR8hKH8Pf0eWKQKsVCg0RtNJdJVPVAtjyL1oc'

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
    return f"{CLIENT_ID}:{data['access_token']}"

def test_quotes(token):
    headers = {'Authorization': token}
    symbol  = 'NSE:RELIANCE-EQ'

    tests = [
        ('GET',  'https://api.fyers.in/data-rest/v3/quotes',  {'symbols': symbol}),
        ('POST', 'https://api.fyers.in/data-rest/v3/quotes',  {'symbols': symbol}),
        ('GET',  'https://api-t1.fyers.in/api/v3/quotes',     {'symbols': symbol}),
        ('POST', 'https://api-t1.fyers.in/api/v3/quotes',     None),
    ]

    for method, url, params in tests:
        try:
            if method == 'GET':
                r = requests.get(url, params=params, headers=headers, timeout=5)
            else:
                r = requests.post(url, json=params or {'symbols': symbol}, headers=headers, timeout=5)
            print(f"{method} {r.status_code} | {url.split('fyers.in')[1]} | {r.text[:100]}")
        except Exception as e:
            print(f"{method} ERROR | {url} | {e}")

if __name__ == '__main__':
    token = get_token(AUTH_CODE)
    if token:
        print(f"✅ Token OK\n")
        test_quotes(token)
