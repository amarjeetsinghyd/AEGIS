import os
from dotenv import load_dotenv

# Base Directory (Project Root)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

# Load env variables from C:\Kronos_Testing\.env
load_dotenv(os.path.join(BASE_DIR, ".env"))

# Data & Logs Directories
DATA_DIR = os.path.join(BASE_DIR, "astra_bot", "data")
LOGS_DIR = os.path.join(BASE_DIR, "astra_bot", "logs")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Database Paths
STRIKE_DB_PATH = os.path.join(DATA_DIR, "strike_research.db")

# Option Selection Premium Limits
NIFTY_PREMIUM_MIN = 15
NIFTY_PREMIUM_MAX = 120
SENSEX_PREMIUM_MIN = 20
SENSEX_PREMIUM_MAX = 200

# Entry Marketable Limit Order Buffer Constants
ENTRY_LIMIT_BUFFER_PCT = 0.025  # 2.5% dynamic percentage buffer
ENTRY_LIMIT_MIN_BUFFER_PTS = 3.0 # ₹3.00 minimum fixed buffer floor
OPTION_TICK_SIZE = 0.05          # Exchange tick size step

# Capital Engine Setup
INITIAL_PAPER_CAPITAL = 2500.0

# Bot Trading Controls
TIME_FRAME = "5m"                # Standard 5-minute candles
PREDICTION_THRESHOLD = 0.0025    # 0.25% predicted move required to trade
COOLDOWN_MINUTES = 15            # 15 minutes cooldown between trades
UNLIMITED_DAILY_TRADES = True    # No daily count restrictions

# Model Configurations
KRONOS_MODEL_KEY = "NeoQuasar/Kronos-base"
KRONOS_TOKENIZER_KEY = "NeoQuasar/Kronos-Tokenizer-base"
KRONOS_CONTEXT_LENGTH = 512

# Broker Selection
BROKER = os.getenv("BROKER", "ANGEL")  # Target AngelOne API as default
ACTIVE_BROKER = BROKER.upper()

# Execution Controls & Real Money Live Trading Switch
ENABLE_LIVE_BROKERAGE_EXECUTION = os.getenv("ENABLE_LIVE_BROKERAGE_EXECUTION", "False").upper() == "TRUE"

# OI Reversal Early-Exit & Order Parameters
OI_REVERSAL_EXIT_ENABLED = os.getenv("OI_REVERSAL_EXIT_ENABLED", "False").upper() == "TRUE" # Shadow logging mode for threshold calibration
OI_REVERSAL_EXIT_THRESHOLD = 50000.0   # Net Option Delta OI reversal limit for early exit
OI_REVERSAL_MIN_HOLD_SEC = 60.0         # 60-second (1 minute) minimum hold floor before OI reversal exit can fire
EXIT_LIMIT_GRACE_PERIOD_SEC = 5.0      # Grace period for limit exit fill before market fallback
DEFAULT_PRODUCT_TYPE = "N"              # "N" = NRML / Regular (Shoonya requires NRML for SENSEX/NIFTY options)

# Lot Sizes — Single source of truth. SEBI-mandated as of 2026-07.
# WARNING: Update here if SEBI revises lot sizes. Do NOT hardcode elsewhere.
NIFTY_LOT_SIZE = 65    # NIFTY 50 options: 65 shares per lot
SENSEX_LOT_SIZE = 20   # BSE SENSEX options: 20 shares per lot


