"""Logging utility ensuring all diagnostic logs go to stderr and maintain structured format."""
import sys
import logging
import json
import time
from typing import Any, Dict


class StderrJsonFormatter(logging.Formatter):
    """Formats logs as JSON or concise structured text strictly directed to stderr."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage()
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


def setup_logging(name: str = "mayacardio", level: int = logging.INFO, json_format: bool = False) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    if json_format:
        handler.setFormatter(StderrJsonFormatter())
    else:
        fmt = logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(fmt)

    logger.addHandler(handler)
    logger.propagate = False
    return logger
