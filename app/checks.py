import time
import logging
import statistics
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from google.cloud import monitoring_v3
from google.protobuf.timestamp_pb2 import Timestamp
from google.api_core import exceptions as google_exceptions

from app.chronicle_client import get_chronicle_client
from app.utils import retry_with_backoff, with_timeout

logger = logging.getLogger(__name__)


# ============================================================
# Cached GCP Monitoring client
# ============================================================
# Instantiating MetricServiceClient is expensive (loads service account
# credentials, opens a gRPC channel). Cache it module-wide.

_metric_client = None


def _get_metric_client():
    global _metric_client
    if _metric_client is None:
        _metric_client = monitoring_v3.MetricServiceClient()
    return _metric_client


# ============================================================
# Retry helper (kept for backwards compat — delegates to utils)
# ============================================================

def _retry(fn, retries=2, delay=10, label="API call", timeout_seconds=None):
    """Retry wrapper with exponential backoff. See app.utils.retry_with_backoff."""
    return retry_with_backoff(
        fn,
        retries=retries,
        base_delay=delay,
        label=label,
        timeout_seconds=timeout_seconds,
    )


def _sdk_timeout(config):
    """Per-call timeout for Chronicle / GCP SDK calls (seconds)."""
    return int(
        (config.get("global_settings") or {}).get("chronicle_timeout_seconds", 60)
    )


def _humanize_hours(hours):
    """Format a duration in hours as a friendly string (e.g. '1h', '24h (1 day)', '720h (30 days)')."""
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return f"{hours}h"
    if h < 24 or h % 24 != 0:
        if h == int(h):
            return f"{int(h)}h"
        return f"{h:g}h"
    days = int(h // 24)
    return f"{int(h)}h ({days} day{'s' if days != 1 else ''})"


def _resolve_window_hours(config, feed_config, key, default=1):
    """
    Resolve a time-window setting (in hours) for a feed.

    Lookup order:
        1. feed_config[key]              (feed-level override)
        2. global_settings[key]          (global default)
        3. default
    """
    global_settings = config.get("global_settings", {}) or {}
    if key in feed_config:
        return feed_config[key]
    if key in global_settings:
        return global_settings[key]
    return default


# ============================================================
# CHECK 1: Feed State via Chronicle API
# ============================================================

def check_feed_state(config, feed_config, feeds_cache=None):
    """
    Check if the feed exists and is ACTIVE in Chronicle.
    Extracts all available feed metadata for LLM investigation.

    Returns dict:
        {
            "healthy": bool,
            "details": str,
            "metadata": dict,
        }
    """
    result = {"healthy": False, "details": "", "metadata": {}}

    chronicle = get_chronicle_client(config)
    if chronicle is None:
        result["details"] = "Chronicle client unavailable"
        return result

    retry_cfg = config.get("global_settings", {})
    retries = retry_cfg.get("retry_count", 2)
    delay = retry_cfg.get("retry_delay_seconds", 10)

    try:
        # Use cache if provided, otherwise fetch
        if feeds_cache is not None:
            feeds = feeds_cache
        else:
            feeds = _retry(
                chronicle.list_feeds,
                retries=retries,
                delay=delay,
                label="list_feeds",
                timeout_seconds=_sdk_timeout(config),
            )

        target_id = feed_config.get("chronicle_feed_id", "")

        for feed in feeds:
            feed_id = feed.get("uid", feed.get("name", "").split("/")[-1])
            if feed_id != target_id:
                continue

            # ── Core fields ──
            state = feed.get("state", "UNKNOWN")
            display = feed.get("displayName", "—")
            last_run = feed.get("lastFeedInitiationTime", None)

            # ── Source details ──
            details_block = feed.get("details", {})
            source_type = details_block.get("feedSourceType", "UNKNOWN")
            log_type = details_block.get("logType", "UNKNOWN")
            namespace = details_block.get("assetNamespace", "")
            labels = details_block.get("labels", {})

            # ── Source-specific settings ──
            source_settings = _extract_source_settings(details_block)

            # ── Build metadata dict ──
            metadata = {
                "feed_id": feed_id,
                "display_name": display,
                "state": state,
                "source_type": source_type,
                "log_type": log_type,
                "namespace": namespace,
                "labels": labels,
                "last_run": last_run,
                "source_settings": source_settings,
                "raw_feed": feed,
            }

            # ── Build human-readable details ──
            # Keep this short — feed identity (id/name/namespace/source) is
            # already shown in the Jira "Feed Info" section.
            details_lines = [f"Feed state reported by Chronicle: {state}"]
            if last_run:
                details_lines.append(f"Last initiation: {last_run}")

            result["details"] = "\n".join(details_lines)
            result["metadata"] = metadata

            # ACTIVE and SUCCEEDED are both healthy states
            healthy_states = ["ACTIVE", "SUCCEEDED"]
            is_healthy = state.upper() in healthy_states
            result["healthy"] = is_healthy
            if is_healthy:
                logger.info(f"  ✅ Feed '{display}' is {state}")
            else:
                logger.warning(f"  ❌ Feed '{display}' is {state}")
                # Log the raw feed payload at DEBUG only — useful for
                # troubleshooting unhealthy feeds without polluting normal logs.
                # Identifiers inside the JSON are masked unless the user
                # has set FEEDHEALTH_UNMASK=1.
                if logger.isEnabledFor(logging.DEBUG):
                    import json
                    from app.utils import scrub_text
                    from app.llm import _deep_redact
                    try:
                        # Field-name-based deep redaction first (catches
                        # nested high-signal values like eventHubNamespace,
                        # s3Uri, endpoint_url anywhere in the structure),
                        # then value-based scrub for the IDs we know about.
                        redacted_feed = _deep_redact(feed) if isinstance(feed, (dict, list)) else feed
                        raw_dump = json.dumps(redacted_feed, indent=2, default=str)
                        raw_dump = scrub_text(
                            raw_dump,
                            config.get("project_id"),
                            config.get("customer_id"),
                            feed.get("uid"),
                            feed.get("referenceId"),
                        )
                        bar_top = "┌─ Raw feed payload (Chronicle API) " + ("─" * 25)
                        bar_bot = "└" + ("─" * 60)
                        logger.debug(f"{bar_top}\n{raw_dump}\n{bar_bot}")
                    except Exception as e:
                        logger.debug(f"(could not serialize raw feed: {e})")

            return result

        # Feed not found
        result["details"] = f"Feed ID '{target_id}' not found in Chronicle"
        logger.warning(f"  ❌ {result['details']}")
        return result

    except Exception as e:
        result["details"] = f"Feed state check failed: {e}"
        logger.error(f"  ❌ {result['details']}")
        return result


def _extract_source_settings(details_block):
    """Extract source-specific settings from feed details."""
    if "azureBlobStoreV2Settings" in details_block:
        s = details_block["azureBlobStoreV2Settings"]
        return {
            "settings_type": "Azure Blob Store V2",
            "azure_uri": s.get("azureUri", "—"),
            "source_deletion_option": s.get("sourceDeletionOption", "—"),
            "max_lookback_days": s.get("maxLookbackDays", "—"),
        }
    elif "azureEventHubSettings" in details_block:
        s = details_block["azureEventHubSettings"]
        return {
            "settings_type": "Azure Event Hub",
            "event_hub_name": s.get("name", "—"),
            "consumer_group": s.get("consumerGroup", "—"),
            "event_hub_namespace": s.get("eventHubNamespace", "—"),
        }
    elif "amazonS3Settings" in details_block:
        s = details_block["amazonS3Settings"]
        return {
            "settings_type": "Amazon S3",
            "s3_uri": s.get("s3Uri", "—"),
            "region": s.get("region", "—"),
        }
    elif "httpsPushWebhookSettings" in details_block:
        return {"settings_type": "HTTPS Push Webhook"}
    return {}


# ============================================================
# CHECK 2: GCP Cloud Monitoring Metrics
# ============================================================

def check_gcp_metrics(config, feed_config, feeds_cache=None):
    """
    Anomaly-based health check for ingestion record_count.

    Pulls the metric over `gcp_metrics_baseline_hours` (default 720h = 30 days),
    bucketed in `gcp_metrics_hours`-sized windows. The most recent bucket is the
    "current" value; prior buckets form the baseline. Same-time-of-day buckets
    are preferred to respect diurnal patterns; falls back to all prior buckets
    if too few same-tod samples are available.

    Anomaly detection uses median + MAD (Median Absolute Deviation) and the
    modified Z-score: z = 0.6745 * (current - median) / MAD.

    Healthy iff:
        - current >= min_expected_records (hard floor), AND
        - modified Z-score >= -gcp_metrics_anomaly_threshold (not abnormally low)

    If baseline sample count is below `gcp_metrics_min_baseline_samples`, only
    the hard-floor check is applied.
    """
    result = {"healthy": False, "details": "", "metadata": {}}

    project_id = config["project_id"]
    global_settings = config.get("global_settings", {}) or {}

    window_hours = _resolve_window_hours(config, feed_config, "gcp_metrics_hours")
    baseline_hours = _resolve_window_hours(
        config, feed_config, "gcp_metrics_baseline_hours", default=24 * 30
    )
    anomaly_threshold = float(feed_config.get(
        "gcp_metrics_anomaly_threshold",
        global_settings.get("gcp_metrics_anomaly_threshold", 3.0),
    ))
    min_baseline_samples = int(feed_config.get(
        "gcp_metrics_min_baseline_samples",
        global_settings.get("gcp_metrics_min_baseline_samples", 5),
    ))
    min_expected = feed_config.get("min_expected_records", 1)

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=baseline_hours)

    metric_id = feed_config.get("metric_identifier", {})
    id_type = metric_id.get("type", "")
    id_value = metric_id.get("value", "")

    base_filter = 'metric.type = "chronicle.googleapis.com/ingestion/log/record_count"'

    # ── Build filter ──
    # Note: `log_type` is a label on the chronicle.googleapis.com/Collector
    # RESOURCE (not on the metric itself), so it must be filtered with
    # `resource.labels.log_type`. `namespace` and `feed_id` are metric labels.
    if id_type == "namespace" and id_value:
        metric_filter = f'{base_filter} AND metric.labels.namespace = "{id_value}"'
    elif id_type == "log_type" and id_value:
        metric_filter = f'{base_filter} AND resource.labels.log_type = "{id_value}"'
    elif id_type == "feed_id" and id_value:
        metric_filter = f'{base_filter} AND metric.labels.feed_id = "{id_value}"'
    else:
        # Fallback to dataType
        data_type = feed_config.get("dataType", "")
        if data_type:
            metric_filter = f'{base_filter} AND resource.labels.log_type = "{data_type}"'
            id_type = "log_type (fallback)"
            id_value = data_type
            logger.info(f"  ⚠️ No metric_identifier — falling back to log_type={data_type}")
        else:
            result["healthy"] = True  # Don't false-alarm
            result["details"] = "No metric_identifier or dataType defined — skipping metrics check"
            logger.warning(f"  ⚠️ {result['details']}")
            return result

    retries = global_settings.get("retry_count", 2)
    delay = global_settings.get("retry_delay_seconds", 10)
    bucket_seconds = int(window_hours * 3600)

    try:
        def _fetch_metrics():
            mon_client = _get_metric_client()

            interval = monitoring_v3.TimeInterval(
                end_time=Timestamp(seconds=int(now.timestamp())),
                start_time=Timestamp(seconds=int(start.timestamp())),
            )

            aggregation = monitoring_v3.Aggregation(
                alignment_period={"seconds": bucket_seconds},
                per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
            )

            req = monitoring_v3.ListTimeSeriesRequest(
                name=f"projects/{project_id}",
                filter=metric_filter,
                interval=interval,
                aggregation=aggregation,
                view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
            )

            try:
                return list(mon_client.list_time_series(request=req))
            except Exception as e:
                # Cloud Monitoring returns 404 "Cannot find metric(s)" when no
                # time series has ever been published matching the filter.
                # Treat as an empty result (the silent-feed branch will take
                # over with a friendly message). Don't retry — this is not a
                # transient condition.
                msg = str(e)
                is_not_found = (
                    isinstance(e, google_exceptions.NotFound)
                    or "Cannot find metric" in msg
                    or msg.startswith("404")
                )
                if is_not_found:
                    logger.info(
                        "  ℹ️ Cloud Monitoring has no time series matching this filter "
                        "(treated as empty / silent feed)."
                    )
                    return []
                raise

        series = _retry(
            _fetch_metrics,
            retries=retries,
            delay=delay,
            label="GCP metrics",
            timeout_seconds=_sdk_timeout(config),
        )

        # ── Collapse all series into a single time-bucket -> count map ──
        bucket_counts = defaultdict(int)
        for ts in series:
            for p in ts.points:
                # `end_time` may be a protobuf Timestamp or a
                # DatetimeWithNanoseconds depending on the client version.
                end_time = p.interval.end_time
                if hasattr(end_time, "timestamp"):
                    t = int(end_time.timestamp())
                else:
                    t = int(end_time.seconds)
                bucket_counts[t] += int(p.value.int64_value)

        if not bucket_counts:
            result["metadata"] = {
                "current_records": 0,
                "window_hours": window_hours,
                "baseline_hours": baseline_hours,
                "baseline_samples": 0,
            }
            result["details"] = (
                f"No records ingested at all in the last {_humanize_hours(baseline_hours)} — "
                f"the feed appears completely silent."
            )
            logger.warning(f"  ❌ {result['details']}")
            return result

        sorted_times = sorted(bucket_counts.keys(), reverse=True)
        now_ts = int(now.timestamp())

        # The "current" bucket is the one covering the last window_hours.
        # If GCP returned no point for it, treat current_count as 0 (silence
        # is itself an anomaly when the baseline is non-zero).
        if sorted_times and (now_ts - sorted_times[0]) <= bucket_seconds:
            current_time = sorted_times[0]
            current_count = bucket_counts[current_time]
            baseline_times = sorted_times[1:]
        else:
            # No recent point — current bucket is empty / silent.
            current_time = now_ts - (now_ts % bucket_seconds)
            current_count = 0
            baseline_times = sorted_times

        # ── Baseline: prefer same time-of-day buckets ──
        seconds_per_day = 86400
        current_tod = current_time % seconds_per_day
        same_tod_samples = [
            bucket_counts[t] for t in baseline_times
            if t % seconds_per_day == current_tod
        ]

        if len(same_tod_samples) >= min_baseline_samples:
            baseline_samples = same_tod_samples
            baseline_strategy = "same-time-of-day"
        else:
            baseline_samples = [bucket_counts[t] for t in baseline_times]
            baseline_strategy = "all-prior-buckets"

        # ── Hard floor check (always applies) ──
        if current_count < min_expected:
            result["metadata"] = {
                "current_records": current_count,
                "min_expected_records": min_expected,
                "window_hours": window_hours,
                "baseline_hours": baseline_hours,
                "baseline_samples": len(baseline_samples),
            }
            if current_count == 0:
                result["details"] = (
                    f"No records ingested in the last {_humanize_hours(window_hours)} "
                    f"(minimum expected per window: {min_expected:,})."
                )
            else:
                result["details"] = (
                    f"Only {current_count:,} records ingested in the last "
                    f"{_humanize_hours(window_hours)} — below the minimum expected "
                    f"per window: {min_expected:,}."
                )
            logger.warning(f"  ❌ {result['details']}")
            return result

        # ── Anomaly detection (if enough samples) ──
        if len(baseline_samples) < min_baseline_samples:
            result["healthy"] = True
            result["metadata"] = {
                "current_records": current_count,
                "window_hours": window_hours,
                "baseline_hours": baseline_hours,
                "baseline_samples": len(baseline_samples),
                "min_baseline_samples": min_baseline_samples,
                "note": "insufficient baseline — anomaly detection skipped",
            }
            result["details"] = (
                f"{current_count:,} records ingested in the last "
                f"{_humanize_hours(window_hours)} (above the {min_expected:,} minimum). "
                f"Anomaly detection skipped — only {len(baseline_samples)} of the "
                f"{min_baseline_samples} required baseline samples are available yet."
            )
            logger.info(f"  ✅ {result['details']}")
            return result

        median = statistics.median(baseline_samples)
        mad = statistics.median([abs(x - median) for x in baseline_samples])

        if mad == 0:
            # Baseline is perfectly flat — use absolute comparison
            if current_count < median:
                z_score = float("-inf")
            elif current_count > median:
                z_score = float("inf")
            else:
                z_score = 0.0
        else:
            z_score = 0.6745 * (current_count - median) / mad

        is_anomaly = z_score < -anomaly_threshold

        result["metadata"] = {
            "current_records": current_count,
            "baseline_median": median,
            "baseline_mad": mad,
            "baseline_samples": len(baseline_samples),
            "baseline_strategy": baseline_strategy,
            "modified_z_score": (
                round(z_score, 2) if z_score not in (float("inf"), float("-inf"))
                else str(z_score)
            ),
            "anomaly_threshold": anomaly_threshold,
            "window_hours": window_hours,
            "baseline_hours": baseline_hours,
        }

        if is_anomaly:
            drop_pct = (
                (1 - current_count / median) * 100 if median > 0 else 100.0
            )
            result["details"] = (
                f"Ingestion is abnormally low: {current_count:,} records in the last "
                f"{_humanize_hours(window_hours)} vs a typical {median:,.0f} "
                f"(≈{drop_pct:.0f}% below the {baseline_strategy.replace('-', ' ')} "
                f"average over the last {_humanize_hours(baseline_hours)}, "
                f"based on {len(baseline_samples)} samples)."
            )
            logger.warning(f"  ❌ {result['details']}")
            return result

        result["healthy"] = True
        result["details"] = (
            f"{current_count:,} records ingested in the last "
            f"{_humanize_hours(window_hours)} — within the normal range "
            f"(typical ≈ {median:,.0f}, based on {len(baseline_samples)} "
            f"{baseline_strategy.replace('-', ' ')} samples from the last "
            f"{_humanize_hours(baseline_hours)})."
        )
        logger.info(f"  ✅ {result['details']}")
        return result

    except Exception as e:
        result["details"] = f"GCP metrics check failed: {e}"
        logger.error(f"  ❌ {result['details']}")
        return result


# ============================================================
# CHECK 3: UDM Search
# ============================================================

def check_udm_search(config, feed_config, feeds_cache=None):
    """
    Run a UDM search to verify events exist for this feed.
    Returns dict:
        {
            "healthy": bool,
            "details": str,
            "metadata": {},
        }
    """
    result = {"healthy": False, "details": "", "metadata": {}}

    chronicle = get_chronicle_client(config)
    if chronicle is None:
        result["details"] = "Chronicle client unavailable"
        return result

    udm_query = feed_config.get("udm_query", "")

    # ── Auto-build query if not provided ──
    if not udm_query:
        namespace = feed_config.get("namespace", "")
        data_type = feed_config.get("dataType", "")

        if namespace:
            udm_query = f'namespace = "{namespace}"'
        elif data_type:
            udm_query = f'metadata.log_type = "{data_type}"'
        else:
            result["healthy"] = True  # Skip gracefully
            result["details"] = "No udm_query, namespace, or dataType — skipping UDM search"
            logger.info(f"  ⏭️ {result['details']}")
            return result

        logger.info(f"  ℹ️ Auto-built UDM query: {udm_query}")

    window_hours = _resolve_window_hours(config, feed_config, "udm_search_hours")

    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(hours=window_hours)

    retry_cfg = config.get("global_settings", {})
    retries = retry_cfg.get("retry_count", 2)
    delay = retry_cfg.get("retry_delay_seconds", 10)

    try:
        def _search():
            return chronicle.search_udm(
                query=udm_query,
                start_time=start_time,
                end_time=end_time,
                max_events=1,
            )

        search_result = _retry(
            _search,
            retries=retries,
            delay=delay,
            label="UDM search",
            timeout_seconds=_sdk_timeout(config),
        )

        count = search_result.get("total_events", 0)
        result["metadata"] = {"event_count": count, "query": udm_query}

        if count > 0:
            result["healthy"] = True
            result["details"] = (
                f"UDM search found {count:,} matching event(s) in the last "
                f"{_humanize_hours(window_hours)}."
            )
            logger.info(f"  ✅ {result['details']}")
        else:
            result["details"] = (
                f"UDM search found no matching events in the last "
                f"{_humanize_hours(window_hours)} — expected at least 1."
            )
            logger.warning(f"  ❌ {result['details']}")

        return result

    except Exception as e:
        result["details"] = f"UDM search failed: {e}"
        logger.error(f"  ❌ {result['details']}")
        return result
