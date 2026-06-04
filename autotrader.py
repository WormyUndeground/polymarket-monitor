"""
Polymarket US Auto-Trader — Roland Garros
Reads smart-money signals from regular Polymarket, mirrors $5 bets on polymarket.us
Requires: PM_KEY_ID and PM_SECRET environment variables
"""
import os, re, base64, time, json, urllib.request, urllib.parse, sys, threading
from datetime import datetime, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

PM_KEY_ID = os.environ.get("PM_KEY_ID", "")
PM_SECRET  = os.environ.get("PM_SECRET", "")
NTFY_TOPIC = "wormypolymarket"

# Execution venue: "pmus" (default, Polymarket US) or "global" (polymarket.com via CLOB).
# Global trades the SAME market the smart-money signal came from (no PMUS bridge,
# no listing lag, no opponent matching) — it bets directly on the signal's token.
EXEC_VENUE       = os.environ.get("EXEC_VENUE", "pmus").strip().lower()
PM_GLOBAL_KEY    = os.environ.get("PM_GLOBAL_KEY", "")     # Polygon wallet private key (signs orders)
PM_GLOBAL_FUNDER = os.environ.get("PM_GLOBAL_FUNDER", "")  # email/magic proxy wallet (holds USDC)
CLOB_HOST        = "https://clob.polymarket.com"
CLOB_CHAIN_ID    = 137                                     # Polygon mainnet
CLOB_SIG_TYPE    = 1                                       # 1 = email/magic proxy wallet

# Bankroll & Kelly Criterion sizing
BANKROLL_USD     = float(os.environ.get("BANKROLL_USD", "50"))
KELLY_FRACTION   = 0.25      # quarter-Kelly: safer than full Kelly, still captures most of the edge
MIN_BET_USD      = 5.0       # floor: skip if Kelly says less than this (edge too small)
MAX_BET_USD      = 15.0      # ceiling per single bet

# Conviction-stacking gate (must pass before sizing kicks in)
MIN_HOLDERS      = 2         # at least N elite traders must hold same side
MIN_TOTAL_SIZE   = 5000.0    # combined smart-money $ on player must exceed this

MAX_BETS_PER_RUN = 3         # safety: never spend more than 3 * MAX_BET_USD per cycle
MIN_PROB         = 0.20      # skip near-locks (low ROI)
MAX_PROB         = 0.60      # skip extreme underdogs

# Total-exposure cap: the most the bot is allowed to have at risk across all
# open positions at once. Counts the cost of positions already held plus bets
# placed this cycle, so the bot can't overcommit the bankroll. Defaults to the
# full bankroll; override with the MAX_EXPOSURE_USD env var.
MAX_EXPOSURE_USD = float(os.environ.get("MAX_EXPOSURE_USD", str(BANKROLL_USD)))

# Conviction-tiered bet ceiling: with 20 traders followed, more of them backing
# the same side = stronger signal = larger allowed bet. The tier sets the *cap*;
# Kelly still sizes within it based on the price edge (so a thin edge stays small
# even at high conviction). Checked highest-threshold first.
CONVICTION_TIERS = [
    (7, 15.0),   # 7+ pros (~35% consensus) -> up to $15
    (4, 10.0),   # 4-6 pros               -> up to $10
    (2,  5.0),   # 2-3 pros (gate floor)  -> up to $5
]


def conviction_cap(n_holders: int) -> float:
    for threshold, cap in CONVICTION_TIERS:
        if n_holders >= threshold:
            return cap
    return MIN_BET_USD

# Anti-chase guard (percentage points). We copy the pros even when PMUS has drifted
# UP from their entry price, but refuse to chase a market that has already run more
# than this far past what they paid. 15pp = "medium": fires on most real signals,
# skips runaway moves. Tune via env var.
MAX_CHASE_PP = float(os.environ.get("MAX_CHASE_PP", "15"))

# Fresh-buy window: only count traders who *bought* a side within this many hours,
# not anyone still holding a stale/underwater position. 79% of the pool's RG buys
# land within 24h, so this captures pre-match conviction while dropping old bags.
FRESH_BUY_HOURS = float(os.environ.get("FRESH_BUY_HOURS", "24"))

# Dry-run mode: run the full pipeline (signals, guards, opponent check, sizing,
# build the real order body) but DON'T submit the order — just log what it would do.
# Flip on with DRY_RUN=1 to test a live cycle without risking money.
DRY_RUN = os.environ.get("DRY_RUN", "").strip() in ("1", "true", "True", "yes")

# How often to run a full cycle. Tightened from 60 -> 15 min: global Polymarket
# lists next-round markets ahead of PMUS, so the bettable window (PMUS has the
# market AND the match hasn't started) is narrow. Checking 4x as often means the
# bot grabs a market the moment PMUS posts it, before tip-off. Tune via env var.
CHECK_INTERVAL_MIN = float(os.environ.get("CHECK_INTERVAL_MIN", "15"))


def kelly_bet_size(p_pro: float, market_price: float) -> float:
    """Quarter-Kelly fraction of bankroll, given pro-implied probability and market price.
    Returns 0 if no positive edge."""
    if p_pro <= market_price or market_price <= 0 or market_price >= 1:
        return 0.0
    edge_fraction = (p_pro - market_price) / (1 - market_price)
    raw = BANKROLL_USD * edge_fraction * KELLY_FRACTION
    return round(raw, 2)

TENNIS_TRADERS = [
    # original active anchors
    ("swisstony",       "0x204f72f35326db932158cba6adff0b9a1da95e14"),
    ("anon18",          "0x5966db1fe50763c9e3c014d756369bad07e1f804"),
    ("HomeRunHazard",   "0x5268527977f700f9bf9b6d5cd843859e4e70135d"),
    ("ferrariChampions","0xfe787d2da716d60e8acff57fb87eb13cd4d10319"),
    ("strike123",       "0xf284ad6d607f777f34bc643cea587c33a886b9f9"),
    # 15 added — active tennis traders, recurring co-holders with the anchors
    ("RN1",             "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"),
    ("mooseborzoi",     "0x84cfffc3f16dcc353094de30d4a45226eccd2f63"),
    ("degenfren",       "0x979fc186184ae75754d32c4ef68d8ca00f744032"),
    ("SpaceEx",         "0xa16a1302ca05463f30faebeb5c045767fde233a1"),
    ("anon5375",        "0x53757615de1c42b83f893b79d4241a009dc2aeea"),
    ("LYnetLY",         "0x1eaf5d5f822dc5211c25b5839e5b7aa70f319bf0"),
    ("tradecraft",      "0xde9f7f4e77a1595623ceb58e469f776257ccd43c"),
    ("benwyatt",        "0x1117eade222413335b7ec959e5b48c1d3dbc3532"),
    ("sentrio",         "0xdb83e85ffd22faa4009273034770f96ffc5b1e50"),
    ("NewTeamSosed4",   "0x437961a3b2684a4835da753e894d4b5cffdb2e16"),
    ("KnightDasCapital","0xadfb6cba33cebca02eab6111ace1e3924b9cc2ef"),
    ("anonE907",        "0xe9076a87c5ed90ef16e6fe6529c943baeca0cff6"),
    ("mwenya",          "0xde0463ea7f611b065e8ab06bbfbddad75e6dfa37"),
    ("LBAIsport",       "0xcb1bcdee78e4b50e64aabb109cf4be33dbb569f2"),
    ("cigarettes",      "0xd218e474776403a330142299f7796e8ba32eb5c9"),
]
KEYWORDS    = ["Roland Garros", "Roland-Garros", "French Open"]
MOBILE_UA   = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15"

placed_bets: dict = {}   # market_slug -> order info
TRADE_LOG = os.environ.get("TRADE_LOG") or ("/data/trades.json" if os.path.isdir("/data") else "trades.json")


def append_trade_log(entry: dict):
    """Append a trade entry to the shared trade log file."""
    try:
        existing = []
        if os.path.exists(TRADE_LOG):
            with open(TRADE_LOG) as f:
                existing = json.load(f)
        existing.append(entry)
        with open(TRADE_LOG, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        print(f"  [warn] trade log write failed: {e}")


# ── resolution tracking ──────────────────────────────────────────────────────
# When a bet's market resolves, PMUS drops it from open positions. We detect that
# disappearance and classify WON/LOST from the last price we saw before it left
# (tennis prices converge to ~1.0/0.0 by the time a market closes). Outputs land
# in resolved_trades.json, which the dashboard merges into Closed Trade History.

_DATA_DIR    = os.path.dirname(TRADE_LOG) or "."
RESOLVED_LOG = os.environ.get("RESOLVED_LOG") or os.path.join(_DATA_DIR, "resolved_trades.json")
SNAPSHOT_LOG = os.path.join(_DATA_DIR, "position_snapshots.json")
WIN_PRICE    = 0.80   # last-seen price at/above this => treat as WON
LOSS_PRICE   = 0.20   # last-seen price at/below this => treat as LOST


def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception as e:
        print(f"  [warn] could not read {path}: {e}")
    return default


def _save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"  [warn] could not write {path}: {e}")


def _price_for(slug: str, player: str, matches: list[dict]):
    """Current PMUS price for our side of a market, or None if not in the feed."""
    target = (player or "").lower().strip()
    for event in matches:
        for market in event.get("markets", []):
            if market.get("slug") != slug:
                continue
            for side in market.get("marketSides", []):
                if target and target in side.get("description", "").lower():
                    try:
                        return float(side.get("price", 0))
                    except (TypeError, ValueError):
                        return None
    return None


def track_resolutions(matches: list[dict], open_slugs: set[str]):
    """Snapshot open bot positions and record any that have just resolved."""
    placements = _load_json(TRADE_LOG, [])
    if not placements:
        return
    snapshots = _load_json(SNAPSHOT_LOG, {})
    resolved  = _load_json(RESOLVED_LOG, [])
    already   = {r.get("slug") for r in resolved if r.get("slug")}

    # latest placement per slug (most recent bet on that market)
    by_slug = {}
    for t in placements:
        if t.get("slug"):
            by_slug[t["slug"]] = t

    newly = []
    for slug, t in by_slug.items():
        player = t.get("player", "?")
        cost   = float(t.get("bet_size", 0) or 0)
        qty    = float(t.get("quantity", 0) or 0)
        if slug in open_slugs:
            # still held — refresh last-seen price
            price = _price_for(slug, player, matches)
            snap = snapshots.get(slug, {})
            snap.update({
                "player": player, "opponent": t.get("opponent", "?"),
                "cost": cost, "qty": qty, "last_seen": datetime.now().isoformat(timespec="seconds"),
            })
            if price is not None:
                snap["last_price"] = price
            snapshots[slug] = snap
            continue

        # not currently held
        if slug in already:
            continue
        snap = snapshots.get(slug)
        if not snap:
            # never observed open (likely an order that never filled) — don't invent a result
            continue

        last_price = snap.get("last_price")
        if last_price is None:
            result, pnl = "REVIEW", 0.0
        elif last_price >= WIN_PRICE:
            result, pnl = "WON", round(qty * 1.0 - cost, 2)
        elif last_price <= LOSS_PRICE:
            result, pnl = "LOST", round(-cost, 2)
        else:
            result, pnl = "REVIEW", 0.0   # vanished mid-range — flag, don't guess

        entry = {
            "date":   datetime.now().strftime("%Y-%m-%d"),
            "player": player,
            "match":  f"{player} vs {snap.get('opponent', t.get('opponent','?'))}",
            "cost":   f"${cost:.2f}",
            "result": result,
            "pnl":    pnl,
            "slug":   slug,
            "settled_at": datetime.now().isoformat(timespec="seconds"),
            "last_price": last_price,
        }
        resolved.append(entry)
        newly.append(entry)
        snapshots.pop(slug, None)

    if newly:
        _save_json(RESOLVED_LOG, resolved)
        for e in newly:
            print(f"  [resolved] {e['player']} -> {e['result']} ({e['pnl']:+.2f})")
            if e["result"] != "REVIEW":
                notify(
                    f"Bet {e['result']}: {e['player']}",
                    f"{e['match']} settled {e['result']} | net {e['pnl']:+.2f} (cost {e['cost']})"
                )
    _save_json(SNAPSHOT_LOG, snapshots)


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
    """Aggregate *recent buys* across the elite traders. Returns one signal per
    (match, player) with holder count, fresh-buy $ volume, and the median price the
    smart money actually paid (used as the pro-probability for the edge calc)."""
    raw_by_key: dict[str, dict] = {}
    cutoff = time.time() - FRESH_BUY_HOURS * 3600
    for name, wallet in TENNIS_TRADERS:
        try:
            url = f"https://data-api.polymarket.com/activity?user={wallet}&limit=500"
            for r in _fetch(url):
                if r.get("type") != "TRADE" or r.get("side") != "BUY":
                    continue
                title = r.get("title", "")
                if not any(k.lower() in title.lower() for k in KEYWORDS):
                    continue
                if (r.get("timestamp", 0) or 0) < cutoff:
                    continue
                price = float(r.get("price", 0) or 0)
                if price < MIN_PROB or price > MAX_PROB:
                    continue
                outcome = r.get("outcome", "")
                usd     = float(r.get("usdcSize", 0) or 0)
                key = f"{title}|{outcome}"
                entry = raw_by_key.setdefault(key, {
                    "player": outcome, "title": title,
                    "holders": [], "prices": [], "total_size": 0.0,
                    # market identity carried straight from the buy record, so global
                    # execution can bet on the exact same token with no PMUS lookup.
                    "asset": r.get("asset"), "conditionId": r.get("conditionId"),
                    "slug": r.get("slug"),
                })
                entry["total_size"] += usd        # all fresh $ deployed (incl. scale-ins)
                entry["prices"].append(price)
                if name not in entry["holders"]:  # but count each trader once
                    entry["holders"].append(name)
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
        prices = sorted(entry["prices"])
        median_price = prices[len(prices)//2]
        signals.append({
            "player": entry["player"],
            "prob": median_price,
            "title": entry["title"],
            "holders": entry["holders"],
            "total_size": entry["total_size"],
            "asset": entry.get("asset"),
            "conditionId": entry.get("conditionId"),
            "slug": entry.get("slug"),
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


def match_in_progress(event: dict) -> bool:
    """True if the match has already started / is being played live. We only mirror
    pre-match bets: once a match is in-play the PMUS price reflects live state (sets won,
    momentum) that the pros' lagging position marks don't capture, so the 'edge' is stale."""
    if event.get("live"):
        return True
    start = event.get("startDate") or event.get("startTime")
    if start:
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
            return datetime.now(timezone.utc) >= dt
        except Exception:
            pass
    return False


def find_player_in_matches(player_name: str, matches: list[dict], match_title: str = "") -> dict | None:
    """Look up player_name across the pre-fetched tennis match list.
    Requires full-name substring match, the SAME opponent as the signal's match
    (so we don't grab the player's next-round market), AND PMUS price in range."""
    target = player_name.lower().strip()
    title_l = match_title.lower()
    for event in matches:
        if match_in_progress(event):
            for market in event.get("markets", []):
                for side in market.get("marketSides", []):
                    if target in side.get("description", "").lower():
                        print(f"    [skip] {side['description']} — match in progress (score {event.get('score','?')}), no in-play bets")
                        break
            continue
        for market in event.get("markets", []):
            if market.get("closed"):
                continue
            sides = market.get("marketSides", [])
            for side in sides:
                desc = side.get("description", "").lower()
                # Full-name substring match (prevents Xinyu/Xiyu Wang style collisions)
                if target not in desc:
                    continue
                # Opponent check: the PMUS opponent must appear in the signal's match
                # title, else this is a different round/match for the same player.
                opp = next((s["description"] for s in sides if s["long"] != side["long"]), "")
                if title_l:
                    opp_tokens = [t for t in opp.lower().replace(".", "").split() if len(t) >= 4]
                    if opp_tokens and not any(t in title_l for t in opp_tokens):
                        print(f"    [skip] {side['description']} is vs {opp}, but signal match is '{match_title}' — wrong match")
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
                    "opponent":    opp or "?",
                    "event_title": event.get("title", ""),
                }
    return None


def search_us_market(player_name: str, matches: list[dict] | None = None, match_title: str = "") -> dict | None:
    """Find an active PMUS market featuring player_name in the signal's specific match."""
    if matches is None:
        matches = fetch_all_rg_matches()
    return find_player_in_matches(player_name, matches, match_title)


# ── order placement ─────────────────────────────────────────────────────────

def place_bet(signal: dict, matches: list[dict], budget_left: float | None = None) -> float:
    """Place a bet for this signal. Returns the dollars actually committed
    (0.0 if nothing was placed)."""
    player = signal["player"]
    market = search_us_market(player, matches, signal.get("title", ""))

    if not market:
        print(f"  [skip] No active US market found for '{player}'")
        return 0.0

    slug = market["slug"]
    if slug in placed_bets:
        print(f"  [skip] Already bet on {slug}")
        return 0.0

    # Bid 2 cents above market so the limit order actually crosses and fills
    market_price = market["price"]
    bid_price    = min(round(market_price + 0.02, 2), 0.95)
    price        = bid_price

    # Copy-trading model: the edge is that elite traders are backing this player,
    # not a price discount. We do NOT require PMUS to be cheaper than their entry
    # (it rarely is — the price usually rises after the smart money buys, and PMUS
    # lists late). Instead we size by conviction and only refuse to *chase* a price
    # that has run too far past where the pros got in.
    p_pro       = signal["prob"]                 # median price the pros actually paid
    edge_pct    = (p_pro - bid_price) * 100       # >0 means PMUS still cheaper than their entry
    chase_pp    = (bid_price - p_pro) * 100       # how far above their entry we'd be paying
    kelly_raw   = kelly_bet_size(p_pro, bid_price)  # kept for the trade-log record only
    if chase_pp > MAX_CHASE_PP:
        print(f"  [skip] {player}: PMUS bid {bid_price:.0%} is {chase_pp:.0f}pp above pros' "
              f"entry {p_pro:.0%} (max chase {MAX_CHASE_PP:.0f}pp) — not chasing")
        return 0.0
    # Flat conviction-tier sizing: more pros agreeing = bigger bet.
    n_holders = len(signal.get("holders", []))
    bet_size  = min(conviction_cap(n_holders), MAX_BET_USD)
    print(f"  Conviction: {n_holders} pros -> bet ${bet_size:.0f}  "
          f"(pros paid {p_pro:.0%}, bid {bid_price:.0%}, chase {chase_pp:+.0f}pp)")
    # Respect the remaining exposure budget: trim to fit, or skip if too little left.
    if budget_left is not None:
        if budget_left < MIN_BET_USD:
            print(f"  [skip] Exposure cap: only ${budget_left:.2f} left, need >=${MIN_BET_USD}")
            return 0.0
        if bet_size > budget_left:
            bet_size = round(budget_left, 2)
            print(f"  [cap] Trimming bet to remaining exposure budget: ${bet_size:.2f}")
    quantity  = round(bet_size / bid_price, 4)
    profit    = round(quantity - bet_size, 2)
    print(f"  Sizing: {n_holders} pros -> ${bet_size:.2f}  (pros paid {p_pro:.0%}, bidding {bid_price:.0%})")

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

    if DRY_RUN:
        print(f"  [DRY RUN] WOULD place ${bet_size:.2f} on {player} vs {market['opponent']} "
              f"@ {bid_price:.0%} ({quantity} shares, profit ${profit}) — no order submitted")
        return bet_size

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
        append_trade_log({
            "ts":           datetime.now().isoformat(timespec="seconds"),
            "player":       player,
            "opponent":     market["opponent"],
            "slug":         slug,
            "bid_price":    bid_price,
            "bet_size":     bet_size,
            "quantity":     quantity,
            "holders":      signal.get("holders", []),
            "holders_count": n_holders,
            "smart_money":  signal.get("total_size", 0),
            "edge_pct":     edge_pct,
            "kelly_raw":    kelly_raw,
            "filled":       filled,
            "order_id":     order_id,
        })
        notify(
            f"Bet placed: {player}",
            f"${bet_size:.0f} on {player} vs {market['opponent']} @ {price:.0%}\n"
            f"{n_holders} pros agreeing, wins ${profit:.2f} ({status_msg})\n{market['event_title']}"
        )
        print(f"  Order accepted: {order_id} | {status_msg}")
        return bet_size
    else:
        print(f"  [error] Order rejected: {resp}")
        return 0.0


# ── global (polymarket.com / CLOB) execution ────────────────────────────────
# Bets directly on the token the smart-money signal came from. No PMUS lookup,
# no opponent matching, no listing lag — the signal *is* the market.

_clob = None


def get_clob():
    """Lazily build + authenticate the CLOB client (email/magic proxy wallet)."""
    global _clob
    if _clob is None:
        from py_clob_client.client import ClobClient
        c = ClobClient(
            host=CLOB_HOST, chain_id=CLOB_CHAIN_ID, key=PM_GLOBAL_KEY,
            signature_type=CLOB_SIG_TYPE, funder=PM_GLOBAL_FUNDER,
        )
        c.set_api_creds(c.create_or_derive_api_creds())
        _clob = c
    return _clob


def opponent_from_title(title: str, player: str) -> str:
    """Pull the opponent out of a 'Roland Garros XXX: A vs B' title."""
    core = title.split(":", 1)[-1]
    parts = [p.strip() for p in re.split(r"\bvs\.?\b", core, flags=re.I) if p.strip()]
    pl = (player or "").lower()
    for p in parts:
        if p.lower() not in pl and pl not in p.lower():
            return p
    return parts[-1] if parts else "?"


def load_positions_global() -> dict:
    """Current holdings in the global proxy wallet, keyed by token id (asset)."""
    if not PM_GLOBAL_FUNDER:
        return {}
    try:
        recs = _fetch(f"https://data-api.polymarket.com/positions?user={PM_GLOBAL_FUNDER}&limit=200")
    except Exception as e:
        print(f"  [warn] Could not load global positions: {e}")
        return {}
    out = {}
    for p in recs or []:
        tok = p.get("asset")
        if tok:
            out[tok] = p
    return out


def exposure_global(positions: dict) -> float:
    """Sum cost (USD) of RG positions in the global wallet."""
    total = 0.0
    for p in positions.values():
        if not any(k.lower() in (p.get("title", "") or "").lower() for k in KEYWORDS):
            continue
        try:
            total += float(p.get("initialValue") or p.get("currentValue") or 0)
        except (TypeError, ValueError):
            pass
    return total


def place_bet_global(signal: dict, budget_left: float | None = None) -> float:
    """Place a copy-trade order on polymarket.com via the CLOB. Returns $ committed."""
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    player = signal["player"]
    token  = signal.get("asset")
    if not token:
        print(f"  [skip] {player}: signal has no token id (asset) — can't place global order")
        return 0.0
    if token in placed_bets:
        print(f"  [skip] Already bet on {player} ({signal.get('slug','?')})")
        return 0.0

    try:
        client = get_clob()
        pr = client.get_price(token_id=token, side=BUY)   # best ask to buy this outcome
        market_price = float(pr.get("price") if isinstance(pr, dict) else pr)
    except Exception as e:
        print(f"  [skip] {player}: could not price token on global: {e}")
        return 0.0
    if market_price <= 0 or market_price >= 1:
        print(f"  [skip] {player}: bad global price {market_price}")
        return 0.0

    # Bid a touch above the ask so the limit order crosses and fills, snapped to tick.
    try:
        tick = float(client.get_tick_size(token))
    except Exception:
        tick = 0.01
    bid_price = min(round((market_price + tick) / tick) * tick, 0.95)
    bid_price = round(bid_price, 4)

    if not (MIN_PROB <= bid_price <= MAX_PROB):
        print(f"  [skip] {player}: global price {market_price:.0%} (bid {bid_price:.0%}) outside {MIN_PROB:.0%}-{MAX_PROB:.0%}")
        return 0.0

    p_pro    = signal["prob"]
    edge_pct = (p_pro - bid_price) * 100
    chase_pp = (bid_price - p_pro) * 100
    if chase_pp > MAX_CHASE_PP:
        print(f"  [skip] {player}: bid {bid_price:.0%} is {chase_pp:.0f}pp above pros' entry {p_pro:.0%} (max {MAX_CHASE_PP:.0f}pp) — not chasing")
        return 0.0

    n_holders = len(signal.get("holders", []))
    bet_size  = min(conviction_cap(n_holders), MAX_BET_USD)
    if budget_left is not None:
        if budget_left < MIN_BET_USD:
            print(f"  [skip] Exposure cap: only ${budget_left:.2f} left, need >=${MIN_BET_USD}")
            return 0.0
        if bet_size > budget_left:
            bet_size = round(budget_left, 2)
            print(f"  [cap] Trimming bet to remaining exposure budget: ${bet_size:.2f}")

    size_shares = round(bet_size / bid_price, 2)
    profit      = round(size_shares - bet_size, 2)
    opponent    = opponent_from_title(signal.get("title", ""), player)
    print(f"  [GLOBAL] {n_holders} pros -> ${bet_size:.2f} on {player} vs {opponent} "
          f"@ {bid_price:.0%} ({size_shares} shares, pros paid {p_pro:.0%})")

    if DRY_RUN:
        print(f"  [DRY RUN] WOULD place ${bet_size:.2f} on {player} ({size_shares} shares @ {bid_price}) — no order submitted")
        return bet_size

    try:
        order = client.create_order(OrderArgs(token_id=token, price=bid_price, size=size_shares, side=BUY))
        resp  = client.post_order(order, OrderType.GTC)
    except Exception as e:
        print(f"  [error] Global order failed: {e}")
        return 0.0
    print(f"  Response: {json.dumps(resp)[:300] if isinstance(resp, dict) else str(resp)[:300]}")

    order_id = (resp.get("orderID") or resp.get("orderId") or resp.get("id")) if isinstance(resp, dict) else None
    success  = bool(order_id) or (isinstance(resp, dict) and resp.get("success"))
    if not success:
        print(f"  [error] Global order rejected: {resp}")
        return 0.0

    placed_bets[token] = {"orderId": order_id, "player": player, "price": bid_price,
                          "quantity": size_shares, "placed_at": datetime.now().isoformat()}
    append_trade_log({
        "ts":            datetime.now().isoformat(timespec="seconds"),
        "player":        player,
        "opponent":      opponent,
        "slug":          signal.get("slug", ""),
        "bid_price":     bid_price,
        "bet_size":      bet_size,
        "quantity":      size_shares,
        "holders":       signal.get("holders", []),
        "holders_count": n_holders,
        "smart_money":   signal.get("total_size", 0),
        "edge_pct":      edge_pct,
        "kelly_raw":     kelly_bet_size(p_pro, bid_price),
        "filled":        True,
        "order_id":      order_id,
        "venue":         "global",
    })
    notify(
        f"Bet placed (global): {player}",
        f"${bet_size:.0f} on {player} vs {opponent} @ {bid_price:.0%}\n"
        f"{n_holders} pros agreeing, wins ${profit:.2f}\n{signal.get('title','')}"
    )
    print(f"  Order accepted: {order_id}")
    return bet_size


# ── main loop ───────────────────────────────────────────────────────────────

def load_positions() -> dict:
    """Read actual PMUS positions (slug -> position) so restarts don't double-bet
    and so we can measure current exposure."""
    status, resp = _us_get("/v1/portfolio/positions")
    if status != 200:
        print(f"  [warn] Could not load existing positions: {status}")
        return {}
    return resp.get("positions") or {}


def is_rg_slug(slug: str) -> bool:
    """True if a position slug belongs to a tennis (RG) market — the bot's own
    domain. PMUS tennis slugs look like 'aec-atp-...' / 'aec-wta-...'; other
    holdings (e.g. 'tec-fifa-wc-...') are the user's manual bets and must NOT
    count against the bot's exposure budget."""
    s = (slug or "").lower()
    return "atp" in s or "wta" in s


def total_exposure(positions: dict) -> float:
    """Sum of cost (USD) across the bot's own open RG positions only. Non-tennis
    holdings the user placed manually are excluded so they don't eat the bot's
    betting budget."""
    total = 0.0
    for slug, p in positions.items():
        if not is_rg_slug(slug):
            continue
        try:
            total += float(p.get("cost", {}).get("value", 0) or 0)
        except (TypeError, ValueError):
            pass
    return total


def check():
    print(f"\n{'='*60}")
    print(f"  Auto-trader check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # Reload existing positions every cycle so container restarts don't double-bet
    matches = None
    if EXEC_VENUE == "global":
        positions = load_positions_global()        # keyed by token id
        existing  = set(positions.keys())
        for tok in existing:
            placed_bets.setdefault(tok, {"loaded": True})
        open_exposure = exposure_global(positions)
        print(f"  [GLOBAL] Holding {len(existing)} positions on polymarket.com "
              f"(${open_exposure:.2f} RG exposure of ${MAX_EXPOSURE_USD:.2f} cap)")
    else:
        positions = load_positions()
        existing  = set(positions.keys())
        for slug in existing:
            placed_bets.setdefault(slug, {"loaded": True})
        rg_positions  = [s for s in existing if is_rg_slug(s)]
        open_exposure = total_exposure(positions)
        print(f"  Holding {len(existing)} PMUS positions total; {len(rg_positions)} are tennis "
              f"(${open_exposure:.2f} RG exposure of ${MAX_EXPOSURE_USD:.2f} cap)")

        # Fetch the RG market universe once per cycle (PMUS only: used for both
        # resolution tracking and the player->slug market lookup).
        matches = fetch_all_rg_matches()
        print(f"\n  Fetched {len(matches)} active RG matches on Polymarket US")
        track_resolutions(matches, existing)

    signals = get_smart_money_signals()
    if not signals:
        print("  No Roland Garros smart money signals right now.")
        return

    # Sort by prob — value bets first (lower prob = higher payout)
    signals.sort(key=lambda s: s["prob"])

    print(f"\n  {len(signals)} signal(s) passing conviction filter "
          f"(>={MIN_HOLDERS} traders, >=${MIN_TOTAL_SIZE:,.0f} combined, "
          f"prob {MIN_PROB:.0%}-{MAX_PROB:.0%})")
    print(f"  Bankroll: ${BANKROLL_USD}, conviction-tier sizing ${MIN_BET_USD}-${MAX_BET_USD}/bet, "
          f"max chase {MAX_CHASE_PP:.0f}pp")
    budget_left = MAX_EXPOSURE_USD - open_exposure
    placed = 0
    for s in signals:
        if placed >= MAX_BETS_PER_RUN:
            print(f"\n  [stop] Hit MAX_BETS_PER_RUN ({MAX_BETS_PER_RUN}); skipping remaining signals.")
            break
        if budget_left < MIN_BET_USD:
            print(f"\n  [stop] Exposure cap reached — only ${budget_left:.2f} of "
                  f"${MAX_EXPOSURE_USD:.2f} left; skipping remaining signals.")
            break
        holders = ",".join(s["holders"])
        print(f"\n  Signal: {s['player']} @ {s['prob']:.0%}  |  "
              f"{len(s['holders'])} pros (${s['total_size']:,.0f}): {holders}")
        print(f"    Match: {s['title'][:60]}")
        if EXEC_VENUE == "global":
            spent = place_bet_global(s, budget_left)
        else:
            spent = place_bet(s, matches, budget_left)
        if spent > 0:
            placed += 1
            budget_left -= spent

    print(f"\n  Done: {placed} new bet(s) placed this cycle (max {MAX_BETS_PER_RUN}). "
          f"${budget_left:.2f} of ${MAX_EXPOSURE_USD:.2f} exposure budget left.")


if __name__ == "__main__":
    if EXEC_VENUE == "global":
        if not PM_GLOBAL_KEY or not PM_GLOBAL_FUNDER:
            print("ERROR: EXEC_VENUE=global needs PM_GLOBAL_KEY and PM_GLOBAL_FUNDER env vars.")
            sys.exit(1)
    elif not PM_KEY_ID or not PM_SECRET:
        print("ERROR: Set PM_KEY_ID and PM_SECRET environment variables.")
        print("Example: $env:PM_KEY_ID='your-key-id'; $env:PM_SECRET='your-secret'")
        sys.exit(1)

    print("Polymarket Auto-Trader starting...")
    print(f"Execution venue: {EXEC_VENUE.upper()}")
    print(f"Bankroll: ${BANKROLL_USD:.2f}")
    if DRY_RUN:
        print("*** DRY RUN MODE — no real orders will be placed ***")
    print(f"Trade log path: {TRADE_LOG}  (/data volume mounted: {os.path.isdir('/data')})")

    # Spin up the dashboard HTTP server FIRST, in a background thread, so the
    # web port binds immediately and Railway sees a responsive app. It shares
    # this process's filesystem (and trades.json) with the trader loop.
    try:
        import dashboard
        threading.Thread(target=dashboard.start_server, daemon=True).start()
    except Exception as e:
        print(f"[warn] dashboard failed to start: {e}")

    # Startup auth check
    if EXEC_VENUE == "global":
        print(f"Funder (proxy): {PM_GLOBAL_FUNDER[:6]}...{PM_GLOBAL_FUNDER[-4:]}")
        try:
            get_clob()
            print("CLOB auth OK (api creds derived)")
            pos = load_positions_global()
            print(f"  Global wallet holds {len(pos)} positions; "
                  f"${exposure_global(pos):.2f} RG exposure")
        except Exception as e:
            print(f"[warn] CLOB init failed: {e} — will retry inside loop")
    else:
        print(f"Key: {PM_KEY_ID[:8]}...  Secret length: {len(PM_SECRET)} chars")
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
        print(f"\n  Next check in {CHECK_INTERVAL_MIN:.0f} minutes.\n")
        time.sleep(CHECK_INTERVAL_MIN * 60)
