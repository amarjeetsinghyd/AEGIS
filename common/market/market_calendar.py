import time
from datetime import datetime, date, time as dt_time, timedelta
from common.logger import get_logger

logger = get_logger("market_calendar")

# Official NSE / BSE Trading Holidays 2026
NSE_HOLIDAYS_2026 = {
    date(2026, 1, 26),  # Republic Day
    date(2026, 3, 3),   # Holi
    date(2026, 3, 26),  # Ram Navami
    date(2026, 4, 3),   # Good Friday
    date(2026, 4, 14),  # Ambedkar Jayanti
    date(2026, 5, 1),   # Maharashtra Day
    date(2026, 8, 15),  # Independence Day
    date(2026, 10, 2),  # Gandhi Jayanti
    date(2026, 10, 20), # Dussehra
    date(2026, 11, 8),  # Diwali Laxmi Pujan
    date(2026, 11, 9),  # Diwali Balipratipada
    date(2026, 11, 24), # Gurunanak Jayanti
    date(2026, 12, 25), # Christmas
}

MARKET_OPEN_TIME = dt_time(9, 15, 0)
# Cash continuous close historically 15:30; CAS (from Aug 2026) ends continuous
# trading in F&O-eligible stocks at 15:15, then auction. Index/stock F&O stay open to 15:40.
MARKET_CLOSE_TIME = dt_time(15, 30, 0)
TRADE_ENTRY_CUTOFF_TIME = dt_time(15, 20, 0)  # Shared helper; Kronos uses its own tighter gates
PRE_MARKET_TIME = dt_time(8, 55, 0)
POST_MARKET_TIME = dt_time(15, 31, 0)  # Post-market EOD backfill trigger at 03:31 PM IST
# House rule (Kronos): plan final option flatten by 15:10 (before CAS / late-session chaos).
KRONOS_FORCE_FLATTEN_TIME = dt_time(15, 10, 0)
KRONOS_ENTRY_CUTOFF_TIME = dt_time(14, 50, 0)

def is_trading_day(d: date = None) -> bool:
    """Returns True if the given date is a weekday and not an official NSE/BSE holiday."""
    if d is None:
        d = datetime.now().date()
    if d.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    if d in NSE_HOLIDAYS_2026:
        return False
    return True

def is_market_open(now_dt: datetime = None) -> bool:
    """Returns True if current time is within Indian Market Trading Hours (09:15 - 15:30 IST)."""
    if now_dt is None:
        now_dt = datetime.now()
    if not is_trading_day(now_dt.date()):
        return False
    return MARKET_OPEN_TIME <= now_dt.time() <= MARKET_CLOSE_TIME

def can_enter_new_trades(now_dt: datetime = None) -> bool:
    """Returns True if current time is within valid trade entry window (09:15 - 15:20 IST) and Kill Switch is inactive."""
    if now_dt is None:
        now_dt = datetime.now()
    if not is_trading_day(now_dt.date()):
        return False

    import os
    from common.config import get_data_dir
    if os.path.exists("/dev/shm/KILL_SWITCH_ACTIVE") or os.path.exists(os.path.join(get_data_dir(), "KILL_SWITCH_ACTIVE")):
        return False

    return MARKET_OPEN_TIME <= now_dt.time() <= TRADE_ENTRY_CUTOFF_TIME

def is_bot_active_hours(now_dt: datetime = None) -> bool:
    """Returns True if current time is within active bot runtime (08:55 - 15:31 IST), allowing pre-market setup and post-market EOD processing."""
    if now_dt is None:
        now_dt = datetime.now()
    if not is_trading_day(now_dt.date()):
        return False
    return PRE_MARKET_TIME <= now_dt.time() <= POST_MARKET_TIME

def is_pre_market(now_dt: datetime = None) -> bool:
    """Returns True if today is a trading day and time is between 08:55 AM and 09:15 AM IST."""
    if now_dt is None:
        now_dt = datetime.now()
    if not is_trading_day(now_dt.date()):
        return False
    return PRE_MARKET_TIME <= now_dt.time() < MARKET_OPEN_TIME

def get_next_premarket_datetime(now_dt: datetime = None) -> datetime:
    """Returns the exact datetime for the NEXT pre-market wakeup (08:55:00 AM IST)."""
    if now_dt is None:
        now_dt = datetime.now()
        
    current_date = now_dt.date()
    
    # If today is a trading day and current time is before 08:55 AM
    if is_trading_day(current_date) and now_dt.time() < PRE_MARKET_TIME:
        return datetime.combine(current_date, PRE_MARKET_TIME)
        
    # Otherwise, search forward for the next trading day
    candidate_date = current_date + timedelta(days=1)
    while not is_trading_day(candidate_date):
        candidate_date += timedelta(days=1)
        
    return datetime.combine(candidate_date, PRE_MARKET_TIME)

def sleep_until_next_premarket():
    """Calculates sleep duration until next 08:55 AM pre-market and sleeps peacefully."""
    now_dt = datetime.now()
    next_premarket = get_next_premarket_datetime(now_dt)
    sleep_seconds = (next_premarket - now_dt).total_seconds()
    
    hours = sleep_seconds / 3600.0
    logger.info(f"Market is closed for trading. Deep sleeping for {hours:.2f} hours until next pre-market at {next_premarket.strftime('%Y-%m-%d %H:%M:%S')} IST...")
    
    # Sleep in 60-second chunks to allow graceful SIGINT / SIGTERM shutdowns
    target_ts = next_premarket.timestamp()
    while datetime.now().timestamp() < target_ts:
        remaining = target_ts - datetime.now().timestamp()
        if remaining <= 0:
            break
        time.sleep(min(60.0, remaining))
