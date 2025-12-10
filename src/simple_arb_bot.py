"""
Simple arbitrage bot for Bitcoin 15min markets following Jeremy Whittaker's strategy.

Strategy: Buy both sides (UP and DOWN) when total cost < $1.00
to guarantee profits regardless of the outcome.
"""

import asyncio
import logging
import re
from datetime import datetime
from typing import Optional

import httpx

from .config import load_settings
from .lookup import fetch_market_from_slug
from .trading import get_client, place_order


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
        
        # Get the most recent timestamp (the most current market)
        latest_timestamp = max(int(ts) for ts in matches)
        slug = f"btc-updown-15m-{latest_timestamp}"
        
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
        """Get current USDC balance."""
        from .trading import get_balance
        return get_balance(self.settings)
    
    def get_current_prices(self) -> tuple[Optional[float], Optional[float]]:
        """Get current prices for both sides."""
        try:
            # UP price (YES token)
            up_response = self.client.get_last_trade_price(token_id=self.yes_token_id)
            price_up = float(up_response.get("price", 0))
            
            # DOWN price (NO token)
            down_response = self.client.get_last_trade_price(token_id=self.no_token_id)
            price_down = float(down_response.get("price", 0))
            
            return price_up, price_down
        except Exception as e:
            logger.error(f"Error getting prices: {e}")
            return None, None
    
    def get_order_book(self, token_id: str) -> dict:
        """Get order book for a token."""
        try:
            book = self.client.get_order_book(token_id=token_id)
            # The result is an OrderBookSummary object, not a dict
            bids = book.bids if hasattr(book, 'bids') and book.bids else []
            asks = book.asks if hasattr(book, 'asks') and book.asks else []
            
            best_bid = float(bids[0].price) if bids else None
            best_ask = float(asks[0].price) if asks else None
            spread = (best_ask - best_bid) if (best_bid and best_ask) else None
            
            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "bid_size": float(bids[0].size) if bids else 0,
                "ask_size": float(asks[0].size) if asks else 0
            }
        except Exception as e:
            logger.error(f"Error getting order book: {e}")
            return {}
    
    def check_arbitrage(self) -> Optional[dict]:
        """
        Check if an arbitrage opportunity exists.
        
        Returns dict with information if opportunity exists, None otherwise.
        """
        price_up, price_down = self.get_current_prices()
        
        if price_up is None or price_down is None:
            return None
        
        # Calculate total cost
        total_cost = price_up + price_down
        
        # Check if there's arbitrage (total < 1.0)
        if total_cost < self.settings.target_pair_cost:
            profit = 1.0 - total_cost
            profit_pct = (profit / total_cost) * 100
            
            # Calculate with order size
            investment = total_cost * self.settings.order_size
            expected_payout = 1.0 * self.settings.order_size
            expected_profit = expected_payout - investment
            
            return {
                "price_up": price_up,
                "price_down": price_down,
                "total_cost": total_cost,
                "profit_per_share": profit,
                "profit_pct": profit_pct,
                "order_size": self.settings.order_size,
                "total_investment": investment,
                "expected_payout": expected_payout,
                "expected_profit": expected_profit,
                "timestamp": datetime.now().isoformat()
            }
        
        return None
    
    def execute_arbitrage(self, opportunity: dict):
        """Execute arbitrage by buying both sides."""
        
        logger.info("=" * 70)
        logger.info("🎯 ARBITRAGE OPPORTUNITY DETECTED")
        logger.info("=" * 70)
        logger.info(f"UP price (goes up):   ${opportunity['price_up']:.4f}")
        logger.info(f"DOWN price (goes down): ${opportunity['price_down']:.4f}")
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
            logger.info("=" * 70)
            self.opportunities_found += 1
            # Track simulated investment
            self.total_invested += opportunity['total_investment']
            self.total_shares_bought += opportunity['order_size'] * 2  # UP + DOWN
            self.positions.append(opportunity)
            return
        
        # Check balance before executing (with 20% safety margin)
        logger.info("\nVerifying balance...")
        current_balance = self.get_balance()
        required_balance = opportunity['total_investment'] * 1.2  # 20% safety margin
        
        logger.info(f"Available balance: ${current_balance:.2f}")
        logger.info(f"Required (+ 20% margin): ${required_balance:.2f}")
        
        if current_balance < required_balance:
            logger.error(f"❌ Insufficient balance: need ${required_balance:.2f} but have ${current_balance:.2f}")
            logger.error("20% extra margin required to avoid mid-execution failures")
            logger.error("Arbitrage will not be executed")
            return
        
        try:
            # Execute orders
            logger.info("\n📤 Executing orders...")
            
            # Use exact prices from arbitrage opportunity
            up_price = opportunity['price_up']
            down_price = opportunity['price_down']
            
            # Buy UP (YES token)
            logger.info(f"Buying {self.settings.order_size} shares UP @ ${up_price:.4f}")
            order_up = place_order(
                self.settings,
                side="BUY",
                token_id=self.yes_token_id,
                price=up_price,
                size=self.settings.order_size
            )
            logger.info(f"✅ UP order executed")
            
            # Buy DOWN (NO token)
            logger.info(f"Buying {self.settings.order_size} shares DOWN @ ${down_price:.4f}")
            order_down = place_order(
                self.settings,
                side="BUY",
                token_id=self.no_token_id,
                price=down_price,
                size=self.settings.order_size
            )
            logger.info(f"✅ DOWN order executed")
            
            logger.info("\n" + "=" * 70)
            logger.info("✅ ARBITRAGE EXECUTED SUCCESSFULLY")
            logger.info("=" * 70)
            
            self.trades_executed += 1
            
            # Track real investment
            self.total_invested += opportunity['total_investment']
            self.total_shares_bought += opportunity['order_size'] * 2  # UP + DOWN
            self.positions.append(opportunity)
            
            # Show updated balance
            new_balance = self.get_balance()
            logger.info(f"Updated balance: ${new_balance:.2f}")
            
        except Exception as e:
            logger.error(f"\n❌ Error executing arbitrage: {e}")
            logger.error("❌ Orders were NOT executed - tracking was not updated")
    
    def get_market_result(self) -> Optional[str]:
        """Get which option won the market."""
        try:
            # Get final prices
            price_up, price_down = self.get_current_prices()
            
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
        expected_payout = (self.total_shares_bought / 2) * 1.0  # Each pair pays $1.00
        expected_profit = expected_payout - self.total_invested
        profit_pct = (expected_profit / self.total_invested * 100) if self.total_invested > 0 else 0
        
        logger.info(f"Expected payout at close:        ${expected_payout:.2f}")
        logger.info(f"Expected profit:                 ${expected_profit:.2f} ({profit_pct:.2f}%)")
        logger.info("=" * 70)
    
    def run_once(self) -> bool:
        """Scan once for opportunities."""
        # Check if market closed
        time_remaining = self.get_time_remaining()
        if time_remaining == "CLOSED":
            return False  # Signal to stop the bot
        
        opportunity = self.check_arbitrage()
        
        if opportunity:
            self.execute_arbitrage(opportunity)
            return True
        else:
            price_up, price_down = self.get_current_prices()
            if price_up and price_down:
                total = price_up + price_down
                needed = self.settings.target_pair_cost - total
                logger.info(
                    f"No arbitrage: UP=${price_up:.4f} + DOWN=${price_down:.4f} "
                    f"= ${total:.4f} (needs < ${self.settings.target_pair_cost:.2f}, "
                    f"missing ${-needed:.4f}) [Time remaining: {time_remaining}]"
                )
            return False
    
    async def monitor(self, interval_seconds: int = 30):
        """Continuously monitor for opportunities."""
        logger.info("=" * 70)
        logger.info("🚀 BITCOIN 15MIN ARBITRAGE BOT STARTED")
        logger.info("=" * 70)
        logger.info(f"Market: {self.market_slug}")
        logger.info(f"Time remaining: {self.get_time_remaining()}")
        logger.info(f"Mode: {'🔸 SIMULATION' if self.settings.dry_run else '🔴 REAL TRADING'}")
        logger.info(f"Cost threshold: ${self.settings.target_pair_cost:.2f}")
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
                
                self.run_once()
                
                logger.info(f"Opportunities found: {self.opportunities_found}/{scan_count}")
                if not self.settings.dry_run:
                    logger.info(f"Trades executed: {self.trades_executed}")
                
                logger.info(f"Waiting {interval_seconds}s...\n")
                await asyncio.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("\n" + "=" * 70)
            logger.info("🛑 Bot stopped by user")
            logger.info(f"Total scans: {scan_count}")
            logger.info(f"Opportunities found: {self.opportunities_found}")
            if not self.settings.dry_run:
                logger.info(f"Trades executed: {self.trades_executed}")
            logger.info("=" * 70)


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
        await bot.monitor(interval_seconds=0)
    except Exception as e:
        logger.error(f"❌ Fatal error: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
