import sys
import os

# Add common to path to import shoonya_adapter
sys.path.append("C:\\Users\\Amarjeet Singh\\quant-bot")

from common.broker.shoonya_adapter import ShoonyaAdapter

def check_pending_orders():
    try:
        adapter = ShoonyaAdapter()
        orders = adapter.api.get_order_book()
        if not orders:
            print("No orders found today.")
            return

        pending = []
        for o in orders:
            # typical shoonya status: OPEN, TRIGGER_PENDING, REJECTED, COMPLETE, CANCELLED
            if o.get("status") in ["OPEN", "TRIGGER_PENDING"]:
                pending.append(o)
                
        if not pending:
            print("All orders are CLOSED/CANCELLED/REJECTED. No pending orders.")
        else:
            print(f"Found {len(pending)} PENDING orders:")
            for p in pending:
                print(f"ID: {p.get('norenordno')} | Symbol: {p.get('tsym')} | Status: {p.get('status')} | Price: {p.get('prc')}")
    except Exception as e:
        print(f"Error checking orders: {e}")

if __name__ == "__main__":
    check_pending_orders()
