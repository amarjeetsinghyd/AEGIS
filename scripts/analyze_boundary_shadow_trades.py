import json
import glob
import os

files = sorted(glob.glob("scratch/aegis_boundary_shadow_trades_*.jsonl"))
print(f"Analyzing {len(files)} shadow trade files:")

trades = []
for f in files:
    with open(f, "r") as fp:
        for line in fp:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except Exception:
                    pass

print(f"\nTotal Shadow Trades Found: {len(trades)}")
print("-" * 100)

winners = [t for t in trades if t.get("outcome") == "WINNER"]
losses = [t for t in trades if t.get("outcome") == "LOSSER" or t.get("outcome") == "LOSS"]
expired = [t for t in trades if t.get("outcome") == "EXPIRED"]
active = [t for t in trades if t.get("is_active")]

win_rate = (len(winners) / len(trades) * 100) if trades else 0.0
print(f"Overall Performance: {len(winners)} Wins | {len(losses)} Losses | {len(expired)} Expired | Win Rate: {win_rate:.1f}%")
print("-" * 100)

for i, t in enumerate(trades, 1):
    trade_id = t.get("trade_id")
    boundary = t.get("boundary")
    direction = t.get("direction")
    entry_time = t.get("entry_time_str")
    spot_entry = t.get("spot_entry")
    opt_symbol = t.get("opt_symbol")
    opt_entry = t.get("opt_entry")
    opt_tp = t.get("opt_tp_price")
    opt_sl = t.get("opt_sl_price")
    mfe_30s = t.get("opt_mfe_30s_pct", 0.0)
    mfe_peak = t.get("opt_peak_mfe_pct", 0.0)
    mae_peak = t.get("opt_peak_mae_pct", 0.0)
    duration = t.get("duration_sec", 0.0)
    outcome = t.get("outcome")
    exit_reason = t.get("exit_reason")

    print(f"Trade #{i}: [{trade_id}] Boundary: {boundary} | Dir: {direction} | Time: {entry_time}")
    print(f"   Spot: {spot_entry:.2f} | Opt: {opt_symbol} @ Rs.{opt_entry:.2f} (TP: Rs.{opt_tp:.2f}, SL: Rs.{opt_sl:.2f})")
    print(f"   Peak MFE: {mfe_peak:+.2f}% | Peak MAE: {mae_peak:+.2f}% | 30s MFE: {mfe_30s:+.2f}%")
    print(f"   Outcome: {outcome} ({exit_reason}) in {duration:.1f}s")
    print("-" * 100)
