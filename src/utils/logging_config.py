"""Structured JSON logging configuration for NewsEngine.

This module configures Python's logging module to output structured JSON logs
suitable for log aggregation systems like ELK, Loki, or CloudWatch.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from datetime import datetime, timezone
from typing import Any


class JsonFormatter(logging.Formatter):
    """Custom JSON formatter for structured logging."""
    
    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a JSON object."""
        log_entry = {
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }
        
        # Add exception info if present
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        
        # Add extra fields if present
        if hasattr(record, "__dict__"):
            for key, value in record.__dict__.items():
                if key not in log_entry and key not in [
                    "name", "msg", "args", "levelname", "levelno", "pathname",
                    "filename", "module", "lineno", "funcName", "created",
                    "msecs", "relativeCreated", "thread", "threadName",
                    "processName", "process", "getMessage", "exc_info",
                    "exc_text", "stack_info"
                ]:
                    log_entry[key] = value
        
        return json.dumps(log_entry, ensure_ascii=False)


def setup_logging(level: str = "INFO", log_file: str = "") -> None:
    """Configure structured JSON logging.
    
    Args:
        level: Logging level as string (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional file path for logging. If empty, logs to stdout only.
    """
    # Validate log level
    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        raise ValueError(f"Invalid log level: {level}")
    
    # Get the root logger
    root_logger = logging.getLogger(None)
    
    # Clear existing handlers to avoid duplicates
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()
    
    # Create formatters
    json_formatter = JsonFormatter()
    
    # Create console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(json_formatter)
    
    # Add console handler
    root_logger.addHandler(console_handler)
    root_logger.setLevel(numeric_level)
    
    # Add file handler if log_file is specified
    if log_file:
        try:
            # Create directory if it doesn't exist
            log_dir = os.path.dirname(log_file)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            
            # Create file handler with rotation
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10*1024*1024, backupCount=5  # 10MB files, keep 5 backups
            )
            file_handler.setFormatter(json_formatter)
            root_logger.addHandler(file_handler)
        except OSError as e:
            # Fallback to console-only logging if file creation fails
            logging.warning(f"Could not create log file {log_file}: {e}. Continuing with console logging only.")
    
    logging.info(
        "Logging configured: level=%s, output=%s",
        level.upper(),
        log_file or "stdout only",
        extra={"event": "logging_configured"},
    )


def get_logger(name: str) -> logging.Logger:
    """Get a logger, ensuring at least a basic console handler exists on root.

    Adds a minimal stdout handler with ``JsonFormatter`` to the root logger
    if none exists yet.  This is a lightweight bootstrap so that module-level
    loggers created before ``setup_logging()`` can still emit records.
    The full ``setup_logging()`` call (from main) will replace this handler
    with the user-configured level and optional file output.

    Args:
        name: Logger name, typically ``__name__``.

    Returns:
        A ``Logger`` instance that emits JSON-formatted records.
    """
    root = logging.getLogger(None)
    if not root.handlers:
        # Lightweight bootstrap: add a handler without logging a "configured" message
        json_formatter = JsonFormatter()
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(json_formatter)
        root.addHandler(console_handler)
        root.setLevel(logging.INFO)
    return logging.getLogger(name)


__all__ = ["setup_logging", "get_logger"]