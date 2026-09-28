"""
Check actual option price movement after each PA signal - 22 Sep 2026.
Schema: ts(ms), tok, sym, kind(0=idx,1=CE,2=PE,3=FUT), strike, lp, vol, oi, bp1-5, bq1-5, sp1-5, sq1-5
"""
import polars as pl, glob
from datetime import datetime

DATA_DIR = "/home/opc/AEGIS/data/l2_raw/Nifty"
DATE     = "2026-09-22"

# PA signals within valid trading window (before 15:00)
SIGNALS = [
    ("14:22", "DOWN", "micro_sweep_reclaim", 23394),
    ("14:45", "DOWN", "20_ema_rejection",    23369),
    ("14:49", "DOWN", "20_ema_rejection",    23375),
    ("14:58", "DOWN", "20_ema_rejection",    23366),
]

# Load all parts (only afternoon ones for speed - parts 020-027)
files = sorted(glob.glob(f"{DATA_DIR}/aegis_l2_raw_{DATE}_part_0*.parquet"))
print(f"Loading {len(files)} files...")
dfs = [pl.read_parquet(f) for f in files]
full = pl.concat(dfs)
print(f"Total rows: {len(full):,}")

# kind==2 = PE options
pe_data = full.filter(pl.col("kind") == 2)
print(f"PE rows total: {len(pe_data):,}")
if len(pe_data) == 0:
    print("No PE rows found - check kind column values:")
    print(full["kind"].value_counts())
    exit()

# TS is in milliseconds - confirm
sample_ts = full["ts"][0]
# 1790065195492 ms => / 1000 => 1790065195 sec => datetime check
epoch_sec = sample_ts / 1000
try:
    dt = datetime.fromtimestamp(epoch_sec)
    print(f"TS confirmed as ms: {dt}")
except:
    print(f"TS raw: {sample_ts}")

print()
print("PE options available (unique symbols):")
pe_syms = pe_data["sym"].unique().sort().to_list()
print(pe_syms[:20])
print()

# For each signal: show PE prices at signal time + max price in next 10 min
for sig_time_str, direction, pillar, spot in SIGNALS:
    sig_h, sig_m = map(int, sig_time_str.split(":"))
    sig_dt   = datetime(2026, 9, 22, sig_h, sig_m, 0)
    sig_ms   = int(sig_dt.timestamp() * 1000)
    end_ms   = sig_ms + 10 * 60 * 1000   # +10 minutes
    five_ms  = sig_ms + 5 * 60 * 1000    # +5 minutes

    print("=" * 70)
    print(f"SIGNAL {sig_time_str} | {pillar} | Spot={spot}")
    print(f"Window: {sig_time_str} → +10min")
    print()

    # PE options in window
    window_pe = pe_data.filter(
        (pl.col("ts") >= sig_ms) & (pl.col("ts") <= end_ms)
    )
    print(f"PE ticks in window: {len(window_pe):,}")

    if len(window_pe) == 0:
        print("  No PE data in this window.")
        print()
        continue

    # Group by symbol - get first price (at/near signal) and max price
    result = (
        window_pe.group_by("sym").agg([
            pl.col("strike").first().alias("strike"),
            pl.col("lp").first().alias("price_at_signal"),
            pl.col("lp").max().alias("max_price_10min"),
            pl.col("ts").min().alias("first_ts"),
        ])
    )

    # Also compute 5-min max
    five_min_pe = window_pe.filter(pl.col("ts") <= five_ms)
    if len(five_min_pe) > 0:
        five_result = (
            five_min_pe.group_by("sym").agg([
                pl.col("lp").max().alias("max_price_5min"),
            ])
        )
        result = result.join(five_result, on="sym", how="left")
    else:
        result = result.with_columns(pl.col("max_price_10min").alias("max_price_5min"))

    # Calculate move %
    result = result.with_columns([
        ((pl.col("max_price_10min") - pl.col("price_at_signal")) / pl.col("price_at_signal") * 100).alias("move_pct_10min")
    ])

    # Sort by strike descending (closest to ATM first for PEs)
    result = result.sort("strike", descending=True)

    # Print
    print(f"  {'Symbol':<25} {'Strike':>7} {'@Signal':>8} {'Max5m':>7} {'Max10m':>7} {'Move%':>7} {'4%+?':>6}")
    print(f"  {'-'*25} {'-'*7} {'-'*8} {'-'*7} {'-'*7} {'-'*7} {'-'*6}")
    for row in result.iter_rows(named=True):
        price_sig = row["price_at_signal"] or 0
        max_5m    = row.get("max_price_5min") or price_sig
        max_10m   = row["max_price_10min"] or price_sig
        move      = row["move_pct_10min"] or 0
        hit4      = "*** YES" if move >= 4.0 else "no"
        if price_sig >= 0.5:   # Skip zero-price noise
            print(f"  {str(row['sym']):<25} {row['strike']:>7.0f} {price_sig:>8.2f} {max_5m:>7.2f} {max_10m:>7.2f} {move:>7.1f}% {hit4:>6}")
    print()
