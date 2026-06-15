# python/alerts.py — shim; canonical: src.utils.alerts
from src.utils.alerts import (
    dispatch_alert,
    is_rpc_critical_error,
    maybe_dispatch_rpc_failure_alert,
    schedule_alert,
    schedule_rpc_failure_alert,
)

__all__ = [
    "dispatch_alert",
    "is_rpc_critical_error",
    "maybe_dispatch_rpc_failure_alert",
    "schedule_alert",
    "schedule_rpc_failure_alert",
]
