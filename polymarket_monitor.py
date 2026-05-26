import urllib.request
import json
import time
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

NTFY_TOPIC = "wormypolymarket"

TENNIS_TRADERS = [
    ("lovelystuff",   "0x65b54274eba5c76dee6f0fab18a590653811e82f"),
    ("swisstony",     "0x204f72f35326db932158cba6adff0b9a1da95e14"),
    ("ChloeT1",       "0x9ac2536ed93f8fe8ce91d9662b03bcbb19ccbe3d"),
    ("shakendbake",   "0x5e4dbe95f805e27959532f4845e2e4180017b874"),
    ("anon18",        "0x5966db1fe50763c9e3c014d756369bad07e1f804"),
]

KEYWORDS = ["Roland Garros", "Roland-Garros", "French Open"]

seen_markets = set()

HEADERS = {
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def phone_notify(title, message):
    try:
        data = json.dumps({"topic": NTFY_TOPIC, "title": title, "message": message}).encode()
        req = urllib.request.Request(
            "https://ntfy.sh",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        print("  *** Phone notification sent ***")
    except Exception as e:
        print(f"  [warn] ntfy failed: {e}")


def get_markets_from_traders():
    markets = {}
    for name, wallet in TENNIS_TRADERS:
        try:
            url = f"https://data-api.polymarket.com/positions?user={wallet}&limit=50"
            positions = fetch(url)
            for p in positions:
                title = p.get("title", "")
                if not any(k.lower() in title.lower() for k in KEYWORDS):
                    continue
                market_id = p.get("conditionId") or title
                val  = float(p.get("currentValue", 0) or 0)
                size = float(p.get("size", 0) or 0)
                prob = val / size if size else 0
                if prob < 0.02 or prob > 0.98:
                    continue
                if market_id not in markets:
                    markets[market_id] = {
                        "title": title,
                        "prob": prob,
                        "outcome": p.get("outcome"),
                        "holders": [],
                    }
                markets[market_id]["holders"].append(
                    f"{name} -> {p.get('outcome')} (${size:,.0f})"
                )
        except Exception as e:
            print(f"  [warn] Could not fetch {name}: {e}")
    return markets


def check():
    print(f"\n{'='*60}")
    print(f"  Monitor check — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    new_found = []
    markets = get_markets_from_traders()

    if markets:
        print(f"\n  {len(markets)} live Roland Garros markets:\n")
        for mid, m in markets.items():
            prob    = m["prob"]
            win     = round((1 / prob - 1) * 5, 2) if prob > 0 else 0
            holders = " | ".join(m["holders"])
            print(f"  {m['title'][:65]}")
            print(f"  Pick: {m['outcome']} @ {prob:.0%}  |  $5 wins ${win}")
            print(f"  {holders}\n")
            if mid not in seen_markets:
                new_found.append((m["title"], m["outcome"], prob, win))
                seen_markets.add(mid)
    else:
        print("\n  No live markets found this check.")

    if new_found:
        for title, outcome, prob, win in new_found:
            msg = f"{outcome} @ {prob:.0%} | $5 wins ${win}\n{title[:80]}"
            phone_notify("New Roland Garros market!", msg)
        print(f"\n  {len(new_found)} new market(s) — phone notified.")
    else:
        print("\n  No new markets since last check.")

    print(f"\n  Next check in 60 mins.\n")


if __name__ == "__main__":
    print("Polymarket Roland Garros Monitor — Cloud Edition")
    print("Running 24/7 | Notifications via ntfy -> wormypolymarket\n")
    while True:
        try:
            check()
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as e:
            print(f"\n[error] {e}")
        time.sleep(60 * 60)
