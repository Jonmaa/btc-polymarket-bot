import functools
import logging
from typing import Optional
import time

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType, OrderArgs, OrderType, PostOrdersArgs
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
        
        tif_up = (tif or "GTC").upper()
        order_type = getattr(OrderType, tif_up, OrderType.GTC)
        return client.post_order(signed_order, order_type)
    except Exception as exc:  # pragma: no cover - passthrough from client
        raise RuntimeError(f"place_order failed: {exc}") from exc



def place_orders_fast(settings: Settings, orders: list[dict], *, order_type: str = "GTC") -> list[dict]:
    """Place multiple orders as fast as possible.

    Strategy: pre-sign all orders first, then submit them together.
    This minimizes the time gap between legs.

    Args:
        settings: Bot settings
        orders: List of order dicts with keys: side, token_id, price, size
        order_type: One of OrderType: FOK, FAK, GTC, GTD

    Returns:
        List of order results.
    """
    client = get_client(settings)

    tif_up = (order_type or "GTC").upper()
    ot = getattr(OrderType, tif_up, OrderType.GTC)

    # Step 1: Pre-sign all orders (this is the slow part)
    signed_orders = []
    for order_params in orders:
        side_up = order_params["side"].upper()
        order_args = OrderArgs(
            token_id=order_params["token_id"],
            price=order_params["price"],
            size=order_params["size"],
            side=BUY if side_up == "BUY" else SELL,
        )
        signed_order = client.create_order(order_args)
        signed_orders.append(signed_order)

    # Step 2: Post all orders in a single request when possible.
    try:
        args = [PostOrdersArgs(order=o, orderType=ot) for o in signed_orders]
        result = client.post_orders(args)
        if isinstance(result, list):
            return result
        return [result]
    except Exception:
        # Fallback to sequential posting if batch fails for any reason.
        results: list[dict] = []
        for signed_order in signed_orders:
            try:
                results.append(client.post_order(signed_order, ot))
            except Exception as exc:
                results.append({"error": str(exc)})
        return results


def extract_order_id(result: dict) -> Optional[str]:
    """Best-effort extraction of an order id from API responses."""
    if not isinstance(result, dict):
        return None
    # Common variants observed across APIs/versions
    for key in ("orderID", "orderId", "order_id", "id"):
        val = result.get(key)
        if val:
            return str(val)
    # Sometimes nested
    for key in ("order", "data", "result"):
        nested = result.get(key)
        if isinstance(nested, dict):
            oid = extract_order_id(nested)
            if oid:
                return oid
    return None


def get_order(settings: Settings, order_id: str) -> dict:
    client = get_client(settings)
    return client.get_order(order_id)


def cancel_orders(settings: Settings, order_ids: list[str]) -> Optional[dict]:
    if not order_ids:
        return None
    client = get_client(settings)
    return client.cancel_orders(order_ids)


def _coerce_float(val) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except Exception:
        return None


def summarize_order_state(order_data: dict, *, requested_size: Optional[float] = None) -> dict:
    """Normalize an order payload into a small, stable summary.

    The API field names vary by version; this function is defensive.
    """
    if not isinstance(order_data, dict):
        return {"status": None, "filled_size": None, "requested_size": requested_size, "raw": order_data}

    status = order_data.get("status") or order_data.get("state") or order_data.get("order_status")
    status_str = str(status).lower() if status is not None else None

    filled_size = None
    for key in ("filled_size", "filledSize", "size_filled", "sizeFilled", "matched_size", "matchedSize"):
        if key in order_data:
            filled_size = _coerce_float(order_data.get(key))
            break

    # Some payloads provide remaining size rather than filled size
    remaining_size = None
    for key in ("remaining_size", "remainingSize", "size_remaining", "sizeRemaining"):
        if key in order_data:
            remaining_size = _coerce_float(order_data.get(key))
            break

    original_size = None
    for key in ("original_size", "originalSize", "size", "order_size", "orderSize"):
        if key in order_data:
            original_size = _coerce_float(order_data.get(key))
            break

    if filled_size is None and remaining_size is not None and original_size is not None:
        filled_size = max(0.0, original_size - remaining_size)

    return {
        "status": status_str,
        "filled_size": filled_size,
        "remaining_size": remaining_size,
        "original_size": original_size,
        "requested_size": requested_size,
        "raw": order_data,
    }


def wait_for_terminal_order(
    settings: Settings,
    order_id: str,
    *,
    requested_size: Optional[float] = None,
    timeout_seconds: float = 3.0,
    poll_interval_seconds: float = 0.25,
) -> dict:
    """Poll order state until it is terminal, filled, or timeout."""
    terminal_statuses = {"filled", "canceled", "cancelled", "rejected", "expired"}
    start = time.monotonic()
    last_summary: Optional[dict] = None

    while (time.monotonic() - start) < timeout_seconds:
        try:
            od = get_order(settings, order_id)
            last_summary = summarize_order_state(od, requested_size=requested_size)
        except Exception as exc:
            last_summary = {"status": "error", "error": str(exc), "filled_size": None, "requested_size": requested_size}

        status = (last_summary.get("status") or "").lower() if isinstance(last_summary, dict) else ""
        filled = last_summary.get("filled_size") if isinstance(last_summary, dict) else None

        if requested_size is not None and filled is not None and filled + 1e-9 >= float(requested_size):
            last_summary["terminal"] = True
            last_summary["filled"] = True
            return last_summary

        if status in terminal_statuses:
            last_summary["terminal"] = True
            last_summary["filled"] = (status == "filled")
            return last_summary

        time.sleep(poll_interval_seconds)

    if last_summary is None:
        last_summary = {"status": None, "filled_size": None, "requested_size": requested_size}
    last_summary["terminal"] = False
    last_summary.setdefault("filled", False)
    return last_summary


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
