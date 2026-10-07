"""
ARGUS Stock Trade Journal & Automated Excel Ledger Compiler (v5.0).
Automatically records every completed equity trade into docs/ARGUS_TRADE_JOURNAL.csv
and compiles an institutional styled Excel ledger in docs/ARGUS_TRADE_JOURNAL.xlsx.
"""

from __future__ import annotations

import csv
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List

try:
    from common.logger import get_logger
    logger = get_logger("argus_journal")
except Exception:
    logger = logging.getLogger("argus_journal")

BASE_DIR = Path(__file__).resolve().parents[2]
DOCS_DIR = BASE_DIR / "docs"
CSV_PATH = DOCS_DIR / "ARGUS_TRADE_JOURNAL.csv"
XLSX_PATH = DOCS_DIR / "ARGUS_TRADE_JOURNAL.xlsx"

JOURNAL_HEADERS = [
    "Date",
    "Time",
    "Symbol",
    "Side",
    "Entry Price",
    "Exit Price",
    "Qty",
    "Margin (Rs)",
    "Realized PnL (Rs)",
    "Return on Margin (%)",
    "Exit Reason",
    "Pillar",
    "Conviction",
    "Duration (s)",
]


def init_journal_csv() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    if not CSV_PATH.exists():
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(JOURNAL_HEADERS)
        logger.info(f"[ARGUS_JOURNAL] Initialized {CSV_PATH}")


def record_completed_trade(trade: Dict[str, Any]) -> None:
    """
    Appends a completed trade to docs/ARGUS_TRADE_JOURNAL.csv and updates Excel.
    """
    init_journal_csv()

    now = datetime.now()
    date_str = now.strftime("%d %b %Y")
    time_str = now.strftime("%H:%M:%S")

    entry_px = float(trade.get("entry_price", 0.0))
    exit_px = float(trade.get("exit_price", entry_px))
    qty = int(trade.get("qty", 0))
    side = str(trade.get("type", "BUY")).upper()
    margin = float(trade.get("margin", (qty * entry_px) / 5.0))

    if side == "BUY":
        pnl_rs = (exit_px - entry_px) * qty
    else:
        pnl_rs = (entry_px - exit_px) * qty

    ret_pct = (pnl_rs / margin * 100.0) if margin > 0 else 0.0
    exit_reason = str(trade.get("exit_reason", "MANUAL"))
    pillar = str(trade.get("pillar", trade.get("reason", "P1")))
    conviction = float(trade.get("conviction", 80.0))
    duration_s = int(trade.get("duration_s", 0))

    row = [
        date_str,
        time_str,
        str(trade.get("symbol", "")),
        side,
        f"{entry_px:.2f}",
        f"{exit_px:.2f}",
        qty,
        f"{margin:.2f}",
        f"{pnl_rs:+.2f}",
        f"{ret_pct:+.2f}%",
        exit_reason,
        pillar,
        f"{conviction:.1f}",
        duration_s,
    ]

    try:
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)
        logger.info(f"[ARGUS_JOURNAL] Appended trade for {trade.get('symbol')}: PnL = ₹{pnl_rs:+.2f} ({ret_pct:+.2f}%)")
    except Exception as e:
        logger.error(f"[ARGUS_JOURNAL] Failed to append to CSV: {e}")

    # Compile styled Excel sheet
    compile_argus_excel()


def compile_argus_excel() -> None:
    """
    Compiles docs/ARGUS_TRADE_JOURNAL.csv into styled docs/ARGUS_TRADE_JOURNAL.xlsx.
    """
    if not CSV_PATH.exists():
        return

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter

        rows: List[List[str]] = []
        with open(CSV_PATH, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if not rows:
            return

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "ARGUS Trade Journal"

        # Theme styling (Slate Dark Header)
        hdr_fill = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
        hdr_font = Font(name="Segoe UI", size=11, bold=True, color="F8FAFC")
        win_fill = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
        win_font = Font(name="Segoe UI", size=10, bold=True, color="166534")
        loss_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
        loss_font = Font(name="Segoe UI", size=10, bold=True, color="991B1B")
        regular_font = Font(name="Segoe UI", size=10)

        thin_side = Side(border_style="thin", color="E2E8F0")
        cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

        for r_idx, row in enumerate(rows, start=1):
            for c_idx, val in enumerate(row, start=1):
                cell = ws.cell(row=r_idx, column=c_idx, value=val)
                cell.border = cell_border
                cell.alignment = Alignment(horizontal="center", vertical="center")

                if r_idx == 1:
                    cell.fill = hdr_fill
                    cell.font = hdr_font
                else:
                    cell.font = regular_font
                    # Check PnL columns (col 9 is PnL Rs, col 10 is Return %)
                    if c_idx in (9, 10):
                        clean_val = str(val).replace("%", "").replace("+", "").strip()
                        try:
                            num = float(clean_val)
                            if num > 0:
                                cell.fill = win_fill
                                cell.font = win_font
                            elif num < 0:
                                cell.fill = loss_fill
                                cell.font = loss_font
                        except Exception:
                            pass

        # Adjust column widths
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

        wb.save(XLSX_PATH)
        logger.info(f"[ARGUS_JOURNAL] Compiled {XLSX_PATH}")
    except Exception as e:
        logger.warning(f"[ARGUS_JOURNAL] compile_argus_excel failed ({e})")
