from datetime import datetime
from common.logger import get_logger

logger = get_logger("astra_evaluator")

_CHAIN_DELTA_THR = 0.10  # 10% net delta required to trigger (V4.3 calibration)
_WALL_MULT = 1.5
_WALL_THIN_FRAC = 0.70
_MIN_FLOW_VOL = 500
_PREM_IMPACT_MIN = 0.004   # ~2 ticks on ₹25 premium
_SPOT_VETO_PTS = 2.0
_ICEBERG_DELTA_MIN = 0.15

class ChainFootprintEvaluator:
    def __init__(self):
        # We store one global conflict for the index direction
        # None, "CALL_ASK_WALL_CONFLICT", or "PUT_ASK_WALL_CONFLICT"
        self.pending_conflict = None
        self.last_chain_delta = 0.0
        self.last_wall_size = 0.0

    def _confirmation_block(self, data: dict, delta_pct: float, spot_bar_chg, side: str):
        """Returns a NEUTRAL reason string if the fresh signal must be blocked, else None."""
        if data.get("depth_age_sec", 999) > 1.5:
            return f"Stale depth on battleground ({data.get('depth_age_sec', 999):.1f}s) — abort"
        if data.get("delta", 0) <= 0:
            return "Battleground not confirming chain direction"
        prem = data.get("premium_chg_pct", 0.0)
        if side == "CALL":
            if delta_pct > _ICEBERG_DELTA_MIN and prem <= 0.0:
                return f"ICEBERG_SHADOW: Chain Δ +{delta_pct*100:.1f}% but premium {prem*100:+.2f}% (hidden seller suspected; shadow-only)"
            if prem < _PREM_IMPACT_MIN:
                return f"NO PRICE IMPACT: Chain Δ +{delta_pct*100:.1f}% but battleground premium {prem*100:+.2f}% → absorbed/noise"
            if spot_bar_chg is not None and spot_bar_chg <= -_SPOT_VETO_PTS:
                return f"SPOT DIVERGENCE: CALL flow but spot bar {spot_bar_chg:+.1f} pts"
        else:
            if delta_pct < -_ICEBERG_DELTA_MIN and prem <= 0.0:
                return f"ICEBERG_SHADOW: Chain Δ {delta_pct*100:.1f}% but premium {prem*100:+.2f}% (shadow-only)"
            if prem > -_PREM_IMPACT_MIN:
                return f"NO PRICE IMPACT: Chain Δ {delta_pct*100:.1f}% but battleground premium {prem*100:+.2f}% → absorbed/noise"
            if spot_bar_chg is not None and spot_bar_chg >= _SPOT_VETO_PTS:
                return f"SPOT DIVERGENCE: PUT flow but spot bar {spot_bar_chg:+.1f} pts"
        if data.get("ofi", 0) <= 0:
            return f"OFI not confirming on battleground ({data.get('ofi', 0):+.0f})"
        return None

    def evaluate_1m_boundary(self, compiled_footprints, index_spot, now=None, spot_bar_chg=None, consideration_strikes=None):
        """
        Evaluates the aggregated 1-minute footprint across all tracked strikes.
        Returns (signal_type, reason, target_strike)
        signal_type ∈ {"CALL", "PUT", "NEUTRAL"}
        """
        now = now or datetime.now()
        
        if not compiled_footprints:
            return "NEUTRAL", "No footprints available", None

        # Aggregate Chain Data
        total_chain_buy = 0.0
        total_chain_sell = 0.0
        ce_buy, ce_sell, pe_buy, pe_sell = 0.0, 0.0, 0.0, 0.0
        
        for token, data in compiled_footprints.items():
            buy_vol = data.get("total_buy_vol", 0)
            sell_vol = data.get("total_sell_vol", 0)
            total_chain_buy += buy_vol
            total_chain_sell += sell_vol
            
            if data.get("option_type") == "CE":
                ce_buy += buy_vol
                ce_sell += sell_vol
            elif data.get("option_type") == "PE":
                pe_buy += buy_vol
                pe_sell += sell_vol
            
        flow = total_chain_buy + total_chain_sell
        signed_flow = (ce_buy - ce_sell) + (pe_sell - pe_buy)   # PE buying = bearish, PE selling = bullish
        delta_pct = signed_flow / flow if flow > 0 else 0.0
        self.last_chain_delta = delta_pct
        if flow < _MIN_FLOW_VOL:
            return "NEUTRAL", f"Flow too low ({flow:.0f} < {_MIN_FLOW_VOL}) — noise gate", None
        
        # CE vs PE Skew Divergence
        ce_delta = (ce_buy - ce_sell) / max(ce_buy + ce_sell, 1)
        pe_delta = (pe_buy - pe_sell) / max(pe_buy + pe_sell, 1)
        
        # We also want to record the biggest Ask Wall for the dashboard
        max_wall = 0
        for token, data in compiled_footprints.items():
            if data.get("avg_l2_ask", 0) > max_wall:
                max_wall = data.get("avg_l2_ask", 0)
        self.last_wall_size = max_wall

        # T+1 Conflict Resolution Phase
        if self.pending_conflict is not None:
            # Bug 14: Conflict Expiry Check
            age_minutes = (now - self.pending_conflict["bar_ts"]).total_seconds() / 60
            if age_minutes > 2.5: # Allow slightly more than 1 minute for timing slack
                self.pending_conflict = None
                logger.warning("Stale conflict expired — exceeded T+1 window")
            else:
                kind = self.pending_conflict["kind"]
                wall_then = self.pending_conflict["wall_size"]
                
                # Find the current wall size on the battleground token
                battle_token = self.pending_conflict["token"]
                
                # Bug 9: Rollover False Positive Check
                if battle_token not in compiled_footprints:
                    self.pending_conflict = None
                    return "NEUTRAL", "Battleground token rolled out of window — conflict invalidated", None
                    
                battle_data = compiled_footprints.get(battle_token, {})
                wall_now = battle_data.get("last_l2_ask", 0)
                bids_now = battle_data.get("last_l2_bid", 0)
                
                if wall_now == 0 and bids_now == 0:
                    self.pending_conflict = None
                    return "NEUTRAL", "No aggressive trades on battleground in T+1 — inconclusive", None
                
                thinned = wall_then > 0 and wall_now < wall_then * _WALL_THIN_FRAC
                wall_no_longer_dominant = not (wall_now > bids_now * _WALL_MULT and wall_now > 0)
                
                # Bug 11: Absolute Wall Decay Check for Absorption
                wall_held_absolute = wall_now >= wall_then * 0.80
                wall_held_relative = wall_now >= bids_now * _WALL_MULT and wall_now > 0
                wall_held = wall_held_absolute and wall_held_relative
                
                if kind == "CALL_ASK_WALL_CONFLICT":
                    still_buying = delta_pct > (_CHAIN_DELTA_THR * 0.5) # Bug 12: Relaxed but non-zero
                    if (thinned or wall_no_longer_dominant) and still_buying:
                        reason = f"LATE BREAKOUT CALL (T+1): Ask wall broken. Chain Δ {delta_pct*100:.1f}%"
                        self.pending_conflict = None
                        return "CALL", reason, battle_token
                    
                    if wall_held:
                        reason = f"ABSORPTION PUT (T+1 confirmed): Call Ask wall held! Chain Δ {delta_pct*100:.1f}%"
                        self.pending_conflict = None
                        return "PUT", reason, None 
                        
                    self.pending_conflict = None
                    return "NEUTRAL", "CALL_ASK_WALL_CONFLICT cleared without clear breakout", None

                elif kind == "PUT_ASK_WALL_CONFLICT":
                    still_selling = delta_pct < -(_CHAIN_DELTA_THR * 0.5)
                    if (thinned or wall_no_longer_dominant) and still_selling:
                        reason = f"LATE BREAKOUT PUT (T+1): Put Ask wall broken. Chain Δ {delta_pct*100:.1f}%"
                        self.pending_conflict = None
                        return "PUT", reason, battle_token
                        
                    if wall_held:
                        reason = f"ABSORPTION CALL (T+1 confirmed): Put Ask wall held! Chain Δ {delta_pct*100:.1f}%"
                        self.pending_conflict = None
                        return "CALL", reason, None

                    self.pending_conflict = None
                    return "NEUTRAL", "PUT_ASK_WALL_CONFLICT cleared without clear breakout", None

        # Fresh Evaluation Phase (T=0)
        
        # 1. Chain Delta heavily positive (Aggressive Call Buying or Put Selling)
        if delta_pct > _CHAIN_DELTA_THR:
            # Straddle Filter (CE vs PE Skew)
            if pe_delta > 0.05 and ce_delta < (pe_delta * 3):
                return "NEUTRAL", f"Straddle Flow (False Breakout): CE Δ {ce_delta*100:.1f}%, PE Δ {pe_delta*100:.1f}%", None

            # Pass 1: Find the most heavily traded Call (Battleground)
            target_ce_token = None
            max_ce_vol = 0
            
            for token, data in compiled_footprints.items():
                if data.get("option_type") != "CE":
                    continue
                # Only consider battlegrounds within the 3+3 target strikes
                if consideration_strikes and data.get("strike") not in consideration_strikes:
                    continue
                vol = data.get("total_buy_vol", 0) + data.get("total_sell_vol", 0)
                if vol > max_ce_vol:
                    max_ce_vol = vol
                    target_ce_token = token
                    
            # Pass 2: Check for Wall strictly on the battleground token
            if target_ce_token:
                data = compiled_footprints[target_ce_token]
                
                blk = self._confirmation_block(data, delta_pct, spot_bar_chg, "CALL")
                if blk:
                    return "NEUTRAL", blk, None

                asks = data.get("last_l2_ask", 0)
                bids = data.get("last_l2_bid", 0)
                is_spoof = data.get("ask_wall_spoof", False)
                
                if asks > bids * _WALL_MULT and asks > 0:
                    if is_spoof:
                        return "NEUTRAL", f"Spoofing Detected: Flash ask wall on {target_ce_token} ignored.", None
                        
                    self.pending_conflict = {
                        "kind": "CALL_ASK_WALL_CONFLICT",
                        "token": target_ce_token,
                        "wall_size": asks,
                        "bar_ts": now
                    }
                    return "NEUTRAL", f"ABSORB_CONFLICT: Chain Δ +{delta_pct*100:.1f}% but Ask Wall detected on {target_ce_token}. Waiting for T+1.", None
            
            return "CALL", f"Confluence BUY CALL: Chain Δ +{delta_pct*100:.1f}%", target_ce_token

        # 2. Chain Delta heavily negative (Aggressive Put Buying or Call Selling)
        if delta_pct < -_CHAIN_DELTA_THR:
            # Straddle Filter (CE vs PE Skew)
            if ce_delta < -0.05:
                # If Market is going down, CE_delta should be negative and PE_delta positive. 
                # Wait, if Market is going down, PE_delta is positive (Put Buying).
                # CE_delta should be negative (Call Selling).
                # Wait, what is the straddle trap? If BOTH are being bought!
                # If PE is being bought (PE_delta > 0) AND CE is being bought (CE_delta > 0)
                pass # Fixed below
            if ce_delta > 0.05 and pe_delta < (ce_delta * 3):
                return "NEUTRAL", f"Straddle Flow (False Breakout): CE Δ {ce_delta*100:.1f}%, PE Δ {pe_delta*100:.1f}%", None

            # Pass 1: Find the most heavily traded Put (Battleground)
            target_pe_token = None
            max_pe_vol = 0
            
            for token, data in compiled_footprints.items():
                if data.get("option_type") != "PE":
                    continue
                # Only consider battlegrounds within the 3+3 target strikes
                if consideration_strikes and data.get("strike") not in consideration_strikes:
                    continue
                vol = data.get("total_buy_vol", 0) + data.get("total_sell_vol", 0)
                if vol > max_pe_vol:
                    max_pe_vol = vol
                    target_pe_token = token
                    
            # Pass 2: Check for Wall strictly on the battleground token
            if target_pe_token:
                data = compiled_footprints[target_pe_token]
                
                blk = self._confirmation_block(data, delta_pct, spot_bar_chg, "PUT")
                if blk:
                    return "NEUTRAL", blk, None

                asks = data.get("last_l2_ask", 0)
                bids = data.get("last_l2_bid", 0)
                is_spoof = data.get("ask_wall_spoof", False)
                
                if asks > bids * _WALL_MULT and asks > 0:
                    if is_spoof:
                        return "NEUTRAL", f"Spoofing Detected: Flash ask wall on {target_pe_token} ignored.", None

                    self.pending_conflict = {
                        "kind": "PUT_ASK_WALL_CONFLICT",
                        "token": target_pe_token,
                        "wall_size": asks,
                        "bar_ts": now
                    }
                    return "NEUTRAL", f"ABSORB_CONFLICT: Chain Δ {delta_pct*100:.1f}% but Ask Wall detected on {target_pe_token}. Waiting for T+1.", None
            
            return "PUT", f"Confluence BUY PUT: Chain Δ {delta_pct*100:.1f}%", target_pe_token

        return "NEUTRAL", f"Chain Δ {delta_pct*100:.1f}% - No Momentum", None
