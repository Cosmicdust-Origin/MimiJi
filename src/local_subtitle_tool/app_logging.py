from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "local_subtitle_tool"


def log_directory() -> Path:
    from .appinfo import APP_ID

    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / ".local" / "share"
    return base / APP_ID / "logs"


def setup_logging(directory: Path | None = None) -> Path | None:
    """Configure a rotating UTF-8 log file and return its path.

    Logging must never stop the app from starting, so failures to create the log
    directory are tolerated and reported by returning ``None``.
    """

    logger = logging.getLogger(LOGGER_NAME)
    for handler in logger.handlers:
        if isinstance(handler, RotatingFileHandler):
            return Path(handler.baseFilename)
    target_dir = directory or log_directory()
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / "app.log"
        handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    except OSError:
        return None
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return path


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
