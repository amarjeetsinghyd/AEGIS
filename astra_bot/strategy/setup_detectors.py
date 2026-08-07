"""
Astra V4.4 — Setup Detectors
Each detector returns Candidate(setup, side, strength) or None.
norm_delta = signed_flow / total_flow
progress(bar, side) = (close−open) for CALL pressure, (open−close) for PUT pressure
"""
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class Candidate:
    setup: str      # 'A', 'B', 'C', 'D'
    side: str       # 'CALL' or 'PUT'
    strength: float
    norm_delta: float = 0.0
    progress_pts: float = 0.0


class SetupDetectors:
    """
    Setup A — Momentum Breakout
    Setup B — VWAP Absorption (reversal)
    Setup C — Delta Exhaustion (VETO SHIELD)
    Setup D — Stacked Imbalances (temporal proxy)
    """
    
    def __init__(self):
        self.veto_until: Optional[float] = None
    
    # ==================== SETUP A: MOMENTUM BREAKOUT ====================
    def detect_A(self, bars: List[dict], norm_delta: float, structure) -> Optional[Candidate]:
        """
        A1: last TWO consecutive 1m closes > upper AND > ema9
        A2: norm_delta >= +0.10 (directional)
        A3: strength = norm_delta
        Mirror for PUT side.
        """
        if len(bars) < 2:
            return None
        
        upper = structure.upper
        lower = structure.lower
        ema9 = structure.ema9
        
        if upper <= 0 or ema9 <= 0:
            return None
        
        # Check CALL side
        call_bars = bars[-2:]
        call_condition = all(
            b['c'] > upper and b['c'] > ema9
            for b in call_bars
        )
        
        if call_condition and norm_delta >= 0.10:
            progress = call_bars[-1]['c'] - call_bars[-1]['o']
            return Candidate('A', 'CALL', norm_delta, norm_delta, progress)
        
        # Check PUT side (mirror)
        put_condition = all(
            b['c'] < lower and b['c'] < ema9
            for b in call_bars
        )
        
        if put_condition and norm_delta <= -0.10:
            progress = call_bars[-1]['o'] - call_bars[-1]['c']
            return Candidate('A', 'PUT', abs(norm_delta), norm_delta, progress)
        
        return None
    
    # ==================== SETUP B: VWAP ABSORPTION ====================
    def detect_B(self, bars: List[dict], norm_delta: float, prev_norm_delta: float, structure) -> Optional[Candidate]:
        """
        B1: current or previous bar's span overlaps the zone (h >= lower and l <= upper)
        B2: |norm_delta| >= 0.15 (heavy aggression INTO the zone)
        B3: progress in the aggression direction < 2.0 pts (effort-without-result)
        B4: close did NOT breach the far band
        B5: TRIGGER: next bar's norm_delta flips sign with |flip| >= 0.05
        strength = |norm_delta|
        """
        if len(bars) < 1:
            return None
        
        upper = structure.upper
        lower = structure.lower
        
        if upper <= 0 or lower <= 0:
            return None
        
        bar = bars[-1]
        
        # B1: zone overlap
        zone_overlap = bar['h'] >= lower and bar['l'] <= upper
        
        if not zone_overlap:
            return None
        
        # B2: heavy aggression
        if abs(norm_delta) < 0.15:
            return None
        
        # B3: effort without result
        if norm_delta > 0:
            # Buying aggression -> check CALL progress
            progress = bar['c'] - bar['o']
            if progress >= 2.0:
                return None
            # B4: selling absorbed => close >= lower -> CALL candidate
            if bar['c'] < lower:
                return None
            side = 'CALL'
        else:
            # Selling aggression -> check PUT progress
            progress = bar['o'] - bar['c']
            if progress >= 2.0:
                return None
            # B4: buying absorbed => close <= upper -> PUT candidate
            if bar['c'] > upper:
                return None
            side = 'PUT'
        
        # B5: trigger on flip (checked at router level with prev_norm_delta)
        flip_detected = False
        if prev_norm_delta is not None:
            flip_detected = (norm_delta * prev_norm_delta < 0) and (abs(norm_delta - prev_norm_delta) >= 0.05)
        
        # Return candidate even without flip; router checks B5 trigger condition
        return Candidate('B', side, abs(norm_delta), norm_delta, progress)
    
    # ==================== SETUP C: DELTA EXHAUSTION (VETO SHIELD) ====================
    def detect_C(self, bar: dict, norm_delta: float, structure, now: float) -> bool:
        """
        C1: context_state == AT_EXTREME (day high/low or ORB extremes)
        C2: |norm_delta| >= 0.20
        C3: progress in the aggression direction < 2.0 pts
        => FIRE VETO: veto_until = now + 300s
        Returns True if veto should be activated.
        """
        ctx = structure.context_state(bar['c'])
        
        if ctx != 'AT_EXTREME':
            return False
        
        if abs(norm_delta) < 0.20:
            return False
        
        # Progress check
        if norm_delta > 0:
            progress = bar['c'] - bar['o']
        else:
            progress = bar['o'] - bar['c']
        
        if progress >= 2.0:
            return False
        
        # Fire veto
        self.veto_until = now + 300.0
        return True
    
    def is_veto_active(self, now: float) -> bool:
        """Check if veto is currently active."""
        if self.veto_until is None:
            return False
        return now < self.veto_until
    
    # ==================== SETUP D: STACKED IMBALANCES ====================
    def detect_D(self, bars: List[dict], norm_delta: float, structure) -> Optional[Candidate]:
        """
        D1: 2-3 consecutive bars, EACH with norm_delta >= +0.50 (buy:sell >= 3:1)
        D2: EACH bar closes higher than its open AND higher than previous close
        strength = mean(norm_delta of the stack)
        Mirror for PUT side.
        """
        if len(bars) < 2:
            return None
        
        # Check for 2-3 consecutive bars with strong delta
        stack_size = 0
        sum_delta = 0.0
        all_higher = True
        
        # Check CALL side
        for i in range(min(3, len(bars))):
            bar_idx = -(i + 1)
            bar = bars[bar_idx]
            
            # Need historical norm_delta per bar; using current as proxy
            # In production, this would need bar-level delta storage
            if norm_delta >= 0.50:
                if bar['c'] > bar['o']:
                    if i > 0 and bar['c'] <= bars[bar_idx + 1]['c']:
                        all_higher = False
                        break
                    stack_size += 1
                    sum_delta += norm_delta
                else:
                    all_higher = False
                    break
            else:
                break
        
        if stack_size >= 2 and all_higher:
            strength = sum_delta / stack_size
            progress = bars[-1]['c'] - bars[-1]['o']
            return Candidate('D', 'CALL', strength, norm_delta, progress)
        
        # Check PUT side (mirror)
        stack_size = 0
        sum_delta = 0.0
        all_lower = True
        
        for i in range(min(3, len(bars))):
            bar_idx = -(i + 1)
            bar = bars[bar_idx]
            
            if norm_delta <= -0.50:
                if bar['c'] < bar['o']:
                    if i > 0 and bar['c'] >= bars[bar_idx + 1]['c']:
                        all_lower = False
                        break
                    stack_size += 1
                    sum_delta += abs(norm_delta)
                else:
                    all_lower = False
                    break
            else:
                break
        
        if stack_size >= 2 and all_lower:
            strength = sum_delta / stack_size
            progress = bars[-1]['o'] - bars[-1]['c']
            return Candidate('D', 'PUT', strength, norm_delta, progress)
        
        return None
