import time
import uuid
from typing import Optional

from kronos_bot.core.logger import get_logger
from kronos_bot.execution.trade_context import TradeLeg
from kronos_bot.config.settings import EXIT_LIMIT_GRACE_PERIOD_SEC, DEFAULT_PRODUCT_TYPE, NIFTY_LOT_SIZE, SENSEX_LOT_SIZE

logger = get_logger("order_lifecycle")

class OrderLifecycle:
    def execute_entry(self, leg: TradeLeg) -> bool:
        raise NotImplementedError

    def execute_exit(self, leg: TradeLeg, reason: str, exit_price: Optional[float] = None) -> bool:
        raise NotImplementedError

class PaperOrderLifecycle(OrderLifecycle):
    def execute_entry(self, leg: TradeLeg) -> bool:
        leg.status = "OPEN"
        leg.paper_order_id = f"PAPER_{uuid.uuid4().hex[:8].upper()}"
        logger.info(f"[PaperOrderLifecycle] Entry executed for leg {leg.leg_id}. Paper Order ID: {leg.paper_order_id}")
        return True

    def execute_exit(self, leg: TradeLeg, reason: str, exit_price: Optional[float] = None) -> bool:
        if leg.status != "OPEN":
            logger.warning(f"[PaperOrderLifecycle] Attempted to exit leg {leg.leg_id} which is not OPEN.")
            return False
        leg.close_leg(exit_price or leg.current_price, reason)
        logger.info(f"[PaperOrderLifecycle] Exit executed for leg {leg.leg_id} at {leg.exit_price}. Reason: {reason}")
        return True

def get_marketable_entry_limit_price(best_ask: float, tick_size: float = 0.05) -> float:
    if best_ask <= 0:
        return 0.0
    buffer = max(3.0, best_ask * 0.025)
    raw_price = best_ask + buffer
    return round(round(raw_price / tick_size) * tick_size, 2)

def get_marketable_exit_limit_price(best_bid: float, tick_size: float = 0.05) -> float:
    if best_bid <= 0:
        return 0.0
    buffer = max(3.0, best_bid * 0.025)
    raw_price = max(0.05, best_bid - buffer)
    return round(round(raw_price / tick_size) * tick_size, 2)

class ShoonyaOrderLifecycle(OrderLifecycle):
    def __init__(self, broker_gateway, grace_period_sec: float = EXIT_LIMIT_GRACE_PERIOD_SEC):
        self.broker = broker_gateway
        self.grace_period_sec = grace_period_sec
        self.product_type = "N"

    def _build_kwargs(self, action: str, exch: str, symbol: str, qty: str, limit_price: str):
        """Dynamically builds kwargs by reading the installed NorenApi SDK signature."""
        import inspect
        kwargs = {}
        try:
            api_obj = self.broker._api if hasattr(self.broker, "_api") else self.broker
            sig = inspect.signature(api_obj.place_order)
            
            # Master map of all possible parameter names Shoonya might use
            mapping = {
                "buy_or_sell": action, "buy_sell": action, "transaction_type": action, "trantype": action, "side": action,
                "product_type": self.product_type, "prd": self.product_type,
                "exchange": exch, "exch": exch,
                "tradingsymbol": symbol, "tsym": symbol, "symbol": symbol,
                "quantity": qty, "qty": qty,
                "price_type": "LMT", "prctype": "LMT", "ordertype": "LMT",
                "price": limit_price, "prc": limit_price,
                "discloseqty": 0, "disclose_qty": 0,
                "retention": "DAY", "ret": "DAY"
            }
            
            for p_name in sig.parameters.keys():
                if p_name.lower() in mapping:
                    kwargs[p_name] = mapping[p_name.lower()]
                    
            kwargs.pop("self", None)
            kwargs.pop("args", None)
            kwargs.pop("kwargs", None)
            
            if not kwargs:
                raise ValueError("Inspection returned empty parameters.")
                
        except Exception as e:
            logger.warning(f"[ShoonyaOrderLifecycle] API Inspect Failed: {e}. Using fallback kwargs.")
            kwargs = {
                "buy_or_sell": action,
                "product_type": self.product_type,
                "exchange": exch,
                "tradingsymbol": symbol,
                "quantity": qty,
                "price_type": "LMT",
                "price": limit_price
            }
        return kwargs

    def execute_entry(self, leg: TradeLeg) -> bool:
        try:
            exch_seg = getattr(leg, 'exchange', 'BFO' if 'SENSEX' in leg.symbol else 'NFO')
            qty = leg.quantity * (SENSEX_LOT_SIZE if "SENSEX" in leg.symbol else NIFTY_LOT_SIZE)
            best_ask = leg.current_price
            
            try:
                if hasattr(self.broker, "market_data_provider"):
                    quote = self.broker.market_data_provider.get_quote(exch_seg, str(leg.token))
                    if quote and quote.get("ask", 0) > 0:
                        best_ask = float(quote["ask"])
            except Exception:
                pass

            limit_price = get_marketable_entry_limit_price(best_ask)
            formatted_price = f"{limit_price:.2f}"
            
            # Dynamic Reflection
            kwargs = self._build_kwargs("B", exch_seg, leg.symbol, str(qty), formatted_price)
            logger.info(f"[ShoonyaOrderLifecycle] Placing Limit Entry Order using dynamic SDK params: {list(kwargs.keys())}...")
            
            resp = self.broker.place_order(**kwargs)
            
            if resp and resp.get("stat") == "Ok" and resp.get("norenordno"):
                ordno = resp["norenordno"]
                leg.broker_order_id = ordno
                leg.status = "OPEN"
                logger.info(f"[ShoonyaOrderLifecycle] ✅ Entry Marketable Limit Order Placed! Order ID: {ordno}")
                return True
            else:
                err_msg = resp.get("emsg", "Unknown Broker Error") if isinstance(resp, dict) else str(resp)
                logger.error(f"[ShoonyaOrderLifecycle] 🚨 SHOONYA API REJECTION REASON: {err_msg}")
                return False
        except Exception as e:
            logger.error(f"[ShoonyaOrderLifecycle] Exception in execute_entry: {e}")
            return False

    def execute_exit(self, leg: TradeLeg, reason: str, exit_price: Optional[float] = None) -> bool:
        if leg.status != "OPEN":
            logger.warning(f"[ShoonyaOrderLifecycle] Leg {leg.symbol} is not OPEN. Skipping exit.")
            return False

        exch_seg = getattr(leg, 'exchange', 'BFO' if 'SENSEX' in leg.symbol else 'NFO')
        qty = leg.quantity * (SENSEX_LOT_SIZE if "SENSEX" in leg.symbol else NIFTY_LOT_SIZE)
        token = str(leg.token)
        best_bid = leg.current_price
        
        try:
            if hasattr(self.broker, "market_data_provider"):
                quote = self.broker.market_data_provider.get_quote(exch_seg, token)
                if quote and quote.get("bid", 0) > 0:
                    best_bid = float(quote["bid"])
        except Exception:
            pass

        limit_price = get_marketable_exit_limit_price(best_bid)
        formatted_price = f"{limit_price:.2f}"

        # Dynamic Reflection
        kwargs = self._build_kwargs("S", exch_seg, leg.symbol, str(qty), formatted_price)
        logger.info(f"[ShoonyaOrderLifecycle] Initiating Limit Exit with dynamic SDK params: {list(kwargs.keys())}...")

        try:
            resp = self.broker.place_order(**kwargs)
            if resp and resp.get("stat") == "Ok" and resp.get("norenordno"):
                ordno = resp["norenordno"]
                logger.info(f"[ShoonyaOrderLifecycle] ✅ Marketable LMT Exit Order Placed! Order ID: {ordno}")
                leg.close_leg(exit_price or limit_price, reason)
                return True
            else:
                err_msg = resp.get("emsg", "Unknown Broker Error") if isinstance(resp, dict) else str(resp)
                logger.error(f"[ShoonyaOrderLifecycle] 🚨 SHOONYA API EXIT REJECTION REASON: {err_msg}")
                leg.close_leg(exit_price or leg.current_price, f"{reason} (LMT Exit Warning)")
                return False
        except Exception as e:
            logger.error(f"[ShoonyaOrderLifecycle] Exception in execute_exit: {e}")
            leg.close_leg(exit_price or leg.current_price, f"{reason} (Local Exit)")
            return False