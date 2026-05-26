"""
Quick auth test — verifies Ed25519 signing against polymarket.us
Run: python test_auth.py
Set env vars: PM_KEY_ID, PM_SECRET
"""
import os, base64, time, json, urllib.request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

KEY_ID = os.environ.get("PM_KEY_ID", "")
SECRET  = os.environ.get("PM_SECRET", "")

BASE_URL = "https://api.polymarket.us"
MOBILE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"


def make_headers(method: str, path: str) -> dict:
    ts = str(int(time.time() * 1000))
    msg = (ts + method.upper() + path).encode()
    raw = base64.b64decode(SECRET + "=" * (4 - len(SECRET) % 4) if len(SECRET) % 4 else SECRET)
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    sig = base64.b64encode(key.sign(msg)).decode()
    return {
        "X-PM-Access-Key": KEY_ID,
        "X-PM-Timestamp":  ts,
        "X-PM-Signature":  sig,
        "Accept":          "application/json",
        "User-Agent":      MOBILE_UA,
    }


def fetch(method, path, body=None):
    url = BASE_URL + path
    headers = make_headers(method, path)
    data = json.dumps(body).encode() if body else None
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


if __name__ == "__main__":
    if not KEY_ID or not SECRET:
        print("ERROR: Set PM_KEY_ID and PM_SECRET environment variables")
        exit(1)

    print(f"KEY_ID: {KEY_ID}")
    print(f"SECRET length: {len(SECRET)} chars")
    print()

    # Test 1: GET portfolio positions
    print("--- Test 1: GET /v1/portfolio/positions ---")
    status, resp = fetch("GET", "/v1/portfolio/positions")
    print(f"Status: {status}")
    print(json.dumps(resp, indent=2)[:800])
    print()

    # Test 2: GET account balance
    print("--- Test 2: GET /v1/account ---")
    status, resp = fetch("GET", "/v1/account")
    print(f"Status: {status}")
    print(json.dumps(resp, indent=2)[:800])
