"""Prometheus metrics endpoint for the digital employee runtime.

Outputs the Prometheus text exposition format without any external
dependency (prometheus_client is not in the runtime image's venv).
Metrics are process-local counters; they reset on restart, which is
standard for Prometheus scraping.
"""
import time
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Response

router = APIRouter(tags=["metrics"])

_START_TIME = time.time()

# Simple in-process metric store: name -> {labels_key -> value}
_counters: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
_gauges: dict[str, float] = {}


def _inc(name: str, labels: dict[str, str] = None, value: float = 1):
    key = ",".join(f'{k}="{v}"' for k, v in (labels or {}).items())
    _counters[name][key] += value


def _set_gauge(name: str, value: float):
    _gauges[name] = value


def inc_conversations():
    _inc("deerflow_conversations_total")


def set_active_conversations(n: int):
    _set_gauge("deerflow_conversations_active", n)


def inc_message(role: str, status: str = "ok"):
    _inc("deerflow_messages_total", {"role": role, "status": status})


def inc_model_call(model: str, status: str = "ok"):
    _inc("deerflow_model_calls_total", {"model": model, "status": status})


def inc_memory_op(operation: str, status: str = "ok"):
    _inc("deerflow_memory_operations_total", {"operation": operation, "status": status})


def inc_governance_decision(decision: str):
    _inc("deerflow_governance_decisions_total", {"decision": decision})


def inc_channel_message(channel: str, direction: str = "inbound"):
    _inc("deerflow_channel_messages_total", {"channel": channel, "direction": direction})


@router.get("/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint (text format v0.0.4)."""
    lines = []

    # Type declarations + counters
    for name, label_map in _counters.items():
        lines.append(f"# TYPE {name} counter")
        for labels, value in label_map.items():
            if labels:
                lines.append(f"{name}{{{labels}}} {value}")
            else:
                lines.append(f"{name} {value}")

    # Gauges
    lines.append("# TYPE deerflow_conversations_active gauge")
    lines.append(f"deerflow_conversations_active {_gauges.get('deerflow_conversations_active', 0)}")
    lines.append("# TYPE deerflow_uptime_seconds gauge")
    lines.append(f"deerflow_uptime_seconds {time.time() - _START_TIME:.0f}")
    lines.append("# TYPE deerflow_process_info gauge")
    lines.append('deerflow_process_info{version="1.0",metrics="minimal"} 1')

    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; version=0.0.4")


@router.get("/metrics/json")
async def json_metrics() -> dict[str, Any]:
    """Lightweight JSON summary for ad-hoc health checks."""
    total_messages = sum(_counters.get("deerflow_messages_total", {}).values())
    total_model_calls = sum(_counters.get("deerflow_model_calls_total", {}).values())
    return {
        "uptime_seconds": round(time.time() - _START_TIME),
        "conversations_active": int(_gauges.get("deerflow_conversations_active", 0)),
        "messages_total": int(total_messages),
        "model_calls_total": int(total_model_calls),
    }
