import logging
import multiprocessing
import re
import warnings
from logging.handlers import RotatingFileHandler
from os import getenv, makedirs
from sys import stdout

from wyzebridge.bridge_utils import env_bool

# The web UI's api key rides in ?api=<key> URLs that werkzeug and go2rtc log; keep it out of the log file.
_API_KEY = re.compile(r"(?i)(\bapi=)[^&\s\"'#]+")


class _RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "api=" in msg:
            record.msg, record.args = _API_KEY.sub(r"\1<redacted>", msg), None
        return True


redact_filter = _RedactFilter()

log_level: int = getattr(logging, env_bool("LOG_LEVEL").upper(), 20)
log_time = "%X" if env_bool("LOG_TIME") else ""

multiprocessing.current_process().name = "WyzeBridge"
logger: logging.Logger = logging.getLogger("WyzeBridge")
logger.setLevel(logging.DEBUG)

warnings.formatwarning = lambda msg, *args, **kwargs: f"WARNING: {msg}"
logging.captureWarnings(True)


def clear_handler(handler: logging.Handler):
    for logger_name in ("WyzeBridge", "", "werkzeug", "py.warnings"):
        target_logger = logging.getLogger(logger_name)
        for existing_handler in target_logger.handlers:
            if type(existing_handler) == type(handler):
                target_logger.removeHandler(existing_handler)


def format_logging(handler: logging.Handler, level: int, date_format: str = ""):
    clear_handler(handler)
    handler.addFilter(redact_filter)
    log_format = "[%(processName)s] %(message)s"
    if level < logging.INFO:
        target_logger = logging.getLogger()
        log_format = f"[%(levelname)s]{log_format}"
        warnings.simplefilter("always")
    else:
        target_logger = logging.getLogger("WyzeBridge")
        logging.getLogger("werkzeug").addHandler(handler)
        logging.getLogger("wyzecam.iotc").addHandler(handler)
        logging.getLogger("py.warnings").addHandler(handler)

    date_format = "%X" if not date_format and level < 20 else date_format
    log_format = f"%(asctime)s {log_format}" if date_format else log_format
    handler.setFormatter(logging.Formatter(log_format, date_format))
    target_logger.addHandler(handler)
    target_logger.setLevel(level)


format_logging(logging.StreamHandler(stdout), log_level, log_time)

# Recent lines for the diagnostics download; same loggers the console shows.
from wyzebridge.diagnostics import log_buffer  # noqa: E402

log_buffer.setLevel(log_level)
log_buffer.addFilter(redact_filter)
for name in ("WyzeBridge", "werkzeug", "wyzecam.iotc", "py.warnings"):
    logging.getLogger(name).addHandler(log_buffer)


if env_bool("LOG_FILE"):
    log_path = getenv("LOG_PATH", "/logs/").rstrip("/") + "/"
    log_file = f"{log_path}debug.log"
    logger.info(f"Logging to file: {log_file}")
    makedirs(log_path, exist_ok=True)
    # Rotate so a debug log can't fill the disk: 3 x 5 MB backups.
    handler = RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3)
    format_logging(handler, logging.DEBUG, "%Y/%m/%d %X")
