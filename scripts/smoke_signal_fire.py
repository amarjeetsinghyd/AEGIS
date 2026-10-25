# smoke_signal_fire.py — Runtime proof that the post-debloat trade-fire path is clean.
# Pre-fix: on_tick_parsed crashed with NameError (ml_score) at live_signal_metrics build
#          -> _emit_signal NEVER reached -> zero trades. Post-fix: signal lands in queue.
import os, sys, queue, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["BOT_RUNTIME"] = "aegis"

import AEGIS.config.settings  # noqa: F401
from AEGIS.aegis_gamma_engine import AegisGammaEngine
from AEGIS.core.composite_conviction_engine import ConvictionResult

class FakeTick:
    def __init__(self, tok, ts, lp):
        self.tok = tok; self.ts = ts; self.sym = None; self.lp = lp
        self.bp = [lp - 0.05] * 5; self.sp = [lp + 0.05] * 5
        self.bq = [50] * 5; self.sq = [50] * 5
        self.oi = 1000.0; self.vol = 5000.0

def main():
    q = queue.Queue(maxsize=100)
    tok = "55555"
    sym = "NIFTY30OCT2622500CE"
    eng = AegisGammaEngine(
        token_to_symbol={tok: sym},
        token_map={tok: {"side": 0, "strike": 22500.0}},
        strike_step=50.0,
        underlying_tokens={"26000"},
        signal_queue=q,
    )
    print("Engine boots OK")

    # --- Force a PA setup through the ENTIRE dispatch pipeline ---
    eng.price_action_engine.evaluate_setup = lambda d, v, current_time=0, gamma_regime="NEUTRAL": (
        True, "20_ema_rejection", {
            "pillar": 1, "pa_setup_name": "20_ema_rejection",
            "sl_price": 22480.0, "bar_open": 22500.0, "bar_high": 22525.0,
            "bar_low": 22495.0, "bar_close": 22518.0,
            "wick_ratio": 0.40, "vr_t": 1.5,
        })
    eng.scorer.compute_score = lambda *a, **k: (85.0, True, {})
    eng.adaptive_micro_validator.evaluate = lambda **k: (
        True, 100.0, {"touch_i1": 0.30, "micro_ret_30s": 0.02,
                      "bp1": 10.0, "sp1": 10.5, "spread_pct": 0.5, "n_ticks": 10})
    eng.conviction_engine.compute_conviction_score = lambda **k: ConvictionResult(
        total_score=85.0, is_approved=True, sizing_tier="STANDARD_SIZE",
        recommended_tp_pct=0.040, sub_scores={}, breakdown={}, reason="smoke")
    eng.top5_depth_guard = None  # bypass L2 guard (no real book)

    t0 = time.time()
    eng.on_tick_parsed(FakeTick(tok, t0 * 1000.0, 12.50))  # ts in ms (compiler normalizes)
    # Warm-up: first option tick seeds cum-vol baseline; setup forces on 2nd tick
    eng.on_tick_parsed(FakeTick(tok, (t0 + 1.0) * 1000.0, 12.55))

    n = q.qsize()
    assert n == 1, f"FAIL: expected 1 emitted signal, got {n} (pre-fix behaviour = 0 with NameError crash)"
    payload = q.get_nowait()
    m = payload["metrics"]
    assert "ml_gatekeeper_score" not in m and "ml_gatekeeper_mode" not in m, "ML vestige keys still in metrics!"
    assert "conviction" in payload and payload["conviction"] == 85.0
    print(f"PASS: signal EMITTED & landed in queue | type={payload['type']} sym={payload['sym']} "
          f"conviction={payload['conviction']} | ml/shadow vestige keys absent")
    print(f"PASS: metrics keys sample: {sorted(list(m.keys()))[:8]} ...")

    # --- Second smoke: no-setup tick path silent, no exceptions ---
    eng.price_action_engine.evaluate_setup = lambda d, v, current_time=0, gamma_regime="NEUTRAL": (
        False, "no_setup", {})
    before = q.qsize()
    eng.on_tick_parsed(FakeTick(tok, (t0 + 2.0) * 1000.0, 12.60))
    assert q.qsize() == before, "no-setup tick unexpectedly emitted!"
    print("PASS: no-setup tick silent & exception-free")
    print("\nSMOKE TEST: ALL PASS — trade-fire pipeline fully operational post-debloat")

if __name__ == "__main__":
    main()
