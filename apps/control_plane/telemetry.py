"""Bounded structured logs, W3C trace context and CloudWatch embedded metrics."""

import json
import logging
import math
import re
import secrets
import time
from contextvars import ContextVar
from datetime import UTC, datetime

trace_context: ContextVar[str] = ContextVar("traceparent", default="")
METRICS = {
    "RequestDurationMs": "Milliseconds",
    "WebhookDurationMs": "Milliseconds",
    "WorkDurationMs": "Milliseconds",
    "WorkFailed": "Count",
    "CoverageAgeSeconds": "Seconds",
    "DetectionDelaySeconds": "Seconds",
}


def new_trace(parent: str = "") -> str:
    match = re.fullmatch(r"00-([a-f0-9]{32})-[a-f0-9]{16}-[a-f0-9]{2}", parent)
    trace_id = match[1] if match and match[1] != "0" * 32 else secrets.token_hex(16)
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


def metric(name: str, value: float) -> None:
    if name not in METRICS or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid operational metric")
    logging.getLogger("ledgerguard.metrics").info(
        "metric", extra={"event_code": "metric", "metric_name": name, "metric_value": value}
    )


class SafeFormatter(logging.Formatter):
    """Only explicit operational fields can leave the process through logging."""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }
        for key in (
            "event_code",
            "correlation_id",
            "message_id",
            "connector_id",
            "rule_code",
            "duration_ms",
            "error_code",
            "traceparent",
        ):
            value = getattr(record, key, None)
            if isinstance(value, (str, int, float)):
                data[key] = str(value)[:200]
        if trace_context.get():
            data["traceparent"] = trace_context.get()
        data.setdefault("event_code", "framework_event")
        name, value = getattr(record, "metric_name", ""), getattr(record, "metric_value", None)
        if name in METRICS and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
            data["Service"] = "LedgerGuard"
            data[name] = value
            data["_aws"] = {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": "LedgerGuard",
                        "Dimensions": [["Service"]],
                        "Metrics": [{"Name": name, "Unit": METRICS[name]}],
                    }
                ],
            }
        return json.dumps(data, separators=(",", ":"))
