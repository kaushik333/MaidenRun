import logging
import sys
import uuid
from pathlib import Path

from app.core.config import settings

RUN_ID = uuid.uuid4().hex[:8]


def setup_logging() -> logging.Logger:
    run_dir = Path(settings.log_dir) / settings.user_id / RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("maidenrun")
    logger.setLevel(settings.log_level)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = logging.FileHandler(run_dir / "backend.log")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    logger.addHandler(stream_handler)

    return logger


logger = setup_logging()
