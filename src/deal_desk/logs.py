"""JSON logs on Cloud Run (parsed by Cloud Logging: severity, message, fields), plain text locally."""

import json
import logging
import os
import sys
from datetime import UTC, datetime


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {"severity": record.levelname, "message": record.getMessage(),
                 "time": datetime.fromtimestamp(record.created, UTC).isoformat(), "logger": record.name,
                 **getattr(record, "fields", {})}
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    on_cloud_run = "K_SERVICE" in os.environ  # set by Cloud Run
    handler.setFormatter(_JsonFormatter() if on_cloud_run else logging.Formatter("%(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
