# app/jira_client.py
"""
Jira Cloud integration — API token + Basic auth (headless, Cloud-Run friendly).

Builds a rich ADF-formatted ticket with sections for error info, feed info,
troubleshooting check results, restart status, and the LLM investigation.
"""
import logging
import os
import re
from datetime import datetime, timezone

import requests
from requests.auth import HTTPBasicAuth

from app.checks import _resolve_window_hours

logger = logging.getLogger(__name__)

# Path to the on-disk secrets file (only read if the matching env var is unset).
_VARIABLES_FILE = "variables.yaml"


def _load_jira_secrets():
    """
    Resolve Jira secrets with env vars taking precedence over variables.yaml.

    Order for each value:
        1. Environment variable (JIRA_API_KEY / JIRA_API_URL / JIRA_USER_EMAIL)
        2. variables.yaml (jira_api_key / jira_api_url / jira_user_email)

    Secrets are NOT read from the main config dict — keeps them out of any
    object that may be logged or passed to the LLM.
    """
    file_vars = {}
    if os.path.exists(_VARIABLES_FILE):
        try:
            import yaml
            with open(_VARIABLES_FILE, "r", encoding="utf-8") as f:
                file_vars = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"  🎫 [JIRA] Could not read {_VARIABLES_FILE}: {e}")

    return {
        "api_key":    os.environ.get("JIRA_API_KEY")    or file_vars.get("jira_api_key", ""),
        "api_url":    os.environ.get("JIRA_API_URL")    or file_vars.get("jira_api_url", ""),
        "user_email": os.environ.get("JIRA_USER_EMAIL") or file_vars.get("jira_user_email", ""),
    }


def _redact_response_body(text):
    """
    Return a safe, short snippet of an HTTP response body.

    Strips obvious token/credential-like strings before logging. Used in the
    Jira error path so a 401/403 body cannot leak an API token, JWT fragment,
    or internal URL with credentials.
    """
    if not text:
        return "(empty body)"
    snippet = text[:200]
    # Hide things that look like tokens / secrets / Basic-auth headers.
    snippet = re.sub(r"(?i)(authorization|x-api-key|api[_-]?key|token|bearer)\s*[:=]\s*\S+",
                     r"\1=<redacted>", snippet)
    snippet = re.sub(r"\b[A-Za-z0-9_\-]{24,}\b", "<redacted>", snippet)
    return snippet


def create_jira_ticket(
    config,
    feed_config,
    check_results,
    feed_metadata=None,
    was_restarted=False,
    llm_findings=None,
):
    """Create a Jira issue describing the failed feed. Returns issue dict or None."""
    jira_cfg = config.get("actions", {}).get("jira", {})
    if not jira_cfg.get("enabled", False):
        logger.info("  🎫 [JIRA] Disabled in config — skipping")
        return None

    # Project key + issue type are non-sensitive — read from config.yaml.
    project_key = jira_cfg.get("project_key", "")
    issue_type = jira_cfg.get("issue_type", "Bug")

    # Secrets: env vars first (preferred for prod / Cloud Run / Secret Manager),
    # falling back to variables.yaml. Never read from the main config dict so
    # secrets are not part of any object that gets serialized elsewhere.
    secrets = _load_jira_secrets()
    api_url = (secrets.get("api_url") or "").rstrip("/")
    user_email = secrets.get("user_email") or ""
    api_key = secrets.get("api_key") or ""

    # ── Refuse to send Basic-auth credentials over cleartext HTTP. ──
    # The Jira API key + user email are sent in an Authorization header on
    # every request; if api_url is http:// they would be observable on the
    # wire. Hard-fail rather than silently downgrade.
    if api_url and not api_url.lower().startswith("https://"):
        logger.error(
            f"  🎫 [JIRA] api_url must use HTTPS (got: {api_url.split('://', 1)[0]}://…) "
            f"— refusing to send credentials over cleartext."
        )
        return None

    missing = [
        k for k, v in {
            "api_url": api_url,
            "api_key": api_key,
            "user_email": user_email,
            "project_key": project_key,
        }.items() if not v
    ]
    if missing:
        logger.error(f"  🎫 [JIRA] Missing config: {', '.join(missing)} — skipping")
        return None

    feed_name = feed_config.get("name", "unknown")
    feed_id = feed_config.get("chronicle_feed_id", "")
    summary = _build_summary(feed_config)

    auth = HTTPBasicAuth(user_email, api_key)

    # ── DEDUPE — skip if an unresolved ticket with the same summary exists ──
    dedupe_cfg = jira_cfg.get("dedupe", {}) or {}
    if dedupe_cfg.get("enabled", True):
        existing = _find_existing_ticket(api_url, auth, project_key, summary)
        if existing:
            logger.warning(
                f"  🎫 [JIRA] Skipping — unresolved ticket {existing} already "
                f"exists for '{feed_name}'"
            )
            return {"key": existing, "deduped": True}

    # Detect API version from the URL (v3 needs ADF; v2 accepts plain text).
    use_v3 = "/rest/api/3" in api_url
    if use_v3:
        description_field = _build_adf(
            config, feed_config, check_results, feed_metadata, was_restarted, llm_findings
        )
    else:
        description_field = _build_plaintext(
            config, feed_config, check_results, feed_metadata, was_restarted, llm_findings
        )

    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
            "description": description_field,
        }
    }

    endpoint = f"{api_url}/issue"
    try:
        resp = requests.post(
            endpoint,
            json=payload,
            auth=auth,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=30,
        )
    except requests.RequestException as e:
        logger.error(f"  🎫 [JIRA] Network error calling {endpoint}: {e}")
        return None

    if resp.status_code >= 300:
        # Do NOT log the response body at WARNING/ERROR — even after
        # redaction it can echo back tokens, internal endpoints, or PII
        # from a misconfigured Jira site. Body goes to DEBUG only.
        logger.error(
            f"  🎫 [JIRA] Failed to create ticket (HTTP {resp.status_code})"
        )
        logger.debug(
            f"  🎫 [JIRA] Create-ticket response body: {_redact_response_body(resp.text)}"
        )
        return None

    issue = resp.json()
    issue_key = issue.get("key", "?")
    logger.warning(
        f"  🎫 [JIRA] Ticket {issue_key} created for '{feed_name}'"
    )

    # ── ASSIGN — best-effort, never fail the ticket-creation flow ──
    assign_cfg = jira_cfg.get("assign", {}) or {}
    if assign_cfg.get("enabled", False) and issue_key and issue_key != "?":
        assignees = assign_cfg.get("assignees") or []
        _assign_ticket(api_url, auth, issue_key, assignees)

    return issue


# ============================================================
# Dedupe + assignment helpers
# ============================================================

def _build_summary(feed_config):
    """Single source of truth for the Jira issue summary string."""
    # Allow callers (e.g. the global ingestion-volume monitor) to override
    # the summary so dedup works on a stable, identifier-free string.
    override = feed_config.get("_summary_override")
    if override:
        return str(override)
    feed_name = feed_config.get("name", "unknown")
    return f"[Feed Health] Unhealthy feed: {feed_name}"


def find_existing_jira_ticket(config, feed_config):
    """
    Public dedupe lookup — callable from the orchestrator BEFORE running any
    expensive work (e.g. LLM investigation).

    Returns:
        - issue key (str) if an unresolved ticket with the same summary
          already exists in the configured project,
        - None if no duplicate exists, dedupe is disabled, jira is disabled,
          required config is missing, or the lookup itself fails (fail-open
          so a transient Jira hiccup never blocks a real alert).
    """
    jira_cfg = config.get("actions", {}).get("jira", {}) or {}
    if not jira_cfg.get("enabled", False):
        return None
    if not (jira_cfg.get("dedupe", {}) or {}).get("enabled", True):
        return None

    project_key = jira_cfg.get("project_key", "")
    secrets = _load_jira_secrets()
    api_url = (secrets.get("api_url") or "").rstrip("/")
    user_email = secrets.get("user_email") or ""
    api_key = secrets.get("api_key") or ""

    if not (project_key and api_url and user_email and api_key):
        # Missing config — defer to create_jira_ticket() to log the proper error.
        return None
    if not api_url.lower().startswith("https://"):
        # Same hard-fail rationale as create_jira_ticket(); don't leak creds.
        return None

    summary = _build_summary(feed_config)
    return _find_existing_ticket(
        api_url, HTTPBasicAuth(user_email, api_key), project_key, summary
    )


def _escape_jql_string(value):
    """
    Escape a value for safe embedding inside a double-quoted JQL string.

    Per Atlassian's JQL syntax, only backslash and double-quote need escaping
    inside a quoted string. We additionally:
      - coerce to str (defensive — feed names should already be strings),
      - strip ASCII control characters (incl. CR/LF/TAB) which can confuse
        the JQL parser or smuggle clauses past simple log scrapers,
      - bound the length to 200 chars (Jira summaries are 255; we leave
        headroom for the surrounding quotes and clause text).
    """
    s = str(value)
    s = "".join(ch for ch in s if ch == " " or (ch.isprintable() and ord(ch) >= 0x20))
    if len(s) > 200:
        s = s[:200]
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _find_existing_ticket(api_url, auth, project_key, summary):
    """
    Return the issue key of an unresolved ticket in `project_key` whose
    summary matches `summary` exactly, or None if no such ticket exists
    (or if the lookup fails for any reason — fail-open so dedupe never
    blocks a real alert).
    """
    # JQL: project = "KEY" AND summary ~ "\"exact summary\"" AND resolution = Unresolved
    # The escaped-quote wrapper around the summary forces a phrase match
    # in the text index instead of a token-prefix match.
    safe_summary = _escape_jql_string(summary)
    safe_project = _escape_jql_string(project_key)
    jql = (
        f'project = "{safe_project}" '
        f'AND summary ~ "\\"{safe_summary}\\"" '
        f'AND resolution = Unresolved'
    )
    # /rest/api/3/search was removed in 2025; use the enhanced search endpoint.
    # /rest/api/2/search is still available, so this same path works for both
    # API versions only on v3 sites — fall back to /search for v2 if needed.
    if "/rest/api/3" in api_url:
        endpoint = f"{api_url}/search/jql"
    else:
        endpoint = f"{api_url}/search"
    try:
        resp = requests.get(
            endpoint,
            params={"jql": jql, "fields": "summary", "maxResults": 50},
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as e:
        logger.warning(f"  🎫 [JIRA] Dedupe lookup failed (network): {e} — proceeding with create")
        return None

    if resp.status_code >= 300:
        logger.warning(
            f"  🎫 [JIRA] Dedupe lookup failed (HTTP {resp.status_code}) — proceeding with create"
        )
        logger.debug(
            f"  🎫 [JIRA] Dedupe response body: {_redact_response_body(resp.text)}"
        )
        return None

    try:
        issues = resp.json().get("issues") or []
    except ValueError:
        return None

    # `summary ~ ...` is a fuzzy text match; require an exact match on the
    # returned summary field to avoid deduping against a similarly-named
    # ticket for a different feed.
    for issue in issues:
        if (issue.get("fields") or {}).get("summary", "") == summary:
            return issue.get("key")
    return None


def _lookup_account_id(api_url, auth, identifier):
    """
    Resolve a Jira account id for an assignee identifier.

    `identifier` can be either:
      - an email address (e.g. "alice@example.com"), or
      - a raw Atlassian accountId (e.g. "5b10a2844c20165700ede21g").

    Email lookup uses /user/search, which is unreliable on sites where
    Atlassian Profile Visibility hides emails (the default for new sites
    since 2019) — in that case the search returns no results and the
    only way to assign is to configure the accountId directly.
    Returns the accountId on success, None otherwise.
    """
    if not identifier or not isinstance(identifier, str):
        return None

    # Heuristic: if it doesn't look like an email, assume it's an accountId
    # and skip the lookup entirely. /user/search would return nothing for
    # an opaque accountId anyway.
    if "@" not in identifier:
        return identifier.strip()

    endpoint = f"{api_url}/user/search"
    try:
        resp = requests.get(
            endpoint,
            params={"query": identifier},
            auth=auth,
            headers={"Accept": "application/json"},
            timeout=30,
        )
    except requests.RequestException as e:
        logger.warning(f"  🎫 [JIRA] User lookup failed (network): {e}")
        return None

    if resp.status_code >= 300:
        logger.warning(
            f"  🎫 [JIRA] User lookup failed (HTTP {resp.status_code})"
        )
        logger.debug(
            f"  🎫 [JIRA] User-lookup response body: {_redact_response_body(resp.text)}"
        )
        return None

    try:
        users = resp.json() or []
    except ValueError:
        return None

    # Prefer an exact emailAddress match; fall back to the first hit only if
    # exactly one user is returned (avoids assigning to the wrong person when
    # the query is ambiguous). Note: emailAddress is often hidden by Atlassian
    # privacy settings, in which case only displayName/accountId are returned.
    email_lc = identifier.strip().lower()
    for u in users:
        if (u.get("emailAddress") or "").lower() == email_lc:
            return u.get("accountId")
    if len(users) == 1:
        return users[0].get("accountId")
    return None


def _assign_ticket(api_url, auth, issue_key, assignees):
    """
    Assign `issue_key` to the first email in `assignees` whose Jira account
    lookup succeeds. Best-effort: failures are logged but never raised.
    """
    if not assignees:
        logger.info(f"  🎫 [JIRA] Assignment enabled but no assignees configured — skipping")
        return

    for entry in assignees:
        if not entry or not isinstance(entry, str):
            continue
        account_id = _lookup_account_id(api_url, auth, entry)
        if not account_id:
            logger.info(f"  🎫 [JIRA] No Jira account found for one configured assignee — trying next")
            continue

        endpoint = f"{api_url}/issue/{issue_key}/assignee"
        try:
            resp = requests.put(
                endpoint,
                json={"accountId": account_id},
                auth=auth,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=30,
            )
        except requests.RequestException as e:
            logger.warning(f"  🎫 [JIRA] Assign request failed (network): {e}")
            continue

        if resp.status_code >= 300:
            logger.warning(
                f"  🎫 [JIRA] Assign failed for {issue_key} (HTTP {resp.status_code})"
            )
            logger.debug(
                f"  🎫 [JIRA] Assign response body: {_redact_response_body(resp.text)}"
            )
            continue

        # Log the configured entry as-is. If it's an email it identifies the
        # person; if it's an accountId it is not personally identifying.
        logger.warning(f"  🎫 [JIRA] Ticket {issue_key} assigned to {entry}")
        return

    logger.warning(
        f"  🎫 [JIRA] Could not assign {issue_key} — none of the configured "
        f"assignees resolved to a Jira account (on sites where Atlassian "
        f"hides email addresses, configure assignees as accountIds instead)"
    )


# ============================================================
# Data extraction
# ============================================================

def _extract_error_info(feed_metadata):
    """Pull state / failureMsg / failureDetails from feed_state metadata + raw feed."""
    state = "unknown"
    failure_msg = ""
    failure_details = ""
    if not feed_metadata or "feed_state" not in feed_metadata:
        return state, failure_msg, failure_details

    fs = feed_metadata["feed_state"]
    state = fs.get("state", "unknown")
    raw = fs.get("raw_feed") or {}

    # Try common locations for failure info in the Chronicle SDK response
    failure_msg = (
        raw.get("failureMsg")
        or raw.get("failureMessage")
        or (raw.get("lastError") or {}).get("message", "")
        or ""
    )
    failure_details = (
        raw.get("failureDetails")
        or (raw.get("lastError") or {}).get("details", "")
        or ""
    )
    if isinstance(failure_details, (dict, list)):
        import json
        failure_details = json.dumps(failure_details, indent=2, default=str)
    return state, str(failure_msg), str(failure_details)


def _extract_feed_info(feed_config, feed_metadata):
    """Build a dict of feed properties for the Feed Info section."""
    info = {
        "Feed Name": feed_config.get("name", "—"),
        "Feed ID": feed_config.get("chronicle_feed_id", "—"),
        "Log Type": "—",
        "Source Type": "—",
        "Namespace": feed_config.get("namespace", "") or "—",
        "State": "—",
        "Last Feed Initiation Time": "—",
        "Labels": "—",
        "Source Settings": "—",
    }
    if feed_metadata and "feed_state" in feed_metadata:
        fs = feed_metadata["feed_state"]
        info["Log Type"] = fs.get("log_type", "—")
        info["Source Type"] = fs.get("source_type", "—")
        info["Namespace"] = fs.get("namespace", "") or info["Namespace"]
        info["State"] = fs.get("state", "—")
        info["Last Feed Initiation Time"] = fs.get("last_run", "—") or "—"
        labels = fs.get("labels") or {}
        if labels:
            info["Labels"] = ", ".join(f"{k}={v}" for k, v in labels.items())
        ss = fs.get("source_settings") or {}
        if ss:
            info["Source Settings"] = ", ".join(f"{k}={v}" for k, v in ss.items())
    return info


# ============================================================
# ADF builder (Jira REST API v3)
# ============================================================

def _adf_text(text, marks=None):
    node = {"type": "text", "text": text if text else " "}
    if marks:
        node["marks"] = marks
    return node


def _adf_para(*nodes):
    return {"type": "paragraph", "content": list(nodes) if nodes else [_adf_text(" ")]}


def _adf_heading(text, level=2):
    return {
        "type": "heading",
        "attrs": {"level": level},
        "content": [_adf_text(text)],
    }


def _adf_bullets(items):
    """items = list[str | list[node]]"""
    list_items = []
    for it in items:
        if isinstance(it, str):
            content = [_adf_para(_adf_text(it))]
        else:
            content = [_adf_para(*it)]
        list_items.append({"type": "listItem", "content": content})
    return {"type": "bulletList", "content": list_items}


def _adf_codeblock(text, language=None):
    return {
        "type": "codeBlock",
        "attrs": {"language": language} if language else {},
        "content": [_adf_text(text or " ")],
    }


def _adf_panel(panel_type, content_nodes):
    """panel_type: info | note | warning | success | error"""
    return {
        "type": "panel",
        "attrs": {"panelType": panel_type},
        "content": content_nodes,
    }


def _adf_expand(title, content_nodes):
    """Collapsible section with a clickable title."""
    return {
        "type": "expand",
        "attrs": {"title": title},
        "content": content_nodes,
    }


def _build_adf(config, feed_config, check_results, feed_metadata, was_restarted, llm_findings):
    state, failure_msg, failure_details = _extract_error_info(feed_metadata)
    feed_info = _extract_feed_info(feed_config, feed_metadata)
    feed_name = feed_config.get("name", "—")
    now_iso = datetime.now(timezone.utc).isoformat()
    last_ingested = feed_info.get("Last Feed Initiation Time", "—") or "—"

    blocks = []

    # ── Header summary panel — includes Error Info (always visible, red) ──
    error_panel_content = [
        _adf_para(
            _adf_text("Unhealthy Feed Detected", marks=[{"type": "strong"}]),
        ),
        _adf_para(
            _adf_text("Feed Name: ", marks=[{"type": "strong"}]),
            _adf_text(feed_name),
        ),
        _adf_para(
            _adf_text("Reported At: ", marks=[{"type": "strong"}]),
            _adf_text(f"{now_iso} (UTC)"),
        ),
        _adf_para(
            _adf_text("Last time ingested: ", marks=[{"type": "strong"}]),
            _adf_text(str(last_ingested)),
        ),
        _adf_para(
            _adf_text("Health Check: ", marks=[{"type": "strong"}]),
            _adf_text(state),
        ),
        _adf_para(
            _adf_text("Failure Message: ", marks=[{"type": "strong"}]),
            _adf_text(failure_msg or "(none reported)"),
        ),
    ]
    if was_restarted:
        error_panel_content.append(_adf_para(
            _adf_text("Auto-restart attempted: ", marks=[{"type": "strong"}]),
            _adf_text("Yes"),
        ))
    blocks.append(_adf_panel("error", error_panel_content))
    blocks.append(_adf_para())  # spacer

    # ── FEED INFO (always visible, inline) ──
    feed_info_items = [
        [_adf_text(f"{k}: ", marks=[{"type": "strong"}]), _adf_text(str(v))]
        for k, v in feed_info.items()
    ]
    blocks.append(_adf_panel("info", [
        _adf_para(_adf_text("Feed Info", marks=[{"type": "strong"}])),
        _adf_bullets(feed_info_items),
    ]))
    blocks.append(_adf_para())  # spacer

    # ── LLM INVESTIGATION (always visible, green success panel) ──
    llm_panel_content = [_adf_para(
        _adf_text("Suggested Fix", marks=[{"type": "strong"}])
    )]
    if llm_findings:
        for line in llm_findings.strip().split("\n"):
            llm_panel_content.append(_adf_para(_adf_text(line or " ")))
    else:
        llm_panel_content.append(_adf_para(
            _adf_text("(LLM investigation not enabled or no findings produced)")
        ))
    llm_panel_content.append(_adf_para(
        _adf_text(
            "Note: This suggestion was generated by AI and may be inaccurate. "
            "Please verify before taking action.",
            marks=[{"type": "em"}],
        )
    ))
    blocks.append(_adf_panel("success", llm_panel_content))
    blocks.append(_adf_para())  # spacer

    # ── EXPANDABLE SECTIONS (collapsed at bottom) ──

    # Health Scan Results (expandable) — check results only, no auto-restart now
    health_nodes = []
    if check_results:
        # Stable order: feed_state first, then gcp_metrics, then udm_search,
        # then any other checks in their original order.
        preferred_order = ["feed_state", "gcp_metrics", "udm_search"]
        ordered_names = [n for n in preferred_order if n in check_results] + [
            n for n in check_results if n not in preferred_order
        ]
        gcp_hours = _resolve_window_hours(config, feed_config, "gcp_metrics_hours")
        udm_hours = _resolve_window_hours(config, feed_config, "udm_search_hours")
        gcp_baseline_hours = _resolve_window_hours(
            config, feed_config, "gcp_metrics_baseline_hours", default=24 * 30
        )

        def _describe(name, healthy, details):
            details = (details or "").strip()
            if name == "feed_state":
                if healthy:
                    return "Feed is reporting a healthy state in SecOps."
                return details or "SecOps reports this feed is in an error state."
            if name == "gcp_metrics":
                if healthy:
                    return (
                        details
                        or f"Ingestion volume in the last {gcp_hours}h is within the "
                           f"normal range built from the prior {gcp_baseline_hours}h."
                    )
                # Failed — surface the structured reason from checks.py which
                # already includes current vs median, MAD, z-score, etc.
                return (
                    details
                    or f"Ingestion volume in the last {gcp_hours}h is abnormally low "
                       f"vs the {gcp_baseline_hours}h baseline."
                )
            if name == "udm_search":
                if healthy:
                    return details or f"UDM search returned events in the last {udm_hours}h."
                return details or f"UDM search returned no events in the last {udm_hours}h."
            return details

        items = []
        for name in ordered_names:
            healthy, details = check_results[name]
            status = "Passed" if healthy else "Failed"
            line_nodes = [
                _adf_text(f"{name} ({status})", marks=[{"type": "strong"}]),
            ]
            desc = _describe(name, healthy, details)
            if desc:
                line_nodes.append(_adf_text(f" — {desc}"))
            items.append(line_nodes)
        health_nodes.append(_adf_bullets(items))
    else:
        health_nodes.append(_adf_para(_adf_text("(no checks recorded)")))

    blocks.append(_adf_expand("Health Scan Results", health_nodes))
    blocks.append(_adf_para())  # spacer

    # Failure Details (expandable) — at the bottom
    if failure_details:
        detail_nodes = [
            _adf_para(_adf_text(line))
            for line in str(failure_details).splitlines()
            if line.strip()
        ] or [_adf_para(_adf_text(str(failure_details)))]
    else:
        detail_nodes = [_adf_para(_adf_text("(none reported)"))]
    blocks.append(_adf_expand("Failure Details", detail_nodes))

    return {"type": "doc", "version": 1, "content": blocks}


# ============================================================
# Plain-text fallback (Jira REST API v2)
# ============================================================

def _build_plaintext(config, feed_config, check_results, feed_metadata, was_restarted, llm_findings):
    state, failure_msg, failure_details = _extract_error_info(feed_metadata)
    feed_info = _extract_feed_info(feed_config, feed_metadata)
    now_iso = datetime.now(timezone.utc).isoformat()
    out = []
    out.append(f"Reported at {now_iso} (UTC)")
    out.append("")
    out.append("== Error Info ==")
    out.append(f"State:           {state}")
    out.append(f"failureMsg:      {failure_msg or '(none reported)'}")
    out.append("failureDetails:")
    out.append(failure_details or "(none reported)")
    out.append("")
    out.append("== Feed Info ==")
    for k, v in feed_info.items():
        out.append(f"{k}: {v}")
    out.append("")
    out.append("== Troubleshooting — Check Results ==")
    if check_results:
        for name, (healthy, details) in check_results.items():
            icon = "PASSED" if healthy else "FAILED"
            line = f"- {name}: {icon}"
            if details:
                line += f" — {details}"
            out.append(line)
    else:
        out.append("(no checks recorded)")
    out.append("")
    out.append("== Auto-Restart ==")
    out.append("Yes — feed was restarted (still unhealthy after retry)"
               if was_restarted else "No — feed was not restarted")
    out.append("")
    out.append("== Suggested Fix ==")
    out.append((llm_findings or "(LLM investigation not enabled or no findings produced)").strip())
    out.append("")
    out.append("Note: This suggestion was generated by AI and may be inaccurate. "
               "Please verify before taking action.")
    return "\n".join(out)
