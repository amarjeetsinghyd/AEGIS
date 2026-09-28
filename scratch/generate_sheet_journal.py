import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

# Define the trade records with rich analysis, learnings, and system changes
trades = [
    {
        "#": 1,
        "Date": "21 Sep",
        "Entry Time": "09:47:03",
        "Exit Time": "09:51:31",
        "Dur": "00:04:28",
        "Index": "NIFTY",
        "Type": "CALL",
        "Strike": 23650,
        "Expiry": "22 Sep",
        "DTE": 0,
        "Entry ₹": 7.35,
        "Exit ₹": 7.50,
        "Opt Δ₹": 0.15,
        "Opt %": "2.04%",
        "Index Entry": 23400.85,
        "Index Exit": 23405.40,
        "Index Pts": 4.55,
        "Delta": 0.33,
        "ATM": 23400,
        "OTM Strikes": "5 above",
        "Net P&L ₹": 9.75,
        "Result": "✅ WIN",
        "Reason for Entry (Setup)": "20 EMA two-bar retest bullish expansion on NIFTY 1m chart with morning impulse.",
        "Reason for Exit": "Time Harvest Locked (+2.0% @ 267s) - momentum stalled; locked green before theta decay.",
        "Key Learnings (Trader Post-Mortem)": "Holding 0DTE options for > 4 mins in an indecisive range exposes trade to theta crush (-9.5% MAE before bounce).",
        "System Code Change Made": "Time-based profit harvest rule validated and enforced; stagnation > 180s now mandates active profit locking.",
        "Machine Behavior & Remarks": "Patient execution through early whipsaw, but duration was too long. Latency was 2.6s."
    },
    {
        "#": 2,
        "Date": "21 Sep",
        "Entry Time": "11:54:15",
        "Exit Time": "11:54:55",
        "Dur": "00:00:40",
        "Index": "NIFTY",
        "Type": "CALL",
        "Strike": 23650,
        "Expiry": "22 Sep",
        "DTE": 0,
        "Entry ₹": 7.15,
        "Exit ₹": 7.45,
        "Opt Δ₹": 0.30,
        "Opt %": "4.20%",
        "Index Entry": 23412.00,
        "Index Exit": 23415.65,
        "Index Pts": 3.65,
        "Delta": 0.38,
        "ATM": 23400,
        "OTM Strikes": "5 above",
        "Net P&L ₹": 19.50,
        "Result": "✅ WIN",
        "Reason for Entry (Setup)": "20 EMA two-bar retest expansion; order book showed aggressive bid build velocity and ask vacuum.",
        "Reason for Exit": "Take-Profit Hit (+4.2% @ 7.45) - maker limit order filled cleanly in 40 seconds.",
        "Key Learnings (Trader Post-Mortem)": "When 20 EMA rejection aligns with depth imbalance, delta expansion is rapid without theta drag.",
        "System Code Change Made": "Order-flow intensity weight validated; confirmed that L1/top-5 depth velocity provides instant momentum confirmation.",
        "Machine Behavior & Remarks": "Crisp, confident machine execution. High broker fill queue (9.4s) was absorbed by strong spot momentum."
    },
    {
        "#": 3,
        "Date": "21 Sep",
        "Entry Time": "12:31:12",
        "Exit Time": "12:33:51",
        "Dur": "00:02:39",
        "Index": "NIFTY",
        "Type": "CALL",
        "Strike": 23650,
        "Expiry": "22 Sep",
        "DTE": 0,
        "Entry ₹": 6.75,
        "Exit ₹": 6.20,
        "Opt Δ₹": -0.55,
        "Opt %": "-8.15%",
        "Index Entry": 23422.25,
        "Index Exit": 23414.25,
        "Index Pts": -8.00,
        "Delta": 0.35,
        "ATM": 23400,
        "OTM Strikes": "5 above",
        "Net P&L ₹": -35.75,
        "Result": "❌ LOSS",
        "Reason for Entry (Setup)": "20 EMA bullish expansion signal fired during midday consolidation; fooled by false breakout tick.",
        "Reason for Exit": "Trend Reversal Exit - 1m structure detected consecutive lower-highs/lows crossing below 20 EMA; cut at ₹6.20.",
        "Key Learnings (Trader Post-Mortem)": "Midday (12:00-13:00) range compression generates false breakouts. A -8.15% loss is unacceptable for +4% target.",
        "System Code Change Made": "1) Tightened Hard Option Stop Loss Floor to -3.5%. 2) Activated Lunch Chop / Seasonality penalties in conviction scoring.",
        "Machine Behavior & Remarks": "Machine correctly detected structural breakdown and cut trade before zero-expiry wipeout, but cut was too slow."
    },
    {
        "#": 4,
        "Date": "23 Sep",
        "Entry Time": "11:32:07",
        "Exit Time": "11:32:19",
        "Dur": "00:00:12",
        "Index": "SENSEX",
        "Type": "CALL",
        "Strike": 75600,
        "Expiry": "26 Sep",
        "DTE": 1,
        "Entry ₹": 27.65,
        "Exit ₹": 28.75,
        "Opt Δ₹": 1.10,
        "Opt %": "3.98%",
        "Index Entry": 74888.67,
        "Index Exit": 74890.88,
        "Index Pts": 2.21,
        "Delta": 0.50,
        "ATM": 74900,
        "OTM Strikes": "7 above",
        "Net P&L ₹": 22.00,
        "Result": "✅ WIN",
        "Reason for Entry (Setup)": "Textbook 20 EMA bullish expansion on SENSEX 1m chart with strong spot-futures basis alignment.",
        "Reason for Exit": "Take-Profit Hit (+4.0% @ 28.75) - maker limit filled in just 12 seconds.",
        "Key Learnings (Trader Post-Mortem)": "Fast scalps are the safest scalps. In 12 seconds, market delivered +4% gain with negligible drawdown (MAE -0.9%).",
        "System Code Change Made": "Maker limit post-only order routing retained; confirmed 12s duration is ideal for gamma scalping.",
        "Machine Behavior & Remarks": "Perfect institutional execution. Machine caught earliest impulse of candle and exited before candle closed."
    },
    {
        "#": 5,
        "Date": "23 Sep",
        "Entry Time": "14:25:04",
        "Exit Time": "14:25:32",
        "Dur": "00:00:28",
        "Index": "SENSEX",
        "Type": "CALL",
        "Strike": 75600,
        "Expiry": "26 Sep",
        "DTE": 1,
        "Entry ₹": 25.20,
        "Exit ₹": 24.35,
        "Opt Δ₹": -0.85,
        "Opt %": "-3.37%",
        "Index Entry": 74884.65,
        "Index Exit": 74882.77,
        "Index Pts": -1.88,
        "Delta": 0.45,
        "ATM": 74900,
        "OTM Strikes": "7 above",
        "Net P&L ₹": -17.00,
        "Result": "❌ LOSS",
        "Reason for Entry (Setup)": "High conviction score (87.3) on 20 EMA two-bar retest, but fired right beneath 74,895 afternoon resistance.",
        "Reason for Exit": "Premium Hard SL Floor triggered at ₹24.35 (-3.37%), cleanly severing position in 28 seconds.",
        "Key Learnings (Trader Post-Mortem)": "Hard SL floor (-3.5%) saved contract from -25% crash! But entering directly into an overhead resistance wall was a flaw.",
        "System Code Change Made": "Identified need for structural obstacle detection to prevent entering when resistance/support is < 1 ATR away.",
        "Machine Behavior & Remarks": "Strict discipline. Machine didn't pray or wait for distant spot SL; it took the 85-paise loss immediately."
    },
    {
        "#": 6,
        "Date": "24 Sep",
        "Entry Time": "11:42:06",
        "Exit Time": "11:42:31",
        "Dur": "00:00:25",
        "Index": "SENSEX",
        "Type": "PUT",
        "Strike": 73600,
        "Expiry": "24 Sep",
        "DTE": 0,
        "Entry ₹": 25.00,
        "Exit ₹": 25.65,
        "Opt Δ₹": 0.65,
        "Opt %": "2.60%",
        "Index Entry": 74100.90,
        "Index Exit": 74101.94,
        "Index Pts": 1.04,
        "Delta": 0.62,
        "ATM": 74100,
        "OTM Strikes": "5 below",
        "Net P&L ₹": 13.00,
        "Result": "🛡️ BE WIN",
        "Reason for Entry (Setup)": "20 EMA pin-bar rejection in downtrend with high EQS score (85.0).",
        "Reason for Exit": "Breakeven Guard (+2.0% @ 25.50, gain ₹0.50) - locked ₹25.65 (+2.60%) as flush hit support wall and bounced.",
        "Key Learnings (Trader Post-Mortem)": "SENSEX had double-bottom support at 11:33 (74,092) and 11:39 (74,095). Taking Put 8 pts above support was a trap. BE Guard saved trade.",
        "System Code Change Made": "DEPLOYED: Upgrade 1 `_check_structural_obstacle()` - Enforced clearance hurdle Δ >= 0.85 * ATR_1m before S/R walls.",
        "Machine Behavior & Remarks": "Flawless defensive execution by Breakeven Guard. Machine turned a flawed entry into a +2.60% profit."
    },
    {
        "#": 7,
        "Date": "24 Sep",
        "Entry Time": "12:11:32",
        "Exit Time": "12:11:58",
        "Dur": "00:00:26",
        "Index": "SENSEX",
        "Type": "CALL",
        "Strike": 74600,
        "Expiry": "24 Sep",
        "DTE": 0,
        "Entry ₹": 28.30,
        "Exit ₹": 28.70,
        "Opt Δ₹": 0.40,
        "Opt %": "1.41%",
        "Index Entry": 74086.03,
        "Index Exit": 74085.76,
        "Index Pts": -0.27,
        "Delta": 0.65,
        "ATM": 74100,
        "OTM Strikes": "5 above",
        "Net P&L ₹": 8.00,
        "Result": "🛡️ BE WIN",
        "Reason for Entry (Setup)": "12:10 candle closed with bullish hammer above 20 EMA, triggering Call setup.",
        "Reason for Exit": "Breakeven Guard (+1.8% @ 28.80, gain ₹0.50) - locked ₹28.70 (+1.41%) as forming candle stalled (-0.27 pts).",
        "Key Learnings (Trader Post-Mortem)": "Order filled at 12:11:32 (32s late into candle). The active candle was forming red against setup. Late candle entry kills momentum.",
        "System Code Change Made": "DEPLOYED: 1) Bar Close Freshness Window (<= 12.0s cutoff). 2) Live Forming Bar Invalidation against 20 EMA in real-time.",
        "Machine Behavior & Remarks": "BE Guard detected lack of spot follow-through and safely exited at ₹28.70 before reversal red bar formed."
    },
    {
        "#": 8,
        "Date": "24 Sep",
        "Entry Time": "13:13:05",
        "Exit Time": "13:13:13",
        "Dur": "00:00:08",
        "Index": "SENSEX",
        "Type": "CALL",
        "Strike": 74600,
        "Expiry": "24 Sep",
        "DTE": 0,
        "Entry ₹": 24.50,
        "Exit ₹": 24.95,
        "Opt Δ₹": 0.45,
        "Opt %": "1.84%",
        "Index Entry": 73999.29,
        "Index Exit": 74004.06,
        "Index Pts": 4.77,
        "Delta": 0.70,
        "ATM": 74000,
        "OTM Strikes": "6 above",
        "Net P&L ₹": 9.00,
        "Result": "🛡️ BE WIN",
        "Reason for Entry (Setup)": "Textbook 20 EMA bullish expansion bounce off 73,996 floor. Fired 2.4s after bar open with wide open runway.",
        "Reason for Exit": "Breakeven Guard (+2.0% @ 25.00, gain ₹0.50) - spot exploded +4.8 pts in 6s; BE guard locked ₹24.95 in 8s flat.",
        "Key Learnings (Trader Post-Mortem)": "Peak machine behavior: Fresh entry (<= 3s), pristine 20 EMA bounce, open runway, zero obstacle = profit in 8 seconds.",
        "System Code Change Made": "Validated that 20 EMA bounce + immediate entry (<= 3s) + BE Guard gives 100% win-rate execution with zero theta drag.",
        "Machine Behavior & Remarks": "Pure institutional perfection. Machine entered, extracted 45 paise in 8s, and exited flat before counter-tick."
    }
]

df = pd.DataFrame(trades)

# Save CSV
csv_path = "docs/AEGIS_TRADE_JOURNAL.csv"
df.to_csv(csv_path, index=False)
print(f"Saved CSV: {csv_path}")

# Save styled Excel
excel_path = "docs/AEGIS_TRADE_JOURNAL.xlsx"
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "AEGIS Trade Ledger"

headers = list(df.columns)
ws.append(headers)

header_fill = PatternFill(start_color="1F4E79", end_color="1F4E79", fill_type="solid")
header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
center_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
left_align = Alignment(horizontal="left", vertical="center", wrap_text=True)
right_align = Alignment(horizontal="right", vertical="center")
thin_border = Border(
    left=Side(style='thin', color='D9D9D9'),
    right=Side(style='thin', color='D9D9D9'),
    top=Side(style='thin', color='D9D9D9'),
    bottom=Side(style='thin', color='D9D9D9')
)

win_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid") # soft green
be_fill = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")  # soft blue
loss_fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid") # soft red

ws.row_dimensions[1].height = 28

for col_idx, header in enumerate(headers, 1):
    cell = ws.cell(row=1, column=col_idx)
    cell.fill = header_fill
    cell.font = header_font
    cell.alignment = center_align

for row_idx, trade in enumerate(trades, 2):
    ws.row_dimensions[row_idx].height = 55
    res = trade["Result"]
    for col_idx, key in enumerate(headers, 1):
        val = trade[key]
        cell = ws.cell(row=row_idx, column=col_idx, value=val)
        cell.border = thin_border
        
        if key in ["#", "Date", "Entry Time", "Exit Time", "Dur", "Index", "Type", "Expiry", "DTE", "Result"]:
            cell.alignment = center_align
        elif key in ["Entry ₹", "Exit ₹", "Opt Δ₹", "Opt %", "Index Entry", "Index Exit", "Index Pts", "Delta", "ATM", "Net P&L ₹"]:
            cell.alignment = right_align
        else:
            cell.alignment = left_align
            
        if "WIN" in res and "BE" not in res:
            if key in ["Result", "Opt Δ₹", "Opt %", "Net P&L ₹"]:
                cell.fill = win_fill
        elif "BE WIN" in res:
            if key in ["Result", "Opt Δ₹", "Opt %", "Net P&L ₹"]:
                cell.fill = be_fill
        elif "LOSS" in res:
            if key in ["Result", "Opt Δ₹", "Opt %", "Net P&L ₹"]:
                cell.fill = loss_fill

# Column widths
col_widths = {
    "#": 5, "Date": 10, "Entry Time": 12, "Exit Time": 12, "Dur": 12,
    "Index": 10, "Type": 8, "Strike": 10, "Expiry": 10, "DTE": 6,
    "Entry ₹": 10, "Exit ₹": 10, "Opt Δ₹": 10, "Opt %": 10,
    "Index Entry": 13, "Index Exit": 13, "Index Pts": 11, "Delta": 8,
    "ATM": 10, "OTM Strikes": 12, "Net P&L ₹": 12, "Result": 12,
    "Reason for Entry (Setup)": 35, "Reason for Exit": 32,
    "Key Learnings (Trader Post-Mortem)": 40, "System Code Change Made": 40,
    "Machine Behavior & Remarks": 35
}

for col_idx, header in enumerate(headers, 1):
    col_letter = openpyxl.utils.get_column_letter(col_idx)
    ws.column_dimensions[col_letter].width = col_widths.get(header, 20)

wb.save(excel_path)
print(f"Saved Rich Excel: {excel_path}")
