import logging
import sys

from app.core.config import settings


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("feedback_stack")
    logger.setLevel(settings.log_level)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    return logger


logger = setup_logging()
