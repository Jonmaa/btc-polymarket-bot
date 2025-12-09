"""
Bot de arbitraje simple para Bitcoin 15min siguiendo la estrategia de Jeremy Whittaker.

Estrategia: Comprar ambos lados (UP y DOWN) cuando el costo total < $1.00
para garantizar ganancias independientemente del resultado.
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

# Deshabilitar logs HTTP de httpx
logging.getLogger("httpx").setLevel(logging.WARNING)


def find_current_btc_15min_market() -> str:
    """
    Busca el mercado activo de BTC 15min actual en Polymarket.
    
    Busca mercados que coincidan con el patrón 'btc-updown-15m-<timestamp>'
    y devuelve el slug del mercado más reciente/activo.
    """
    logger.info("Buscando mercado BTC 15min actual...")
    
    try:
        # Buscar en la página de crypto 15min de Polymarket
        page_url = "https://polymarket.com/crypto/15M"
        resp = httpx.get(page_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        resp.raise_for_status()
        
        # Buscar el slug del mercado BTC en el HTML
        pattern = r'btc-updown-15m-(\d+)'
        matches = re.findall(pattern, resp.text)
        
        if not matches:
            raise RuntimeError("No se encontró ningún mercado BTC 15min activo")
        
        # Obtener el timestamp más reciente (el mercado más actual)
        latest_timestamp = max(int(ts) for ts in matches)
        slug = f"btc-updown-15m-{latest_timestamp}"
        
        logger.info(f"✅ Mercado encontrado: {slug}")
        return slug
        
    except Exception as e:
        logger.error(f"Error buscando mercado BTC 15min: {e}")
        # Fallback: intentar con el último conocido
        logger.warning("Usando mercado por defecto de configuración...")
        raise


class SimpleArbitrageBot:
    """Bot simple que implementa la estrategia de Jeremy Whittaker."""
    
    def __init__(self, settings):
        self.settings = settings
        self.client = get_client(settings)
        
        # Intentar buscar mercado BTC 15min actual automáticamente
        try:
            market_slug = find_current_btc_15min_market()
        except Exception as e:
            # Fallback: usar el slug configurado en .env
            if settings.market_slug:
                logger.info(f"Usando mercado configurado: {settings.market_slug}")
                market_slug = settings.market_slug
            else:
                raise RuntimeError("No se pudo encontrar mercado BTC 15min y no hay slug configurado en .env")
        
        # Obtener token IDs del mercado
        logger.info(f"Obteniendo información del mercado: {market_slug}")
        market_info = fetch_market_from_slug(market_slug)
        
        self.market_id = market_info["market_id"]
        self.yes_token_id = market_info["yes_token_id"]
        self.no_token_id = market_info["no_token_id"]
        
        logger.info(f"Market ID: {self.market_id}")
        logger.info(f"UP Token (YES): {self.yes_token_id}")
        logger.info(f"DOWN Token (NO): {self.no_token_id}")
        
        # Extraer timestamp del mercado para calcular tiempo restante
        # El timestamp en el slug es cuando ABRE, no cuando cierra
        # Los mercados 15min cierran 15 minutos (900 segundos) después
        import re
        match = re.search(r'btc-updown-15m-(\d+)', market_slug)
        market_start = int(match.group(1)) if match else None
        self.market_end_timestamp = market_start + 900 if market_start else None  # +15 min
        self.market_slug = market_slug
        
        self.last_check = None
        self.opportunities_found = 0
        self.trades_executed = 0
        
        # Tracking de inversión
        self.total_invested = 0.0
        self.total_shares_bought = 0
        self.positions = []  # Lista de posiciones abiertas
    
    def get_time_remaining(self) -> str:
        """Obtener tiempo restante hasta que cierre el mercado."""
        if not self.market_end_timestamp:
            return "Desconocido"
        
        from datetime import datetime
        now = int(datetime.now().timestamp())
        remaining = self.market_end_timestamp - now
        
        if remaining <= 0:
            return "CERRADO"
        
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        return f"{minutes}m {seconds}s"
    
    def get_balance(self) -> float:
        """Get current USDC balance."""
        from .trading import get_balance
        return get_balance(self.settings)
    
    def get_current_prices(self) -> tuple[Optional[float], Optional[float]]:
        """Obtener precios actuales de ambos lados."""
        try:
            # Precio UP (token YES)
            up_response = self.client.get_last_trade_price(token_id=self.yes_token_id)
            price_up = float(up_response.get("price", 0))
            
            # Precio DOWN (token NO)
            down_response = self.client.get_last_trade_price(token_id=self.no_token_id)
            price_down = float(down_response.get("price", 0))
            
            return price_up, price_down
        except Exception as e:
            logger.error(f"Error obteniendo precios: {e}")
            return None, None
    
    def get_order_book(self, token_id: str) -> dict:
        """Obtener order book para un token."""
        try:
            book = self.client.get_order_book(token_id=token_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            
            best_bid = float(bids[0]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            spread = (best_ask - best_bid) if (best_bid and best_ask) else None
            
            return {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
                "bid_size": float(bids[0]["size"]) if bids else 0,
                "ask_size": float(asks[0]["size"]) if asks else 0
            }
        except Exception as e:
            logger.error(f"Error obteniendo order book: {e}")
            return {}
    
    def check_arbitrage(self) -> Optional[dict]:
        """
        Verificar si existe oportunidad de arbitraje.
        
        Retorna dict con información si hay oportunidad, None si no.
        """
        price_up, price_down = self.get_current_prices()
        
        if price_up is None or price_down is None:
            return None
        
        # Calcular costo total
        total_cost = price_up + price_down
        
        # Verificar si hay arbitraje (total < 1.0)
        if total_cost < self.settings.target_pair_cost:
            profit = 1.0 - total_cost
            profit_pct = (profit / total_cost) * 100
            
            # Calcular con el tamaño de orden
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
        """Ejecutar el arbitraje comprando ambos lados."""
        
        logger.info("=" * 70)
        logger.info("🎯 OPORTUNIDAD DE ARBITRAJE DETECTADA")
        logger.info("=" * 70)
        logger.info(f"Precio UP (sube):     ${opportunity['price_up']:.4f}")
        logger.info(f"Precio DOWN (baja):   ${opportunity['price_down']:.4f}")
        logger.info(f"Costo total:          ${opportunity['total_cost']:.4f}")
        logger.info(f"Ganancia por share:   ${opportunity['profit_per_share']:.4f}")
        logger.info(f"Ganancia %:           {opportunity['profit_pct']:.2f}%")
        logger.info("-" * 70)
        logger.info(f"Tamaño de orden:      {opportunity['order_size']} shares cada lado")
        logger.info(f"Inversión total:      ${opportunity['total_investment']:.2f}")
        logger.info(f"Pago esperado:        ${opportunity['expected_payout']:.2f}")
        logger.info(f"GANANCIA ESPERADA:    ${opportunity['expected_profit']:.2f}")
        logger.info("=" * 70)
        
        if self.settings.dry_run:
            logger.info("🔸 MODO SIMULACIÓN - No se ejecutarán órdenes reales")
            logger.info("=" * 70)
            self.opportunities_found += 1
            # Trackear inversión simulada
            self.total_invested += opportunity['total_investment']
            self.total_shares_bought += opportunity['order_size'] * 2  # UP + DOWN
            self.positions.append(opportunity)
            return
        
        # Verificar balance antes de ejecutar (con 20% margen de seguridad)
        logger.info("\nVerificando balance...")
        current_balance = self.get_balance()
        required_balance = opportunity['total_investment'] * 1.2  # 20% safety margin
        
        logger.info(f"Balance disponible: ${current_balance:.2f}")
        logger.info(f"Requerido (+ 20% margen): ${required_balance:.2f}")
        
        if current_balance < required_balance:
            logger.error(f"❌ Balance insuficiente: necesitas ${required_balance:.2f} pero tienes ${current_balance:.2f}")
            logger.error("Se requiere 20% extra como margen de seguridad para evitar fallos a mitad de ejecución")
            logger.error("No se ejecutará el arbitraje")
            return
        
        # Verificar spreads antes de ejecutar
        logger.info("\nVerificando spreads y liquidez...")
        up_book = self.get_order_book(self.yes_token_id)
        down_book = self.get_order_book(self.no_token_id)
        
        logger.info(f"Spread UP:   ${up_book.get('spread', 0):.4f} (liquidez: {up_book.get('ask_size', 0):.0f})")
        logger.info(f"Spread DOWN: ${down_book.get('spread', 0):.4f} (liquidez: {down_book.get('ask_size', 0):.0f})")
        
        # Verificar si hay suficiente liquidez
        if up_book.get('ask_size', 0) < self.settings.order_size:
            logger.warning(f"⚠️ Liquidez insuficiente en UP (disponible: {up_book.get('ask_size', 0)})")
        
        if down_book.get('ask_size', 0) < self.settings.order_size:
            logger.warning(f"⚠️ Liquidez insuficiente en DOWN (disponible: {down_book.get('ask_size', 0)})")
        
        try:
            # Ejecutar órdenes
            logger.info("\n📤 Ejecutando órdenes...")
            
            # Comprar UP (token YES)
            logger.info(f"Comprando {self.settings.order_size} shares UP @ ${opportunity['price_up']:.4f}")
            order_up = place_order(
                self.settings,
                side="BUY",
                token_id=self.yes_token_id,
                price=opportunity['price_up'],
                size=self.settings.order_size
            )
            logger.info(f"✅ Orden UP ejecutada")
            
            # Comprar DOWN (token NO)
            logger.info(f"Comprando {self.settings.order_size} shares DOWN @ ${opportunity['price_down']:.4f}")
            order_down = place_order(
                self.settings,
                side="BUY",
                token_id=self.no_token_id,
                price=opportunity['price_down'],
                size=self.settings.order_size
            )
            logger.info(f"✅ Orden DOWN ejecutada")
            
            logger.info("\n" + "=" * 70)
            logger.info("✅ ARBITRAJE EJECUTADO EXITOSAMENTE")
            logger.info("=" * 70)
            
            self.trades_executed += 1
            
            # Trackear inversión real
            self.total_invested += opportunity['total_investment']
            self.total_shares_bought += opportunity['order_size'] * 2  # UP + DOWN
            self.positions.append(opportunity)
            
            # Mostrar balance actualizado
            new_balance = self.get_balance()
            logger.info(f"Balance actualizado: ${new_balance:.2f}")
            
        except Exception as e:
            logger.error(f"\n❌ Error ejecutando arbitraje: {e}")
            logger.error("❌ Las órdenes NO se ejecutaron - el tracking no se actualizó")
    
    def get_market_result(self) -> Optional[str]:
        """Obtener qué opción ganó el mercado."""
        try:
            # Obtener precios finales
            price_up, price_down = self.get_current_prices()
            
            if price_up is None or price_down is None:
                return None
            
            # En mercados cerrados, el ganador tiene precio 1.0 y el perdedor 0.0
            if price_up >= 0.99:
                return "UP (sube) 📈"
            elif price_down >= 0.99:
                return "DOWN (baja) 📉"
            else:
                # Mercado aún no resuelto, ver cuál tiene mayor probabilidad
                if price_up > price_down:
                    return f"UP liderando ({price_up:.2%})"
                else:
                    return f"DOWN liderando ({price_down:.2%})"
        except Exception as e:
            logger.error(f"Error obteniendo resultado: {e}")
            return None
    
    def show_final_summary(self):
        """Mostrar resumen final al cerrar el mercado."""
        logger.info("\n" + "=" * 70)
        logger.info("🏁 MERCADO CERRADO - RESUMEN FINAL")
        logger.info("=" * 70)
        logger.info(f"Mercado: {self.market_slug}")
        
        # Obtener resultado del mercado
        result = self.get_market_result()
        if result:
            logger.info(f"Resultado: {result}")
        
        logger.info(f"Modo: {'🔸 SIMULACIÓN' if self.settings.dry_run else '🔴 TRADING REAL'}")
        logger.info("-" * 70)
        logger.info(f"Total oportunidades detectadas:  {self.opportunities_found}")
        logger.info(f"Total trades ejecutados:         {self.trades_executed if not self.settings.dry_run else self.opportunities_found}")
        logger.info(f"Total shares compradas:          {self.total_shares_bought}")
        logger.info("-" * 70)
        logger.info(f"Total invertido:                 ${self.total_invested:.2f}")
        
        # Calcular ganancia esperada
        expected_payout = (self.total_shares_bought / 2) * 1.0  # Cada par paga $1.00
        expected_profit = expected_payout - self.total_invested
        profit_pct = (expected_profit / self.total_invested * 100) if self.total_invested > 0 else 0
        
        logger.info(f"Pago esperado al cierre:         ${expected_payout:.2f}")
        logger.info(f"Ganancia esperada:               ${expected_profit:.2f} ({profit_pct:.2f}%)")
        logger.info("=" * 70)
    
    def run_once(self) -> bool:
        """Escanear una vez por oportunidades."""
        # Verificar si el mercado cerró
        time_remaining = self.get_time_remaining()
        if time_remaining == "CERRADO":
            return False  # Señal para detener el bot
        
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
                    f"No hay arbitraje: UP=${price_up:.4f} + DOWN=${price_down:.4f} "
                    f"= ${total:.4f} (necesita < ${self.settings.target_pair_cost:.2f}, "
                    f"falta ${-needed:.4f}) [Tiempo restante: {time_remaining}]"
                )
            return False
    
    async def monitor(self, interval_seconds: int = 30):
        """Monitorear continuamente por oportunidades."""
        logger.info("=" * 70)
        logger.info("🚀 BOT DE ARBITRAJE BITCOIN 15MIN INICIADO")
        logger.info("=" * 70)
        logger.info(f"Mercado: {self.market_slug}")
        logger.info(f"Tiempo restante: {self.get_time_remaining()}")
        logger.info(f"Modo: {'🔸 SIMULACIÓN' if self.settings.dry_run else '🔴 TRADING REAL'}")
        logger.info(f"Umbral de costo: ${self.settings.target_pair_cost:.2f}")
        logger.info(f"Tamaño de orden: {self.settings.order_size} shares")
        logger.info(f"Intervalo: {interval_seconds}s")
        logger.info("=" * 70)
        logger.info("")
        
        scan_count = 0
        
        try:
            while True:
                scan_count += 1
                logger.info(f"\n[Escaneo #{scan_count}] {datetime.now().strftime('%H:%M:%S')}")
                
                # Verificar si el mercado cerró
                if self.get_time_remaining() == "CERRADO":
                    logger.info("\n🚨 El mercado ha cerrado!")
                    self.show_final_summary()
                    
                    # Buscar el siguiente mercado
                    logger.info("\n🔄 Buscando siguiente mercado BTC 15min...")
                    try:
                        new_market_slug = find_current_btc_15min_market()
                        if new_market_slug != self.market_slug:
                            logger.info(f"✅ Nuevo mercado encontrado: {new_market_slug}")
                            logger.info("Reiniciando bot con nuevo mercado...")
                            # Reiniciar el bot con el nuevo mercado
                            self.__init__(self.settings)
                            scan_count = 0
                            continue
                        else:
                            logger.info("⏳ Esperando nuevo mercado... (30s)")
                            await asyncio.sleep(30)
                            continue
                    except Exception as e:
                        logger.error(f"Error buscando nuevo mercado: {e}")
                        logger.info("Reintentando en 30 segundos...")
                        await asyncio.sleep(30)
                        continue
                
                self.run_once()
                
                logger.info(f"Oportunidades encontradas: {self.opportunities_found}/{scan_count}")
                if not self.settings.dry_run:
                    logger.info(f"Trades ejecutados: {self.trades_executed}")
                
                logger.info(f"Esperando {interval_seconds}s...\n")
                await asyncio.sleep(interval_seconds)
                
        except KeyboardInterrupt:
            logger.info("\n" + "=" * 70)
            logger.info("🛑 Bot detenido por usuario")
            logger.info(f"Total escaneos: {scan_count}")
            logger.info(f"Oportunidades encontradas: {self.opportunities_found}")
            if not self.settings.dry_run:
                logger.info(f"Trades ejecutados: {self.trades_executed}")
            logger.info("=" * 70)


async def main():
    """Punto de entrada principal."""
    
    # Cargar configuración
    settings = load_settings()
    
    # Validar configuración
    if not settings.private_key:
        logger.error("❌ Error: POLYMARKET_PRIVATE_KEY no configurado en .env")
        return
    
    # Crear y ejecutar bot
    try:
        bot = SimpleArbitrageBot(settings)
        await bot.monitor(interval_seconds=0)
    except Exception as e:
        logger.error(f"❌ Error fatal: {e}", exc_info=True)


if __name__ == "__main__":
    asyncio.run(main())
