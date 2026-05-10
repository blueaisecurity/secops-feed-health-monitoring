# app/main.py
import logging
import os
import sys
from app.config import load_config, ConfigError
from app.checks import check_feed_state, check_gcp_metrics, check_udm_search
from app.actions import execute_actions
from app.chronicle_client import get_chronicle_client, restart_feed
from app.utils import mask_id, masking_hint, unmask_enabled


# ============================================================
# Logging setup
# ============================================================

# Sentinel value for the configured log_level field. "PROD" is not a real
# Python logging level — it maps to ERROR (suppresses all INFO/WARNING/DEBUG)
# and triggers a single sanitized COMPLETED line at the end of the run, so
# Cloud Logging alerts can fire on severity>=ERROR without being drowned
# in routine output.
_PROD_LEVEL = "PROD"

# Levels that may render unmasked-ish detail to the terminal. We always mask
# IDs by default, but DEBUG also dumps SDK payloads from upstream libraries,
# so we prompt for explicit confirmation before enabling it.
_SENSITIVE_LEVELS = {"DEBUG"}

# Third-party loggers that flood DEBUG with HTTP traces, OAuth handshakes,
# and unmasked request URLs (which embed project_id + customer_id). We pin
# them to WARNING regardless of our level — the user almost never wants
# urllib3 / google.auth chatter, and silencing them keeps secrets out of
# the terminal even when our own log_level is DEBUG.
_NOISY_LOGGERS = (
    "urllib3",
    "urllib3.connectionpool",
    "urllib3.util.retry",
    "google.auth",
    "google.auth._default",
    "google.auth.transport",
    "google.auth.transport.requests",
    "google.api_core",
    "googleapiclient",
    "googleapiclient.discovery",
    "grpc",
    "requests",
)


class _BlockFormatter(logging.Formatter):
    """Formatter that prints the prefix once per record.

    For single-line messages this behaves exactly like the default
    formatter. For multi-line messages (e.g. a pretty-printed JSON dump)
    only the first line gets the ``timestamp [LEVEL] logger —`` prefix;
    continuation lines are indented to line up underneath the message
    column. This makes DEBUG dumps scannable instead of producing many
    nearly-identical prefix lines.
    """

    def format(self, record):
        msg = record.getMessage()
        prefix = (
            f"{self.formatTime(record, self.datefmt)} "
            f"[{record.levelname}] {record.name} — "
        )
        if "\n" not in msg:
            return prefix + msg
        lines = msg.split("\n")
        indent = " " * 4
        first = prefix + lines[0]
        rest = "\n".join(indent + ln for ln in lines[1:])
        return f"{first}\n{rest}"


def _is_prod_mode(level):
    return isinstance(level, str) and level.strip().upper() == _PROD_LEVEL


def setup_logging(level="INFO"):
    if _is_prod_mode(level):
        effective = logging.ERROR
    else:
        effective = getattr(logging, level.upper(), logging.INFO)

    # Reset any handlers basicConfig may have already attached so we own the
    # output format end-to-end (matters when run_monitoring is invoked twice
    # in the same process, e.g. by tests).
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(_BlockFormatter(datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(effective)

    # Cap noisy upstream libraries at WARNING. They will still surface real
    # errors, just not per-request HTTP traces or OAuth token chatter.
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _confirm_sensitive_level(level):
    """For DEBUG (and any other sensitive level), require explicit consent.

    Skipped automatically when:
      - stdin is not a TTY (cron, Cloud Run Job, CI),
      - FEEDHEALTH_NO_CONFIRM=1 is set.

    Note: we deliberately do NOT skip the prompt when FEEDHEALTH_UNMASK=1 —
    unmasking actively *increases* the sensitivity of DEBUG output, so the
    user should still consciously confirm.
    """
    norm = (level or "").strip().upper()
    if norm not in _SENSITIVE_LEVELS:
        return
    if os.environ.get("FEEDHEALTH_NO_CONFIRM") == "1":
        return
    if not sys.stdin.isatty():
        return

    if unmask_enabled():
        masking_line = (
            "   Identifier masking is OFF (FEEDHEALTH_UNMASK=1) — project_id,\n"
            "   customer_id, and feed UUIDs WILL appear in plain text.\n"
        )
    else:
        masking_line = (
            "   Identifier masking is ON — project_id, customer_id, and feed\n"
            "   UUIDs are still hidden. Set FEEDHEALTH_UNMASK=1 if you also\n"
            "   need raw values (not recommended for shared terminals).\n"
        )

    print(
        f"\n⚠️  log_level is set to {norm}.\n"
        f"   Verbose output may include SDK request/response bodies, error\n"
        f"   payloads, and other diagnostic detail. Run on a trusted terminal\n"
        f"   and avoid pasting the output into tickets / chat without review.\n"
        f"{masking_line}",
        file=sys.stderr,
    )
    try:
        answer = input(f"Continue with {norm} logging? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print("Aborted by user.", file=sys.stderr)
        sys.exit(130)


def _announce_log_level(level, logger):
    """Log a single line describing the active log level + masking state."""
    norm = (level or "INFO").strip().upper()
    if _is_prod_mode(norm):
        # PROD intentionally suppresses INFO; nothing to announce.
        return
    if unmask_enabled():
        masking = "OFF (FEEDHEALTH_UNMASK=1)"
    else:
        masking = "ON (set FEEDHEALTH_UNMASK=1 to disable)"
    logger.warning(f"🔧 log_level={norm} | output masking: {masking}")


# ============================================================
# Check runner — normalises all return formats
# ============================================================

def run_check(check_fn, config, feed_config, feeds_cache=None):
    """
    Run a single check and normalise the return value.
    Now passes feeds_cache through to checks that support it.
    """
    result = check_fn(config, feed_config, feeds_cache=feeds_cache)

    # ── Dict return ──
    if isinstance(result, dict):
        healthy = result.get("healthy", result.get("is_healthy", False))
        details = result.get("details", "No details")
        metadata = result.get("metadata", result.get("feed_data", {}))
        return healthy, details, metadata

    # ── Tuple of 3 ──
    if isinstance(result, tuple) and len(result) == 3:
        return result[0], result[1], result[2]

    # ── Tuple of 2 ──
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], result[1], {}

    # ── Fallback ──
    return False, f"Unexpected return format: {type(result)}", {}


# ============================================================
# Feed cache loader
# ============================================================

def _load_feeds_cache(config):
    """
    Fetch all feeds from Chronicle once.
    Returns the list, or None if unavailable.
    """
    logger = logging.getLogger(__name__)

    chronicle = get_chronicle_client(config)
    if chronicle is None:
        logger.warning("Chronicle client unavailable — feed cache will be empty")
        return None

    retry_cfg = config.get("global_settings", {})
    retries = retry_cfg.get("retry_count", 2)
    delay = retry_cfg.get("retry_delay_seconds", 10)

    try:
        from app.utils import retry_with_backoff
        from app.checks import _sdk_timeout

        feeds = retry_with_backoff(
            chronicle.list_feeds,
            retries=retries,
            base_delay=delay,
            label="list_feeds",
            timeout_seconds=_sdk_timeout(config),
        )
        logger.info(f"📡 Cached {len(feeds)} feed(s) from Chronicle API")
        return feeds

    except Exception as e:
        logger.error(f"Failed to cache feeds from Chronicle: {e}")
        return None


# ============================================================
# Main monitoring logic
# ============================================================

def run_monitoring():
    """Core monitoring function — runs all checks for all enabled feeds."""

    config = load_config("config.yaml")
    log_level = config.get("global_settings", {}).get("log_level", "WARNING")
    _confirm_sensitive_level(log_level)
    setup_logging(log_level)

    logger = logging.getLogger(__name__)
    _announce_log_level(log_level, logger)

    # ── Optional: auto-sync feeds from Chronicle on startup ──
    auto_sync_cfg = config.get("global_settings", {}).get("auto_sync", {})
    if auto_sync_cfg.get("enabled", False):
        logger.info("🔄 Auto-sync enabled — syncing feeds from Chronicle...")
        try:
            from app.sync_feeds import sync_feeds
            sync_feeds("config.yaml")
            # Reload config so the in-memory feed list reflects the sync result
            config = load_config("config.yaml")
        except Exception as e:
            logger.error(f"Auto-sync failed: {e} — continuing with current config")

    feeds = config.get("feeds", [])
    # Note: don't early-return on empty feeds — the global ingestion-volume
    # monitor (if enabled) is project-wide and should still run.
    if not feeds:
        logger.warning("No feeds defined in config — skipping per-feed checks.")

    enabled_feeds = [f for f in feeds if f.get("enabled", False)]

    logger.info(
        f"Starting feed health monitoring — "
        f"{len(enabled_feeds)}/{len(feeds)} feed(s) enabled"
    )
    logger.info(
        f"Project: {mask_id(config['project_id'])} | "
        f"Customer: {mask_id(config['customer_id'])} | "
        f"Region: {config['region']}"
    )

    # ── Pre-fetch all feeds ONCE ──
    feeds_cache = _load_feeds_cache(config)

    check_map = {
        "feed_state":  check_feed_state,
        "gcp_metrics": check_gcp_metrics,
        "udm_search":  check_udm_search,
    }

    summary = {"healthy": 0, "unhealthy": 0, "skipped": 0}

    for feed_config in feeds:
        if not feed_config.get("enabled", False):
            logger.info(f"  ⏭️ Feed '{feed_config['name']}' is disabled — skipping")
            summary["skipped"] += 1
            continue

        feed_name = feed_config["name"]
        checks = feed_config.get("checks", [])

        bar = "─" * 60
        logger.info(
            f"\n{bar}\n"
            f"📡 Checking feed: {feed_name}\n"
            f"   Checks to run: {checks}\n"
            f"{bar}"
        )

        check_results = {}
        feed_metadata = {}
        feed_healthy = True

        for check_name in checks:
            check_fn = check_map.get(check_name)
            if check_fn is None:
                logger.warning(f"  ❓ Unknown check: {check_name} — skipping")
                continue

            logger.info(f"\n  🔍 Running check: {check_name}")

            try:
                healthy, details, metadata = run_check(
                    check_fn, config, feed_config,
                    feeds_cache=feeds_cache,           # ← passed through
                )

                check_results[check_name] = (healthy, details)

                if metadata:
                    feed_metadata[check_name] = metadata

                if healthy:
                    logger.info(f"  ✅ {check_name}: PASSED")
                else:
                    logger.warning(f"  ❌ {check_name}: FAILED — {details}")
                    feed_healthy = False

            except Exception as e:
                logger.error(f"  💥 Check '{check_name}' crashed: {e}")
                check_results[check_name] = (False, f"Exception: {e}")
                feed_healthy = False

        # ── Results ──
        if feed_healthy:
            logger.info(f"\n  ✅ Feed '{feed_name}' is HEALTHY — all checks passed")
            summary["healthy"] += 1
        else:
            was_restarted = False
            # ── Check if auto-restart is enabled ──
            auto_restart_cfg = config.get("global_settings", {}).get("auto_restart", {})
            auto_restart_enabled = auto_restart_cfg.get("enabled", False)
            
            # Check if feed_state check failed (indicates error state)
            feed_state_failed = not check_results.get("feed_state", (True, ""))[0]
            
            if auto_restart_enabled and feed_state_failed:
                feed_id = feed_config.get("chronicle_feed_id", "")
                # NOTE: no per-feed cooldown is enforced yet — a permanently
                # broken feed will be disable→re-enabled on every run. Add a
                # cooldown (backed by a managed store such as Firestore /
                # Secret Manager / Cloud Run volume) before deploying to a
                # high-frequency schedule.
                logger.warning(
                    f"\n  🔄 [AUTO-RESTART] Feed '{feed_name}' is in error state — attempting restart..."
                )

                wait_after_disable = auto_restart_cfg.get("wait_after_disable_seconds", 15)
                wait_after_enable = auto_restart_cfg.get("wait_after_enable_seconds", 15)

                restart_ok = bool(feed_id) and restart_feed(
                    config, feed_id, wait_after_disable, wait_after_enable
                )

                if not restart_ok:
                    logger.error(f"  🔄 [AUTO-RESTART] Restart failed for feed '{feed_name}'")
                else:
                    was_restarted = True

                    # Re-run checks after restart (just once)
                    logger.info(f"\n  🔄 [AUTO-RESTART] Re-running checks after restart...")

                    # Refresh feeds cache after restart
                    feeds_cache_refreshed = _load_feeds_cache(config)

                    check_results_retry = {}
                    feed_metadata_retry = {}
                    feed_healthy_retry = True

                    for check_name in checks:
                        check_fn = check_map.get(check_name)
                        if check_fn is None:
                            continue

                        logger.info(f"\n  🔍 [RETRY] Running check: {check_name}")

                        try:
                            healthy, details, metadata = run_check(
                                check_fn, config, feed_config,
                                feeds_cache=feeds_cache_refreshed,
                            )
                            check_results_retry[check_name] = (healthy, details)
                            if metadata:
                                feed_metadata_retry[check_name] = metadata

                            if healthy:
                                logger.info(f"  ✅ {check_name}: PASSED")
                            else:
                                logger.warning(f"  ❌ {check_name}: FAILED — {details}")
                                feed_healthy_retry = False
                        except Exception as e:
                            logger.error(f"  💥 Check '{check_name}' crashed: {e}")
                            check_results_retry[check_name] = (False, f"Exception: {e}")
                            feed_healthy_retry = False

                    if feed_healthy_retry:
                        logger.info(f"\n  ✅ [AUTO-RESTART] Feed '{feed_name}' recovered after restart!")
                        summary["healthy"] += 1
                        continue  # Skip to next feed, don't execute failure actions

                    logger.warning(f"\n  ❌ [AUTO-RESTART] Feed '{feed_name}' still unhealthy after restart")
                    # Use the retry results for actions
                    check_results = check_results_retry
                    feed_metadata = feed_metadata_retry

            logger.warning(f"\n  ❌ Feed '{feed_name}' is UNHEALTHY")
            summary["unhealthy"] += 1
            execute_actions(config, feed_config, check_results, feed_metadata, was_restarted=was_restarted)

    # ── Global ingestion-volume guardrail (project-wide, runs once per cycle) ──
    summary["ingestion_over_threshold"] = 0
    try:
        from app.ingestion_monitor import (
            check_global_ingestion,
            build_synthetic_feed_config,
        )
        ingest_result = check_global_ingestion(config)
        if ingest_result.get("enabled") and ingest_result.get("over_threshold"):
            summary["ingestion_over_threshold"] = 1
            logger.warning(f"\n  📈 INGESTION VOLUME BREACH: {ingest_result['details']}")
            synthetic_feed = build_synthetic_feed_config(config)
            synthetic_check_results = {
                "global_ingestion": (False, ingest_result["details"]),
            }
            execute_actions(
                config,
                synthetic_feed,
                synthetic_check_results,
                feed_metadata={},
                was_restarted=False,
            )
    except Exception as e:
        logger.error(f"  📈 [INGEST-MONITOR] Failed to run global ingestion check: {e}")

    # ── Final summary ──
    log_level = config.get("global_settings", {}).get("log_level", "INFO")
    if _is_prod_mode(log_level):
        # PROD mode: emit one terse, sanitized line via stdout so it lands in
        # Cloud Logging at severity=DEFAULT (won't trigger ERROR-based alerts)
        # and contains NO sensitive data (no project/customer/region/feed
        # names or IDs — counts only).
        print(
            f"COMPLETED: healthy={summary['healthy']} "
            f"unhealthy={summary['unhealthy']} "
            f"skipped={summary['skipped']} "
            f"total={len(feeds)} "
            f"ingestion_over_threshold={summary['ingestion_over_threshold']}",
            flush=True,
        )
    else:
        logger.info(f"\n{'=' * 60}")
        logger.info(f"📊 Monitoring Complete")
        logger.info(f"   ✅ Healthy:   {summary['healthy']}")
        logger.info(f"   ❌ Unhealthy: {summary['unhealthy']}")
        logger.info(f"   ⏭️ Skipped:   {summary['skipped']}")
        logger.info(f"   📋 Total:     {len(feeds)}")
        if summary['ingestion_over_threshold']:
            logger.warning(f"   📈 Ingestion volume OVER threshold")
        logger.info(f"{'=' * 60}")

    return summary


# ============================================================
# Entry point
# ============================================================

if __name__ == "__main__":
    try:
        run_monitoring()
    except ConfigError as e:
        # Configuration / variables.yaml problem — surface a clear message
        # and exit non-zero so cron / Cloud Scheduler / Cloud Run treat the
        # run as failed.
        logging.basicConfig(level=logging.ERROR)
        logging.getLogger(__name__).error(f"Configuration error: {e}")
        sys.exit(2)
