import os
import sys
import glob
import json
import time
from datetime import datetime, timezone, timedelta
import pandas as pd
import numpy as np

# Ensure quant-bot root is on sys.path
sys.path.insert(0, '/home/opc/quant-bot')

from AEGIS.core.price_action_engine import PriceActionEngine

def run_replay():
    files = sorted(glob.glob('/home/opc/AEGIS/data/l2_raw/Nifty/aegis_l2_raw_2026-09-28_part_*.parquet'))
    print(f"Found {len(files)} partition files for 2026-09-28.")
    if not files:
        print("No files found!")
        return

    engine = PriceActionEngine(strike_step=50.0, atr_period=14)
    IST = timezone(timedelta(hours=5, minutes=30))

    timeline = []
    setups = []

    last_recorded_bucket = -1

    # We will read each parquet file
    total_ticks = 0
    start_t = time.time()

    for file_idx, fpath in enumerate(files):
        df = pd.read_parquet(fpath)
        if df.empty:
            continue

        # Sort by ts just in case
        df = df.sort_values('ts')
        total_ticks += len(df)

        for row in df.itertuples(index=False):
            # ts is in ms
            ts_sec = float(row.ts) / 1000.0 if row.ts > 1e11 else float(row.ts)
            kind = int(row.kind)
            lp = float(row.lp)
            vol = float(row.vol)

            if kind == 0:  # Spot underlying
                if lp > 0:
                    engine.update_tick(lp, ts_sec)

            elif kind in (1, 2):  # 1 = CE, 2 = PE
                if lp > 0 and vol > 0:
                    side = 0 if kind == 1 else 1
                    strike = float(row.strike)
                    bp1 = float(row.bp1) if not np.isnan(row.bp1) else 0.0
                    sp1 = float(row.sp1) if not np.isnan(row.sp1) else 0.0
                    engine.update_option_tick(
                        sym=str(row.sym),
                        strike=strike,
                        side=side,
                        lp=lp,
                        bp1=bp1,
                        sp1=sp1,
                        vol=vol,
                    )

            # Check if a 1m bar has closed or current minute bucket advanced
            curr_bucket = int(ts_sec // 60)
            if curr_bucket > last_recorded_bucket and len(engine.bars_1m) > 0:
                last_recorded_bucket = curr_bucket
                bar = engine.bars_1m[-1]
                bar_dt = datetime.fromtimestamp(bar.ts_start, tz=IST)
                time_str = bar_dt.strftime("%H:%M")

                pz = engine.get_pain_zone_vwaps(lot_size=65.0, min_bars=0)

                # Check setups
                ok_ce, s_ce, d_ce = engine.evaluate_setup("UP", current_time=ts_sec)
                ok_pe, s_pe, d_pe = engine.evaluate_setup("DOWN", current_time=ts_sec)

                bar_entry = {
                    "time": time_str,
                    "ts": int(bar.ts_start),
                    "open": round(bar.open, 2),
                    "high": round(bar.high, 2),
                    "low": round(bar.low, 2),
                    "close": round(bar.close, 2),
                    "ema20": round(engine.ema_20_1m, 2) if engine.ema_20_1m > 0 else None,
                    "ema50": round(engine.ema_50_1m, 2) if engine.ema_50_1m > 0 else None,
                    "atr": round(engine.current_atr_1m, 2),
                    "vwap_composite": round(engine.current_vwap, 2) if engine.current_vwap > 0 else None,
                    "vwap_call_buy": round(pz["vwap_call_buy"], 2) if pz["vwap_call_buy"] > 0 else None,
                    "vwap_call_sell": round(pz["vwap_call_sell"], 2) if pz["vwap_call_sell"] > 0 else None,
                    "vwap_put_buy": round(pz["vwap_put_buy"], 2) if pz["vwap_put_buy"] > 0 else None,
                    "vwap_put_sell": round(pz["vwap_put_sell"], 2) if pz["vwap_put_sell"] > 0 else None,
                    "active_setup": None,
                }

                if ok_ce:
                    setup_info = {
                        "direction": "CE",
                        "setup": s_ce,
                        "time": time_str,
                        "spot": round(bar.close, 2),
                        "details": d_ce,
                        "pz_score": d_ce.get("pain_zone_score", 0.0),
                        "pz_reason": d_ce.get("pain_zone_reason", ""),
                    }
                    bar_entry["active_setup"] = setup_info
                    setups.append(setup_info)
                elif ok_pe:
                    setup_info = {
                        "direction": "PE",
                        "setup": s_pe,
                        "time": time_str,
                        "spot": round(bar.close, 2),
                        "details": d_pe,
                        "pz_score": d_pe.get("pain_zone_score", 0.0),
                        "pz_reason": d_pe.get("pain_zone_reason", ""),
                    }
                    bar_entry["active_setup"] = setup_info
                    setups.append(setup_info)

                timeline.append(bar_entry)

        if (file_idx + 1) % 5 == 0 or file_idx == len(files) - 1:
            print(f"Processed partition {file_idx+1}/{len(files)} ({total_ticks} ticks)...")

    # Flush final bar if exists
    if engine._curr_1m_bar is not None and (not timeline or timeline[-1]["ts"] != engine._curr_1m_bar.ts_start):
        engine._on_1m_bar_close(engine._curr_1m_bar)
        bar = engine.bars_1m[-1]
        bar_dt = datetime.fromtimestamp(bar.ts_start, tz=IST)
        pz = engine.get_pain_zone_vwaps(lot_size=65.0, min_bars=0)
        timeline.append({
            "time": bar_dt.strftime("%H:%M"),
            "ts": int(bar.ts_start),
            "open": round(bar.open, 2),
            "high": round(bar.high, 2),
            "low": round(bar.low, 2),
            "close": round(bar.close, 2),
            "ema20": round(engine.ema_20_1m, 2),
            "ema50": round(engine.ema_50_1m, 2),
            "atr": round(engine.current_atr_1m, 2),
            "vwap_composite": round(engine.current_vwap, 2),
            "vwap_call_buy": round(pz["vwap_call_buy"], 2) if pz["vwap_call_buy"] > 0 else None,
            "vwap_call_sell": round(pz["vwap_call_sell"], 2) if pz["vwap_call_sell"] > 0 else None,
            "vwap_put_buy": round(pz["vwap_put_buy"], 2) if pz["vwap_put_buy"] > 0 else None,
            "vwap_put_sell": round(pz["vwap_put_sell"], 2) if pz["vwap_put_sell"] > 0 else None,
            "active_setup": None,
        })

    elapsed = time.time() - start_t
    print(f"Replay complete in {elapsed:.2f}s! Total 1m bars: {len(timeline)}, Setups triggered: {len(setups)}")

    diag = engine.get_flow_symmetry_diagnostic()
    print("EOD Flow Symmetry Diagnostic:", diag)

    output = {
        "date": "2026-09-28",
        "instrument": "NIFTY",
        "bars_count": len(timeline),
        "setups_count": len(setups),
        "setups": setups,
        "symmetry_diagnostic": diag,
        "timeline": timeline,
    }

    out_file = '/home/opc/quant-bot/scripts/today_nifty_pain_zone_chart_2026-09-28.json'
    with open(out_file, 'w') as f:
        json.dump(output, f)
    print(f"Saved timeline data to {out_file} ({os.path.getsize(out_file)} bytes)")

if __name__ == '__main__':
    run_replay()
