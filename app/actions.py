import logging
from datetime import datetime, timezone
from app.llm import run_llm_investigation
from app.jira_client import create_jira_ticket, find_existing_jira_ticket
from app.email_client import send_email_alert
from app.utils import mask_log_type, mask_source_settings, masking_hint, unmask_enabled

logger = logging.getLogger(__name__)


# ============================================================
# Action Executor (unhealthy feeds only)
# ============================================================

def execute_actions(config, feed_config, check_results, feed_metadata=None, was_restarted=False):
    """
    Execute configured actions when a feed is unhealthy.
    """
    feed_name = feed_config["name"]
    actions = feed_config.get("actions_on_failure", ["log_only"])

    # ── Build summary of failures ──
    failures = []
    for check_name, (healthy, details) in check_results.items():
        if not healthy:
            failures.append(f"  - [{check_name}] {details}")

    failure_summary = "\n".join(failures)

    # ── Pre-flight Jira dedupe ──
    # If a Jira ticket would be created and an unresolved one already exists,
    # skip BOTH the LLM investigation AND the create call. LLM is the only
    # paid call in this chain, so deduping before it matters cost-wise.
    # The same dedupe key is used to suppress repeated email alerts so a
    # stuck-broken feed doesn't spam the inbox every run.
    jira_will_run = (
        "jira" in actions
        and config.get("actions", {}).get("jira", {}).get("enabled", False)
    )
    email_will_run = (
        "email" in actions
        and config.get("actions", {}).get("email", {}).get("enabled", False)
    )
    email_dedupe_via_jira = (
        config.get("actions", {}).get("email", {}).get("dedupe_via_jira", True)
    )
    existing_jira_key = None
    # Look up an existing ticket if Jira itself will run, OR if email is
    # configured to dedupe via Jira (lets email reuse the same signal even
    # when the ticket was created on a previous run).
    if jira_will_run or (email_will_run and email_dedupe_via_jira):
        try:
            existing_jira_key = find_existing_jira_ticket(config, feed_config)
        except Exception as e:
            logger.warning(f"  🎫 [JIRA] Pre-flight dedupe lookup failed: {e} — proceeding")

    # ── LLM investigation (lazy) ──
    # Only run if (a) some action actually consumes the result, AND (b) we
    # are not about to dedupe-skip every consumer.
    #
    # When Jira is in the action list AND an unresolved ticket already
    # exists, we treat the whole feed as already-handled and skip every
    # subsequent action (LLM, email, restart, log_only). Rationale: the
    # original ticket already carries the LLM findings + restart status
    # from the run that created it, so re-running them adds cost/noise
    # without producing any new operator-visible output.
    jira_deduped = jira_will_run and existing_jira_key is not None

    llm_enabled = config.get("investigation", {}).get("llm", {}).get("enabled", False)
    llm_findings = None
    needs_llm_for_jira = jira_will_run and existing_jira_key is None
    needs_llm_for_email = (
        email_will_run
        and not (email_dedupe_via_jira and existing_jira_key)
    )
    needs_llm_standalone = "llm" in actions and not jira_deduped
    if llm_enabled and (needs_llm_for_jira or needs_llm_for_email or needs_llm_standalone):
        try:
            llm_findings = run_llm_investigation(config, feed_config, check_results, feed_metadata)
        except Exception as e:
            logger.error(f"  🤖 [LLM] Investigation failed: {e}")

    bar = "=" * 60
    logger.warning(
        f"\n{bar}\n"
        f"⚠️  UNHEALTHY FEED: {feed_name}\n"
        f"{bar}\n"
        f"Failed checks:\n{failure_summary}"
    )

    # ── Log rich metadata if available ──
    if feed_metadata and "feed_state" in feed_metadata:
        meta = feed_metadata["feed_state"]
        details_lines = [
            "📋 Feed Details:",
            f"   State:           {meta.get('state', '—')}",
            f"   Source Type:     {meta.get('source_type', '—')}",
            f"   Log Type:        {mask_log_type(meta.get('log_type'))}",
            f"   Namespace:       {meta.get('namespace', '—')}",
            f"   Last Run:        {meta.get('last_run', '—')}",
        ]
        if meta.get("source_settings"):
            details_lines.append(
                f"   Source Settings: {mask_source_settings(meta['source_settings'])}"
            )
        if not unmask_enabled():
            details_lines.append(f"   ({masking_hint()})")
        logger.warning("\n".join(details_lines))

    logger.warning(f"Configured actions: {actions}")

    # ── Execute each action ──
    for action in actions:
        # Short-circuit: if Jira is in the action list AND an unresolved
        # ticket already exists for this feed, skip every other action
        # too (LLM, email, restart, log_only). The existing ticket is
        # the canonical record for this outage; further actions would
        # only add cost (LLM) or noise (duplicate emails, repeated
        # restarts) without changing operator outcome.
        if jira_deduped and action != "jira":
            logger.info(
                f"  ⏭️ [{action.upper()}] Skipping — Jira ticket "
                f"{existing_jira_key} already tracks this feed"
            )
            continue

        if action == "log_only":
            logger.info(f"  📝 [LOG_ONLY] Issue logged for feed: {feed_name}")

        elif action == "jira":
            if config.get("actions", {}).get("jira", {}).get("enabled"):
                if existing_jira_key:
                    logger.warning(
                        f"  🎫 [JIRA] Skipping — unresolved ticket "
                        f"{existing_jira_key} already exists for '{feed_name}' "
                        f"(all subsequent actions skipped — see ticket for details)"
                    )
                else:
                    create_jira_ticket(
                        config, feed_config, check_results, feed_metadata,
                        was_restarted=was_restarted,
                        llm_findings=llm_findings,
                    )
            else:
                logger.info(f"  🎫 [JIRA] Disabled in config — skipping")

        elif action == "email":
            if config.get("actions", {}).get("email", {}).get("enabled"):
                if email_dedupe_via_jira and existing_jira_key:
                    logger.warning(
                        f"  📧 [EMAIL] Skipping — unresolved Jira ticket "
                        f"{existing_jira_key} already exists for '{feed_name}' "
                        f"(set actions.email.dedupe_via_jira: false to override)"
                    )
                else:
                    send_email_alert(
                        config, feed_config, check_results, feed_metadata,
                        was_restarted=was_restarted,
                        llm_findings=llm_findings,
                    )
            else:
                logger.info(f"  📧 [EMAIL] Disabled in config — skipping")

        elif action == "restart_feed":
            logger.info(f"  🔄 [RESTART] Would restart feed: {feed_name}")
            # TODO: Implement feed restart via Chronicle API

        elif action == "llm":
            if config.get("investigation", {}).get("llm", {}).get("enabled"):
                # Re-use pre-computed findings to avoid double-call
                findings = llm_findings
                if findings is None:
                    logger.info(f"  🤖 [LLM] Running investigation for: {feed_name}")
                    findings = run_llm_investigation(config, feed_config, check_results, feed_metadata)
                if findings:
                    import textwrap

                    # Route LLM output through the logging framework so it
                    # respects the configured log_level and is captured by
                    # any log sink (Cloud Logging, file handlers, etc.).
                    separator = "─" * 60
                    logger.warning(separator)
                    logger.warning(f"🤖 LLM Investigation Results for '{feed_name}'")
                    logger.warning(separator)
                    for line in findings.strip().split("\n"):
                        if len(line) > 70:
                            for wrapped in textwrap.wrap(line, width=70):
                                logger.warning(wrapped)
                        else:
                            logger.warning(line)
                    logger.warning(separator)
            else:
                logger.info(f"  🤖 [LLM] Disabled in config — skipping")

        else:
            logger.warning(f"  ❓ Unknown action: {action}")

    logger.warning(f"{'='*60}\n")
