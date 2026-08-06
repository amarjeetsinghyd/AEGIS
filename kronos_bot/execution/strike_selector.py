"""
Broker-agnostic Strike Selector.
Select nearest affordable OTM strike(s) based on available funds (1 lot).
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

import pandas as pd

logger = logging.getLogger("strike_selector")


class StrikeSelector:
    @staticmethod
    def calculate_atm_strike(index_price: float, index_name: str) -> float:
        step = 100 if "SENSEX" in str(index_name).upper() else 50
        return round(index_price / step) * step

    @staticmethod
    def get_otm_strikes(atm_strike: float, signal_type: str, index_name: str, max_depth: int = 40) -> List[float]:
        step = 100 if "SENSEX" in str(index_name).upper() else 50
        strikes = []
        for i in range(max_depth):
            if signal_type == "CALL":
                strikes.append(atm_strike + (i * step))
            else:
                strikes.append(atm_strike - (i * step))
        return strikes

    @staticmethod
    def _resolve_ltp(token: str, option_cache: Dict, market_data_provider, exch: str):
        ltp = None
        if isinstance(option_cache, dict):
            ltp = option_cache.get(token)
            if ltp is None and token.isdigit():
                ltp = option_cache.get(int(token))
        if (ltp is None or ltp <= 0) and market_data_provider is not None:
            try:
                q = market_data_provider.get_quote(exch, token)
                if isinstance(q, dict) and float(q.get("ltp") or 0) > 0:
                    ltp = float(q["ltp"])
                    if isinstance(option_cache, dict):
                        option_cache[token] = ltp
            except Exception as e_quote:
                logger.debug(f"[StrikeSelector] quote fail token={token}: {e_quote}")
        return float(ltp) if ltp is not None and float(ltp) > 0 else None

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
        if weekly_opts is None or weekly_opts.empty:
            logger.warning("[StrikeSelector] Option chain is empty. Aborting selection.")
            return []

        pattern = r"CE|C\d+$" if signal_type == "CALL" else r"PE|P\d+$"
        opts = weekly_opts[weekly_opts["symbol"].astype(str).str.contains(pattern, regex=True)].copy()
        if opts.empty:
            logger.warning(f"[StrikeSelector] No {signal_type} rows in weekly chain ({len(weekly_opts)} total).")
            return []

        atm_strike = StrikeSelector.calculate_atm_strike(index_price, index_name)
        otm_strikes = StrikeSelector.get_otm_strikes(atm_strike, signal_type, index_name, max_depth=40)
        market_data_provider = kwargs.get("market_data_provider")
        exch = "BFO" if "SENSEX" in str(index_name).upper() else "NFO"
        premium_min = float(kwargs.get("premium_min") or 0)
        premium_max = float(kwargs.get("premium_max") or 0)

        opts["strike_i"] = opts["strike"].astype(float).astype(int)
        by_strike = {int(k): g for k, g in opts.groupby("strike_i")}

        quoted = 0
        candidates = []  # (opt_row, ltp, cost, strike)

        def consider(opt_row, ltp):
            nonlocal quoted
            quoted += 1
            ls = int(opt_row["ls"]) if "ls" in opt_row and str(opt_row.get("ls", "")).isdigit() else lot_size
            cost = float(ltp) * ls
            candidates.append((opt_row, float(ltp), cost, int(float(opt_row["strike"]))))

        # Pass 1: ATM → OTM walk (prefer nearer strikes)
        for target_strike in otm_strikes:
            match = by_strike.get(int(target_strike))
            if match is None or match.empty:
                continue
            opt_row = match.iloc[0]
            ltp = StrikeSelector._resolve_ltp(str(opt_row["token"]), option_cache, market_data_provider, exch)
            if ltp is not None:
                consider(opt_row, ltp)

        # Pass 2: if still nothing affordable, scan remaining chain quotes (cheap far OTM)
        affordable_so_far = [c for c in candidates if c[2] <= available_funds]
        if not affordable_so_far:
            scanned = 0
            for _, opt_row in opts.iterrows():
                if scanned >= 80:
                    break
                tok = str(opt_row["token"])
                if isinstance(option_cache, dict) and (tok in option_cache or (tok.isdigit() and int(tok) in option_cache)):
                    # already considered if we had it
                    pass
                ltp = StrikeSelector._resolve_ltp(tok, option_cache, market_data_provider, exch)
                scanned += 1
                if ltp is not None:
                    # avoid dupes
                    if any(str(c[0]["token"]) == tok for c in candidates):
                        continue
                    consider(opt_row, ltp)

        if not candidates:
            logger.warning(
                f"[StrikeSelector] No valid option quotes for {signal_type} {index_name} "
                f"(chain={len(opts)} ATM={atm_strike} funds=₹{available_funds:.2f})"
            )
            return []

        # Prefer in-band premium when possible; else any affordable from ATM outward
        def pick(pool):
            for opt_row, ltp, cost, _strike in pool:
                if cost <= available_funds:
                    if premium_min and premium_max and not (premium_min <= ltp <= premium_max):
                        continue
                    return opt_row, ltp, cost
            for opt_row, ltp, cost, _strike in pool:
                if cost <= available_funds:
                    return opt_row, ltp, cost
            return None

        # Sort candidates: nearer ATM first (original walk order preserved for pass1),
        # then by absolute distance to ATM for pass2 extras
        candidates.sort(key=lambda c: abs(c[3] - atm_strike))
        picked = pick(candidates)
        if not picked:
            cheapest = min(candidates, key=lambda c: c[2])
            logger.warning(
                f"[StrikeSelector] Funds ₹{available_funds:.2f} too low for 1 lot (size {lot_size}). "
                f"quoted={quoted} cheapest={cheapest[0]['symbol']} LTP={cheapest[1]:.2f} "
                f"cost=₹{cheapest[2]:.2f}"
            )
            return []

        opt_row, ltp, cost = picked
        logger.info(
            f"[StrikeSelector] Affordable {signal_type}: {opt_row['symbol']} LTP={ltp:.2f} "
            f"cost=₹{cost:.2f} <= funds=₹{available_funds:.2f} (ATM={atm_strike}, quoted={quoted})"
        )
        return [(opt_row, 1, ltp)]
