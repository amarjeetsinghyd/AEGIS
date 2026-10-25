# friday_check_vps.py — Runtime proof: Friday 30 Oct 2026 pe bot kya karega (VPS LIVE code se)
from datetime import datetime
import os

os.environ.pop("AEGIS_FRIDAY_TRADING_ENABLED", None)

from common.market.market_calendar import (
    is_trading_day,
    get_next_premarket_datetime,
    is_bot_active_hours,
    can_enter_new_trades,
)

fri = datetime(2026, 10, 30, 11, 0, 0)      # Aane wala Friday
thu_close = datetime(2026, 10, 29, 15, 20, 0)  # Thursday post-close (15:17 ke baad)
sat = datetime(2026, 10, 31, 10, 0, 0)      # Saturday

print("Fri 30 Oct  is_trading_day:      ", is_trading_day(fri.date()))
print("Fri 30 Oct  bot_active_hours:    ", is_bot_active_hours(fri))
print("Fri 30 Oct  can_enter_new_trades:", can_enter_new_trades(fri))
print("Thu 29 Oct 15:20 -> next wake:   ", get_next_premarket_datetime(thu_close))
print("Fri 30 Oct 11:00 -> next wake:   ", get_next_premarket_datetime(fri))
print("Sat 31 Oct 10:00 -> next wake:   ", get_next_premarket_datetime(sat))

os.environ["AEGIS_FRIDAY_TRADING_ENABLED"] = "1"
print("--- re-enable path (ENV=1) sirf documentation ke liye: ---")
print("Fri 30 Oct with ENV=1 is_trading_day:", is_trading_day(fri.date()))
