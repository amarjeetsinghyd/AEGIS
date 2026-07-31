import logging
import os
import sys


class ColorFormatter(logging.Formatter):
    """ANSI color formatter for console output only. File handler stays plain."""
    COLORS = {
        'DEBUG':    '\033[90m',    # Grey
        'INFO':     '\033[36m',    # Cyan
        'WARNING':  '\033[33m',    # Yellow
        'ERROR':    '\033[31m',    # Red
        'CRITICAL': '\033[1;31m',  # Bold Red
    }
    RESET = '\033[0m'

    def format(self, record):
        original_msg = record.getMessage()
        
        # Signal-specific color overrides for console output
        if "CALL" in original_msg and ("TRIGGERED" in original_msg or "Executed" in original_msg or "Entry" in original_msg):
            color = '\033[1;32m'  # Bold Green
        elif "PUT" in original_msg and ("TRIGGERED" in original_msg or "Executed" in original_msg or "Entry" in original_msg):
            color = '\033[1;31m'  # Bold Red
        elif "NEGATIVE_GAMMA" in original_msg or "GEX" in original_msg:
            color = '\033[36m'    # Cyan
        elif "Orderflow Exhaustion" in original_msg or "TRADE CLOSED" in original_msg:
            color = '\033[35m'    # Magenta
        elif "Signal is NEUTRAL" in original_msg or "NEUTRAL:" in original_msg:
            color = '\033[33m'    # Yellow
        else:
            color = self.COLORS.get(record.levelname, '')

        if color:
            record.msg = f"{color}{original_msg}{self.RESET}"
            record.args = ()  # args already consumed by getMessage()

        formatted = super().format(record)
        record.msg = original_msg
        return formatted


def get_logger(name: str) -> logging.Logger:
    """
    Returns a unified logger that prints colored output to console and plain text to file.
    """
    logger = logging.getLogger(name)
    if logger.hasHandlers():
        return logger

    logger.setLevel(logging.INFO)

    # Plain formatter (used by file handler)
    plain_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Color formatter (used by console handler)
    color_formatter = ColorFormatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # Console Handler — colored
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(color_formatter)
    logger.addHandler(console_handler)

    # File Handler — plain
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    logs_dir = os.path.join(base_dir, "astra_bot", "logs")
    os.makedirs(logs_dir, exist_ok=True)

    file_handler = logging.FileHandler(
        os.path.join(logs_dir, "astra_bot.log"),
        encoding="utf-8"
    )
    file_handler.setFormatter(plain_formatter)
    logger.addHandler(file_handler)

    return logger
