"""
Polymarket Auto-Trader Dashboard
Live ROI + position tracker. Reads positions + market prices from PMUS.
Deploy as a Railway service with start command: python dashboard.py
"""
import os, base64, time, json, urllib.request, urllib.parse, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

PM_KEY_ID = os.environ.get("PM_KEY_ID", "")
PM_SECRET = os.environ.get("PM_SECRET", "")
PORT      = int(os.environ.get("PORT", 8080))
MOBILE_UA = "Mozilla/5.0 (iPhone)"

_price_cache: dict = {}
_CACHE_TTL = 60


def _auth_headers(method, path):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    ts  = str(int(time.time() * 1000))
    msg = (ts + method + path).encode()
    raw = base64.b64decode(PM_SECRET + "=" * (-len(PM_SECRET) % 4))
    k   = Ed25519PrivateKey.from_private_bytes(raw[:32])
    return {
        "X-PM-Access-Key": PM_KEY_ID,
        "X-PM-Timestamp":  ts,
        "X-PM-Signature":  base64.b64encode(k.sign(msg)).decode(),
        "Accept":          "application/json",
        "User-Agent":      MOBILE_UA,
    }


def _us_get(path):
    req = urllib.request.Request("https://api.polymarket.us" + path, headers=_auth_headers("GET", path))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}


def fetch_market_long_price(slug):
    now = time.time()
    cached = _price_cache.get(slug)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    status, resp = _us_get(f"/v1/markets/{slug}")
    if status != 200:
        return None
    m = resp.get("market", resp)
    for side in m.get("marketSides", []):
        if side.get("long"):
            price = float(side.get("price", 0))
            _price_cache[slug] = (now, price)
            return price
    return None


def classify(slug):
    if "atp" in slug or "wta" in slug:
        return "🎾 Tennis"
    if "fifa" in slug:
        return "⚽ Soccer"
    if "nba" in slug:
        return "🏀 NBA"
    if "nhl" in slug:
        return "🏒 NHL"
    if "mlb" in slug:
        return "⚾ MLB"
    return "📊 Other"


def get_portfolio_state():
    status, port = _us_get("/v1/portfolio/positions")
    if status != 200:
        return None
    positions = port.get("positions", {})

    rows = []
    total_cost = total_value = 0.0
    for slug, p in positions.items():
        qty   = float(p.get("netPosition", 0))
        cost  = float(p.get("cost", {}).get("value", 0))
        realized = float(p.get("realized", {}).get("value", 0))

        long_price = fetch_market_long_price(slug)
        if long_price is None:
            current = cost  # fallback if price lookup fails
        else:
            # netPosition is positive for the side actually held; value = qty * the side's price
            # We need to know which side. If qty > 0 we assume same side as 'long', else short side.
            current = qty * long_price if qty >= 0 else abs(qty) * (1 - long_price)

        pnl = current - cost + realized
        roi = (pnl / cost * 100) if cost else 0
        rows.append({
            "slug": slug, "kind": classify(slug),
            "qty": qty, "cost": cost, "current": current,
            "pnl": pnl, "roi": roi,
        })
        total_cost  += cost
        total_value += current

    rows.sort(key=lambda r: -r["cost"])
    total_pnl = total_value - total_cost
    total_roi = (total_pnl / total_cost * 100) if total_cost else 0
    bot_rows  = [r for r in rows if r["kind"].startswith("🎾")]
    bot_cost  = sum(r["cost"] for r in bot_rows)
    bot_value = sum(r["current"] for r in bot_rows)
    bot_pnl   = bot_value - bot_cost
    bot_roi   = (bot_pnl / bot_cost * 100) if bot_cost else 0
    return {
        "rows": rows,
        "n_positions": len(rows),
        "total_cost": total_cost,
        "total_value": total_value,
        "total_pnl": total_pnl,
        "total_roi": total_roi,
        "bot_cost": bot_cost,
        "bot_value": bot_value,
        "bot_pnl":  bot_pnl,
        "bot_roi":  bot_roi,
    }


def render_html(state):
    if not state:
        return "<html><body><h1>Failed to load portfolio</h1></body></html>"
    color_total = "#22c55e" if state["total_pnl"] >= 0 else "#ef4444"
    color_bot   = "#22c55e" if state["bot_pnl"]   >= 0 else "#ef4444"
    sign_total  = "+" if state["total_pnl"] >= 0 else ""
    sign_bot    = "+" if state["bot_pnl"]   >= 0 else ""

    rows_html = ""
    for r in state["rows"]:
        c = "#22c55e" if r["pnl"] >= 0 else "#ef4444"
        s = "+" if r["pnl"] >= 0 else ""
        short_slug = r["slug"].split("-", 1)[-1] if "-" in r["slug"] else r["slug"]
        rows_html += (
            f"<tr>"
            f"<td>{r['kind']}</td>"
            f"<td class='slug'>{short_slug}</td>"
            f"<td>{r['qty']:.0f}</td>"
            f"<td>${r['cost']:.2f}</td>"
            f"<td>${r['current']:.2f}</td>"
            f"<td style='color:{c}'>{s}${r['pnl']:.2f}</td>"
            f"<td style='color:{c}'>{s}{r['roi']:.1f}%</td>"
            f"</tr>"
        )

    return f"""<!DOCTYPE html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Portfolio</title>
<meta http-equiv="refresh" content="60">
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 16px; }}
  h1 {{ font-size: 18px; margin: 0 0 16px; }}
  h2 {{ font-size: 14px; color: #94a3b8; margin: 24px 0 8px; }}
  .stats {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; margin-bottom: 14px; }}
  .card {{ background: #1e293b; padding: 14px; border-radius: 12px; }}
  .card .label {{ font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: .5px; }}
  .card .value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-bottom: 24px; }}
  th, td {{ padding: 10px 6px; text-align: left; border-bottom: 1px solid #334155; }}
  th {{ font-size: 10px; color: #94a3b8; text-transform: uppercase; }}
  .slug {{ font-size: 10px; color: #64748b; font-family: monospace; max-width: 140px; word-break: break-all; }}
  .footer {{ margin-top: 20px; font-size: 11px; color: #64748b; text-align: center; }}
</style></head><body>
<h1>📊 Polymarket Portfolio</h1>

<h2>OVERALL</h2>
<div class="stats">
  <div class="card"><div class="label">Total ROI</div>
    <div class="value" style="color:{color_total}">{sign_total}{state['total_roi']:.1f}%</div></div>
  <div class="card"><div class="label">Total P&L</div>
    <div class="value" style="color:{color_total}">{sign_total}${state['total_pnl']:.2f}</div></div>
  <div class="card"><div class="label">Invested</div>
    <div class="value">${state['total_cost']:.2f}</div></div>
  <div class="card"><div class="label">Current Value</div>
    <div class="value">${state['total_value']:.2f}</div></div>
</div>

<h2>BOT ONLY (Tennis)</h2>
<div class="stats">
  <div class="card"><div class="label">Bot ROI</div>
    <div class="value" style="color:{color_bot}">{sign_bot}{state['bot_roi']:.1f}%</div></div>
  <div class="card"><div class="label">Bot P&L</div>
    <div class="value" style="color:{color_bot}">{sign_bot}${state['bot_pnl']:.2f}</div></div>
</div>

<h2>ALL POSITIONS ({state['n_positions']})</h2>
<table>
  <tr><th>Type</th><th>Market</th><th>Qty</th><th>Cost</th><th>Now</th><th>P&L</th><th>ROI</th></tr>
  {rows_html}
</table>
<div class="footer">refreshes every 60s · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404); self.end_headers(); return
        try:
            state = get_portfolio_state()
            body  = render_html(state).encode()
        except Exception as e:
            body = f"<h1>error</h1><pre>{e}</pre>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return  # quiet


if __name__ == "__main__":
    if not PM_KEY_ID or not PM_SECRET:
        print("ERROR: Set PM_KEY_ID and PM_SECRET environment variables")
        sys.exit(1)
    print(f"Polymarket Dashboard starting on 0.0.0.0:{PORT}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
