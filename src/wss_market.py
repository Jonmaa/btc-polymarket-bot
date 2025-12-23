import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import websockets


def _now_s() -> float:
    return time.time()


@dataclass
class L2BookState:
    bids: dict[float, float] = field(default_factory=dict)  # price -> size
    asks: dict[float, float] = field(default_factory=dict)  # price -> size
    last_timestamp_ms: Optional[int] = None
    last_hash: Optional[str] = None

    def apply_snapshot(self, msg: dict[str, Any]) -> None:
        # Docs use bids/asks; some older tables mention buys/sells. Support both.
        bids = msg.get("bids") or msg.get("buys") or []
        asks = msg.get("asks") or msg.get("sells") or []

        self.bids.clear()
        self.asks.clear()

        for lvl in bids:
            try:
                price = float(lvl["price"] if isinstance(lvl, dict) else lvl.price)
                size = float(lvl["size"] if isinstance(lvl, dict) else lvl.size)
            except Exception:
                continue
            if size <= 0:
                continue
            self.bids[price] = size

        for lvl in asks:
            try:
                price = float(lvl["price"] if isinstance(lvl, dict) else lvl.price)
                size = float(lvl["size"] if isinstance(lvl, dict) else lvl.size)
            except Exception:
                continue
            if size <= 0:
                continue
            self.asks[price] = size

        ts = msg.get("timestamp")
        if ts is not None:
            try:
                self.last_timestamp_ms = int(ts)
            except Exception:
                pass
        self.last_hash = msg.get("hash") or self.last_hash

    def apply_price_changes(self, msg: dict[str, Any]) -> None:
        ts = msg.get("timestamp")
        if ts is not None:
            try:
                self.last_timestamp_ms = int(ts)
            except Exception:
                pass

        for ch in msg.get("price_changes", []) or []:
            try:
                price = float(ch.get("price"))
                size = float(ch.get("size"))
                side = str(ch.get("side", "")).upper()
            except Exception:
                continue

            book = self.bids if side == "BUY" else self.asks

            if size <= 0:
                book.pop(price, None)
            else:
                book[price] = size

            # hash field refers to the order; keep as last_hash for debugging
            if ch.get("hash"):
                self.last_hash = ch.get("hash")

    def to_levels(self) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        bid_levels = sorted(((p, s) for p, s in self.bids.items() if s > 0), key=lambda x: x[0], reverse=True)
        ask_levels = sorted(((p, s) for p, s in self.asks.items() if s > 0), key=lambda x: x[0])
        return bid_levels, ask_levels


class MarketWssClient:
    """Minimal Polymarket CLOB Market channel WebSocket client.

    Keeps an in-memory L2 aggregated book per asset_id and emits updates via an async iterator.
    """

    def __init__(self, *, ws_base_url: str, asset_ids: list[str]):
        self.ws_base_url = ws_base_url.rstrip("/")
        self.asset_ids = asset_ids
        self._books: dict[str, L2BookState] = {asset_id: L2BookState() for asset_id in asset_ids}

    def get_book(self, asset_id: str) -> Optional[L2BookState]:
        return self._books.get(asset_id)

    async def run(self):
        """Async generator yielding (asset_id, event_type) for each processed message."""
        url = f"{self.ws_base_url}/ws/market"

        # Throttle only the noisy connect attempts. Never throttle errors.
        last_connect_log_s = 0.0
        def log_connect_throttled(msg: str) -> None:
            nonlocal last_connect_log_s
            now = _now_s()
            if (now - last_connect_log_s) >= 10.0:
                print(msg)
                last_connect_log_s = now

        # Avoid hanging forever on network/proxy/firewall issues
        open_timeout_s = 10

        # Basic reconnect loop
        while True:
            try:
                log_connect_throttled(f"[WSS] Connecting to {url}...")
                async with websockets.connect(
                    url,
                    ping_interval=10,
                    ping_timeout=10,
                    open_timeout=open_timeout_s,
                    close_timeout=5,
                ) as ws:
                    # Subscribe to initial assets
                    # Per docs: type is "MARKET" and the field name is "assets_ids".
                    await ws.send(json.dumps({"assets_ids": self.asset_ids, "type": "MARKET"}))
                    print(f"[WSS] Subscribed to {len(self.asset_ids)} asset_ids")

                    while True:
                        raw = await ws.recv()
                        payload = json.loads(raw)
                        # Some servers may batch multiple events in a single message.
                        msgs = payload if isinstance(payload, list) else [payload]

                        for msg in msgs:
                            if not isinstance(msg, dict):
                                continue

                            event_type = msg.get("event_type")
                            asset_id = msg.get("asset_id")

                            if event_type == "book" and asset_id in self._books:
                                self._books[asset_id].apply_snapshot(msg)
                                yield asset_id, event_type
                            elif event_type == "price_change":
                                # price_change carries an array of changes, each includes asset_id.
                                # Apply each change to the right book.
                                ts = msg.get("timestamp")
                                for ch in msg.get("price_changes", []) or []:
                                    if not isinstance(ch, dict):
                                        continue
                                    aid = ch.get("asset_id")
                                    if aid not in self._books:
                                        continue
                                    # Wrap into a per-asset message to reuse apply_price_changes
                                    per_asset_msg = {"timestamp": ts, "price_changes": [ch]}
                                    self._books[aid].apply_price_changes(per_asset_msg)
                                    yield aid, event_type
                            else:
                                # ignore other event types (tick_size_change, last_trade_price, etc)
                                continue

            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Simple backoff on disconnect/errors
                extra = ""
                try:
                    # websockets raises ConnectionClosed (OK or Error) with code/reason
                    code = getattr(e, "code", None)
                    reason = getattr(e, "reason", None)
                    if code is not None:
                        extra = f" code={code}"
                    if reason:
                        extra += f" reason={reason}"
                except Exception:
                    extra = ""

                print(f"[WSS] Connection error ({type(e).__name__}: {e}){extra}; retrying...")
                await asyncio.sleep(1.0)
                continue
