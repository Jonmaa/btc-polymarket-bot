import functools
import logging
from typing import Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

from .config import Settings

logger = logging.getLogger(__name__)


_cached_client = None

def get_client(settings: Settings) -> ClobClient:
    global _cached_client
    
    if _cached_client is not None:
        return _cached_client
    
    if not settings.private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY is required for trading")
    
    host = "https://clob.polymarket.com"
    
    # Create client with signature_type=1 for Magic/Email accounts
    _cached_client = ClobClient(
        host, 
        key=settings.private_key.strip(), 
        chain_id=137, 
        signature_type=settings.signature_type, 
        funder=settings.funder.strip() if settings.funder else None
    )
    
    # Derive API credentials - simple method that works
    logger.info("Deriving User API credentials from private key...")
    derived_creds = _cached_client.create_or_derive_api_creds()
    _cached_client.set_api_creds(derived_creds)
    
    logger.info("✅ API credentials configured")
    logger.info(f"   API Key: {derived_creds.api_key}")
    logger.info(f"   Wallet: {_cached_client.get_address()}")
    logger.info(f"   Funder: {settings.funder}")
    
    return _cached_client


def get_balance(settings: Settings) -> float:
    """Get USDC balance from Polymarket account."""
    try:
        client = get_client(settings)
        # Get USDC (COLLATERAL) balance
        params = BalanceAllowanceParams(
            asset_type=AssetType.COLLATERAL,
            signature_type=settings.signature_type
        )
        result = client.get_balance_allowance(params)
        
        if isinstance(result, dict):
            balance_raw = result.get("balance", "0")
            balance_wei = float(balance_raw)
            # USDC has 6 decimals
            balance_usdc = balance_wei / 1_000_000
            return balance_usdc
        
        logger.warning(f"Respuesta inesperada obteniendo balance: {result}")
        return 0.0
    except Exception as e:
        logger.error(f"Error getting balance: {e}")
        return 0.0


def place_order(settings: Settings, *, side: str, token_id: str, price: float, size: float, tif: str = "GTC") -> dict:
    if price <= 0:
        raise ValueError("price must be > 0")
    if size <= 0:
        raise ValueError("size must be > 0")
    if not token_id:
        raise ValueError("token_id is required")

    side_up = side.upper()
    if side_up not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")

    client = get_client(settings)
    
    try:
        # Create order args
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=BUY if side_up == "BUY" else SELL
        )
        
        # DO NOT use PartialCreateOrderOptions(neg_risk=True) - it causes "invalid signature"
        # The client will auto-detect neg_risk from the token_id
        signed_order = client.create_order(order_args)
        
        # Post order as GTC (Good-Til-Cancelled) - stays in book until filled
        return client.post_order(signed_order, OrderType.GTC)
    except Exception as exc:  # pragma: no cover - passthrough from client
        raise RuntimeError(f"place_order failed: {exc}") from exc


def place_orders_fast(settings: Settings, orders: list[dict]) -> list[dict]:
    """
    Place multiple orders as fast as possible.
    
    Strategy: Pre-sign all orders first, then post them in rapid succession.
    This minimizes the time between order submissions.
    
    Args:
        settings: Bot settings
        orders: List of order dicts with keys: side, token_id, price, size
        
    Returns:
        List of order results
    """
    client = get_client(settings)
    
    # Step 1: Pre-sign all orders (this is the slow part)
    signed_orders = []
    for order_params in orders:
        side_up = order_params["side"].upper()
        order_args = OrderArgs(
            token_id=order_params["token_id"],
            price=order_params["price"],
            size=order_params["size"],
            side=BUY if side_up == "BUY" else SELL
        )
        signed_order = client.create_order(order_args)
        signed_orders.append(signed_order)
    
    # Step 2: Post all orders as fast as possible (GTC = stays in book until filled)
    results = []
    for signed_order in signed_orders:
        try:
            result = client.post_order(signed_order, OrderType.GTC)
            results.append(result)
        except Exception as e:
            results.append({"error": str(e)})
    
    return results


def get_positions(settings: Settings, token_ids: list[str] = None) -> dict:
    """
    Get current positions (shares owned) for the user.
    
    Args:
        settings: Bot settings
        token_ids: Optional list of token IDs to filter by
        
    Returns:
        Dictionary with token_id -> position data
    """
    try:
        client = get_client(settings)
        
        # Get all positions for the user
        positions = client.get_positions()
        
        # Filter by token_ids if provided
        result = {}
        for pos in positions:
            token_id = pos.get("asset", {}).get("token_id") or pos.get("token_id")
            if token_id:
                if token_ids is None or token_id in token_ids:
                    size = float(pos.get("size", 0))
                    avg_price = float(pos.get("avg_price", 0))
                    result[token_id] = {
                        "size": size,
                        "avg_price": avg_price,
                        "raw": pos
                    }
        
        return result
    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        return {}
