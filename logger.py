import logging
import os
import sys


class ColorFormatter(logging.Formatter):
    """ANSI color formatter for console output only. File handler stays plain."""

    COLORS = {
        "DEBUG": "\033[90m",
        "INFO": "\033[36m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }
    RESET = "\033[0m"

    def format(self, record):
        original_msg = record.getMessage()

        if "CALL" in original_msg and ("TRIGGERED" in original_msg or "Executed" in original_msg or "Entry" in original_msg):
            color = "\033[1;32m"
        elif "PUT" in original_msg and ("TRIGGERED" in original_msg or "Executed" in original_msg or "Entry" in original_msg):
            color = "\033[1;31m"
        elif "NEGATIVE_GAMMA" in original_msg or "GEX" in original_msg:
            color = "\033[36m"
        elif "Orderflow Exhaustion" in original_msg or "TRADE CLOSED" in original_msg:
            color = "\033[35m"
        elif "Signal is NEUTRAL" in original_msg or "NEUTRAL:" in original_msg:
            color = "\033[33m"
        else:
            color = self.COLORS.get(record.levelname, "")

        if color:
            record.msg = f"{color}{original_msg}{self.RESET}"
            record.args = ()

        formatted = super().format(record)
        record.msg = original_msg
        return formatted


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.hasHandlers():
        return logger

    logger.setLevel(logging.INFO)

    plain_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    color_formatter = ColorFormatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(color_formatter)
    logger.addHandler(console_handler)

    try:
        from config.settings import get_logs_dir

        logs_dir = get_logs_dir()
        runtime = os.environ.get("BOT_RUNTIME", "app")
        log_name = f"{runtime}_bot.log" if not runtime.endswith("_bot") else f"{runtime}.log"
        if runtime in ("astra", "kronos"):
            log_name = f"{runtime}_bot.log"
        elif runtime == "hub":
            log_name = "shoonya_data_hub.log"
        else:
            log_name = f"{runtime}.log"
    except Exception:
        logs_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs")
        log_name = "app.log"

    os.makedirs(logs_dir, exist_ok=True)
    file_handler = logging.FileHandler(os.path.join(logs_dir, log_name), encoding="utf-8")
    file_handler.setFormatter(plain_formatter)
    logger.addHandler(file_handler)

    return logger
