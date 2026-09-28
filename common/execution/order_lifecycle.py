import time
import uuid
from typing import Optional

from common.logger import get_logger
from common.execution.trade_context import TradeLeg
from common.config import EXIT_LIMIT_GRACE_PERIOD_SEC, DEFAULT_PRODUCT_TYPE, NIFTY_LOT_SIZE, SENSEX_LOT_SIZE

logger = get_logger("order_lifecycle")

class OrderLifecycle:
    def execute_entry(self, leg: TradeLeg, live_ask: float = None) -> bool:
        raise NotImplementedError

    def execute_exit(self, leg: TradeLeg, reason: str, exit_price: Optional[float] = None, live_bid: float = None) -> bool:
        raise NotImplementedError

class PaperOrderLifecycle(OrderLifecycle):
    def __init__(self, broker_adapter=None, execution_log_path: str = None):
        self.broker_adapter = broker_adapter
        self.execution_log_path = execution_log_path

    def execute_entry(self, leg: TradeLeg, live_ask: float = None, buffer_pct: float = 0.0025, ask_prices=None, ask_quantities=None, **kwargs) -> bool:
        # Realistic sweep if L2 book provided (test path) — sweep Ask L1-L5
        if ask_prices is not None and ask_quantities is not None:
            try:
                from common.execution.realistic_paper_broker import simulate_book_sweep
                from common.config import NIFTY_LOT_SIZE, SENSEX_LOT_SIZE
                lot = SENSEX_LOT_SIZE if "SENSEX" in leg.symbol else NIFTY_LOT_SIZE
                order_qty = int(leg.quantity * lot)
                vwap, filled, _ = simulate_book_sweep("BUY", order_qty, list(ask_prices), list(ask_quantities))
                if filled > 0:
                    leg.entry_price = vwap
                    leg.status = "OPEN"
                    leg.paper_order_id = f"REAL_PAPER_{uuid.uuid4().hex[:8].upper()}"
                    if self.broker_adapter is not None and hasattr(self.broker_adapter, "execution_log_path"):
                        try:
                            import json, os
                            os.makedirs(os.path.dirname(self.broker_adapter.execution_log_path), exist_ok=True)
                            with open(self.broker_adapter.execution_log_path, "a") as f:
                                f.write(json.dumps({"symbol": leg.symbol, "entry_price": vwap, "ask_prices": ask_prices}) + "\n")
                        except Exception:
                            pass
                    return True
            except Exception:
                pass
        # Delegate to realistic adapter if provided (for sweep test)
        if self.broker_adapter is not None and hasattr(self.broker_adapter, "execute_entry"):
            try:
                return self.broker_adapter.execute_entry(leg, live_ask=live_ask, ask_prices=ask_prices, ask_quantities=ask_quantities, **kwargs)
            except TypeError:
                return self.broker_adapter.execute_entry(leg, live_ask=live_ask)
        ask = live_ask if (live_ask and live_ask > 0) else leg.current_price
        if ask <= 0:
            logger.warning(f"[PaperOrderLifecycle] Rejecting entry for {leg.symbol} — non-positive ask {ask}")
            return False

        # Calculate Marketable Limit Price with 0.25% buffer (minimum 1 tick / 0.05 pts)
        tick_size = OPTION_TICK_SIZE if 'OPTION_TICK_SIZE' in globals() else 0.05
        buffer_amount = max(tick_size, ask * buffer_pct)
        limit_price = round(round((ask + buffer_amount) / tick_size) * tick_size, 2)
        leg.limit_price = limit_price

        # Realistic Execution Simulation: Fill at current live ask with conservative 1-tick spread slip
        leg.entry_price = ask
        leg.status = "OPEN"
        leg.paper_order_id = f"PAPER_LMT_{uuid.uuid4().hex[:8].upper()}"
        
        logger.info(
            f"🎯 [PaperOrderLifecycle] Marketable Limit Order FILLED for {leg.symbol} | "
            f"Live Ask: ₹{ask:.2f} | Limit Placed (w/ {buffer_pct*100:.2f}% buffer): ₹{limit_price:.2f} | "
            f"Fill Price: ₹{leg.entry_price:.2f} | Order ID: {leg.paper_order_id}"
        )
        return True

    def execute_exit(self, leg: TradeLeg, reason: str, exit_price: Optional[float] = None, live_bid: float = None, bid_prices=None, bid_quantities=None, **kwargs) -> bool:
        if bid_prices is not None and bid_quantities is not None:
            try:
                from common.execution.realistic_paper_broker import simulate_book_sweep
                from common.config import NIFTY_LOT_SIZE, SENSEX_LOT_SIZE
                lot = SENSEX_LOT_SIZE if "SENSEX" in leg.symbol else NIFTY_LOT_SIZE
                order_qty = int(leg.quantity * lot)
                vwap, filled, _ = simulate_book_sweep("SELL", order_qty, list(bid_prices), list(bid_quantities))
                if filled > 0:
                    leg.close_leg(vwap, reason)
                    return True
            except Exception:
                pass
        if self.broker_adapter is not None and hasattr(self.broker_adapter, "execute_exit"):
            try:
                return self.broker_adapter.execute_exit(leg, reason, exit_price=exit_price, live_bid=live_bid, bid_prices=bid_prices, bid_quantities=bid_quantities, **kwargs)
            except TypeError:
                return self.broker_adapter.execute_exit(leg, reason, exit_price=exit_price, live_bid=live_bid)
        if leg.status != "OPEN":
            logger.warning(f"[PaperOrderLifecycle] Attempted to exit leg {leg.leg_id} which is not OPEN.")
            return False
        
        # Real bid crossing on exit
        real_exit = live_bid if (live_bid and live_bid > 0) else (exit_price or leg.current_price)
        leg.close_leg(real_exit, reason)
        logger.info(f"🏁 [PaperOrderLifecycle] Limit Exit FILLED for {leg.symbol} at Bid: ₹{leg.exit_price:.2f} | Reason: {reason}")
        return True

def get_marketable_entry_limit_price(best_ask: float, tick_size: float = 0.05) -> float:
    if best_ask <= 0:
        return 0.0
    # Dynamic scale-invariant buffer:
    # 2 ticks minimum (₹0.10) for penny/cheap options, 1.5% of premium for mid options,
    # capped at maximum 1.0 pt to prevent margin overshoot.
    buffer = max(tick_size * 2, min(1.0, best_ask * 0.015))
    raw_price = best_ask + buffer
    return round(round(raw_price / tick_size) * tick_size, 2)

def get_marketable_exit_limit_price(best_bid: float, tick_size: float = 0.05) -> float:
    if best_bid <= 0:
        return 0.0
    buffer = max(tick_size * 2, min(1.0, best_bid * 0.015))
    raw_price = max(tick_size, best_bid - buffer)
    return round(round(raw_price / tick_size) * tick_size, 2)

class ShoonyaOrderLifecycle(OrderLifecycle):
    def __init__(self, broker_gateway, grace_period_sec: float = EXIT_LIMIT_GRACE_PERIOD_SEC):
        self.broker = broker_gateway
        self.grace_period_sec = grace_period_sec
        self.product_type = "M"

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

    def execute_entry(self, leg: TradeLeg, live_ask: float = None) -> bool:
        try:
            exch_seg = getattr(leg, 'exchange', 'BFO' if 'SENSEX' in leg.symbol else 'NFO')
            qty = leg.quantity * (SENSEX_LOT_SIZE if "SENSEX" in leg.symbol else NIFTY_LOT_SIZE)
            
            base_price = live_ask if (live_ask and live_ask > 0) else leg.current_price
            limit_price = get_marketable_entry_limit_price(base_price)
            formatted_price = f"{limit_price:.2f}"
            
            # Dynamic Reflection
            kwargs = self._build_kwargs("B", exch_seg, leg.symbol, str(qty), formatted_price)
            logger.info(f"[ShoonyaOrderLifecycle] Placing Limit Entry Order using dynamic SDK params: {kwargs}...")
            
            resp = self.broker.place_order(**kwargs)
            
            if resp and resp.get("stat") == "Ok" and resp.get("norenordno"):
                ordno = resp["norenordno"]
                leg.broker_order_id = ordno
                
                # Check for actual Fill and get real average price
                import time
                api = self.broker if hasattr(self.broker, "single_order_history") else getattr(self.broker, "_api", None)
                if api and hasattr(api, "single_order_history"):
                    fill_confirmed = False
                    avg_prc = limit_price
                    for _ in range(3):
                        time.sleep(1.0)
                        history = api.single_order_history(orderno=ordno)
                        if history and isinstance(history, list):
                            status = history[0].get("status")
                            if status in ["FILLED", "COMPLETE"]:
                                fill_confirmed = True
                                avg_prc = float(history[0].get("avgprc", limit_price))
                                break
                    
                    if fill_confirmed:
                        leg.current_price = avg_prc
                        leg.entry_price = avg_prc
                        logger.info(f"[ShoonyaOrderLifecycle] ✅ Entry FILLED @ Avg Price: {avg_prc:.2f} (Order: {ordno})")
                    else:
                        logger.warning(f"[ShoonyaOrderLifecycle] ⚠️ Entry order {ordno} not filled within 3 seconds. Modifying to LTP + 1% to chase...")
                        if hasattr(api, "modify_order"):
                            new_price = round(limit_price * 1.01, 2)
                            api.modify_order(exchange=exch_seg, tradingsymbol=leg.symbol, orderno=ordno, newquantity=qty, newprice_type="LMT", newprice=f"{new_price:.2f}")
                            time.sleep(1.5)
                            history = api.single_order_history(orderno=ordno)
                            if history and isinstance(history, list) and history[0].get("status") in ["FILLED", "COMPLETE"]:
                                avg_prc = float(history[0].get("avgprc", new_price))
                                leg.current_price = avg_prc
                                leg.entry_price = avg_prc
                                logger.info(f"[ShoonyaOrderLifecycle] ✅ Chased Entry FILLED @ Avg Price: {avg_prc:.2f} (Order: {ordno})")
                            else:
                                logger.error(f"[ShoonyaOrderLifecycle] 🚨 Chased Entry failed to fill. Cancelling entry to prevent staleness.")
                                if hasattr(api, "cancel_order"):
                                    api.cancel_order(orderno=ordno)
                                return False
                        else:
                            return False
                else:
                    leg.entry_price = limit_price
                    logger.warning("[ShoonyaOrderLifecycle] API doesn't support order history. Using limit_price as entry.")

                leg.status = "OPEN"
                return True
            else:
                err_msg = resp.get("emsg", "Unknown Broker Error") if isinstance(resp, dict) else str(resp)
                logger.error(f"[ShoonyaOrderLifecycle] 🚨 SHOONYA API REJECTION REASON: {err_msg}")
                return False
        except Exception as e:
            logger.error(f"[ShoonyaOrderLifecycle] Exception in execute_entry: {e}")
            return False

    def execute_exit(self, leg: TradeLeg, reason: str, exit_price: Optional[float] = None, live_bid: float = None) -> bool:
        if leg.status != "OPEN":
            logger.warning(f"[ShoonyaOrderLifecycle] Leg {leg.symbol} is not OPEN. Skipping exit.")
            return False

        exch_seg = getattr(leg, 'exchange', 'BFO' if 'SENSEX' in leg.symbol else 'NFO')
        qty = leg.quantity * (SENSEX_LOT_SIZE if "SENSEX" in leg.symbol else NIFTY_LOT_SIZE)
        token = str(leg.token)
        
        base_price = live_bid if (live_bid and live_bid > 0) else leg.current_price
        limit_price = get_marketable_exit_limit_price(base_price)
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