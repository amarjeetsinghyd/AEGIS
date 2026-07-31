import math
import collections
import numpy as np
from astra_bot.core.logger import get_logger

logger = get_logger("microstructure_engine")


def norm_pdf(x: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * x**2) / 2.5066282746310002


def norm_cdf(x: np.ndarray) -> np.ndarray:
    a1 = 0.319381530
    a2 = -0.356563782
    a3 = 1.781477937
    a4 = -1.821255978
    a5 = 1.330274429
    x = np.asarray(x, dtype=np.float64)
    absx = np.abs(x)
    k = 1.0 / (1.0 + 0.2316419 * absx)
    poly = k * (a1 + k * (a2 + k * (a3 + k * (a4 + k * a5))))
    pdf = norm_pdf(absx)
    cdf = 1.0 - pdf * poly
    return np.where(x >= 0, cdf, 1.0 - cdf)


def bsm_vectorized_gex(S_spot: float,
                       K_arr: np.ndarray,
                       T_arr: np.ndarray,
                       prices_arr: np.ndarray,
                       is_call_arr: np.ndarray,
                       oi_arr: np.ndarray,
                       r: float = 0.07):
    if S_spot <= 0 or len(K_arr) == 0:
        return np.array([]), np.array([]), 0.0

    K_arr = np.asarray(K_arr, dtype=np.float64)
    T_arr = np.asarray(T_arr, dtype=np.float64)
    prices_arr = np.asarray(prices_arr, dtype=np.float64)
    is_call_arr = np.asarray(is_call_arr, dtype=bool)
    oi_arr = np.asarray(oi_arr, dtype=np.float64)

    sqrtT = np.sqrt(np.maximum(T_arr, 1e-6))
    sigma = np.full_like(K_arr, 0.25, dtype=np.float64)

    for _ in range(10):
        d1 = (np.log(np.maximum(S_spot, 1e-9) / np.maximum(K_arr, 1e-9)) + (r + 0.5 * sigma**2) * T_arr) / np.maximum(sigma * sqrtT, 1e-9)
        d2 = d1 - sigma * sqrtT
        call_price = S_spot * norm_cdf(d1) - K_arr * np.exp(-r * T_arr) * norm_cdf(d2)
        put_price = K_arr * np.exp(-r * T_arr) * norm_cdf(-d2) - S_spot * norm_cdf(-d1)
        model_price = np.where(is_call_arr, call_price, put_price)
        vega = S_spot * norm_pdf(d1) * sqrtT
        diff = model_price - prices_arr
        sigma = np.clip(sigma - np.where(vega > 1e-8, diff / np.maximum(vega, 1e-9), 0.0), 0.01, 5.0)

    d1 = (np.log(np.maximum(S_spot, 1e-9) / np.maximum(K_arr, 1e-9)) + (r + 0.5 * sigma**2) * T_arr) / np.maximum(sigma * sqrtT, 1e-9)
    gamma = norm_pdf(d1) / np.maximum(S_spot * sigma * sqrtT, 1e-9)
    gex_k = oi_arr * gamma * S_spot
    gex_net = float(np.sum(np.where(is_call_arr, -gex_k, gex_k)))
    return sigma, gamma, gex_net


class EmpiricalTail:
    def __init__(self, maxlen: int = 256):
        self.values = collections.deque(maxlen=maxlen)

    def upper_p(self, x: float) -> float:
        if not np.isfinite(x):
            return 1.0
        if len(self.values) < 16:
            return 0.5
        arr = np.fromiter(self.values, dtype=np.float64)
        ge = int(np.sum(arr >= x))
        return (1.0 + ge) / (len(arr) + 1.0)

    def update(self, x: float):
        if np.isfinite(x):
            self.values.append(float(x))


class KineticMicrostructureEngine:
    """
    Full replacement architecture:
    - Causal alpha-beta-gamma state estimation for low-lag kinematics.
    - Distribution-free empirical tail scoring (conformal-style p-values).
    - No absolute OI acceleration threshold.
    - Negative GEX gate preserved.
    - Rollover continuity-aware signal gating.
    """
    def __init__(self,
                 bucket_seconds: float = 5.0,
                 conformal_alpha: float = 0.08,
                 tail_memory: int = 256,
                 min_window: int = 10):
        self.bucket_seconds = float(bucket_seconds)
        self.conformal_alpha = float(conformal_alpha)
        self.min_window = int(min_window)

        self.acc_tail = EmpiricalTail(tail_memory)
        self.vel_tail = EmpiricalTail(tail_memory)
        self.resid_tail = EmpiricalTail(tail_memory)
        self.breadth_tail = EmpiricalTail(tail_memory)
        self.flow_tail = EmpiricalTail(tail_memory)

        self.acceleration_threshold = 0.0

    @staticmethod
    def _mad(values) -> float:
        arr = np.asarray(values, dtype=np.float64)
        if arr.size == 0:
            return 1.0
        med = np.median(arr)
        mad = np.median(np.abs(arr - med))
        return float(max(1.4826 * mad, 1e-9))

    def _estimate_state(self, series):
        y = np.asarray(series, dtype=np.float64)
        if y.size < 4:
            return {
                "level": 0.0,
                "velocity": 0.0,
                "acceleration": 0.0,
                "residual_scale": 1.0,
                "raw_velocity": 0.0,
                "raw_acceleration": 0.0,
                "velocity_path": [],
                "acceleration_path": [],
                "residuals": [],
            }

        dt = self.bucket_seconds
        alpha = 0.70
        beta = 0.22
        gamma = 0.06

        x = float(y[0])
        v = 0.0
        a = 0.0

        residuals = []
        velocity_path = []
        acceleration_path = []

        for z in y[1:]:
            x_pred = x + v * dt + 0.5 * a * dt * dt
            v_pred = v + a * dt
            a_pred = a

            r = float(z - x_pred)

            x = x_pred + alpha * r
            v = v_pred + beta * r / max(dt, 1e-9)
            a = a_pred + gamma * 2.0 * r / max(dt * dt, 1e-9)

            residuals.append(r)
            velocity_path.append(v)
            acceleration_path.append(a)

        raw_velocity = float((y[-1] - y[-2]) / max(dt, 1e-9))
        prev_velocity = float((y[-2] - y[-3]) / max(dt, 1e-9))
        raw_acceleration = float((raw_velocity - prev_velocity) / max(dt, 1e-9))
        residual_scale = self._mad(residuals[-8:]) if residuals else 1.0

        return {
            "level": float(x),
            "velocity": float(v),
            "acceleration": float(a),
            "residual_scale": float(residual_scale),
            "raw_velocity": raw_velocity,
            "raw_acceleration": raw_acceleration,
            "velocity_path": velocity_path,
            "acceleration_path": acceleration_path,
            "residuals": residuals,
        }

    @staticmethod
    def _same_sign_streak(values):
        if not values:
            return 0
        last = np.sign(values[-1])
        if last == 0:
            return 0
        streak = 0
        for v in reversed(values):
            if np.sign(v) == last and v != 0:
                streak += 1
            else:
                break
        return streak

    def calculate_kinematics(self, snapshots: list) -> dict:
        if len(snapshots) < 4:
            return {
                "net_delta_oi": 0.0,
                "velocity": 0.0,
                "acceleration": 0.0,
                "raw_velocity": 0.0,
                "raw_acceleration": 0.0,
                "zscore": 0.0,
                "noisefloor": 0.0,
                "flowdurationsec": 0,
                "persistent": False,
                "coverage": 0.0,
            }

        series = [float(s.get("net_delta_oi", 0.0)) for s in snapshots]
        state = self._estimate_state(series)

        streak = self._same_sign_streak(state["velocity_path"])
        flowdurationsec = int(streak * self.bucket_seconds)
        persistent = streak >= 2 and len(state["velocity_path"]) >= 2
        coverage = float(snapshots[-1].get("coverage", 0.0))

        score = abs(state["acceleration"]) / max(state["residual_scale"], 1e-9)

        return {
            "net_delta_oi": series[-1],
            "velocity": state["velocity"],
            "acceleration": state["acceleration"],
            "raw_velocity": state["raw_velocity"],
            "raw_acceleration": state["raw_acceleration"],
            "zscore": round(float(score), 3),
            "noisefloor": round(float(state["residual_scale"]), 3),
            "flowdurationsec": flowdurationsec,
            "persistent": persistent,
            "coverage": coverage,
            "residual_scale": state["residual_scale"],
            "velocity_path": state["velocity_path"],
            "acceleration_path": state["acceleration_path"],
            "residuals": state["residuals"],
        }

    def analyze_trade_signing(self, snapshots: list) -> dict:
        if len(snapshots) < 2:
            return {
                "dominant_signer": "NEUTRAL",
                "puts_delta_oi_60s": 0.0,
                "calls_delta_oi_60s": 0.0,
            }

        first_snap = snapshots[0]
        last_snap = snapshots[-1]

        calls_delta = float(last_snap.get("calls_delta_oi", 0.0) - first_snap.get("calls_delta_oi", 0.0))
        puts_delta = float(last_snap.get("puts_delta_oi", 0.0) - first_snap.get("puts_delta_oi", 0.0))

        dominant = "NEUTRAL"
        if puts_delta > 0 and calls_delta < 0:
            dominant = "BULLISH_ACCUMULATION"
        elif calls_delta > 0 and puts_delta < 0:
            dominant = "BEARISH_ACCUMULATION"
        elif puts_delta > 0 and calls_delta >= 0:
            dominant = "PUT_BUILDUP" if puts_delta >= calls_delta else "MIXED_BULLISH"
        elif calls_delta > 0 and puts_delta >= 0:
            dominant = "CALL_BUILDUP" if calls_delta >= puts_delta else "MIXED_BEARISH"
        elif puts_delta < 0 and calls_delta < 0:
            dominant = "UNWINDING"

        return {
            "dominant_signer": dominant,
            "puts_delta_oi_60s": puts_delta,
            "calls_delta_oi_60s": calls_delta,
        }

    def _participation_metrics(self, snapshots: list, optionmetadata: list):
        if len(snapshots) < 2 or not optionmetadata:
            return 0.0, 0, 0

        first_oi = snapshots[0].get("option_ois", {}) or {}
        last_oi = snapshots[-1].get("option_ois", {}) or {}

        bullish = 0
        bearish = 0
        total = 0

        for meta in optionmetadata:
            tok = str(meta.get("token", ""))
            if not tok:
                continue

            if tok not in first_oi and tok not in last_oi:
                continue

            start = float(first_oi.get(tok, 0.0))
            end = float(last_oi.get(tok, start))
            delta = end - start
            if delta == 0:
                continue

            total += 1
            symbol = str(meta.get("symbol", "")).upper()
            option_type = str(meta.get("optiontype", "")).upper()

            is_call = option_type == "CE" or "CE" in symbol or symbol.endswith("C")
            is_put = option_type == "PE" or "PE" in symbol or symbol.endswith("P")

            if is_put and delta > 0:
                bullish += 1
            elif is_call and delta < 0:
                bullish += 1
            elif is_call and delta > 0:
                bearish += 1
            elif is_put and delta < 0:
                bearish += 1

        if total <= 0:
            return 0.0, 0, 0

        if bullish > bearish:
            direction = 1
            breadth = bullish / float(total)
        elif bearish > bullish:
            direction = -1
            breadth = bearish / float(total)
        else:
            direction = 0
            breadth = max(bullish, bearish) / float(total)

        return float(breadth), int(direction), int(total)

    def _compute_gex(self, snapshots: list, optionmetadata: list, spotprice: float, dtedays: float):
        if not optionmetadata or not snapshots or spotprice <= 0:
            return 0.0

        latest_snap = snapshots[-1]
        opt_prices = latest_snap.get("option_prices", {}) or {}
        opt_ois = latest_snap.get("option_ois", {}) or {}

        K_list, T_list, P_list, call_list, oi_list = [], [], [], [], []
        T_val = max(float(dtedays), 0.25) / 365.0

        for meta in optionmetadata:
            tok = str(meta.get("token", ""))
            strike = float(meta.get("strike", 0.0) or 0.0)
            if strike <= 0:
                continue

            price = float(opt_prices.get(tok, meta.get("ltp", 0.0)) or 0.0)
            oi = float(opt_ois.get(tok, meta.get("oi", 0.0)) or 0.0)
            if price <= 0 or oi <= 0:
                continue

            symbol = str(meta.get("symbol", "")).upper()
            option_type = str(meta.get("optiontype", "")).upper()
            is_call = option_type == "CE" or "CE" in symbol or symbol.endswith("C")

            K_list.append(strike)
            T_list.append(T_val)
            P_list.append(price)
            call_list.append(is_call)
            oi_list.append(oi)

        if not K_list:
            return 0.0

        _, _, gex_net = bsm_vectorized_gex(
            spotprice,
            np.array(K_list, dtype=np.float64),
            np.array(T_list, dtype=np.float64),
            np.array(P_list, dtype=np.float64),
            np.array(call_list, dtype=bool),
            np.array(oi_list, dtype=np.float64),
        )
        return float(gex_net)

    def evaluate_signals(self, snapshots: list, optionmetadata: list, spotprice: float = 0.0, dtedays: float = 4.0, spot_price: float = None) -> dict:
        if spot_price is not None:
            spotprice = spot_price
        result = {
            "signal": "NEUTRAL",
            "gex_net": 0.0,
            "gex_regime": "NEUTRAL_GAMMA",
            "velocity": 0.0,
            "acceleration": 0.0,
            "zscore": 0.0,
            "noisefloor": 0.0,
            "flowdurationsec": 0,
            "tradesigner": "NEUTRAL",
            "coverage": 0.0,
            "combined_p": 1.0,
            "surprisal": 0.0,
            "reason": "Insufficient Data",
        }

        if len(snapshots) < self.min_window or spotprice <= 0:
            return result

        k = self.calculate_kinematics(snapshots)
        t = self.analyze_trade_signing(snapshots)
        breadth, breadth_dir, participants = self._participation_metrics(snapshots, optionmetadata)
        gex_net = self._compute_gex(snapshots, optionmetadata, spotprice, dtedays)

        velocity = float(k["velocity"])
        acceleration = float(k["acceleration"])
        residual_scale = float(max(k.get("residual_scale", 1.0), 1e-9))
        flowdurationsec = int(k["flowdurationsec"])
        persistent = bool(k["persistent"])
        coverage = float(k["coverage"])
        rollover_active = bool(snapshots[-1].get("rollover_active", False))

        resid_tail_value = abs(k["residuals"][-1]) / residual_scale if k["residuals"] else 0.0
        abs_acc = abs(acceleration)
        abs_vel = abs(velocity)

        p_acc = self.acc_tail.upper_p(abs_acc)
        p_vel = self.vel_tail.upper_p(abs_vel)
        p_resid = self.resid_tail.upper_p(resid_tail_value)
        p_breadth = self.breadth_tail.upper_p(breadth if breadth > 0 else 0.0)
        p_flow = self.flow_tail.upper_p(flowdurationsec if flowdurationsec > 0 else 0.0)

        combined_p = max((p_acc * p_vel * p_resid * p_breadth * p_flow) ** 0.2, 1e-12)
        surprisal = -math.log(combined_p)

        kin_dir = 1 if velocity > 0 and acceleration > 0 else -1 if velocity < 0 and acceleration < 0 else 0

        bullish_signers = {"BULLISH_ACCUMULATION", "PUT_BUILDUP", "MIXED_BULLISH"}
        bearish_signers = {"BEARISH_ACCUMULATION", "CALL_BUILDUP", "MIXED_BEARISH"}

        signer = t["dominant_signer"]
        signer_dir = 1 if signer in bullish_signers else -1 if signer in bearish_signers else 0

        result.update({
            "gex_net": float(gex_net),
            "gex_regime": "NEGATIVE_GAMMA" if gex_net < 0 else "POSITIVE_GAMMA",
            "velocity": velocity,
            "acceleration": acceleration,
            "zscore": round(float(surprisal), 3),
            "noisefloor": round(float(residual_scale), 3),
            "flowdurationsec": flowdurationsec,
            "persistent": persistent,
            "tradesigner": signer,
            "coverage": coverage,
            "combined_p": float(combined_p),
            "surprisal": float(surprisal),
        })

        if gex_net >= 0:
            result["reason"] = f"NEUTRAL: Positive-gamma regime blocks momentum entries (GEX={gex_net:,.0f})."
        elif kin_dir == 0:
            result["reason"] = (
                f"NEUTRAL: Direction not yet coherent. "
                f"V={velocity:,.0f}/s, A={acceleration:,.0f}/s², p={combined_p:.3f}, coverage={coverage:.0%}."
            )
        elif not persistent:
            result["reason"] = (
                f"NEUTRAL: Flow lacks persistence. "
                f"Flow={flowdurationsec}s, p={combined_p:.3f}, coverage={coverage:.0%}."
            )
        elif coverage < 0.55:
            result["reason"] = (
                f"NEUTRAL: Basket coverage too thin for reliable inference "
                f"({coverage:.0%}, participants={participants})."
            )
        elif rollover_active and coverage < 0.70:
            result["reason"] = (
                f"NEUTRAL: Rollover transition active with incomplete participation "
                f"({coverage:.0%}, participants={participants})."
            )
        elif breadth_dir != 0 and breadth_dir != kin_dir:
            result["reason"] = (
                f"NEUTRAL CONFLICT: kinematics disagree with token participation "
                f"(breadth={breadth:.0%}, participants={participants})."
            )
        elif signer_dir != 0 and signer_dir != kin_dir:
            result["reason"] = f"NEUTRAL CONFLICT: kinematics oppose trade signer ({signer})."
        elif combined_p > self.conformal_alpha:
            result["reason"] = (
                f"NEUTRAL: Acceleration is not statistically unusual enough yet "
                f"(empirical p={combined_p:.3f}, surprisal={surprisal:.2f})."
            )
        else:
            result["signal"] = "CALL" if kin_dir > 0 else "PUT"
            result["reason"] = (
                f"{result['signal']} Entry Triggered | "
                f"GEX={gex_net:,.0f}, V={velocity:,.0f}/s, A={acceleration:,.0f}/s², "
                f"empirical p={combined_p:.3f}, surprisal={surprisal:.2f}, "
                f"flow={flowdurationsec}s, coverage={coverage:.0%}, signer={signer}."
            )

        self.acc_tail.update(abs_acc)
        self.vel_tail.update(abs_vel)
        self.resid_tail.update(resid_tail_value)
        self.breadth_tail.update(max(breadth, 0.0))
        self.flow_tail.update(max(flowdurationsec, 0.0))

        return result
