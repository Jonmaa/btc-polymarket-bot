import asyncio
import math
from datetime import datetime
from typing import Optional

from .config import Settings
from .state import TradeState
from .client import OrderBookStream
from .trading import place_order


class HedgedBot:
    def __init__(self, settings: Settings, end_time: datetime | None = None):
        self.settings = settings
        self.state = TradeState()
        self.end_time = end_time
        self.msg_count = 0
        self.last_up = None
        self.last_down = None
        self.last_trade_at: datetime | None = None
        self.sim_balance = settings.sim_balance if settings.dry_run else None

    def should_buy(self, side: str, price: float, qty: float) -> bool:
        candidate = self.state.simulate_buy(side, price, qty)
        pair_cost, _, _ = candidate.pair_cost()
        # Allow seeding the first leg even if pair_cost is infinite (one side empty).
        if math.isfinite(pair_cost):
            if pair_cost >= self.settings.target_pair_cost:
                return False
        qy, qn = candidate.qty_yes, candidate.qty_no
        if min(qy, qn) == 0 and max(qy, qn) > 0:
            # Allow the first leg without balance check.
            return True
        if max(qy, qn) > 0:
            imbalance = abs(qy - qn) / max(qy, qn)
            if imbalance > self.settings.balance_slack:
                return False
        return True

    def _state_summary(self) -> str:
        # Display state using feed-observed mapping: YES->DOWN, NO->UP
        return f"UP qty={self.state.qty_no:.2f} cost={self.state.cost_no:.2f} | DOWN qty={self.state.qty_yes:.2f} cost={self.state.cost_yes:.2f}"

    def lock_condition(self) -> bool:
        pair_cost, _, _ = self.state.pair_cost()
        return pair_cost < 1.0 and self.state.locked_profit() > 0

    def _best_from_book(self, msg):
        buys = msg.get("buys") or []
        sells = msg.get("sells") or []
        best_bid = float(buys[0]["price"]) if buys else None
        best_ask = float(sells[0]["price"]) if sells else None
        return best_bid, best_ask, msg.get("asset_id")

    def _best_from_price_change(self, msg):
        pcs = msg.get("price_changes") or []
        best_bid = None
        best_ask = None
        asset_id = msg.get("asset_id")
        for pc in pcs:
            bb = pc.get("best_bid")
            ba = pc.get("best_ask")
            if bb is not None:
                best_bid = float(bb)
            if ba is not None:
                best_ask = float(ba)
            asset_id = asset_id or pc.get("asset_id")
        return best_bid, best_ask, asset_id

    def _label_for_asset(self, asset_id: str | None) -> str:
        if not asset_id:
            return "?"
        # Observed mapping: YES token = DOWN, NO token = UP
        if asset_id == self.settings.yes_token_id:
            return "DOWN"
        if asset_id == self.settings.no_token_id:
            return "UP"
        return "?"

    async def run_once(self) -> Optional[str]:
        token_ids = [self.settings.yes_token_id, self.settings.no_token_id]
        async with OrderBookStream(self.settings.ws_url, token_ids, verbose=self.settings.verbose) as stream:
            if self.settings.verbose:
                print(f"[bot] start market_id={self.settings.market_id} yes={self.settings.yes_token_id} no={self.settings.no_token_id}")
                if self.end_time:
                    print(f"[bot] will stop at {self.end_time}")
            try:
                async for msg in stream.messages():
                    if self.end_time and datetime.utcnow().astimezone(self.end_time.tzinfo) >= self.end_time:
                        return "Market window ended"

                    batch = msg if isinstance(msg, list) else [msg]
                    for item in batch:
                        event_type = item.get("event_type") if isinstance(item, dict) else None
                        if not event_type:
                            continue
                        self.msg_count += 1
                        best_bid = best_ask = None
                        asset_id = None
                        if event_type == "book":
                            best_bid, best_ask, asset_id = self._best_from_book(item)
                        elif event_type == "price_change":
                            best_bid, best_ask, asset_id = self._best_from_price_change(item)

                        label = self._label_for_asset(asset_id)
                        if label == "UP":
                            if best_bid is not None:
                                self.last_up = best_bid
                        elif label == "DOWN":
                            if best_ask is not None:
                                self.last_down = best_ask

                        if self.settings.verbose and (best_bid is not None or best_ask is not None):
                            print(f"[price] UP={self.last_up} DOWN={self.last_down}")

                        now = datetime.utcnow()

                        # Observed mapping: UP uses NO token (best_bid), DOWN uses YES token (best_ask)
                        if label == "UP" and best_bid is not None and best_bid < self.settings.no_buy_threshold:
                            if self.should_buy("NO", best_bid, self.settings.order_size):
                                if self.last_trade_at and (now - self.last_trade_at).total_seconds() < self.settings.cooldown_seconds:
                                    pass  # silent cooldown
                                else:
                                    try:
                                        cost = best_bid * self.settings.order_size
                                        if self.sim_balance is not None and cost > self.sim_balance:
                                            continue
                                        if self.settings.dry_run:
                                            print(f"[trade] DRY-RUN BUY UP @{best_bid} size={self.settings.order_size}")
                                        else:
                                            await asyncio.to_thread(
                                                place_order,
                                                self.settings,
                                                side="BUY",
                                                token_id=self.settings.no_token_id,
                                                price=best_bid,
                                                size=self.settings.order_size,
                                            )
                                        self.state.update_after_fill("NO", best_bid, self.settings.order_size)
                                        if self.sim_balance is not None:
                                            self.sim_balance -= cost
                                        print(f"[trade] BUY UP @{best_bid} size={self.settings.order_size} state={self._state_summary()} bal={self.sim_balance if self.sim_balance is not None else 'n/a'}")
                                        self.last_trade_at = now
                                    except Exception as exc:
                                        if self.settings.verbose:
                                            print(f"[trade] BUY UP failed: {exc}")

                        if label == "DOWN" and best_ask is not None and best_ask < self.settings.yes_buy_threshold:
                            if self.should_buy("YES", best_ask, self.settings.order_size):
                                if self.last_trade_at and (now - self.last_trade_at).total_seconds() < self.settings.cooldown_seconds:
                                    pass  # silent cooldown
                                else:
                                    try:
                                        cost = best_ask * self.settings.order_size
                                        if self.sim_balance is not None and cost > self.sim_balance:
                                            continue
                                        if self.settings.dry_run:
                                            print(f"[trade] DRY-RUN BUY DOWN @{best_ask} size={self.settings.order_size}")
                                        else:
                                            await asyncio.to_thread(
                                                place_order,
                                                self.settings,
                                                side="BUY",
                                                token_id=self.settings.yes_token_id,
                                                price=best_ask,
                                                size=self.settings.order_size,
                                            )
                                        self.state.update_after_fill("YES", best_ask, self.settings.order_size)
                                        if self.sim_balance is not None:
                                            self.sim_balance -= cost
                                        print(f"[trade] BUY DOWN @{best_ask} size={self.settings.order_size} state={self._state_summary()} bal={self.sim_balance if self.sim_balance is not None else 'n/a'}")
                                        self.last_trade_at = now
                                    except Exception as exc:
                                        if self.settings.verbose:
                                            print(f"[trade] BUY DOWN failed: {exc}")

                        if self.lock_condition():
                            if self.settings.verbose:
                                print("[bot] Locked profit, exiting window")
                            return "Locked profit"

                    await asyncio.sleep(0)  # yield control
            except asyncio.CancelledError:
                if self.settings.verbose:
                    print("[bot] Cancelled")
                return "Cancelled"
        return None


async def main():
    from .config import load_settings

    settings = load_settings()
    bot = HedgedBot(settings)
    result = await bot.run_once()
    if settings.verbose:
        print(result or "Exited without lock condition")


if __name__ == "__main__":
    asyncio.run(main())
