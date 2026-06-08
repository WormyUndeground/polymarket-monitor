"""
Polymarket Auto-Trader Dashboard
Live ROI + position tracker. Reads positions + market prices from PMUS.
Deploy as a Railway service with start command: python dashboard.py
"""
import os, base64, time, json, urllib.request, urllib.parse, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

PM_KEY_ID = os.environ.get("PM_KEY_ID", "")
PM_SECRET = os.environ.get("PM_SECRET", "")
PORT      = int(os.environ.get("PORT", 8080))
MOBILE_UA = "Mozilla/5.0 (iPhone)"

_price_cache: dict = {}
_events_cache: dict = {"data": None, "ts": 0}
_CACHE_TTL = 60
TRADE_LOG       = os.environ.get("TRADE_LOG") or ("/data/trades.json" if os.path.isdir("/data") else "trades.json")
MANUAL_HISTORY  = os.environ.get("MANUAL_HISTORY", "manual_history.json")
RESOLVED_LOG    = os.environ.get("RESOLVED_LOG") or (os.path.dirname(TRADE_LOG) or ".") + "/resolved_trades.json"


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


def _load_events():
    """Cache all open events from the public gateway once per cycle."""
    now = time.time()
    if _events_cache["data"] is not None and now - _events_cache["ts"] < _CACHE_TTL:
        return _events_cache["data"]
    by_market_slug = {}
    try:
        req = urllib.request.Request(
            "https://gateway.polymarket.us/v1/events?limit=500&closed=false",
            headers={"User-Agent": MOBILE_UA},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        for ev in data.get("events", []):
            for m in ev.get("markets", []):
                by_market_slug[m.get("slug", "")] = m
    except Exception as e:
        print(f"  [warn] gateway events fetch failed: {e}")
    _events_cache["data"] = by_market_slug
    _events_cache["ts"]   = now
    return by_market_slug


def fetch_position_prices(slug):
    """Return (long_price, short_price) for the given market slug, or (None,None)."""
    markets = _load_events()
    m = markets.get(slug)
    if not m:
        return None, None
    long_p = short_p = None
    for side in m.get("marketSides", []):
        if side.get("long"):
            long_p = float(side.get("price", 0))
        else:
            short_p = float(side.get("price", 0))
    return long_p, short_p


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

        long_price, short_price = fetch_position_prices(slug)
        if long_price is None and short_price is None:
            # Fallback: estimate from cost per share
            avg_cost = (cost / abs(qty)) if qty else 0
            current = qty * avg_cost
        else:
            # Try to figure out which side we're on by comparing cost/qty to the two side prices
            avg_cost = (cost / abs(qty)) if qty else 0
            # Pick whichever side's price is closest to our avg cost (that's the side we bought)
            candidates = []
            if long_price  is not None: candidates.append(long_price)
            if short_price is not None: candidates.append(short_price)
            our_side = min(candidates, key=lambda x: abs(x - avg_cost)) if candidates else avg_cost
            current = abs(qty) * our_side

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


def load_trade_log():
    if not os.path.exists(TRADE_LOG):
        return []
    try:
        with open(TRADE_LOG) as f:
            return json.load(f)
    except Exception:
        return []


def _live_params():
    """Pull the active gate values straight from autotrader so this eval never drifts
    from what the bot actually enforces. Falls back to the known defaults if import fails."""
    try:
        import autotrader as a
        return {
            "MIN_HOLDERS":    a.MIN_HOLDERS,
            "MIN_TOTAL_SIZE": a.MIN_TOTAL_SIZE,
            "MIN_PROB":       a.MIN_PROB,
            "MAX_PROB":       a.MAX_PROB,
            "MIN_BET_USD":    a.MIN_BET_USD,
            "MAX_BET_USD":    a.MAX_BET_USD,
            "MAX_CHASE_PP":   a.MAX_CHASE_PP,
            "conviction_bet": a.conviction_bet,
        }
    except Exception:
        return {
            "MIN_HOLDERS": 2, "MIN_TOTAL_SIZE": 5000.0,
            "MIN_PROB": 0.20, "MAX_PROB": 0.60,
            "MIN_BET_USD": 5.0, "MAX_BET_USD": 15.0,
            "MAX_CHASE_PP": 15.0,
            "conviction_bet": None,
        }


def eval_compliance(trades):
    """Audit each logged bet against the live gates. Returns (n_pass, n_total, violations)
    where violations is a list of (trade, [reason, ...])."""
    p = _live_params()
    violations = []
    for t in trades:
        reasons = []
        holders = t.get("holders_count", 0) or 0
        money   = float(t.get("smart_money", 0) or 0)
        price   = float(t.get("bid_price", 0) or 0)
        size    = float(t.get("bet_size", 0) or 0)
        edge    = float(t.get("edge_pct", 0) or 0)
        if holders < p["MIN_HOLDERS"]:
            reasons.append(f"only {holders} pros (need ≥{p['MIN_HOLDERS']})")
        if money < p["MIN_TOTAL_SIZE"]:
            reasons.append(f"${money:,.0f} smart money (need ≥${p['MIN_TOTAL_SIZE']:,.0f})")
        if not (p["MIN_PROB"] <= price <= p["MAX_PROB"]):
            reasons.append(f"price {price:.0%} outside {p['MIN_PROB']:.0%}–{p['MAX_PROB']:.0%}")
        if not (p["MIN_BET_USD"] <= size <= p["MAX_BET_USD"]):
            reasons.append(f"bet ${size:.2f} outside ${p['MIN_BET_USD']:.0f}–${p['MAX_BET_USD']:.0f}")
        if p.get("conviction_bet"):
            opp      = t.get("opp_holders", 0) or 0
            expected = p["conviction_bet"](holders, opp)
            if size > expected + 0.01:
                reasons.append(f"bet ${size:.2f} exceeds ${expected:.2f} for {holders} vs {opp} pros")
        # Copy-trading model: negative edge is fine (PMUS usually drifts up after the
        # pros buy). We only flag *chasing* — paying more than MAX_CHASE_PP above their
        # entry. edge = (pros_paid - bid)*100, so chase = -edge.
        if -edge > p["MAX_CHASE_PP"]:
            reasons.append(f"chased {-edge:.0f}pp above pros' entry (max {p['MAX_CHASE_PP']:.0f}pp)")
        if reasons:
            violations.append((t, reasons))
    return len(trades) - len(violations), len(trades), violations


def load_manual_history():
    if not os.path.exists(MANUAL_HISTORY):
        return []
    try:
        with open(MANUAL_HISTORY) as f:
            return json.load(f)
    except Exception:
        return []


def load_resolved_trades():
    """Bot bets the auto-resolution tracker has settled (same schema as manual)."""
    if not os.path.exists(RESOLVED_LOG):
        return []
    try:
        with open(RESOLVED_LOG) as f:
            return json.load(f)
    except Exception:
        return []


# Manual corrections to auto-resolved bets. The resolution tracker classifies a
# bet by its last-seen price, so a position SOLD by hand before it settled can be
# mis-logged (e.g. a break-even sale recorded as a loss). Each correction matches
# by player + date and overrides the result/pnl shown on the dashboard.
CLOSED_TRADE_CORRECTIONS = [
    {"player": "Flavio Cobolli", "date": "2026-06-07",
     "result": "BREAK EVEN", "pnl": 0.0},
]


def _apply_corrections(history):
    for h in history:
        for c in CLOSED_TRADE_CORRECTIONS:
            if h.get("player") == c["player"] and h.get("date") == c["date"]:
                h["result"] = c["result"]
                h["pnl"]    = c["pnl"]
    return history


def load_closed_history():
    """Hand-entered history + auto-resolved bot bets, oldest-first by date."""
    combined = load_manual_history() + load_resolved_trades()
    combined = _apply_corrections(combined)
    return sorted(combined, key=lambda h: h.get("date", ""))


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

    # Closed history: hand-entered trades + auto-resolved bot bets
    history = load_closed_history()
    hist_html = ""
    hist_total = 0.0
    hist_wins  = 0
    hist_decided = 0   # win-rate denominator: excludes break-evens
    if history:
        for h in reversed(history):
            pnl = float(h.get("pnl", 0))
            hist_total += pnl
            # Win rate counts only decided bets — clear wins and losses. Anything
            # ambiguous (BREAK EVEN, REVIEW, etc.) is excluded from the denominator.
            result = h.get("result", "").upper()
            if result in ("WON", "LOST"):
                hist_decided += 1
                if result == "WON":
                    hist_wins += 1
            c = "#22c55e" if pnl >= 0 else "#ef4444"
            s = "+" if pnl >= 0 else ""
            hist_html += (
                f"<tr>"
                f"<td>{h.get('date','')}</td>"
                f"<td>{h.get('player','?')}</td>"
                f"<td>{h.get('match','')}</td>"
                f"<td>{h.get('cost','')}</td>"
                f"<td style='color:{c}'>{h.get('result','')}</td>"
                f"<td style='color:{c}'>{s}${pnl:.2f}</td>"
                f"</tr>"
            )
        win_rate = (hist_wins / hist_decided * 100) if hist_decided else 0
        hist_summary = (
            f"<div class='stats'>"
            f"<div class='card'><div class='label'>Closed P&L</div>"
            f"<div class='value' style='color:{'#22c55e' if hist_total >= 0 else '#ef4444'}'>"
            f"{'+' if hist_total >= 0 else ''}${hist_total:.2f}</div></div>"
            f"<div class='card'><div class='label'>Win Rate</div>"
            f"<div class='value'>{hist_wins}/{hist_decided} ({win_rate:.0f}%)</div></div>"
            f"</div>"
        )
    else:
        hist_summary = ""
        hist_html = "<tr><td colspan='6' style='text-align:center;color:#64748b'>No manual history added yet</td></tr>"

    trades = load_trade_log()
    trades_html = ""
    if trades:
        for t in reversed(trades[-50:]):
            edge = t.get("edge_pct", 0)
            trades_html += (
                f"<tr>"
                f"<td>{t.get('ts','')[:16]}</td>"
                f"<td>{t.get('player','?')}</td>"
                f"<td>${t.get('bet_size',0):.2f}</td>"
                f"<td>{t.get('bid_price',0):.0%}</td>"
                f"<td>{t.get('holders_count',0)} pros</td>"
                f"<td>{edge:+.1f}pp</td>"
                f"</tr>"
            )
    else:
        trades_html = "<tr><td colspan='6' style='text-align:center;color:#64748b'>No bot trades logged yet</td></tr>"

    n_pass, n_total, violations = eval_compliance(trades)
    if n_total == 0:
        compliance_html = (
            "<div class='card'><div class='label'>Parameter Compliance</div>"
            "<div class='value' style='color:#64748b'>No bets to audit yet</div></div>"
        )
    elif not violations:
        compliance_html = (
            "<div class='card'><div class='label'>Parameter Compliance</div>"
            f"<div class='value' style='color:#22c55e'>✓ {n_pass}/{n_total} in bounds</div>"
            "<div style='font-size:11px;color:#64748b;margin-top:4px'>"
            "every logged bet passed all gates</div></div>"
        )
    else:
        viol_rows = ""
        for t, reasons in reversed(violations[-20:]):
            viol_rows += (
                "<div style='font-size:12px;color:#fca5a5;margin-top:6px'>"
                f"<b>{t.get('player','?')}</b> ({t.get('ts','')[:16]}): "
                + "; ".join(reasons) + "</div>"
            )
        compliance_html = (
            "<div class='card'><div class='label'>Parameter Compliance</div>"
            f"<div class='value' style='color:#ef4444'>⚠ {len(violations)}/{n_total} out of bounds</div>"
            + viol_rows + "</div>"
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

<h2>CLOSED TRADE HISTORY ({len(history)})</h2>
{hist_summary}
<table>
  <tr><th>Date</th><th>Player</th><th>Match</th><th>Cost</th><th>Result</th><th>P&L</th></tr>
  {hist_html}
</table>

<h2>PARAMETER COMPLIANCE</h2>
{compliance_html}

<h2>BOT TRADE LOG ({len(trades)})</h2>
<table>
  <tr><th>Time</th><th>Player</th><th>Bet</th><th>Price</th><th>Conviction</th><th>Edge</th></tr>
  {trades_html}
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


def start_server(port: int = None):
    """Start the dashboard HTTP server. Blocks. Call in a thread to run alongside trader."""
    p = port or PORT
    print(f"Polymarket Dashboard serving on 0.0.0.0:{p}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", p), Handler).serve_forever()


if __name__ == "__main__":
    if not PM_KEY_ID or not PM_SECRET:
        print("ERROR: Set PM_KEY_ID and PM_SECRET environment variables")
        sys.exit(1)
    start_server()
