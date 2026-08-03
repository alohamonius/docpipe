"""Structured JSON logging and CloudWatch EMF metrics.

Both Lambda and EKS ship stdout to CloudWatch Logs; EMF-formatted lines are
extracted into CloudWatch Metrics automatically — no PutMetricData calls,
no metric API permissions needed.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, TextIO


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "context", None)
        if isinstance(extra, dict):
            entry.update(extra)
        return json.dumps(entry, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def emit_metric(
    name: str,
    value: float,
    unit: str = "Count",
    namespace: str = "docpipe",
    dimensions: dict[str, str] | None = None,
    stream: TextIO = sys.stdout,
) -> None:
    """Write one metric in CloudWatch Embedded Metric Format."""
    dimensions = dimensions or {}
    document: dict[str, Any] = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [list(dimensions.keys())],
                    "Metrics": [{"Name": name, "Unit": unit}],
                }
            ],
        },
        name: value,
        **dimensions,
    }
    stream.write(json.dumps(document) + "\n")
