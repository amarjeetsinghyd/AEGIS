import pandas as pd
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
csv_path = BASE_DIR / "docs" / "AEGIS_TRADE_JOURNAL.csv"
excel_path = BASE_DIR / "docs" / "AEGIS_TRADE_JOURNAL.xlsx"

df = pd.read_csv(csv_path)

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

win_fill = PatternFill(start_color="E2EFDA", end_color="E2EFDA", fill_type="solid")  # soft green
be_fill = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")   # soft blue
loss_fill = PatternFill(start_color="FCE4D6", end_color="FCE4D6", fill_type="solid") # soft red

ws.row_dimensions[1].height = 28

for col_idx, header in enumerate(headers, 1):
    cell = ws.cell(row=1, column=col_idx)
    cell.fill = header_fill
    cell.font = header_font
    cell.alignment = center_align

for row_idx, row in enumerate(df.to_dict(orient="records"), 2):
    ws.row_dimensions[row_idx].height = 55
    res = str(row.get("Result", ""))
    for col_idx, key in enumerate(headers, 1):
        val = row.get(key)
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
print(f"Saved Rich Excel: {excel_path} with {len(df)} trades.")
