# Polymarket Tennis Copy-Trader

An automated trading bot for [Polymarket](https://polymarket.com) prediction markets. It follows 20 profitable tennis traders, detects when several of them independently back the same player before a match, and places a sized limit order on that player. It runs 24/7 on Railway with a live web dashboard and phone alerts.

## How it works

Every 15 minutes the bot runs one cycle:

1. **Load positions** from the exchange, so a restart never bets on the same market twice, and measure current exposure.
2. **Read signals.** Pull each tracked trader's recent activity from Polymarket's public data API. Keep only buys in head-to-head tennis matches, made in the last 24 hours, priced between 20¢ and 60¢.
3. **Require consensus.** Group by match and player. A signal needs at least 2 traders and at least $5,000 combined.
4. **One side per match.** If the traders are split, keep the side with more of them and record how many disagreed.
5. **Find the market** on Polymarket US by player and opponent name, skipping matches that have already started.
6. **Size and place the order.** The bet scales with the consensus margin, and the order is a limit order 2¢ above the market price, good-till-cancelled.
7. **Track results.** When a held position disappears, record it as won or lost from its last observed price.

### Bet sizing

```
net = traders on our side − traders on the other side
bet = $5 + ($15 − $5) × clamp(net / 7, 0, 1)
```

Kelly sizing was tried first, using the traders' entry price as the probability estimate. Prices usually rise after informed traders buy, so Kelly almost always showed a negative edge and bet nothing. The bot now sizes by agreement and refuses to chase. Kelly is still computed and logged for analysis.

## Risk controls

| Control | Limit |
|---|---|
| Consensus | ≥ 2 traders and ≥ $5,000 combined |
| Signal freshness | buys from the last 24 hours only |
| Price band | 20¢ – 60¢ |
| Anti-chase | skip if the bid is more than 15¢ above the traders' median entry |
| Timing | pre-match only, never in-play |
| Bet size | $5 – $15 |
| Bets per cycle | 3 |
| Total exposure | capped at the bankroll (default $50) |
| Dry run | `DRY_RUN=1` runs the full pipeline without sending orders |

## Architecture

One Python process with two threads:

- **Trading loop** (`autotrader.py`, main thread) runs the cycle above.
- **Dashboard** (`dashboard.py`, background thread) serves a web page with positions, ROI, closed trades, the bot's trade log, and a compliance audit that re-checks every logged bet against the bot's current limits.

State is stored as JSON on a persistent volume (`/data` on Railway, or the working directory locally). Alerts go to a phone through [ntfy](https://ntfy.sh).

### Execution venues

Set with `EXEC_VENUE`:

- `pmus` (default): Polymarket US REST API. Each request is signed with an Ed25519 key.
- `global`: polymarket.com's order book through [`py-clob-client`](https://github.com/Polymarket/py-clob-client), betting on the exact token the traders bought. Orders are only accepted from permitted regions.

## Files

| File | Purpose |
|---|---|
| `autotrader.py` | Signal detection, sizing, order placement, result tracking, main loop |
| `dashboard.py` | Web dashboard |
| `polymarket_monitor.py` | Original monitor-only version (alerts disabled) |
| `test_auth.py` | Checks API key signing against read-only endpoints |
| `manual_history.json` | Hand-entered closed trades shown on the dashboard |
| `Procfile`, `requirements.txt` | Deployment config and dependencies |

## Running it

Requires Python 3.10+.

```bash
pip install -r requirements.txt

# Polymarket US
export PM_KEY_ID=...
export PM_SECRET=...

python test_auth.py           # verify credentials
DRY_RUN=1 python autotrader.py  # full cycle, no orders sent
python autotrader.py          # live
```

The dashboard starts automatically on `PORT` (default 8080).

Optional settings: `BANKROLL_USD`, `MAX_EXPOSURE_USD`, `CHECK_INTERVAL_MIN`, `MAX_CHASE_PP`, `FRESH_BUY_HOURS`, `NET_FULL_CONVICTION`. For the global venue, set `EXEC_VENUE=global`, `PM_GLOBAL_KEY` and `PM_GLOBAL_FUNDER`.

## Limitations

- Parameters were set by judgment, not by a backtest.
- The trader list is hand-picked, which risks survivorship bias.
- Results are inferred from the last observed price rather than official settlement.
- Markets are matched across two exchanges by player name.
- The sample of closed trades is small (one tournament).
