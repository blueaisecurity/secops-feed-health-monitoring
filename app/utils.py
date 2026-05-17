# app/utils.py
"""
Cross-module helpers: bounded-time SDK calls, retry-with-backoff, and
output-masking helpers used to keep sensitive identifiers (project IDs,
customer UUIDs, connection strings) out of terminal output by default.
"""
import atexit
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

logger = logging.getLogger(__name__)


# ============================================================
# Output-masking helpers
# ============================================================
#
# All masking is opt-out, not opt-in: anything routed through ``mask_id`` /
# ``scrub_text`` is masked unless the user sets ``FEEDHEALTH_UNMASK=1``.
# Files on disk (feeds.yaml, Jira tickets, emails) are never masked — only
# the terminal/log stream is.

_UNMASK_ENV = "FEEDHEALTH_UNMASK"

# Keys inside Chronicle source_settings that are safe to show as-is. Anything
# else gets value-masked. This is conservative on purpose.
_SAFE_SETTINGS_KEYS = {
    "settings_type",
    "source_type",
}

# Module-level cache so the "ignored on non-TTY" warning fires at most once.
_UNMASK_WARNED = False


def unmask_enabled():
    """True if the user has opted out of terminal masking.

    Hard-restricted to interactive terminals: ``FEEDHEALTH_UNMASK=1`` is
    silently ignored when stdout is not a TTY (Cloud Run Job, cron, CI,
    redirected pipes). This prevents an operator who set the env var for a
    local debug session from accidentally shipping unmasked IDs to Cloud
    Logging if the same env list is re-used in a Cloud Run deploy.
    """
    import sys
    global _UNMASK_WARNED
    if os.environ.get(_UNMASK_ENV) != "1":
        return False
    if not sys.stdout.isatty():
        if not _UNMASK_WARNED:
            logger.warning(
                "FEEDHEALTH_UNMASK=1 ignored: stdout is not a TTY "
                "(non-interactive run — masking stays ON to keep identifiers "
                "out of persistent logs)."
            )
            _UNMASK_WARNED = True
        return False
    return True


def mask_id(value, keep=4):
    """Mask an identifier (UUID, project number, etc.) for terminal output.

    Keeps the first ``keep`` chars and a fixed-width ``*******`` tail so
    the masked form is still grep-able but doesn't leak the full ID.
    """
    if value in (None, ""):
        return "—"
    if unmask_enabled():
        return str(value)
    s = str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return f"{s[:keep]}…*******"


def scrub_text(text, *secrets):
    """Replace each non-empty secret in ``text`` with its masked form.

    Useful for sanitising SDK error messages / log lines that may embed the
    project ID or customer UUID inside a longer string.
    """
    if not text:
        return text
    if unmask_enabled():
        return str(text)
    out = str(text)
    for s in secrets:
        if s:
            out = out.replace(str(s), mask_id(s))
    return out


def mask_log_type(log_type):
    """Strip the ``projects/.../instances/.../logTypes/<NAME>`` prefix.

    Returns just ``<NAME>`` when masking is on, or the full path when the
    user has set ``FEEDHEALTH_UNMASK=1``.
    """
    if not log_type:
        return "—"
    if unmask_enabled():
        return str(log_type)
    s = str(log_type)
    # Last "/<segment>" — works for both full resource paths and short names.
    return s.rsplit("/", 1)[-1] if "/" in s else s


def mask_source_settings(settings):
    """Mask values inside a Chronicle source_settings dict.

    Connection strings, hostnames, account IDs and similar high-signal
    fields are routed through ``mask_id``; only a small allow-list of
    structural keys (e.g. ``settings_type``) is shown verbatim.
    """
    if not settings:
        return settings
    if unmask_enabled():
        return settings
    if not isinstance(settings, dict):
        return mask_id(settings, keep=6)
    out = {}
    for k, v in settings.items():
        if k in _SAFE_SETTINGS_KEYS or v in (None, "", [], {}):
            out[k] = v
        else:
            out[k] = mask_id(v, keep=4)
    return out


_HINT = (
    "(masked — set FEEDHEALTH_UNMASK=1 to show full values)"
)


def masking_hint():
    """One-line hint string telling the user how to disable masking."""
    return _HINT


# Single shared executor — daemon threads so abandoned calls never block exit.
_EXECUTOR = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="fhm-sdk",
)

# Make sure the executor is torn down cleanly on interpreter shutdown so we
# don't leave gRPC channels or sockets dangling. wait=False because timed-out
# SDK calls may never return; we don't want shutdown to block forever.
atexit.register(_EXECUTOR.shutdown, wait=False, cancel_futures=True)


class SDKTimeoutError(Exception):
    """Raised when an SDK call exceeds its allotted time budget."""


def with_timeout(fn, timeout_seconds, label="SDK call"):
    """
    Run ``fn()`` in a worker thread and raise ``SDKTimeoutError`` if it does
    not complete within ``timeout_seconds``.

    The thread is not forcibly killed (Python doesn't permit that), but the
    caller stops waiting and the daemon worker is allowed to die with the
    process. Treat any state mutated after the timeout as untrusted.
    """
    if not timeout_seconds or timeout_seconds <= 0:
        return fn()
    future = _EXECUTOR.submit(fn)
    try:
        return future.result(timeout=timeout_seconds)
    except FuturesTimeoutError:
        future.cancel()
        raise SDKTimeoutError(
            f"{label} did not complete within {timeout_seconds}s"
        )


def retry_with_backoff(
    fn,
    retries=2,
    base_delay=10,
    max_delay=60,
    label="API call",
    timeout_seconds=None,
):
    """
    Retry ``fn()`` with exponential backoff (delay * 2**attempt, capped).

    If ``timeout_seconds`` is set, each attempt is bounded by that deadline.
    A ``SDKTimeoutError`` counts as a failed attempt and triggers a retry.
    """
    last_exc = None
    for attempt in range(retries + 1):
        try:
            if timeout_seconds:
                return with_timeout(fn, timeout_seconds, label=label)
            return fn()
        except Exception as e:
            last_exc = e
            if attempt == retries:
                raise
            sleep_for = min(base_delay * (2 ** attempt), max_delay)
            logger.warning(
                f"  ⚠️ {label} failed (attempt {attempt + 1}/{retries + 1}): {e} "
                f"— retrying in {sleep_for}s"
            )
            time.sleep(sleep_for)
    # Unreachable, but keeps type-checkers happy.
    raise last_exc  # type: ignore[misc]
