import logging
import time
import uuid
from secops import SecOpsClient
from app.utils import with_timeout, SDKTimeoutError

logger = logging.getLogger(__name__)

_chronicle = None


def _is_valid_feed_id(feed_id):
    """
    Chronicle feed IDs are UUIDs. Validate strictly so a typo / wrong value
    in config can never act on an unrelated feed.
    """
    if not feed_id or not isinstance(feed_id, str):
        return False
    try:
        uuid.UUID(feed_id)
        return True
    except (ValueError, AttributeError):
        return False


def _sdk_timeout(config):
    """Per-call timeout for Chronicle SDK calls (seconds, default 60)."""
    return int(
        (config.get("global_settings") or {}).get("chronicle_timeout_seconds", 60)
    )


def get_chronicle_client(config):
    """Create and cache a Chronicle client."""
    global _chronicle
    if _chronicle is not None:
        return _chronicle

    try:
        client = SecOpsClient()
        _chronicle = client.chronicle(
            customer_id=config["customer_id"],
            project_id=config["project_id"],
            region=config["region"],
        )
        logger.info("Chronicle client created OK")
        return _chronicle
    except Exception as e:
        logger.error(f"Failed to create Chronicle client: {e}")
        return None


def disable_feed(config, feed_id):
    """
    Disable a feed in Chronicle using the SecOps SDK.
    
    Args:
        config: Application config dict
        feed_id: The feed's unique ID (e.g., "00000000-0000-0000-0000-000000000000")
    
    Returns:
        bool: True if successful, False otherwise
    """
    chronicle = get_chronicle_client(config)
    if chronicle is None:
        return False

    if not _is_valid_feed_id(feed_id):
        logger.error(f"Refusing to disable feed: invalid feed_id {feed_id!r} (must be a UUID)")
        return False

    try:
        with_timeout(
            lambda: chronicle.disable_feed(feed_id),
            _sdk_timeout(config),
            label=f"disable_feed({feed_id})",
        )
        logger.info(f"  🔴 Feed {feed_id} DISABLED")
        return True
    except SDKTimeoutError as e:
        logger.error(f"Failed to disable feed {feed_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to disable feed {feed_id}: {e}")
        return False


def enable_feed(config, feed_id):
    """
    Enable a feed in Chronicle using the SecOps SDK.
    
    Args:
        config: Application config dict
        feed_id: The feed's unique ID (e.g., "00000000-0000-0000-0000-000000000000")
    
    Returns:
        bool: True if successful, False otherwise
    """
    chronicle = get_chronicle_client(config)
    if chronicle is None:
        return False

    if not _is_valid_feed_id(feed_id):
        logger.error(f"Refusing to enable feed: invalid feed_id {feed_id!r} (must be a UUID)")
        return False

    try:
        with_timeout(
            lambda: chronicle.enable_feed(feed_id),
            _sdk_timeout(config),
            label=f"enable_feed({feed_id})",
        )
        logger.info(f"  🟢 Feed {feed_id} ENABLED")
        return True
    except SDKTimeoutError as e:
        logger.error(f"Failed to enable feed {feed_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Failed to enable feed {feed_id}: {e}")
        return False


def restart_feed(config, feed_id, wait_after_disable_seconds=15, wait_after_enable_seconds=15):
    """
    Restart a feed by disabling, waiting, re-enabling, and waiting again.
    
    Args:
        config: Application config dict
        feed_id: The feed's unique ID
        wait_after_disable_seconds: Seconds to wait after disabling before re-enabling (default: 15)
        wait_after_enable_seconds: Seconds to wait after re-enabling before continuing (default: 15)
    
    Returns:
        bool: True if restart completed successfully, False otherwise
    """
    logger.info(f"  🔄 [AUTO-RESTART] Attempting to restart feed {feed_id}")
    
    # Step 1: Disable
    if not disable_feed(config, feed_id):
        logger.error(f"  🔄 [AUTO-RESTART] Failed to disable feed — aborting restart")
        return False
    
    # Step 2: Wait after disable
    logger.info(f"  🔄 [AUTO-RESTART] Waiting {wait_after_disable_seconds}s before re-enabling...")
    time.sleep(wait_after_disable_seconds)
    
    # Step 3: Enable
    if not enable_feed(config, feed_id):
        logger.error(f"  🔄 [AUTO-RESTART] Failed to re-enable feed — FEED LEFT DISABLED!")
        return False
    
    # Step 4: Wait after enable
    logger.info(f"  🔄 [AUTO-RESTART] Waiting {wait_after_enable_seconds}s for feed to stabilize...")
    time.sleep(wait_after_enable_seconds)
    
    logger.info(f"  🔄 [AUTO-RESTART] Feed restarted successfully")
    return True
