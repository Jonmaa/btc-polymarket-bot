# Polymarket Hedged Bot

15-minute Polymarket hedging bot for the “pair cost < 1” strategy (avg YES/NO price < 1). It connects to the CLOB websocket, watches mispricings, and adds UP/DOWN asymmetrically until profit is locked. Uses market slug auto-rotation (increments the slug by +900 seconds for each next 15m window) and fetches token IDs per window.

## Quick start
1) Create a virtualenv and install deps:
```
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```
2) Set env vars (see `.env.example`). Set `POLYMARKET_MARKET_SLUG` (e.g. `btc-updown-15m-1765176300`).
3) Run the bot (auto-rotates every 15m using the slug base):
```
python -m src.main
```

### Get IDs from a slug (helper)
```
python -m src.lookup btc-updown-15m-1765176300
```
Returns `market_id`, `yes_token_id`, `no_token_id`, and outcomes; `main` does this automatically before each window.

## Configuration
Environment variables (defaults in `src/config.py`):
- `POLYMARKET_API_KEY` / `POLYMARKET_API_SECRET` / `POLYMARKET_API_PASSPHRASE`: credentials (passphrase used for user channel; current bot still uses stub orders).
- `POLYMARKET_MARKET_SLUG`: base slug to start the rotation.
- `POLYMARKET_MARKET_ID`, `POLYMARKET_YES_TOKEN_ID`, `POLYMARKET_NO_TOKEN_ID`: optional overrides; typically left blank because the bot fetches them from the slug.
- `POLYMARKET_WS_URL`: websocket endpoint (default `wss://ws-subscriptions-clob.polymarket.com`).
- `TARGET_PAIR_COST`: max combined average cost (default 0.99).
- `BALANCE_SLACK`: allowed qty imbalance fraction (default 0.15).
- `ORDER_SIZE`: per-order size (default 50).
- `YES_BUY_THRESHOLD` / `NO_BUY_THRESHOLD`: price thresholds to consider a buy (defaults 0.45 / 0.45).
- `VERBOSE`: set `true` to print current UP/DOWN prices, connection info, and trade simulations.

## Current behavior
- Connects to the market websocket channel and subscribes with the current UP/DOWN token IDs.
- Continuously tracks best prices; prints `[price] UP=<p> DOWN=<p>` when updates arrive (verbose mode).
- Simulates buys when thresholds and pair-cost/balance checks pass; updates internal state to track the hedge and pair cost.
- Stops a window when the lock condition is met (`pair_cost < 1` and `min(qty_yes, qty_no) > cost_yes + cost_no`) or when the market window end time is reached.
- Automatically advances to the next 15m slug.

## Notes
- `place_order_stub` does **not** place real orders (no auth/no signature). Replace with signed trading messages on the user channel to trade live.
- Add persistence/logging and reconnection handling for production use.
