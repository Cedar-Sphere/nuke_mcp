import logging
import sys

_MAIN_LOGGER_NAME = "nuke_mcp"
_MAIN_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def _configure_main_logging() -> logging.Logger:
    logger = logging.getLogger(_MAIN_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_MAIN_LOG_FORMAT))
        logger.addHandler(handler)
    return logger


def main():
    """Entry point for the nuke-mcp package."""
    logger = _configure_main_logging()
    logger.debug("Starting Nuke MCP entrypoint")
    from nuke_mcp_server import main as server_main

    logger.debug("Imported server module")
    server_main()


if __name__ == "__main__":
    logger = _configure_main_logging()
    try:
        main()
    except Exception:
        logger.exception("Fatal error during import or execution")
        sys.exit(1)
