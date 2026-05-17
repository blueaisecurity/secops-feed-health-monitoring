import os
import logging
from datetime import datetime, timezone

import yaml

from app.config import load_config
from app.chronicle_client import get_chronicle_client
from app.utils import mask_id as _mask_id

logger = logging.getLogger(__name__)


def _log_friendly_api_error(e, config):
    """Log a Chronicle API failure as a short summary + actionable hint.

    Mirrors the formatting in tests/test_connection.py — masks the
    project ID inside error text so logs are safe to share, and
    suggests the most likely fix instead of dumping a JSON error blob.
    """
    project_id = config.get("project_id", "")
    customer_id = config.get("customer_id", "")
    region = config.get("region", "us")

    msg = str(e)
    if project_id:
        msg = msg.replace(str(project_id), _mask_id(project_id))
    if customer_id:
        msg = msg.replace(str(customer_id), _mask_id(customer_id))

    # Pull status code + the API's own 'message' field if present (much
    # more useful than the URL).  Falls back to the first line, trimmed.
    import re
    status_match = re.search(r"status[=:]\s*(\d+)", msg)
    msg_match = re.search(r"'message':\s*'([^']+)'", msg)
    reason_match = re.search(r"'reason':\s*'([A-Z_]+)'", msg)
    parts = []
    if status_match:
        parts.append(f"HTTP {status_match.group(1)}")
    if reason_match:
        parts.append(reason_match.group(1))
    if msg_match:
        parts.append(msg_match.group(1))
    if parts:
        summary = " — ".join(parts)
    else:
        summary = msg.splitlines()[0]
    if len(summary) > 200:
        summary = summary[:200] + "…"

    low = msg.lower()
    fix = ""
    if "service_disabled" in low or "has not been used in project" in low:
        fix = (
            f"Chronicle API is disabled for project {_mask_id(project_id)}. "
            "Verify project_id in variables.yaml, or enable it: "
            "gcloud services enable chronicle.googleapis.com --project=<your-project-id>"
        )
    elif "consumer_invalid" in low:
        fix = (
            f"Project {_mask_id(project_id)} is not registered with this "
            "Chronicle tenant. Verify project_id matches the GCP project "
            "bound to your Chronicle customer_id."
        )
    elif "not found" in low or "404" in msg or "instance" in low and "not exist" in low:
        fix = (
            f"Resource not found. Most common cause: customer_id "
            f"({_mask_id(customer_id)}) is wrong. Also verify region "
            f"({region}). Both are set in variables.yaml."
        )
    elif "permission denied" in low or "403" in msg:
        fix = (
            f"Permission denied. Check that project_id ({_mask_id(project_id)}) "
            f"and customer_id ({_mask_id(customer_id)}) are correct, that the "
            "Chronicle API is enabled, and that the caller has roles/chronicle.viewer "
            "(or roles/chronicle.editor for restart). For local dev run: "
            "gcloud auth application-default login"
        )
    elif "unauthenticated" in low or "401" in msg:
        fix = (
            "Authentication failed. Run: gcloud auth application-default login "
            "(or check GOOGLE_APPLICATION_CREDENTIALS)."
        )
    elif "invalid_argument" in low or "400" in msg or "invalid argument" in low:
        fix = (
            f"Invalid request. Most common cause: customer_id "
            f"({_mask_id(customer_id)}) is malformed — it must be the full "
            "36-character UUID of your Chronicle instance. Verify it in variables.yaml."
        )

    logger.error(f"❌ Failed to list feeds from Chronicle: {summary}")
    if fix:
        logger.error(f"   💡 {fix}")


def _short_log_type(raw_log_type):
    """
    Extract short log type name from full Chronicle resource path.
    'projects/.../logTypes/AZURE_AD' → 'AZURE_AD'
    """
    if "/" in raw_log_type:
        return raw_log_type.split("/")[-1]
    return raw_log_type


def _build_default_checks(details):
    """
    Build default checks list based on feed source type.
    Webhook feeds only get feed_state since they rely on external pushes.
    """
    source_type = details.get("feedSourceType", "UNKNOWN")
    webhook_types = ["HTTPS_PUSH_WEBHOOK", "WEBHOOK"]

    if source_type in webhook_types:
        return ["feed_state"]
    return ["feed_state", "gcp_metrics", "udm_search"]


def _build_default_actions():
    """Default actions for new feeds."""
    return ["jira"]


def _build_metric_identifier(namespace, short_type):
    """Build metric_identifier — prefer namespace, fall back to log_type."""
    if namespace:
        return {"type": "namespace", "value": namespace}
    elif short_type and short_type != "UNKNOWN":
        return {"type": "log_type", "value": short_type}
    return {"type": "log_type", "value": "UNKNOWN"}


def _build_udm_query(namespace, short_type):
    """Build a default UDM query — prefer namespace, fall back to log_type."""
    if namespace:
        return f'namespace = "{namespace}"'
    elif short_type and short_type != "UNKNOWN":
        return f'metadata.log_type = "{short_type}"'
    return ""


def _build_source_settings(details):
    """Extract source-specific settings from feed details."""
    source_type = details.get("feedSourceType", "UNKNOWN")
    settings = {"source_type": source_type}

    if "azureBlobStoreV2Settings" in details:
        blob = details["azureBlobStoreV2Settings"]
        settings["azure_uri"] = blob.get("azureUri", "—")
        settings["source_deletion_option"] = blob.get("sourceDeletionOption", "—")

    elif "azureEventHubSettings" in details:
        hub = details["azureEventHubSettings"]
        settings["event_hub_name"] = hub.get("name", "—")
        settings["consumer_group"] = hub.get("consumerGroup", "—")
        settings["event_hub_namespace"] = hub.get("eventHubNamespace", "—")

    elif "amazonS3Settings" in details:
        s3 = details["amazonS3Settings"]
        settings["s3_uri"] = s3.get("s3Uri", "—")
        settings["region"] = s3.get("region", "—")

    return settings


def sync_feeds(config_path=None):
    """
    Auto-discover Chronicle feeds and merge them into feeds.yaml.
    - New feeds are added with enabled = global_settings.auto_sync.new_feeds_enabled
      (default: false).
    - Existing feeds are preserved as-is — the user's 'enabled' choice is NEVER
      overridden by a sync.
    - Removed feeds are marked with _stale: true.

    The feeds list is read from and written to feeds.yaml (or whatever path
    FEEDS_PATH env var resolves to). config.yaml itself is never mutated.

    config_path is left as None by default so load_config() can honour the
    CONFIG_PATH env var (used on Cloud Run, where config.yaml is mounted
    at /etc/feed-health/).
    """

    config = load_config(config_path)
    feeds_path = config.get("_feeds_path", "feeds.yaml")
    chronicle = get_chronicle_client(config)

    # Default-enabled value applied ONLY to brand-new feeds discovered now.
    new_feeds_enabled = (
        config.get("global_settings", {})
        .get("auto_sync", {})
        .get("new_feeds_enabled", False)
    )

    if chronicle is None:
        logger.error("Cannot sync feeds — Chronicle client unavailable")
        return

    try:
        api_feeds = chronicle.list_feeds()
        logger.info(f"📡 Found {len(api_feeds)} feed(s) in Chronicle")
    except Exception as e:
        _log_friendly_api_error(e, config)
        return

    # ── Build lookup of existing configured feeds by feed ID ──
    existing_feeds = config.get("feeds", []) or []
    existing_by_id = {}
    for feed in existing_feeds:
        fid = feed.get("chronicle_feed_id", "")
        if fid:
            existing_by_id[fid] = feed

    # ── Process each API feed ──
    updated_feeds = []
    discovered_ids = set()
    new_count = 0
    updated_count = 0

    for api_feed in api_feeds:
        feed_id = api_feed.get("uid", api_feed.get("name", "").split("/")[-1])
        display_name = api_feed.get("displayName", "unknown")
        state = api_feed.get("state", "UNKNOWN")
        details = api_feed.get("details", {})
        source_type = details.get("feedSourceType", "UNKNOWN")
        raw_log_type = details.get("logType", "UNKNOWN")
        short_type = _short_log_type(raw_log_type)
        namespace = details.get("assetNamespace", "")
        labels = details.get("labels", {})

        discovered_ids.add(feed_id)

        if feed_id in existing_by_id:
            # ── Existing feed: preserve user edits, update auto fields ──
            existing = existing_by_id[feed_id]

            existing["_state"] = state
            existing["_source_type"] = source_type
            existing["_log_type_full"] = raw_log_type
            existing["_labels"] = labels
            existing["_source_settings"] = _build_source_settings(details)
            existing["_last_sync"] = datetime.now(timezone.utc).isoformat()
            existing["dataType"] = short_type

            updated_feeds.append(existing)
            updated_count += 1
            logger.info(f"  🔄 Updated: {display_name} ({_mask_id(feed_id)})")

        else:
            # ── New feed: create default config ──
            new_feed = {
                "enabled": new_feeds_enabled,
                "name": display_name,
                "chronicle_feed_id": feed_id,
                "dataType": short_type,
                "namespace": namespace,
                "metric_identifier": _build_metric_identifier(namespace, short_type),
                "gcp_metrics_hours": config.get("global_settings", {}).get("gcp_metrics_hours", 1),
                "udm_search_hours": config.get("global_settings", {}).get("udm_search_hours", 1),
                "checks": _build_default_checks(details),
                "udm_query": _build_udm_query(namespace, short_type),
                "actions_on_failure": _build_default_actions(),
                "_state": state,
                "_source_type": source_type,
                "_log_type_full": raw_log_type,
                "_labels": labels,
                "_source_settings": _build_source_settings(details),
                "_discovered": datetime.now(timezone.utc).isoformat(),
                "_last_sync": datetime.now(timezone.utc).isoformat(),
            }

            updated_feeds.append(new_feed)
            new_count += 1
            logger.info(f"  ✨ New: {display_name} ({_mask_id(feed_id)}) — added as disabled")

    # ── Mark stale feeds ──
    stale_count = 0
    for feed in existing_feeds:
        fid = feed.get("chronicle_feed_id", "")
        if fid and fid not in discovered_ids:
            feed["_stale"] = True
            feed["_stale_since"] = datetime.now(timezone.utc).isoformat()
            updated_feeds.append(feed)
            stale_count += 1
            logger.warning(f"  ⚠️ Stale: {feed.get('name', 'unknown')} ({_mask_id(fid)})")

    # ── Write back ──
    config["feeds"] = updated_feeds
    _write_feeds(updated_feeds, feeds_path)

    logger.info(f"\n{'='*60}")
    logger.info(f"📊 Sync Complete")
    logger.info(f"   New:     {new_count}")
    logger.info(f"   Updated: {updated_count}")
    logger.info(f"   Stale:   {stale_count}")
    logger.info(f"   Total:   {len(updated_feeds)}")
    logger.info(f"{'='*60}")
    logger.info(f"\n💡 To enable a feed, set 'enabled: true' in feeds.yaml")


def _write_feeds(feeds, feeds_path):
    """
    Write the feeds list to feeds.yaml atomically.

    Only the feeds file is mutated — the git-tracked config.yaml is never
    touched. Atomic write: serialize to <feeds_path>.tmp, fsync, then
    os.replace() so a crash mid-write can never leave the file truncated
    or partially written.
    """
    preamble = (
        "# ============================================================\n"
        "# Feed Health Monitoring App - Feeds (SENSITIVE — gitignored)\n"
        "# ============================================================\n"
        "# Auto-generated by `python -m app.sync_feeds`.\n"
        "# Fields prefixed with `_` are auto-populated — do not edit.\n"
        "# ============================================================\n\n"
    )

    # ── Build feeds section manually with spacing ──
    feeds_content = "feeds:\n"

    # Define the order we want fields to appear in
    user_fields = [
        "enabled", "name", "chronicle_feed_id", "dataType", "namespace",
        "gcp_metrics_hours", "udm_search_hours",
        "gcp_metrics_baseline_hours", "gcp_metrics_anomaly_threshold",
        "gcp_metrics_min_baseline_samples", "min_expected_records",
        "metric_identifier", "checks", "udm_query",
        "actions_on_failure",
    ]
    auto_fields_prefix = "_"

    for i, feed in enumerate(feeds):
        if i > 0:
            # Add visual separator between feeds
            feeds_content += "\n  # ────────────────────────────────────────────\n\n"

        # ── User-editable fields first ──
        for key in user_fields:
            if key in feed:
                chunk = yaml.dump(
                    {key: feed[key]},
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                    width=120,
                )
                # Indent and add list marker for first field
                lines = chunk.strip().split("\n")
                for j, line in enumerate(lines):
                    if j == 0 and key == "enabled":
                        feeds_content += f"- {line}\n"
                    elif j == 0:
                        feeds_content += f"  {line}\n"
                    else:
                        feeds_content += f"  {line}\n"

        # ── Auto fields (prefixed with _) ──
        auto_keys = [k for k in feed if k.startswith(auto_fields_prefix)]
        if auto_keys:
            for key in auto_keys:
                chunk = yaml.dump(
                    {key: feed[key]},
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                    width=120,
                )
                lines = chunk.strip().split("\n")
                for line in lines:
                    feeds_content += f"  {line}\n"

    # os.replace() is atomic on POSIX and on Windows (NTFS).
    tmp_path = feeds_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(preamble)
        f.write(feeds_content)
        f.flush()
        try:
            os.fsync(f.fileno())
        except (OSError, AttributeError):
            # fsync isn't available on every platform/file type — best effort.
            pass
    os.replace(tmp_path, feeds_path)

    # Restrict to owner-only on POSIX. feeds.yaml carries Chronicle
    # instance UUID, feed UUIDs, source endpoints and team metadata —
    # default umask (often 022 → mode 0644) would leave it readable by
    # every local user. Skipped on Windows where st_mode is not
    # meaningful (NTFS uses ACLs) and on read-only mounts where chmod
    # raises PermissionError — the GCS-mounted Cloud Run path is
    # already read-only via --readonly=true on the volume mount.
    if os.name != "nt":
        try:
            os.chmod(feeds_path, 0o600)
        except OSError as e:
            logger.debug(f"Could not chmod {feeds_path} to 0600: {e}")

    logger.info(f"✅ Feeds written to {feeds_path}")



# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    sync_feeds()
