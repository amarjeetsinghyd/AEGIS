import sys, json
sys.path.insert(0, '/home/opc/stock_order_flow')
from research_analytics import load_daily_snapshots

# Load all snapshots to see what's there
try:
    df = load_daily_snapshots("vortex", None, None)
    print('Total rows:', len(df))
    print('Columns:', list(df.columns))
    if 'symbol' in df.columns:
        print('Symbols:', df['symbol'].unique().tolist())
    if 'date' in df.columns:
        print('Dates:', df['date'].unique().tolist())
    print(df.head())
except Exception as e:
    print('Error:', e)
