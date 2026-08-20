import logging
import pandas as pd
from typing import List, Dict, Tuple

logger = logging.getLogger("strike_selector")

class StrikeSelector:
    """
    Broker-agnostic Strike Selector.
    Responsibility: Select the nearest affordable OTM strike(s) based on available funds.
    """

    @staticmethod
    def calculate_atm_strike(index_price: float, index_name: str) -> float:
        step = 100 if "SENSEX" in str(index_name).upper() else 50
        return round(index_price / step) * step

    @staticmethod
    def get_otm_strikes(atm_strike: float, signal_type: str, index_name: str, max_depth: int = 4) -> List[float]:
        """
        Returns a list of strikes starting from OTM 1 moving to OTM 3 (high-gamma sweet spot).
        """
        step = 100 if "SENSEX" in str(index_name).upper() else 50
        strikes = []
        for i in range(1, max_depth):
            if signal_type == "CALL":
                strikes.append(atm_strike + (i * step))
            else:
                strikes.append(atm_strike - (i * step))
        # Add ATM as final fallback
        strikes.append(atm_strike)
        return strikes

    @staticmethod
    def select_strikes(
        signal_type: str,
        index_price: float,
        index_name: str,
        weekly_opts: pd.DataFrame,
        option_cache: Dict[str, float],
        available_funds: float,
        lot_size: int,
        **kwargs
    ) -> List[Tuple[pd.Series, int, float]]:
        """
        Selects the most affordable OTM contract within the OTM 1 to 3 sweet spot for 1 lot.
        Prioritizes the highest affordable premium (closest to ATM) for maximum delta.
        """
        if weekly_opts.empty:
            logger.debug("[StrikeSelector] Option chain is empty. Aborting selection.")
            return []

        pattern = r'CE|C\d+$' if signal_type == "CALL" else r'PE|P\d+$'
        opts = weekly_opts[weekly_opts['symbol'].str.contains(pattern, regex=True)]
        
        atm_strike = StrikeSelector.calculate_atm_strike(index_price, index_name)
        otm_strikes = StrikeSelector.get_otm_strikes(atm_strike, signal_type, index_name, max_depth=4)
        
        market_data_provider = kwargs.get("market_data_provider")
        
        valid_opts = []
        for target_strike in otm_strikes:
            strike_angel = int(target_strike * 100)
            strike_shoonya = int(target_strike)
            opts_strike_int = opts['strike'].astype(float).astype(int)
            match = opts[(opts_strike_int == strike_angel) | (opts_strike_int == strike_shoonya)]
            
            if match.empty:
                continue
                
            opt_row = match.iloc[0]
            token = str(opt_row['token'])
            ltp = option_cache.get(token) if isinstance(option_cache, dict) else None
            if ltp is None and token.isdigit() and isinstance(option_cache, dict):
                ltp = option_cache.get(int(token))
                
            if (ltp is None or ltp <= 0) and market_data_provider is not None:
                try:
                    exch = "BFO" if "SENSEX" in str(index_name).upper() else "NFO"
                    q = market_data_provider.get_quote(exch, token)
                    if isinstance(q, dict) and q.get("ltp", 0.0) > 0:
                        ltp = float(q["ltp"])
                        if isinstance(option_cache, dict):
                            option_cache[token] = ltp
                except Exception as e_quote:
                    logger.debug(f"[StrikeSelector] Quote fetch fallback warning for token {token}: {e_quote}")
            
            if ltp is not None and ltp > 0:
                cost_for_one_lot = ltp * lot_size
                if cost_for_one_lot <= available_funds:
                    valid_opts.append((opt_row, ltp, cost_for_one_lot))

        if not valid_opts:
            logger.warning(f"[StrikeSelector] Funds (₹{available_funds}) too low for 1 lot of size {lot_size} among available strikes.")
            return []

        # Sort by highest affordable premium (closest to ATM for highest delta)
        valid_opts.sort(key=lambda x: x[1], reverse=True)
        chosen_row, chosen_ltp, chosen_cost = valid_opts[0]
        logger.info(f"[StrikeSelector] Selected optimal affordable strike: {chosen_row['symbol']} @ LTP ₹{chosen_ltp:.2f} (Cost: ₹{chosen_cost:.2f} <= Funds: ₹{available_funds:.2f})")
        return [(chosen_row, 1, chosen_ltp)]