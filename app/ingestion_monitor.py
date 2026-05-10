# app/ingestion_monitor.py
"""
Global ingestion-volume guardrail.

Sums a Cloud Monitoring metric (default: Chronicle ingestion record_count)
over a trailing window and returns whether the total exceeds a configured
threshold. Designed for a license / quota guardrail — fire one alert per
breach and let Jira dedup keep it from spamming.

This is a single, project-wide check (NOT per-feed). It runs once per
monitoring cycle from app.main.
"""
import logging
from datetime import datetime, timedelta, timezone

from google.api_core import exceptions as google_exceptions
from google.cloud import monitoring_v3
from google.protobuf.timestamp_pb2 import Timestamp

logger = logging.getLogger(__name__)

# Stable synthetic identifiers used by the action layer. The Jira summary
# embeds today's UTC date so dedup collapses repeated runs within the same
# day into ONE ticket, but a sustained breach generates a fresh ticket each
# new day (visible audit trail in the project's issue history).
SYNTHETIC_FEED_NAME = "_global_ingestion"
JIRA_SUMMARY_PREFIX = "[Feed Health] Daily ingestion over threshold"


def _today_summary():
    # UTC date matches Cloud Monitoring's reporting timezone — avoids
    # midnight-boundary ambiguity for runs near local midnight.
    return f"{JIRA_SUMMARY_PREFIX} ({datetime.now(timezone.utc).strftime('%Y-%m-%d')})"


def _get_metric_client():
    # Reuse the cached client from checks.py rather than instantiating a
    # second one (each MetricServiceClient loads creds + opens a gRPC channel).
    from app.checks import _get_metric_client as _shared
    return _shared()


# Friendly threshold shortcuts. Decimal multipliers (1000-based) — matches
# how Chronicle / cloud quotas are sold. Use threshold_kib / mib / gib / tib
# for binary (1024-based) if you ever need it.
_THRESHOLD_UNITS = {
    "threshold":     (1, None),                     # raw, unit comes from unit_label
    "threshold_kb":  (1_000, "bytes"),
    "threshold_mb":  (1_000_000, "bytes"),
    "threshold_gb":  (1_000_000_000, "bytes"),
    "threshold_tb":  (1_000_000_000_000, "bytes"),
    "threshold_kib": (1_024, "bytes"),
    "threshold_mib": (1_048_576, "bytes"),
    "threshold_gib": (1_073_741_824, "bytes"),
    "threshold_tib": (1_099_511_627_776, "bytes"),
}


def _resolve_threshold(cfg):
    """
    Resolve the configured threshold to (value_in_native_units, unit_label).

    Accepts either:
      - threshold: <number>                       (raw value in metric's units)
      - threshold_gb / threshold_tb / etc.        (friendly shortcuts for byte
                                                   metrics — converted to bytes)

    Friendly shortcuts override `unit_label` to "bytes" so the auto-scaling
    formatter (TB/GB/MB) kicks in. Raw `threshold` keeps whatever
    `unit_label` is set to (default: "events").

    Returns (None, None) and logs a warning if no threshold key is present
    or the value is not numeric — caller treats this as a no-op.
    """
    found = [k for k in _THRESHOLD_UNITS if k in cfg]
    if not found:
        logger.warning("  📈 [INGEST-MONITOR] No threshold configured — skipping")
        return None, None
    if len(found) > 1:
        logger.warning(
            f"  📈 [INGEST-MONITOR] Multiple threshold keys set ({found}) — "
            f"using {found[0]} and ignoring the rest"
        )
    key = found[0]
    multiplier, forced_label = _THRESHOLD_UNITS[key]
    raw = cfg.get(key)
    try:
        value = float(raw) * multiplier
    except (TypeError, ValueError):
        logger.warning(f"  📈 [INGEST-MONITOR] Invalid {key} value: {raw!r} — skipping")
        return None, None
    unit_label = forced_label or cfg.get("unit_label", "events")
    return value, unit_label


def _format_value(value, unit_label):
    """Pretty-print the metric total for log/ticket text."""
    if unit_label.lower() in ("bytes", "byte"):
        # Auto-scale to MB / GB / TB for readability.
        for unit, divisor in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
            if value >= divisor:
                return f"{value / divisor:.2f} {unit}"
        return f"{int(value)} bytes"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,} {unit_label}"
    return f"{value:,.2f} {unit_label}"


def check_global_ingestion(config):
    """
    Query Cloud Monitoring for the configured metric, summed across the
    trailing window. Returns a dict:
        {
            "enabled":        bool,   # False if the feature is off in config
            "over_threshold": bool,
            "current":        float,  # observed total in the metric's units
            "threshold":      float,  # configured threshold in the same units
            "unit_label":     str,
            "window_hours":   int,
            "details":        str,    # human-readable summary
        }
    Returns enabled=False when the feature is disabled or misconfigured —
    callers should treat that as a no-op, not a failure.
    """
    cfg = (config.get("global_settings", {}) or {}).get("ingestion_volume_monitor", {}) or {}
    if not cfg.get("enabled", False):
        return {"enabled": False}

    project_id = config.get("project_id", "")
    if not project_id:
        logger.warning("  📈 [INGEST-MONITOR] No project_id in config — skipping")
        return {"enabled": False}

    metric_type = cfg.get(
        "metric_type", "chronicle.googleapis.com/ingestion/log/record_count"
    )
    threshold, unit_label = _resolve_threshold(cfg)
    if threshold is None:
        return {"enabled": False}

    window_hours = int(cfg.get("window_hours", 24))

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=window_hours)

    interval = monitoring_v3.TimeInterval(
        end_time=Timestamp(seconds=int(now.timestamp())),
        start_time=Timestamp(seconds=int(start.timestamp())),
    )
    # Single bucket spanning the whole window — we only need the total.
    aggregation = monitoring_v3.Aggregation(
        alignment_period={"seconds": int(window_hours * 3600)},
        per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
        cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
    )
    req = monitoring_v3.ListTimeSeriesRequest(
        name=f"projects/{project_id}",
        filter=f'metric.type = "{metric_type}"',
        interval=interval,
        aggregation=aggregation,
        view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
    )

    try:
        client = _get_metric_client()
        series = list(client.list_time_series(request=req))
    except google_exceptions.NotFound:
        logger.info(
            "  📈 [INGEST-MONITOR] No time series found for "
            f"{metric_type} (treated as zero ingestion)."
        )
        series = []
    except Exception as e:
        logger.error(f"  📈 [INGEST-MONITOR] Cloud Monitoring query failed: {e}")
        return {"enabled": False}

    # cross_series_reducer collapses everything to a single TimeSeries (or
    # zero series if no data). Sum any remaining points defensively.
    total = 0.0
    for ts in series:
        for point in ts.points:
            v = point.value
            # int64_value or double_value depending on the metric's value type
            total += float(getattr(v, "int64_value", 0) or getattr(v, "double_value", 0.0))

    over = total > threshold
    details = (
        f"Last {window_hours}h ingestion: {_format_value(total, unit_label)} "
        f"({'OVER' if over else 'under'} threshold "
        f"{_format_value(threshold, unit_label)})"
    )

    if over:
        logger.warning(f"  📈 [INGEST-MONITOR] {details}")
    else:
        logger.info(f"  📈 [INGEST-MONITOR] {details}")

    return {
        "enabled": True,
        "over_threshold": over,
        "current": total,
        "threshold": threshold,
        "unit_label": unit_label,
        "window_hours": window_hours,
        "details": details,
    }


def build_synthetic_feed_config(config):
    """
    Build a feed_config-shaped dict so execute_actions() can dispatch the
    breach the same way it dispatches per-feed failures (jira/email +
    the existing dedup gate).

    The `_summary_override` key tells jira_client._build_summary to use a
    stable, identifier-free string instead of the per-feed format.
    """
    cfg = (config.get("global_settings", {}) or {}).get("ingestion_volume_monitor", {}) or {}
    actions_on_breach = cfg.get("actions_on_breach") or ["log_only"]
    return {
        "name": SYNTHETIC_FEED_NAME,
        "actions_on_failure": actions_on_breach,
        "_summary_override": _today_summary(),
    }
