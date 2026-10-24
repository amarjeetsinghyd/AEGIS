import logging
import os
import pandas as pd
from typing import List, Dict, Tuple

logger = logging.getLogger("strike_selector")

# Testing-phase budget band: with tiny funds (e.g. Rs 973, 1 lot x 65 => max
# Rs 14.97), prioritize strikes whose LTP sits inside this band so paper/test
# trades can actually punch through instead of terminal 'funds too low'.
TESTING_BAND_LO = float(os.getenv("AEGIS_TESTING_BAND_LO", "8.0"))
TESTING_BAND_HI = float(os.getenv("AEGIS_TESTING_BAND_HI", "14.5"))
TESTING_TINY_FUNDS = float(os.getenv("AEGIS_TESTING_TINY_FUNDS", "5000.0"))
TESTING_DEEP_DEPTH = int(os.getenv("AEGIS_TESTING_DEEP_DEPTH", "32"))
# Dead-zone safety buffer: effective floor stops this far below the per-lot
# budget ceiling so floor can never exceed what the account can afford.
PREMIUM_SAFETY_BUFFER = float(os.getenv("AEGIS_PREMIUM_SAFETY_BUFFER", "0.50"))


def effective_premium_floor(configured_min: float, available_funds: float, lot_size: int) -> float:
    """Dynamic floor override for Tiny Funds / Testing Mode.

    Formula: Effective Floor = min(env_premium_min, budget_ceiling - safety_buffer),
    where budget_ceiling = available_funds / lot_size.

    Applied ONLY when funds are tiny (< TESTING_TINY_FUNDS) and the configured
    floor (e.g. execution manager NIFTY_PREMIUM_MIN = 15) would otherwise sit
    above the budget ceiling (e.g. Rs 14.97) — a mathematical dead zone in
    which no strike can ever be selected. Production accounts keep the
    configured floor untouched.
    """
    try:
        cfg = float(configured_min)
    except Exception:
        return float(configured_min)
    try:
        funds = float(available_funds or 0)
        lot = int(lot_size or 0)
        if funds <= 0 or lot <= 0:
            return cfg
        if funds >= TESTING_TINY_FUNDS:
            return cfg
        env_floor = float(os.getenv("AEGIS_MIN_PREMIUM", "4.0"))
        ceiling = funds / lot
        dyn = min(env_floor, ceiling - PREMIUM_SAFETY_BUFFER)
        if dyn < cfg:
            return max(0.0, dyn)
        return cfg
    except Exception:
        return cfg

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
    def get_otm_strikes(atm_strike: float, signal_type: str, index_name: str, max_depth: int = 6) -> List[float]:
        """
        Returns OTM 1 to OTM 5 plus ATM fallback (top-5 affordable universe).
        Yesterday-style: always find an affordable strike within budget.
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
        Selects the best affordable OTM contract within the top-5 OTM universe for 1 lot.
        Prioritizes the highest affordable premium (closest to ATM) for maximum delta.
        Always picks within budget like yesterday — cheapest fallback if needed.
        """
        if weekly_opts is None or (hasattr(weekly_opts, "empty") and weekly_opts.empty):
            logger.debug("[StrikeSelector] Option chain is empty. Aborting selection.")
            return []

        pattern = r'CE|C\d+$' if signal_type == "CALL" else r'PE|P\d+$'
        opts = weekly_opts[weekly_opts['symbol'].str.contains(pattern, regex=True)]

        atm_strike = StrikeSelector.calculate_atm_strike(index_price, index_name)

        # Detect 0DTE (Expiry Day)
        is_zero_dte = False
        try:
            if "is_zero_dte" in kwargs:
                is_zero_dte = bool(kwargs["is_zero_dte"])
            elif not weekly_opts.empty and "expiry" in weekly_opts.columns:
                from datetime import datetime
                exp_val = str(weekly_opts["expiry"].iloc[0])
                for fmt in ("%d-%b-%Y", "%d%b%Y", "%Y-%m-%d"):
                    try:
                        exp_dt = datetime.strptime(exp_val, fmt).date()
                        if exp_dt == datetime.now().date():
                            is_zero_dte = True
                        break
                    except ValueError:
                        pass
        except Exception:
            pass

        # 0DTE Guard: Limit search depth to max 5 OTM strikes on expiry day for production accounts.
        # For testing / tiny funds, scan up to OTM 15 (depth 16) so affordable strikes can be selected.
        max_0dte_depth = int(os.getenv("AEGIS_MAX_0DTE_OTM_DEPTH", "5"))
        if is_zero_dte:
            _depth = min(max_0dte_depth, 5)
            try:
                if float(available_funds or 0) < TESTING_TINY_FUNDS:
                    _depth = max(32, TESTING_DEEP_DEPTH)
            except Exception:
                pass
        else:
            _depth = 6
            try:
                if float(available_funds or 0) < TESTING_TINY_FUNDS:
                    _depth = max(32, TESTING_DEEP_DEPTH)
            except Exception:
                pass

        otm_strikes = StrikeSelector.get_otm_strikes(atm_strike, signal_type, index_name, max_depth=_depth)
        _configured_min = float(kwargs.get("premium_min") or float(os.getenv("AEGIS_MIN_PREMIUM", "4.0")))
        # Dead-zone fix: clamp the hardcoded production floor (15) down to the
        # budget-aware effective floor when tiny funds would otherwise make
        # selection mathematically impossible.
        _prem_min = effective_premium_floor(_configured_min, available_funds, lot_size)
        if _prem_min < _configured_min:
            logger.info(
                f"[StrikeSelector][DEAD-ZONE FIX] Clamped premium floor Rs {_configured_min:.2f} -> Rs {_prem_min:.2f} "
                f"(funds Rs {float(available_funds):.2f} / lot {lot_size} = ceiling Rs {float(available_funds) / int(lot_size):.2f})"
            )
        
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
            sym = str(opt_row.get('symbol', ''))
            ltp = option_cache.get(token) if isinstance(option_cache, dict) else None
            if ltp is None and token.isdigit() and isinstance(option_cache, dict):
                ltp = option_cache.get(int(token))
            if (ltp is None or ltp <= 0) and sym and isinstance(option_cache, dict):
                ltp = option_cache.get(sym)
                
            # RAM Fast-Path: check market_data_provider._live_quotes in-memory cache directly (0.001ms)
            if (ltp is None or ltp <= 0) and market_data_provider is not None:
                live_quotes = getattr(market_data_provider, "_live_quotes", None)
                if isinstance(live_quotes, dict):
                    q_mem = live_quotes.get(token) or (live_quotes.get(int(token)) if token.isdigit() else None)
                    if not q_mem and sym:
                        q_mem = live_quotes.get(sym)
                    if isinstance(q_mem, dict) and float(q_mem.get("ltp", 0.0) or 0.0) > 0:
                        ltp = float(q_mem["ltp"])
                        if isinstance(option_cache, dict):
                            option_cache[token] = ltp
                            if sym:
                                option_cache[sym] = ltp

            # Zero-REST Discipline: If token has no tick streaming in memory, it is illiquid.
            # Skip instantly in 0.001ms — NEVER block hot-path with synchronous HTTP REST calls!
            if ltp is None or ltp <= 0:
                continue
            
            if ltp is not None and ltp > 0:
                if ltp < _prem_min:
                    continue
                cost_for_one_lot = ltp * lot_size
                if cost_for_one_lot <= available_funds:
                    valid_opts.append((opt_row, ltp, cost_for_one_lot))

        if not valid_opts:
            dte_msg = " [0DTE Max 5-OTM Guard Active]" if is_zero_dte and float(available_funds or 0) >= TESTING_TINY_FUNDS else ""
            logger.warning(
                f"[StrikeSelector] Funds (₹{available_funds}) too low for 1 lot of size {lot_size} "
                f"among top-{len(otm_strikes)} OTM + ATM{dte_msg}."
            )
            return []

        # Sort by highest affordable premium (closest to ATM for highest delta)
        valid_opts.sort(key=lambda x: x[1], reverse=True)
        chosen_row, chosen_ltp, chosen_cost = valid_opts[0]
        logger.info(
            f"[StrikeSelector] Selected optimal affordable strike: {chosen_row['symbol']} @ LTP ₹{chosen_ltp:.2f} "
            f"(Cost: ₹{chosen_cost:.2f} <= Funds: ₹{available_funds:.2f})"
        )
        return [(chosen_row, 1, chosen_ltp)]