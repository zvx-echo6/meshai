"""Enhanced logging setup for MeshAI."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .config import LoggingConfig

# Custom log levels for message tracking
MESSAGE_IN = 25  # Between INFO (20) and WARNING (30)
MESSAGE_OUT = 26
API_CALL = 15  # Between DEBUG (10) and INFO (20)

logging.addLevelName(MESSAGE_IN, "MSG_IN")
logging.addLevelName(MESSAGE_OUT, "MSG_OUT")
logging.addLevelName(API_CALL, "API")


class MeshAILogger(logging.Logger):
    """Custom logger with message tracking methods."""

    def message_in(self, sender: str, text: str, channel: int = 0):
        """Log an incoming message."""
        if self.isEnabledFor(MESSAGE_IN):
            self._log(MESSAGE_IN, f"[CH{channel}] {sender}: {text}", ())

    def message_out(self, recipient: str, text: str, channel: int = 0):
        """Log an outgoing message."""
        if self.isEnabledFor(MESSAGE_OUT):
            self._log(MESSAGE_OUT, f"[CH{channel}] -> {recipient}: {text}", ())

    def api_call(self, backend: str, model: str, tokens: Optional[int] = None):
        """Log an API call."""
        if self.isEnabledFor(API_CALL):
            msg = f"API call to {backend}/{model}"
            if tokens:
                msg += f" ({tokens} tokens)"
            self._log(API_CALL, msg, ())


# Set the custom logger class
logging.setLoggerClass(MeshAILogger)


def setup_logging(config: LoggingConfig, verbose: bool = False) -> logging.Logger:
    """Configure logging based on config.

    Args:
        config: Logging configuration
        verbose: Override to enable DEBUG level

    Returns:
        The configured root logger
    """
    # Determine log level
    if verbose:
        level = logging.DEBUG
    else:
        level_name = config.level.upper()
        level = getattr(logging, level_name, logging.INFO)

    # Create formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Clear existing handlers
    root_logger.handlers.clear()

    # Console handler (always)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler (if configured)
    if config.file:
        log_path = Path(config.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=config.max_size_mb * 1024 * 1024,
            backupCount=config.backup_count,
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # Configure message logging levels based on config
    meshai_logger = logging.getLogger("meshai")

    if not config.log_messages:
        # Disable message logging
        meshai_logger.addFilter(lambda r: r.levelno not in (MESSAGE_IN, MESSAGE_OUT))

    if not config.log_api_calls:
        # Disable API call logging (it's DEBUG level anyway)
        meshai_logger.addFilter(lambda r: r.levelno != API_CALL)

    return root_logger


def get_logger(name: str = "meshai") -> MeshAILogger:
    """Get a MeshAI logger instance.

    Args:
        name: Logger name (will be prefixed with 'meshai.')

    Returns:
        Configured logger instance
    """
    if not name.startswith("meshai"):
        name = f"meshai.{name}"
    return logging.getLogger(name)
