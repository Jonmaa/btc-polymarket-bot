"""
Simple arbitrage bot for Bitcoin 15min markets following Jeremy Whittaker's strategy.

Strategy: Buy both sides (UP and DOWN) when total cost < $1.00
to guarantee profits regardless of the outcome.
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from typing import Optional

import httpx

from .config import load_settings
from .lookup import fetch_market_from_slug
from .trading import get_client, place_order, get_positions, place_orders_fast
from .wss_market import MarketWssClient


logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Disable HTTP logs from httpx
logging.getLogger("httpx").setLevel(logging.WARNING)


def find_current_btc_15min_market() -> str:
    """
    Find the current active BTC 15min market on Polymarket.
    
    Searches for markets matching the pattern 'btc-updown-15m-<timestamp>'
    and returns the slug of the most recent/active market.
    """
    logger.info("Searching for current BTC 15min market...")
    
    try:
        # Search on Polymarket's crypto 15min page
        page_url = "https://polymarket.com/crypto/15M"
        resp = httpx.get(page_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()
        
        # Find the BTC market slug in the HTML
        pattern = r'btc-updown-15m-(\d+)'
        matches = re.findall(pattern, resp.text)
        
        if not matches:
            raise RuntimeError("No active BTC 15min market found")
        
        # Prefer the most recent timestamp that is still OPEN.
        # 15min markets close 900s after the timestamp in the slug.
        now_ts = int(datetime.now().timestamp())
        all_ts = sorted((int(ts) for ts in matches), reverse=True)
        open_ts = [ts for ts in all_ts if now_ts < (ts + 900)]
        chosen_ts = open_ts[0] if open_ts else all_ts[0]
        slug = f"btc-updown-15m-{chosen_ts}"
        
        logger.info(f"✅ Market found: {slug}")
        return slug
        
    except Exception as e:
        logger.error(f"Error searching for BTC 15min market: {e}")
        # Fallback: try with the last known one
        logger.warning("Using default market from configuration...")
        raise


class SimpleArbitrageBot:
    """Simple bot implementing Jeremy Whittaker's strategy."""
    
    def __init__(self, settings):
        self.settings = settings
        self.client = get_client(settings)
        
        # Try to find current BTC 15min market automatically
        try:
            market_slug = find_current_btc_15min_market()
        except Exception as e:
            # Fallback: use the slug configured in .env
            if settings.market_slug:
                logger.info(f"Using configured market: {settings.market_slug}")
                market_slug = settings.market_slug
            else:
                raise RuntimeError("Could not find BTC 15min market and no slug configured in .env")
        
        # Get token IDs from the market
        logger.info(f"Getting market information: {market_slug}")
        market_info = fetch_market_from_slug(market_slug)
        
        self.market_id = market_info["market_id"]
        self.yes_token_id = market_info["yes_token_id"]
        self.no_token_id = market_info["no_token_id"]
        
        logger.info(f"Market ID: {self.market_id}")
        logger.info(f"UP Token (YES): {self.yes_token_id}")
        logger.info(f"DOWN Token (NO): {self.no_token_id}")
        
        # Extract market timestamp to calculate remaining time
        # The timestamp in the slug is when it OPENS, not when it closes
        # 15min markets close 15 minutes (900 seconds) later
        import re
        match = re.search(r'btc-updown-15m-(\d+)', market_slug)
        market_start = int(match.group(1)) if match else None
        self.market_end_timestamp = market_start + 900 if market_start else None  # +15 min
        self.market_slug = market_slug
        
        self.last_check = None
        self.opportunities_found = 0
        self.trades_executed = 0
        
        # Investment tracking
        self.total_invested = 0.0
        self.total_shares_bought = 0
        self.positions = []  # List of open positions
        
        # Cached balance (updated after each trade)
        self.cached_balance = None
        
        # Simulation balance (used in dry_run mode)
        self.sim_balance = self.settings.sim_balance if self.settings.sim_balance > 0 else 100.0
        self.sim_start_balance = self.sim_balance

        # Simple cooldown to avoid repeated orders on the same fleeting opportunity
        self._last_execution_ts = 0.0
    
    def get_time_remaining(self) -> str:
        """Get remaining time until market closes."""
        if not self.market_end_timestamp:
            return "Unknown"
        
        from datetime import datetime
        now = int(datetime.now().timestamp())
        remaining = self.market_end_timestamp - now
        
        if remaining <= 0:
            return "CLOSED"
        
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        return f"{minutes}m {seconds}s"
    
    def get_balance(self) -> float:
        """Get current USDC balance (or simulated balance in dry_run mode)."""
        if self.settings.dry_run:
            return self.sim_balance
        from .trading import get_balance
        return get_balance(self.settings)
    
    def get_current_prices(self) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
        """
        Get current prices from order book (best ask = lowest price we can BUY at).
        
        Using best_ask ensures we get the actual available price, not a historical
        trade price that may no longer be available.
        
        Returns:
            (up_price, down_price, up_size, down_size) - prices and available sizes
        """
        try:
            # Get order book for both tokens
            up_book = self.get_order_book(self.yes_token_id)
            down_book = self.get_order_book(self.no_token_id)
            
            # Use best_ask (lowest sell price = price we can buy at)
            price_up = up_book.get("best_ask")
            price_down = down_book.get("best_ask")
            
            # Available sizes at best ask prices
            size_up = up_book.get("ask_size", 0)
            size_down = down_book.get("ask_size", 0)
            
            # If no asks available, we can't buy
            if price_up is None or price_down is None:
                logger.warning("No asks available in order book")
                return None, None, None, None
            
            return price_up, price_down, size_up, size_down
        except Exception as e:
            logger.error(f"Error getting prices: {e}")
            return None, None, None, None

    def _levels_to_tuples(self, levels) -> list[tuple[float, float]]:
        """Convert OrderSummary-like objects into (price, size) tuples."""
        tuples: list[tuple[float, float]] = []
        for level in levels or []:
            try:
                price = float(level.price)
                size = float(level.size)
            except Exception:
                continue
            if size <= 0:
                continue
            tuples.append((price, size))
        return tuples

    def _compute_buy_fill(self, asks: list[tuple[float, float]], target_size: float) -> Optional[dict]:
        """
        Compute fill information for buying `target_size` shares using the ask book.

        Returns:
            dict with keys: filled, vwap, worst, best, cost
            or None if not enough liquidity.
        """
        if target_size <= 0:
            return None

        # Cheapest asks first
        sorted_asks = sorted(asks, key=lambda x: x[0])
        filled = 0.0
        cost = 0.0
        worst = None
        best = sorted_asks[0][0] if sorted_asks else None

        for price, size in sorted_asks:
            if filled >= target_size:
                break
            take = min(size, target_size - filled)
            cost += take * price
            filled += take
            worst = price

        if filled + 1e-9 < target_size:
            return None

        vwap = cost / filled if filled > 0 else None
        return {
            "filled": filled,
            "vwap": vwap,
            "worst": worst,
            "best": best,
            "cost": cost,
        }
    
    def get_order_book(self, token_id: str) -> dict:
        """Get order book for a token."""
        try:
            book = self.client.get_order_book(token_id=token_id)
            # The result is an OrderBookSummary object, not a dict
            bids = book.bids if hasattr(book, 'bids') and book.bids else []
            asks = book.asks if hasattr(book, 'asks') and book.asks else []

            bid_levels = self._levels_to_tuples(bids)
            ask_levels = self._levels_to_tuples(asks)

            best_bid = max((p for p, _ in bid_levels), default=None)
            best_ask = min((p for p, _ in ask_levels), default=None)

            bid_size = 0.0
            if best_bid is not None:
                for p, s in bid_levels:
                    if p == best_bid:
                        bid_size = s
                        break

            ask_size = 0.0
            if best_ask is not None:
                for p, s in ask_levels:
                    if p == best_ask:
                        ask_size = s
                        break

            spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None

            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "bid_size": bid_size,
                "ask_size": ask_size,
                "bids": bid_levels,
                "asks": ask_levels,
            }
        except Exception as e:
            logger.error(f"Error getting order book: {e}")
            return {}

    async def _fetch_order_books_parallel(self) -> tuple[dict, dict]:
        """Fetch UP/DOWN order books concurrently to reduce per-scan latency."""
        try:
            up_task = asyncio.to_thread(self.get_order_book, self.yes_token_id)
            down_task = asyncio.to_thread(self.get_order_book, self.no_token_id)
            up_book, down_book = await asyncio.gather(up_task, down_task)
            return up_book, down_book
        except Exception as e:
            logger.warning(f"Parallel order book fetch failed, falling back to sequential: {e}")
            return self.get_order_book(self.yes_token_id), self.get_order_book(self.no_token_id)
    
    def check_arbitrage(self, up_book: Optional[dict] = None, down_book: Optional[dict] = None) -> Optional[dict]:
        """
        Check if an arbitrage opportunity exists.
        
        Uses order book (best ask) to get REAL prices we can buy at.
        Also verifies there's enough liquidity at those prices.
        
        Returns dict with information if opportunity exists, None otherwise.
        """
        # Pull full order books (allow caller to pass pre-fetched books to reduce latency)
        if up_book is None:
            up_book = self.get_order_book(self.yes_token_id)
        if down_book is None:
            down_book = self.get_order_book(self.no_token_id)

        # Basic sanity: in a normal book, best_ask >= best_bid
        for side_name, book in ("UP", up_book), ("DOWN", down_book):
            best_bid = book.get("best_bid")
            best_ask = book.get("best_ask")
            if best_bid is not None and best_ask is not None and best_ask < best_bid:
                logger.warning(
                    f"{side_name} order book looks inverted (best_ask={best_ask:.4f} < best_bid={best_bid:.4f}); skipping scan"
                )
                return None

        asks_up = up_book.get("asks", [])
        asks_down = down_book.get("asks", [])

        # Compute the prices required to actually fill ORDER_SIZE shares (walk the book)
        fill_up = self._compute_buy_fill(asks_up, float(self.settings.order_size))
        fill_down = self._compute_buy_fill(asks_down, float(self.settings.order_size))

        if not fill_up or not fill_down:
            return None

        # For guaranteed arbitrage, use the *worst* price we might have to pay to fill the size
        limit_price_up = fill_up["worst"]
        limit_price_down = fill_down["worst"]
        if limit_price_up is None or limit_price_down is None:
            return None

        total_cost = limit_price_up + limit_price_down

        # Use <= to avoid missing exact-threshold opportunities due to rounding
        if total_cost <= self.settings.target_pair_cost:
            profit = 1.0 - total_cost
            profit_pct = (profit / total_cost) * 100 if total_cost > 0 else 0

            investment = total_cost * self.settings.order_size
            expected_payout = 1.0 * self.settings.order_size
            expected_profit = expected_payout - investment

            return {
                # Prices we will actually place as LIMITs (to ensure fills)
                "price_up": limit_price_up,
                "price_down": limit_price_down,
                "total_cost": total_cost,
                "profit_per_share": profit,
                "profit_pct": profit_pct,
                "order_size": self.settings.order_size,
                "total_investment": investment,
                "expected_payout": expected_payout,
                "expected_profit": expected_profit,

                # Extra diagnostics
                "best_ask_up": fill_up.get("best"),
                "best_ask_down": fill_down.get("best"),
                "vwap_up": fill_up.get("vwap"),
                "vwap_down": fill_down.get("vwap"),
                "timestamp": datetime.now().isoformat(),
            }

        return None
    
    def execute_arbitrage(self, opportunity: dict):
        """Execute arbitrage by buying both sides."""

        # Cooldown guard (applies to both live and dry-run)
        now = asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else time.time()
        if self.settings.cooldown_seconds and (now - self._last_execution_ts) < float(self.settings.cooldown_seconds):
            logger.info(f"Cooldown active ({self.settings.cooldown_seconds}s); skipping execution")
            return
        self._last_execution_ts = now
        
        # Count opportunity found (regardless of execution)
        self.opportunities_found += 1
        
        logger.info("=" * 70)
        logger.info("🎯 ARBITRAGE OPPORTUNITY DETECTED")
        logger.info("=" * 70)
        logger.info(f"UP limit price:       ${opportunity['price_up']:.4f}")
        logger.info(f"DOWN limit price:     ${opportunity['price_down']:.4f}")
        if 'vwap_up' in opportunity and 'vwap_down' in opportunity:
            logger.info(f"UP VWAP (est):        ${opportunity['vwap_up']:.4f}")
            logger.info(f"DOWN VWAP (est):      ${opportunity['vwap_down']:.4f}")
        logger.info(f"Total cost:           ${opportunity['total_cost']:.4f}")
        logger.info(f"Profit per share:     ${opportunity['profit_per_share']:.4f}")
        logger.info(f"Profit %:             {opportunity['profit_pct']:.2f}%")
        logger.info("-" * 70)
        logger.info(f"Order size:           {opportunity['order_size']} shares each side")
        logger.info(f"Total investment:     ${opportunity['total_investment']:.2f}")
        logger.info(f"Expected payout:      ${opportunity['expected_payout']:.2f}")
        logger.info(f"EXPECTED PROFIT:      ${opportunity['expected_profit']:.2f}")
        logger.info("=" * 70)
        
        if self.settings.dry_run:
            logger.info("🔸 SIMULATION MODE - No real orders will be executed")
            
            # Check simulated balance
            if self.sim_balance < opportunity['total_investment']:
                logger.error(f"❌ Insufficient simulated balance: need ${opportunity['total_investment']:.2f} but have ${self.sim_balance:.2f}")
                return
            
            # Deduct from simulated balance
            self.sim_balance -= opportunity['total_investment']
            logger.info(f"💰 Simulated balance: ${self.sim_balance:.2f} (after deducting ${opportunity['total_investment']:.2f})")
            
            # Track simulated investment
            self.total_invested += opportunity['total_investment']
            self.total_shares_bought += opportunity['order_size'] * 2  # UP + DOWN
            self.positions.append(opportunity)
            self.trades_executed += 1
            logger.info("=" * 70)
            return
        
        # Check balance before executing (with 20% safety margin)
        logger.info("\nVerifying balance...")
        # Use cached balance if available, otherwise fetch from API
        if self.cached_balance is not None:
            current_balance = self.cached_balance
            logger.info(f"Available balance (cached): ${current_balance:.2f}")
        else:
            current_balance = self.get_balance()
            self.cached_balance = current_balance
            logger.info(f"Available balance: ${current_balance:.2f}")
        
        required_balance = opportunity['total_investment'] * 1.2  # 20% safety margin
        logger.info(f"Required (+ 20% margin): ${required_balance:.2f}")
        
        if current_balance < required_balance:
            logger.error(f"❌ Insufficient balance: need ${required_balance:.2f} but have ${current_balance:.2f}")
            logger.error("20% extra margin required to avoid mid-execution failures")
            logger.error("Arbitrage will not be executed")
            return
        
        try:
            # Execute orders
            logger.info("\n📤 Executing orders IN PARALLEL...")
            
            # Use exact prices from arbitrage opportunity
            up_price = opportunity['price_up']
            down_price = opportunity['price_down']
            
            # Prepare both orders
            orders = [
                {
                    "side": "BUY",
                    "token_id": self.yes_token_id,
                    "price": up_price,
                    "size": self.settings.order_size
                },
                {
                    "side": "BUY",
                    "token_id": self.no_token_id,
                    "price": down_price,
                    "size": self.settings.order_size
                }
            ]
            
            logger.info(f"   UP:   {self.settings.order_size} shares @ ${up_price:.4f}")
            logger.info(f"   DOWN: {self.settings.order_size} shares @ ${down_price:.4f}")
            
            # Execute both orders as fast as possible
            results = place_orders_fast(self.settings, orders)
            
            # Check results
            errors = [r for r in results if isinstance(r, dict) and "error" in r]
            if errors:
                for err in errors:
                    logger.error(f"❌ Order error: {err['error']}")
                raise RuntimeError(f"Some orders failed: {errors}")
            
            logger.info(f"✅ UP order executed")
            logger.info(f"✅ DOWN order executed")
            
            # Verify positions are balanced
            import time
            time.sleep(1)  # Wait for orders to settle
            
            positions = get_positions(self.settings, [self.yes_token_id, self.no_token_id])
            up_shares = positions.get(self.yes_token_id, {}).get("size", 0)
            down_shares = positions.get(self.no_token_id, {}).get("size", 0)
            
            if abs(up_shares - down_shares) > 0.1:
                logger.warning(f"⚠️ POSITION IMBALANCE DETECTED!")
                logger.warning(f"   UP shares: {up_shares:.2f}")
                logger.warning(f"   DOWN shares: {down_shares:.2f}")
                logger.warning(f"   Difference: {abs(up_shares - down_shares):.2f}")
                logger.warning("   ⚠️ Manual intervention may be needed to balance positions")
            
            logger.info("\n" + "=" * 70)
            logger.info("✅ ARBITRAGE EXECUTED SUCCESSFULLY")
            logger.info("=" * 70)
            
            self.trades_executed += 1
            
            # Track real investment
            self.total_invested += opportunity['total_investment']
            self.total_shares_bought += opportunity['order_size'] * 2  # UP + DOWN
            self.positions.append(opportunity)
            
            # Update cached balance after trade
            new_balance = self.get_balance()
            self.cached_balance = new_balance
            logger.info(f"💰 Updated balance: ${new_balance:.2f}")
            
            # Get and show current positions
            self.show_current_positions()
            
        except Exception as e:
            logger.error(f"\n❌ Error executing arbitrage: {e}")
            logger.error("❌ Orders were NOT executed - tracking was not updated")
    
    def show_current_positions(self):
        """Show current share positions for UP and DOWN tokens."""
        try:
            positions = get_positions(self.settings, [self.yes_token_id, self.no_token_id])
            
            up_shares = positions.get(self.yes_token_id, {}).get("size", 0)
            down_shares = positions.get(self.no_token_id, {}).get("size", 0)
            
            logger.info("-" * 70)
            logger.info("📊 CURRENT POSITIONS:")
            logger.info(f"   UP shares:   {up_shares:.2f}")
            logger.info(f"   DOWN shares: {down_shares:.2f}")
            logger.info("-" * 70)
            
        except Exception as e:
            logger.warning(f"Could not fetch positions: {e}")
    
    def get_market_result(self) -> Optional[str]:
        """Get which option won the market."""
        try:
            # Get final prices
            price_up, price_down, _, _ = self.get_current_prices()
            
            if price_up is None or price_down is None:
                return None
            
            # In closed markets, winner has price 1.0 and loser 0.0
            if price_up >= 0.99:
                return "UP (goes up) 📈"
            elif price_down >= 0.99:
                return "DOWN (goes down) 📉"
            else:
                # Market not resolved yet, see which has higher probability
                if price_up > price_down:
                    return f"UP leading ({price_up:.2%})"
                else:
                    return f"DOWN leading ({price_down:.2%})"
        except Exception as e:
            logger.error(f"Error getting result: {e}")
            return None
    
    def show_final_summary(self):
        """Show final summary when market closes."""
        logger.info("\n" + "=" * 70)
        logger.info("🏁 MARKET CLOSED - FINAL SUMMARY")
        logger.info("=" * 70)
        logger.info(f"Market: {self.market_slug}")
        
        # Get market result
        result = self.get_market_result()
        if result:
            logger.info(f"Result: {result}")
        
        logger.info(f"Mode: {'🔸 SIMULATION' if self.settings.dry_run else '🔴 REAL TRADING'}")
        logger.info("-" * 70)
        logger.info(f"Total opportunities detected:    {self.opportunities_found}")
        logger.info(f"Total trades executed:           {self.trades_executed if not self.settings.dry_run else self.opportunities_found}")
        logger.info(f"Total shares bought:             {self.total_shares_bought}")
        logger.info("-" * 70)
        logger.info(f"Total invested:                  ${self.total_invested:.2f}")
        
        # Calculate expected profit
        if self.settings.dry_run:
            expected_payout = sum(float(p.get("expected_payout", 0.0)) for p in (self.positions or []))
        else:
            expected_payout = (self.total_shares_bought / 2) * 1.0  # Each pair pays $1.00

        expected_profit = expected_payout - self.total_invested
        profit_pct = (expected_profit / self.total_invested * 100) if self.total_invested > 0 else 0
        
        logger.info(f"Expected payout at close:        ${expected_payout:.2f}")
        logger.info(f"Expected profit:                 ${expected_profit:.2f} ({profit_pct:.2f}%)")

        if self.settings.dry_run:
            cash_remaining = float(self.sim_balance)
            cash_after_claim = cash_remaining + float(expected_payout)
            net_change = cash_after_claim - float(self.sim_start_balance)
            net_change_pct = (net_change / float(self.sim_start_balance) * 100) if self.sim_start_balance > 0 else 0
            logger.info("-" * 70)
            logger.info(f"Sim start cash:                  ${self.sim_start_balance:.2f}")
            logger.info(f"Sim cash remaining:              ${cash_remaining:.2f}")
            logger.info(f"Sim cash after claiming:         ${cash_after_claim:.2f}")
            logger.info(f"Sim net change:                  ${net_change:.2f} ({net_change_pct:.2f}%)")
        logger.info("=" * 70)
    
    def run_once(self) -> bool:
        """Scan once for opportunities."""
        # Check if market closed
        time_remaining = self.get_time_remaining()
        if time_remaining == "CLOSED":
            return False  # Signal to stop the bot

        # Fetch both books once per scan (most expensive operations)
        up_book = self.get_order_book(self.yes_token_id)
        down_book = self.get_order_book(self.no_token_id)

        opportunity = self.check_arbitrage(up_book=up_book, down_book=down_book)
        
        if opportunity:
            self.execute_arbitrage(opportunity)
            return True
        else:
            price_up = up_book.get("best_ask")
            price_down = down_book.get("best_ask")
            size_up = up_book.get("ask_size", 0)
            size_down = down_book.get("ask_size", 0)

            if price_up is not None and price_down is not None:
                best_total = price_up + price_down

                # Compute fill-based totals for ORDER_SIZE (more accurate than best_ask)
                fill_up = self._compute_buy_fill(up_book.get("asks", []), float(self.settings.order_size))
                fill_down = self._compute_buy_fill(down_book.get("asks", []), float(self.settings.order_size))

                fill_msg = ""
                if fill_up and fill_down and fill_up.get("worst") is not None and fill_down.get("worst") is not None:
                    worst_total = float(fill_up["worst"]) + float(fill_down["worst"])
                    vwap_total = float(fill_up["vwap"]) + float(fill_down["vwap"]) if (fill_up.get("vwap") is not None and fill_down.get("vwap") is not None) else None
                    if vwap_total is not None:
                        fill_msg = f" | fill(worst)=${worst_total:.4f} vwap=${vwap_total:.4f}"
                    else:
                        fill_msg = f" | fill(worst)=${worst_total:.4f}"

                logger.info(
                    f"No arbitrage: UP=${price_up:.4f} ({size_up:.0f}) + DOWN=${price_down:.4f} ({size_down:.0f}) "
                    f"= ${best_total:.4f} (threshold=${self.settings.target_pair_cost:.3f}){fill_msg} "
                    f"[Time: {time_remaining}]"
                )
            return False

    async def run_once_async(self) -> bool:
        """Scan once for opportunities (async; fetches books in parallel)."""
        # Check if market closed
        time_remaining = self.get_time_remaining()
        if time_remaining == "CLOSED":
            return False  # Signal to stop the bot

        # Fetch both books concurrently (reduces per-scan latency)
        up_book, down_book = await self._fetch_order_books_parallel()

        opportunity = self.check_arbitrage(up_book=up_book, down_book=down_book)

        if opportunity:
            self.execute_arbitrage(opportunity)
            return True

        price_up = up_book.get("best_ask")
        price_down = down_book.get("best_ask")
        size_up = up_book.get("ask_size", 0)
        size_down = down_book.get("ask_size", 0)

        if price_up is not None and price_down is not None:
            best_total = price_up + price_down

            # Compute fill-based totals for ORDER_SIZE (more accurate than best_ask)
            fill_up = self._compute_buy_fill(up_book.get("asks", []), float(self.settings.order_size))
            fill_down = self._compute_buy_fill(down_book.get("asks", []), float(self.settings.order_size))

            fill_msg = ""
            if fill_up and fill_down and fill_up.get("worst") is not None and fill_down.get("worst") is not None:
                worst_total = float(fill_up["worst"]) + float(fill_down["worst"])
                vwap_total = float(fill_up["vwap"]) + float(fill_down["vwap"]) if (fill_up.get("vwap") is not None and fill_down.get("vwap") is not None) else None
                if vwap_total is not None:
                    fill_msg = f" | fill(worst)=${worst_total:.4f} vwap=${vwap_total:.4f}"
                else:
                    fill_msg = f" | fill(worst)=${worst_total:.4f}"

            logger.info(
                f"No arbitrage: UP=${price_up:.4f} ({size_up:.0f}) + DOWN=${price_down:.4f} ({size_down:.0f}) "
                f"= ${best_total:.4f} (threshold=${self.settings.target_pair_cost:.3f}){fill_msg} "
                f"[Time: {time_remaining}]"
            )

        return False
    
    async def monitor(self, interval_seconds: int = 30):
        """Continuously monitor for opportunities."""
        if getattr(self.settings, "use_wss", False):
            await self.monitor_wss()
            return
        logger.info("=" * 70)
        logger.info("🚀 BITCOIN 15MIN ARBITRAGE BOT STARTED")
        logger.info("=" * 70)
        logger.info(f"Market: {self.market_slug}")
        logger.info(f"Time remaining: {self.get_time_remaining()}")
        logger.info(f"Mode: {'🔸 SIMULATION' if self.settings.dry_run else '🔴 REAL TRADING'}")
        logger.info(f"Cost threshold: ${self.settings.target_pair_cost:.3f}")
        logger.info(f"Order size: {self.settings.order_size} shares")
        logger.info(f"Interval: {interval_seconds}s")
        logger.info("=" * 70)
        logger.info("")
        
        scan_count = 0
        
        try:
            while True:
                scan_count += 1
                logger.info(f"\n[Scan #{scan_count}] {datetime.now().strftime('%H:%M:%S')}")
                
                # Check if market closed
                if self.get_time_remaining() == "CLOSED":
                    logger.info("\n🚨 Market has closed!")
                    self.show_final_summary()
                    
                    # Search for the next market
                    logger.info("\n🔄 Searching for next BTC 15min market...")
                    try:
                        new_market_slug = find_current_btc_15min_market()
                        if new_market_slug != self.market_slug:
                            logger.info(f"✅ New market found: {new_market_slug}")
                            logger.info("Restarting bot with new market...")
                            # Restart the bot with the new market
                            self.__init__(self.settings)
                            scan_count = 0
                            continue
                        else:
                            logger.info("⏳ Waiting for new market... (30s)")
                            await asyncio.sleep(30)
                            continue
                    except Exception as e:
                        logger.error(f"Error searching for new market: {e}")
                        logger.info("Retrying in 30 seconds...")
                        await asyncio.sleep(30)
                        continue
                
                # Use async scan to fetch books in parallel
                await self.run_once_async()
                
                logger.info(f"Opportunities found: {self.opportunities_found}/{scan_count}")
                if not self.settings.dry_run:
                    logger.info(f"Trades executed: {self.trades_executed}")
                
                logger.info(f"Waiting {interval_seconds}s...\n")
                await asyncio.sleep(interval_seconds)
                
        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("\n" + "=" * 70)
            logger.info("🛑 Bot stopped by user")
            logger.info(f"Total scans: {scan_count}")
            logger.info(f"Opportunities found: {self.opportunities_found}")
            if not self.settings.dry_run:
                logger.info(f"Trades executed: {self.trades_executed}")
            logger.info("=" * 70)

    def _book_from_state(self, bid_levels: list[tuple[float, float]], ask_levels: list[tuple[float, float]]) -> dict:
        best_bid = max((p for p, _ in bid_levels), default=None)
        best_ask = min((p for p, _ in ask_levels), default=None)

        bid_size = 0.0
        if best_bid is not None:
            for p, s in bid_levels:
                if p == best_bid:
                    bid_size = s
                    break

        ask_size = 0.0
        if best_ask is not None:
            for p, s in ask_levels:
                if p == best_ask:
                    ask_size = s
                    break

        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "bid_size": bid_size,
            "ask_size": ask_size,
            "bids": bid_levels,
            "asks": ask_levels,
        }

    async def monitor_wss(self):
        """Monitor using Polymarket CLOB Market WebSocket instead of polling."""
        # This loop keeps WSS running across market rollovers.
        while True:
            # If the detected market is already closed, rollover immediately.
            if self.get_time_remaining() == "CLOSED":
                logger.info("\n🚨 Market has closed (before WSS start).")
                self.show_final_summary()
                logger.info("\n🔄 Searching for next BTC 15min market...")
                try:
                    new_market_slug = find_current_btc_15min_market()
                    if new_market_slug != self.market_slug:
                        logger.info(f"✅ New market found: {new_market_slug}")
                        logger.info("Restarting bot with new market...")
                        self.__init__(self.settings)
                        continue
                    logger.info("⏳ Waiting for new market... (10s)")
                    await asyncio.sleep(10)
                    continue
                except Exception as e:
                    logger.error(f"Error searching for new market: {e}")
                    logger.info("Retrying in 10 seconds...")
                    await asyncio.sleep(10)
                    continue

            logger.info("=" * 70)
            logger.info("🚀 BITCOIN 15MIN ARBITRAGE BOT STARTED (WSS MODE)")
            logger.info("=" * 70)
            logger.info(f"Market: {self.market_slug}")
            logger.info(f"Time remaining: {self.get_time_remaining()}")
            logger.info(f"Mode: {'🔸 SIMULATION' if self.settings.dry_run else '🔴 REAL TRADING'}")
            logger.info(f"Cost threshold: ${self.settings.target_pair_cost:.3f}")
            logger.info(f"Order size: {self.settings.order_size} shares")
            logger.info(f"WSS URL: {self.settings.ws_url}")
            logger.info("=" * 70)
            logger.info("")

            client = MarketWssClient(
                ws_base_url=self.settings.ws_url,
                asset_ids=[self.yes_token_id, self.no_token_id],
            )

            last_eval = 0.0
            eval_min_interval_s = 0.05  # avoid evaluating too frequently on rapid deltas
            eval_count = 0

            try:
                async for asset_id, event_type in client.run():
                    # Periodic close check
                    if self.get_time_remaining() == "CLOSED":
                        logger.info("\n🚨 Market has closed!")
                        self.show_final_summary()
                        # Roll over to next market
                        logger.info("\n🔄 Searching for next BTC 15min market...")
                        try:
                            new_market_slug = find_current_btc_15min_market()
                            if new_market_slug != self.market_slug:
                                logger.info(f"✅ New market found: {new_market_slug}")
                                logger.info("Restarting bot with new market...")
                                self.__init__(self.settings)
                                break
                            logger.info("⏳ Waiting for new market... (10s)")
                            await asyncio.sleep(10)
                            break
                        except Exception as e:
                            logger.error(f"Error searching for new market: {e}")
                            logger.info("Retrying in 10 seconds...")
                            await asyncio.sleep(10)
                            break

                    # Debounce evaluation
                    now = asyncio.get_running_loop().time()
                    if (now - last_eval) < eval_min_interval_s:
                        continue
                    last_eval = now
                    eval_count += 1
                    logger.info(f"\n[WSS Eval #{eval_count}] {datetime.now().strftime('%H:%M:%S')} (trigger={event_type}:{asset_id[:8]}…)")

                    yes_state = client.get_book(self.yes_token_id)
                    no_state = client.get_book(self.no_token_id)
                    if not yes_state or not no_state:
                        if self.settings.verbose:
                            logger.info("WSS eval skipped: missing book state (waiting for initial snapshots)")
                        continue

                    yes_bids, yes_asks = yes_state.to_levels()
                    no_bids, no_asks = no_state.to_levels()
                    if not yes_asks or not no_asks:
                        if self.settings.verbose:
                            logger.info("WSS eval skipped: missing asks on one side (no buyable liquidity yet)")
                        continue

                    up_book = self._book_from_state(yes_bids, yes_asks)
                    down_book = self._book_from_state(no_bids, no_asks)

                    opportunity = self.check_arbitrage(up_book=up_book, down_book=down_book)
                    if opportunity:
                        self.execute_arbitrage(opportunity)
                        continue

                    # Minimal "no arb" logging using the same in-memory snapshot
                    price_up = up_book.get("best_ask")
                    price_down = down_book.get("best_ask")
                    size_up = up_book.get("ask_size", 0)
                    size_down = down_book.get("ask_size", 0)

                    if price_up is not None and price_down is not None:
                        best_total = float(price_up) + float(price_down)
                        fill_up = self._compute_buy_fill(up_book.get("asks", []), float(self.settings.order_size))
                        fill_down = self._compute_buy_fill(down_book.get("asks", []), float(self.settings.order_size))

                        fill_msg = ""
                        if fill_up and fill_down and fill_up.get("worst") is not None and fill_down.get("worst") is not None:
                            worst_total = float(fill_up["worst"]) + float(fill_down["worst"])
                            vwap_total = float(fill_up["vwap"]) + float(fill_down["vwap"]) if (fill_up.get("vwap") is not None and fill_down.get("vwap") is not None) else None
                            if vwap_total is not None:
                                fill_msg = f" | fill(worst)=${worst_total:.4f} vwap=${vwap_total:.4f}"
                            else:
                                fill_msg = f" | fill(worst)=${worst_total:.4f}"

                        logger.info(
                            f"No arbitrage: UP=${price_up:.4f} ({size_up:.0f}) + DOWN=${price_down:.4f} ({size_down:.0f}) "
                            f"= ${best_total:.4f} (threshold=${self.settings.target_pair_cost:.3f}){fill_msg} "
                            f"[Time: {self.get_time_remaining()}]"
                        )
                    else:
                        if self.settings.verbose:
                            logger.info("WSS eval skipped: best ask missing (book not ready)")
            except (KeyboardInterrupt, asyncio.CancelledError):
                raise
            except Exception as e:
                logger.warning(f"WSS monitor loop error, reconnecting: {e}")
                await asyncio.sleep(1.0)
                continue


async def main():
    """Main entry point."""
    
    # Load configuration
    settings = load_settings()
    
    # Validate configuration
    if not settings.private_key:
        logger.error("❌ Error: POLYMARKET_PRIVATE_KEY not configured in .env")
        return
    
    # Create and run bot
    try:
        bot = SimpleArbitrageBot(settings)
        await bot.monitor(interval_seconds=0)  # Scan continuously
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
