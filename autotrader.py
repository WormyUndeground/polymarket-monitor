"""
Polymarket US Auto-Trader — Roland Garros
Reads smart-money signals from regular Polymarket, mirrors $5 bets on polymarket.us
Requires: PM_KEY_ID and PM_SECRET environment variables
"""
import os, base64, time, json, urllib.request, urllib.parse, sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

PM_KEY_ID = os.environ.get("PM_KEY_ID", "")
PM_SECRET  = os.environ.get("PM_SECRET", "")
NTFY_TOPIC = "wormypolymarket"

# Bankroll & Kelly Criterion sizing
BANKROLL_USD     = float(os.environ.get("BANKROLL_USD", "60"))
KELLY_FRACTION   = 0.25      # quarter-Kelly: safer than full Kelly, still captures most of the edge
MIN_BET_USD      = 5.0       # floor: skip if Kelly says less than this (edge too small)
MAX_BET_USD      = 15.0      # ceiling per single bet

# Conviction-stacking gate (must pass before sizing kicks in)
MIN_HOLDERS      = 2         # at least N elite traders must hold same side
MIN_TOTAL_SIZE   = 5000.0    # combined smart-money $ on player must exceed this

MAX_BETS_PER_RUN = 3         # safety: never spend more than 3 * MAX_BET_USD per cycle
MIN_PROB         = 0.20      # skip near-locks (low ROI)
MAX_PROB         = 0.60      # skip extreme underdogs


def kelly_bet_size(p_pro: float, market_price: float) -> float:
    """Quarter-Kelly fraction of bankroll, given pro-implied probability and market price.
    Returns 0 if no positive edge."""
    if p_pro <= market_price or market_price <= 0 or market_price >= 1:
        return 0.0
    edge_fraction = (p_pro - market_price) / (1 - market_price)
    raw = BANKROLL_USD * edge_fraction * KELLY_FRACTION
    return round(raw, 2)

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
    """Aggregate positions across the elite traders. Returns one signal per
    (match, player) with holder count, total $ size, and median probability."""
    raw_by_key: dict[str, dict] = {}
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
                entry = raw_by_key.setdefault(key, {
                    "player": outcome, "title": title,
                    "holders": [], "probs": [], "total_size": 0.0,
                })
                if name not in entry["holders"]:
                    entry["holders"].append(name)
                    entry["probs"].append(prob)
                    entry["total_size"] += val
        except Exception as e:
            print(f"  [warn] fetch {name}: {e}")

    # Filter by conviction thresholds (sizing happens later, after PMUS price is known)
    signals = []
    for entry in raw_by_key.values():
        n = len(entry["holders"])
        if n < MIN_HOLDERS:
            continue
        if entry["total_size"] < MIN_TOTAL_SIZE:
            continue
        probs = sorted(entry["probs"])
        median_prob = probs[len(probs)//2]
        signals.append({
            "player": entry["player"],
            "prob": median_prob,
            "title": entry["title"],
            "holders": entry["holders"],
            "total_size": entry["total_size"],
        })
    return signals


# ── US market lookup ────────────────────────────────────────────────────────

def get_us_market_by_slug(slug: str) -> dict | None:
    """Fetch a single market on polymarket.us by slug (works for in-progress matches)."""
    status, resp = _us_get(f"/v1/markets/{slug}")
    if status == 200:
        return resp.get("market", resp)
    return None


def fetch_all_rg_matches() -> list[dict]:
    """Fetch every active tennis match on polymarket.us (full list, not 14-result search)."""
    url = "https://gateway.polymarket.us/v1/events?limit=500&closed=false"
    matches = []
    for attempt in range(3):
        try:
            data = _fetch(url)
            for event in data.get("events", []):
                slug = event.get("slug", "")
                if event.get("closed") or event.get("ended"):
                    continue
                if not ("atp" in slug or "wta" in slug):
                    continue
                matches.append(event)
            break
        except Exception as e:
            print(f"  [warn] Events fetch attempt {attempt+1}/3 failed: {e}")
            time.sleep(2)
    if not matches:
        print(f"  [error] Could not fetch any tennis matches this cycle")
    return matches


def find_player_in_matches(player_name: str, matches: list[dict]) -> dict | None:
    """Look up player_name across the pre-fetched tennis match list.
    Requires exact full-name substring match AND PMUS price in MIN_PROB..MAX_PROB range."""
    target = player_name.lower().strip()
    for event in matches:
        for market in event.get("markets", []):
            if market.get("closed"):
                continue
            sides = market.get("marketSides", [])
            for side in sides:
                desc = side.get("description", "").lower()
                # Full-name substring match (prevents Xinyu/Xiyu Wang style collisions)
                if target not in desc:
                    continue
                price = float(side.get("price", 0))
                # Bot will bid 2c above market, so check that bid would still pass range
                bid = price + 0.02
                if not (MIN_PROB <= bid <= MAX_PROB):
                    print(f"    [skip] {side['description']} PMUS price {price:.0%} (bid {bid:.0%}) outside {MIN_PROB:.0%}-{MAX_PROB:.0%}")
                    continue
                intent = "ORDER_INTENT_BUY_LONG" if side["long"] else "ORDER_INTENT_BUY_SHORT"
                return {
                    "slug":        market.get("slug", ""),
                    "player":      side["description"],
                    "price":       price,
                    "intent":      intent,
                    "opponent":    next(
                        (s["description"] for s in sides if s["long"] != side["long"]), "?"
                    ),
                    "event_title": event.get("title", ""),
                }
    return None


def search_us_market(player_name: str, matches: list[dict] | None = None) -> dict | None:
    """Find an active PMUS market featuring player_name."""
    if matches is None:
        matches = fetch_all_rg_matches()
    return find_player_in_matches(player_name, matches)


# ── order placement ─────────────────────────────────────────────────────────

def place_bet(signal: dict, matches: list[dict]) -> bool:
    player = signal["player"]
    market = search_us_market(player, matches)

    if not market:
        print(f"  [skip] No active US market found for '{player}'")
        return False

    slug = market["slug"]
    if slug in placed_bets:
        print(f"  [skip] Already bet on {slug}")
        return False

    # Bid 2 cents above market so the limit order actually crosses and fills
    market_price = market["price"]
    bid_price    = min(round(market_price + 0.02, 2), 0.95)
    price        = bid_price

    # Kelly-sized bet based on edge between pro probability and PMUS market price
    p_pro       = signal["prob"]
    kelly_raw   = kelly_bet_size(p_pro, bid_price)
    edge_pct    = (p_pro - bid_price) * 100
    if kelly_raw < MIN_BET_USD:
        print(f"  [skip] Kelly says ${kelly_raw:.2f} on {player} — edge {edge_pct:.1f}pp too small")
        return False
    bet_size  = min(kelly_raw, MAX_BET_USD)
    quantity  = round(bet_size / bid_price, 4)
    profit    = round(quantity - bet_size, 2)
    print(f"  Edge: pro {p_pro:.0%} vs market {bid_price:.0%}  |  Kelly: ${kelly_raw:.2f}  -> bet ${bet_size:.2f}")

    body = {
        "marketSlug": slug,
        "type":       "ORDER_TYPE_LIMIT",
        "price":      {"value": f"{bid_price:.3f}", "currency": "USD"},
        "quantity":   quantity,
        "tif":        "GTC",
        "intent":     market["intent"],
        "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
    }

    print(f"  Placing ${bet_size:.0f} on {player} (vs {market['opponent']}) @ {price:.0%}")
    print(f"  Conviction: {len(signal.get('holders', []))} traders, ${signal.get('total_size', 0):,.0f} combined")
    print(f"  Market: {slug}  |  Intent: {market['intent']}")
    print(f"  Quantity: {quantity} shares  |  Potential profit: ${profit}")

    status, resp = _us_post("/v1/orders", body)
    print(f"  Response [{status}]: {json.dumps(resp)[:300]}")

    order_id = resp.get("id") or resp.get("orderId")
    if status in (200, 201) and order_id:
        placed_bets[slug] = {
            "orderId":  order_id,
            "player":   player,
            "price":    price,
            "quantity": quantity,
            "placed_at": datetime.now().isoformat(),
        }
        filled = bool(resp.get("executions"))
        status_msg = "FILLED" if filled else "RESTING on book"
        n_holders = len(signal.get("holders", []))
        notify(
            f"Bet placed: {player}",
            f"${bet_size:.0f} on {player} vs {market['opponent']} @ {price:.0%}\n"
            f"{n_holders} pros agreeing, wins ${profit:.2f} ({status_msg})\n{market['event_title']}"
        )
        print(f"  Order accepted: {order_id} | {status_msg}")
        return True
    else:
        print(f"  [error] Order rejected: {resp}")
        return False


# ── main loop ───────────────────────────────────────────────────────────────

def load_existing_positions() -> set[str]:
    """Read actual PMUS positions so restarts don't double-bet."""
    status, resp = _us_get("/v1/portfolio/positions")
    if status != 200:
        print(f"  [warn] Could not load existing positions: {status}")
        return set()
    return set((resp.get("positions") or {}).keys())


def check():
    print(f"\n{'='*60}")
    print(f"  Auto-trader check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # Reload existing positions every cycle so container restarts don't double-bet
    existing = load_existing_positions()
    for slug in existing:
        placed_bets.setdefault(slug, {"loaded": True})
    print(f"  Already holding {len(existing)} positions on PMUS")

    signals = get_smart_money_signals()
    if not signals:
        print("  No Roland Garros smart money signals right now.")
        return

    # Sort by prob — value bets first (lower prob = higher payout)
    signals.sort(key=lambda s: s["prob"])

    # Fetch the RG market universe once per cycle (not per signal)
    matches = fetch_all_rg_matches()
    print(f"\n  Fetched {len(matches)} active RG matches on Polymarket US")

    print(f"\n  {len(signals)} signal(s) passing conviction filter "
          f"(>={MIN_HOLDERS} traders, >=${MIN_TOTAL_SIZE:,.0f} combined, "
          f"prob {MIN_PROB:.0%}-{MAX_PROB:.0%})")
    print(f"  Bankroll: ${BANKROLL_USD}, quarter-Kelly sizing, ${MIN_BET_USD}-${MAX_BET_USD} per bet")
    placed = 0
    for s in signals:
        if placed >= MAX_BETS_PER_RUN:
            print(f"\n  [stop] Hit MAX_BETS_PER_RUN ({MAX_BETS_PER_RUN}); skipping remaining signals.")
            break
        holders = ",".join(s["holders"])
        print(f"\n  Signal: {s['player']} @ {s['prob']:.0%}  |  "
              f"{len(s['holders'])} pros (${s['total_size']:,.0f}): {holders}")
        print(f"    Match: {s['title'][:60]}")
        if place_bet(s, matches):
            placed += 1

    print(f"\n  Done: {placed} new bet(s) placed this cycle (max {MAX_BETS_PER_RUN}).")


if __name__ == "__main__":
    if not PM_KEY_ID or not PM_SECRET:
        print("ERROR: Set PM_KEY_ID and PM_SECRET environment variables.")
        print("Example: $env:PM_KEY_ID='your-key-id'; $env:PM_SECRET='your-secret'")
        sys.exit(1)

    # Quick auth check on startup — try documented endpoint
    print("Polymarket US Auto-Trader starting...")
    print(f"Key: {PM_KEY_ID[:8]}...")
    print(f"Secret length: {len(PM_SECRET)} chars")

    for path in ("/v1/portfolio/positions", "/v1/account", "/v1/account/balance", "/v1/portfolio/balance"):
        status, resp = _us_get(path)
        print(f"  GET {path} -> [{status}] {json.dumps(resp)[:200]}")
        if status == 200:
            print(f"Auth OK via {path}")
            break
    else:
        print("All auth endpoints failed. Continuing anyway to run trade loop...")
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
