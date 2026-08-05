import sys
import os

# Ensure the root is in sys.path
sys.path.insert(0, r"C:\Users\Amarjeet Singh\quant-bot")

try:
    from kronos_bot.strategy.signal_generator import KronosSignalGenerator
    from kronos_bot.config.settings import PREDICTION_THRESHOLD
    
    print(f"PREDICTION_THRESHOLD: {PREDICTION_THRESHOLD}")
    generator = KronosSignalGenerator(device='cpu')
    print("Model loaded successfully.")
except Exception as e:
    import traceback
    print(f"Error loading model: {e}")
    traceback.print_exc()
