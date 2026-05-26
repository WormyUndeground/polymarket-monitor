"""
Polymarket US Auto-Trader — Roland Garros
Reads smart-money signals from regular Polymarket, mirrors $5 bets on polymarket.us
Requires: PM_KEY_ID and PM_SECRET environment variables
"""
import os, base64, time, json, urllib.request, urllib.parse, sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PM_KEY_ID = os.environ.get("PM_KEY_ID", "")
PM_SECRET  = os.environ.get("PM_SECRET", "")
NTFY_TOPIC = "wormypolymarket"
BET_USD          = 5.0
MAX_BETS_PER_RUN = 3        # safety: never spend more than 3*BET_USD per cycle
MIN_PROB         = 0.20     # skip near-locks (low ROI)
MAX_PROB         = 0.60     # skip extreme underdogs

TENNIS_TRADERS = [
    ("lovelystuff",  "0x65b54274eba5c76dee6f0fab18a590653811e82f"),
    ("swisstony",    "0x204f72f35326db932158cba6adff0b9a1da95e14"),
    ("ChloeT1",      "0x9ac2536ed93f8fe8ce91d9662b03bcbb19ccbe3d"),
    ("shakendbake",  "0x5e4dbe95f805e27959532f4845e2e4180017b874"),
    ("anon18",       "0x5966db1fe50763c9e3c014d756369bad07e1f804"),
]
KEYWORDS    = ["Roland Garros", "Roland-Garros", "French Open"]
MOBILE_UA   = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"

placed_bets: dict = {}   # market_slug -> order info


# ── helpers ────────────────────────────────────────────────────────────────

def _fetch(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": MOBILE_UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def _auth_headers(method: str, path: str) -> dict:
    ts  = str(int(time.time() * 1000))
    msg = (ts + method.upper() + path).encode()
    raw = base64.b64decode(PM_SECRET + "=" * (-len(PM_SECRET) % 4))
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.from_private_bytes(raw[:32])
    sig = base64.b64encode(key.sign(msg)).decode()
    return {
        "X-PM-Access-Key": PM_KEY_ID,
        "X-PM-Timestamp":  ts,
        "X-PM-Signature":  sig,
        "Accept":          "application/json",
        "User-Agent":      MOBILE_UA,
    }


def _us_get(path: str):
    headers = _auth_headers("GET", path)
    req = urllib.request.Request("https://api.polymarket.us" + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _us_post(path: str, body: dict):
    headers = _auth_headers("POST", path)
    headers["Content-Type"] = "application/json"
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        "https://api.polymarket.us" + path, data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def notify(title: str, msg: str):
    try:
        data = json.dumps({"topic": NTFY_TOPIC, "title": title, "message": msg}).encode()
        req  = urllib.request.Request(
            "https://ntfy.sh", data=data,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"  [warn] ntfy failed: {e}")


# ── signal detection ────────────────────────────────────────────────────────

def get_smart_money_signals() -> list[dict]:
    """Returns list of {player, outcome, prob, title} dicts from top traders."""
    signals = []
    seen    = set()
    for name, wallet in TENNIS_TRADERS:
        try:
            url = f"https://data-api.polymarket.com/positions?user={wallet}&limit=50"
            for p in _fetch(url):
                title = p.get("title", "")
                if not any(k.lower() in title.lower() for k in KEYWORDS):
                    continue
                val  = float(p.get("currentValue", 0) or 0)
                size = float(p.get("size", 0) or 0)
                prob = val / size if size else 0
                if prob < MIN_PROB or prob > MAX_PROB:
                    continue
                outcome = p.get("outcome", "")
                key = f"{title}|{outcome}"
                if key not in seen:
                    seen.add(key)
                    signals.append({"player": outcome, "prob": prob, "title": title})
        except Exception as e:
            print(f"  [warn] fetch {name}: {e}")
    return signals


# ── US market lookup ────────────────────────────────────────────────────────

def search_us_market(player_name: str) -> dict | None:
    """
    Search polymarket.us for an active tennis match featuring player_name.
    Returns market info with slug, price, intent; or None if not found.
    """
    last = player_name.split()[-1]   # use last name for search
    url  = f"https://gateway.polymarket.us/v1/search?q={urllib.parse.quote(last)}&limit=20"
    try:
        data = _fetch(url)
    except Exception as e:
        print(f"  [warn] US search failed: {e}")
        return None

    now = datetime.utcnow()
    for event in data.get("events", []):
        if event.get("closed") or event.get("ended"):
            continue
        for market in event.get("markets", []):
            if market.get("closed"):
                continue
            slug  = market.get("slug", "")
            sides = market.get("marketSides", [])
            for side in sides:
                desc  = side.get("description", "")
                price = float(side.get("price", 0))
                if last.lower() in desc.lower() and 0.05 < price < 0.95:
                    intent = "ORDER_INTENT_BUY_LONG" if side["long"] else "ORDER_INTENT_BUY_SHORT"
                    return {
                        "slug":        slug,
                        "player":      desc,
                        "price":       price,
                        "intent":      intent,
                        "opponent":    next(
                            (s["description"] for s in sides if s["long"] != side["long"]), "?"
                        ),
                        "event_title": event.get("title", ""),
                    }
    return None


# ── order placement ─────────────────────────────────────────────────────────

def place_bet(signal: dict) -> bool:
    player = signal["player"]
    market = search_us_market(player)

    if not market:
        print(f"  [skip] No active US market found for '{player}'")
        return False

    slug = market["slug"]
    if slug in placed_bets:
        print(f"  [skip] Already bet on {slug}")
        return False

    price    = market["price"]
    quantity = round(BET_USD / price, 4)
    profit   = round(quantity - BET_USD, 2)

    body = {
        "marketSlug": slug,
        "type":       "ORDER_TYPE_LIMIT",
        "price":      {"value": f"{price:.3f}", "currency": "USD"},
        "quantity":   quantity,
        "tif":        "GTC",
        "intent":     market["intent"],
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }

    print(f"  Placing ${BET_USD} on {player} (vs {market['opponent']}) @ {price:.0%}")
    print(f"  Market: {slug}  |  Intent: {market['intent']}")
    print(f"  Quantity: {quantity} shares  |  Potential profit: ${profit}")

    status, resp = _us_post("/v1/orders", body)
    print(f"  Response [{status}]: {json.dumps(resp)[:300]}")

    if status in (200, 201) and resp.get("orderId"):
        placed_bets[slug] = {
            "orderId":  resp["orderId"],
            "player":   player,
            "price":    price,
            "quantity": quantity,
            "placed_at": datetime.now().isoformat(),
        }
        notify(
            f"Bet placed: {player}",
            f"${BET_USD} on {player} vs {market['opponent']} @ {price:.0%}\n"
            f"Wins ${profit:.2f} if correct\n{market['event_title']}"
        )
        return True
    else:
        print(f"  [error] Order failed: {resp}")
        return False


# ── main loop ───────────────────────────────────────────────────────────────

def check():
    print(f"\n{'='*60}")
    print(f"  Auto-trader check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    signals = get_smart_money_signals()
    if not signals:
        print("  No Roland Garros smart money signals right now.")
        return

    # Sort by prob — value bets first (lower prob = higher payout)
    signals.sort(key=lambda s: s["prob"])

    print(f"\n  {len(signals)} signal(s) from top traders (filtered {MIN_PROB:.0%}-{MAX_PROB:.0%}):")
    placed = 0
    for s in signals:
        if placed >= MAX_BETS_PER_RUN:
            print(f"\n  [stop] Hit MAX_BETS_PER_RUN ({MAX_BETS_PER_RUN}); skipping remaining signals.")
            break
        print(f"\n  Signal: {s['player']} @ {s['prob']:.0%}  —  {s['title'][:60]}")
        if place_bet(s):
            placed += 1

    print(f"\n  Done: {placed} new bet(s) placed this cycle (max {MAX_BETS_PER_RUN}).")


if __name__ == "__main__":
    if not PM_KEY_ID or not PM_SECRET:
        print("ERROR: Set PM_KEY_ID and PM_SECRET environment variables.")
        print("Example: $env:PM_KEY_ID='your-key-id'; $env:PM_SECRET='your-secret'")
        sys.exit(1)

    # Quick auth check on startup
    print("Polymarket US Auto-Trader starting...")
    print(f"Key: {PM_KEY_ID[:8]}...")
    status, resp = _us_get("/v1/account")
    if status == 200:
        bal = resp.get("buyingPower", resp.get("balance", "?"))
        print(f"Auth OK. Buying power: ${bal}")
    else:
        print(f"Auth check failed [{status}]: {resp}")
        print("Check your PM_KEY_ID and PM_SECRET. Continuing anyway...")
    print()

    while True:
        try:
            check()
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"\n[error] {e}")
        print(f"\n  Next check in 60 minutes.\n")
        time.sleep(60 * 60)
