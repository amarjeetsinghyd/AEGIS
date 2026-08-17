"""
Astra V4.4 — Setup Detectors
"""
from dataclasses import dataclass
from typing import Optional, List

@dataclass
class Candidate:
    setup: str
    side: str
    strength: float
    norm_delta: float = 0.0
    progress_pts: float = 0.0

class SetupDetectors:
    def __init__(self):
        self.veto_until: Optional[float] = None

    def detect_A(self, bars, norm_delta, structure):
        if len(bars) < 2: return None
        upper, lower, ema9 = structure.upper, structure.lower, structure.ema9
        if upper <= 0 or ema9 <= 0: return None
        last2 = bars[-2:]
        if all(b['c'] > upper and b['c'] > ema9 for b in last2) and norm_delta >= 0.10:
            return Candidate('A', 'CALL', norm_delta, norm_delta, last2[-1]['c'] - last2[-1]['o'])
        if all(b['c'] < lower and b['c'] < ema9 for b in last2) and norm_delta <= -0.10:
            return Candidate('A', 'PUT', abs(norm_delta), norm_delta, last2[-1]['o'] - last2[-1]['c'])
        return None

    def detect_B(self, bars, norm_delta, prev_norm_delta, structure):
        if len(bars) < 2: return None
        abs_bar, trig = bars[-2], bars[-1]
        upper, lower = structure.upper, structure.lower
        if upper <= 0 or lower <= 0: return None
        ad = prev_norm_delta if prev_norm_delta is not None else 0.0
        if not (abs_bar['h'] >= lower and abs_bar['l'] <= upper): return None
        if abs(ad) < 0.15: return None
        prog = (abs_bar['c'] - abs_bar['o']) if ad > 0 else (abs_bar['o'] - abs_bar['c'])
        if prog >= 2.0: return None
        if (ad > 0 and abs_bar['c'] < lower) or (ad < 0 and abs_bar['c'] > upper): return None
        if norm_delta * ad >= 0 or abs(norm_delta) < 0.05: return None
        side = 'CALL' if ad < 0 else 'PUT'
        return Candidate('B', side, abs(ad), norm_delta, prog)

    def detect_C(self, bar, norm_delta, structure, now):
        if structure.context_state(bar['c']) != 'AT_EXTREME': return False
        if abs(norm_delta) < 0.20: return False
        prog = (bar['c'] - bar['o']) if norm_delta > 0 else (bar['o'] - bar['c'])
        if prog >= 2.0: return False
        self.veto_until = now + 300.0
        return True

    def is_veto_active(self, now):
        return self.veto_until is not None and now < self.veto_until

    def detect_D(self, bars, deltas, structure):
        if len(bars) < 2 or len(deltas) < 2: return None
        n = min(3, len(bars), len(deltas))
        s, cnt = 0.0, 0
        for i in range(1, n + 1):
            b = bars[-i]; d = deltas[-i]
            if d < 0.50 or b['c'] <= b['o']: break
            if len(bars) > i and b['c'] <= bars[-i - 1]['c']: break
            s += d; cnt += 1
        if cnt >= 2:
            return Candidate('D', 'CALL', s / cnt, deltas[-1], bars[-1]['c'] - bars[-1]['o'])
        s, cnt = 0.0, 0
        for i in range(1, n + 1):
            b = bars[-i]; d = deltas[-i]
            if d > -0.50 or b['c'] >= b['o']: break
            if len(bars) > i and b['c'] >= bars[-i - 1]['c']: break
            s += abs(d); cnt += 1
        if cnt >= 2:
            return Candidate('D', 'PUT', s / cnt, deltas[-1], bars[-1]['o'] - bars[-1]['c'])
        return None
