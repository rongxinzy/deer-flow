"""Prometheus metrics endpoint for the digital employee runtime.

Exposes counters/gauges/histograms that answer "is this employee
healthy and productive" — the operator and monitoring stack scrape
/metrics to build dashboards and alerts.
"""
import time
from typing import Any

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_metrics,
    multiprocess,
)

router = APIRouter(tags=["metrics"])

REGISTRY = CollectorRegistry()
multiprocess.MultiProcessCollector(REGISTRY)

# --- Conversations ----------------------------------------------------------
CONVERSATIONS_ACTIVE = Gauge(
    "deerflow_conversations_active",
    "Currently open conversation threads",
    registry=REGISTRY,
)
CONVERSATIONS_TOTAL = Counter(
    "deerflow_conversations_total",
    "Total conversation threads created",
    registry=REGISTRY,
)

# --- Messages ----------------------------------------------------------------
MESSAGES_TOTAL = Counter(
    "deerflow_messages_total",
    "Total messages processed",
    labelnames=["role", "status"],
    registry=REGISTRY,
)
MESSAGE_LATENCY = Histogram(
    "deerflow_message_latency_seconds",
    "End-to-end latency from user message to complete reply",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300),
    registry=REGISTRY,
)

# --- Model calls ---------------------------------------------------------------
MODEL_CALLS_TOTAL = Counter(
    "deerflow_model_calls_total",
    "Model API invocations",
    labelnames=["model", "status"],
    registry=REGISTRY,
)
MODEL_LATENCY = Histogram(
    "deerflow_model_latency_seconds",
    "Model API round-trip latency",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60),
    labelnames=["model"],
    registry=REGISTRY,
)

# --- Memory ----------------------------------------------------------------
MEMORY_OPERATIONS_TOTAL = Counter(
    "deerflow_memory_operations_total",
    "Memory store operations",
    labelnames=["operation", "status"],
    registry=REGISTRY,
)

# --- Governance ----------------------------------------------------------------
GOVERNANCE_DECISIONS_TOTAL = Counter(
    "deerflow_governance_decisions_total",
    "Governance middleware decisions",
    labelnames=["decision"],
    registry=REGISTRY,
)

# --- Channel ----------------------------------------------------------------
CHANNEL_MESSAGES_TOTAL = Counter(
    "deerflow_channel_messages_total",
    "Messages received from IM channels",
    labelnames=["channel", "direction"],
    registry=REGISTRY,
)


@router.get("/metrics")
async def prometheus_metrics() -> Response:
    """Prometheus scrape endpoint (text format)."""
    body = generate_metrics(REGISTRY)
    return Response(content=body, media_type="text/plain; version=0.0.4")


@router.get("/metrics/json")
async def json_metrics() -> dict[str, Any]:
    """Lightweight JSON summary for ad-hoc health checks."""
    return {
        "conversations_active": CONVERSATIONS_ACTIVE._value.get()
        if hasattr(CONVERSATIONS_ACTIVE, "_value")
        else 0,
        "messages_total": MESSAGES_TOTAL._value.get()
        if hasattr(MESSAGES_TOTAL, "_value")
        else 0,
        "uptime_seconds": time.time() - _START_TIME,
    }


_START_TIME = time.time()
