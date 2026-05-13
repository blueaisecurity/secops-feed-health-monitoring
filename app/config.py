import os
import stat
import yaml
import logging

logger = logging.getLogger(__name__)

VARIABLES_FILE = "variables.yaml"
FEEDS_FILE = "feeds.yaml"


class ConfigError(Exception):
    """Raised when config or variables.yaml cannot be loaded or validated."""


def _warn_if_world_or_group_readable(path, label):
    """
    Warn (do not fail) when a sensitive file is readable by group or other
    users on POSIX systems. Skipped on Windows (NTFS uses ACLs, st_mode is
    not meaningful) and on read-only mounts where the operator cannot change
    the mode (Cloud Run GCS volumes, Secret Manager mounts).

    The expected secure mode is 0600 (owner read/write only). Operators can
    fix a warning with: chmod 600 <path>
    """
    if os.name == "nt":
        return
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return
    # Bits we don't want set: group r/w/x and other r/w/x.
    bad_bits = stat.S_IRWXG | stat.S_IRWXO
    if mode & bad_bits:
        logger.warning(
            f"Insecure permissions on {label} ({path}): mode {oct(mode & 0o777)} "
            f"is readable by group/other. Run `chmod 600 {path}` to restrict "
            f"to owner only. (Suppressed automatically on Windows and on "
            f"read-only mounts.)"
        )


# Keys that should come from environment variables (and Secret Manager in
# production). If any is present with a non-placeholder value in
# variables.yaml the operator is forced to acknowledge before continuing —
# see _guard_file_secrets().
_SENSITIVE_FILE_KEYS = (
    "customer_id",
    "jira_api_key",
    "email_smtp_username",
    "email_smtp_password",
)
_SECRET_ENV_MAP = {
    "customer_id":         "CUSTOMER_ID",
    "jira_api_key":        "JIRA_API_KEY",
    "email_smtp_username": "EMAIL_SMTP_USERNAME",
    "email_smtp_password": "EMAIL_SMTP_PASSWORD",
}


def _is_placeholder(value):
    """True for None, non-strings, empty/whitespace, or 'your-...' placeholders."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    s = value.strip()
    return (not s) or s.startswith("your-")


def _guard_file_secrets(file_vars, variables_path):
    """
    Refuse to silently load credentials/tenant IDs from variables.yaml.

    These values should come from environment variables (and Secret
    Manager in production), not a clear-text YAML file. If any are
    present with a real (non-placeholder) value:

    - Interactive terminal: warn, then require explicit y/N confirmation.
    - Non-TTY (Cloud Run Job, cron, CI): raise ConfigError — there is
      no way to acknowledge the risk, and a foot-gun production deploy
      is exactly what this guard exists to prevent.

    Override (discouraged): FEEDHEALTH_ALLOW_FILE_SECRETS=1.
    """
    import sys

    offenders = [
        k for k in _SENSITIVE_FILE_KEYS
        if not _is_placeholder(file_vars.get(k))
    ]
    if not offenders:
        return

    banner_lines = [
        f"Sensitive value(s) found in {variables_path}: {', '.join(offenders)}",
        "    These should come from environment variables (Secret Manager",
        "    in production), not a clear-text YAML file. Use:",
    ]
    for key in offenders:
        banner_lines.append(f"      {key:<20s} -> ${_SECRET_ENV_MAP[key]}")
    banner = "\n".join(banner_lines)
    logger.warning("\n%s", banner)

    if os.environ.get("FEEDHEALTH_ALLOW_FILE_SECRETS") == "1":
        logger.warning(
            "FEEDHEALTH_ALLOW_FILE_SECRETS=1 - proceeding with file-resident secrets."
        )
        return

    if not sys.stdin.isatty():
        raise ConfigError(
            f"Refusing to start: {', '.join(offenders)} found in {variables_path}. "
            f"Move these to environment variables "
            f"({', '.join(_SECRET_ENV_MAP[k] for k in offenders)}). "
            f"Override with FEEDHEALTH_ALLOW_FILE_SECRETS=1 for a one-off run."
        )

    print(
        f"\n{banner}\n\n"
        f"   Continuing will load these values from {variables_path} verbatim.\n"
        f"   Recommended: Ctrl-C, move them to env vars, then re-run.\n",
        file=sys.stderr,
    )
    try:
        answer = input("Continue anyway? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        raise ConfigError(
            "Aborted: secret(s) still present in variables.yaml. Move them "
            "to environment variables and re-run."
        )


def _read_variables_file(variables_path=VARIABLES_FILE):
    """Return the raw dict from variables.yaml, or an empty dict if missing."""
    if not os.path.exists(variables_path):
        return {}
    _warn_if_world_or_group_readable(variables_path, "variables.yaml")
    with open(variables_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _split_csv_env(raw):
    """Parse a comma-separated env-var string into a list, trimming whitespace."""
    if not raw:
        return None
    return [item.strip() for item in raw.split(",") if item.strip()]


def _overlay_action_values(config, file_vars):
    """
    Overlay environment-specific / sensitive action values from env vars or
    variables.yaml onto the loaded config dict. Values present in config.yaml
    act as a fallback (kept for backward compat).

    Resolution order per value: env var > variables.yaml > existing config.

    Mapping:
        env var               variables.yaml key       config path
        --------------------  -----------------------  ----------------------------
        JIRA_PROJECT_KEY      jira_project_key         actions.jira.project_key
        JIRA_ASSIGNEES (CSV)  jira_assignees (list)    actions.jira.assign.assignees
        EMAIL_FROM_ADDRESS    email_from_address       actions.email.from_address
        EMAIL_RECIPIENTS(CSV) email_recipients (list)  actions.email.recipients
    """
    actions = config.setdefault("actions", {})
    jira = actions.setdefault("jira", {})
    jira_assign = jira.setdefault("assign", {})
    email = actions.setdefault("email", {})

    def _scalar(env_key, file_key, target_dict, target_field):
        new = os.environ.get(env_key) or file_vars.get(file_key)
        if new:
            target_dict[target_field] = new

    def _list(env_key, file_key, target_dict, target_field):
        env_list = _split_csv_env(os.environ.get(env_key))
        file_list = file_vars.get(file_key)
        new = env_list or (file_list if isinstance(file_list, list) and file_list else None)
        if new:
            target_dict[target_field] = new

    _scalar("JIRA_PROJECT_KEY",  "jira_project_key",  jira,        "project_key")
    _list  ("JIRA_ASSIGNEES",    "jira_assignees",    jira_assign, "assignees")
    _scalar("EMAIL_FROM_ADDRESS","email_from_address",email,       "from_address")
    _list  ("EMAIL_RECIPIENTS",  "email_recipients",  email,       "recipients")


def _load_variables(variables_path=VARIABLES_FILE):
    """
    Load runtime variables.

    Resolution order for each key (env var wins):
        1. Environment variable (PROJECT_ID, CUSTOMER_ID, REGION, LOCATION,
           CREDENTIALS_FILE)
        2. variables.yaml on disk (optional in env-only deployments such as
           Cloud Run with Secret Manager / --set-env-vars)

    On Cloud Run you can ship the container with no variables.yaml at all and
    pass everything via env vars / Secret Manager.
    """
    file_vars = _read_variables_file(variables_path)
    _guard_file_secrets(file_vars, variables_path)

    def _pick(env_key, file_key, default=None):
        return os.environ.get(env_key) or file_vars.get(file_key) or default

    variables = {
        "project_id":       _pick("PROJECT_ID",       "project_id"),
        "customer_id":      _pick("CUSTOMER_ID",      "customer_id"),
        "region":           _pick("REGION",           "region",           "us"),
        "location":         _pick("LOCATION",         "location",         "us-central1"),
        "credentials_file": _pick("CREDENTIALS_FILE", "credentials_file"),
    }

    # ── Validate required values ──
    required = ["project_id", "customer_id"]
    missing = [
        k for k in required
        if not variables.get(k) or str(variables[k]).startswith("your-")
    ]
    if missing:
        # CUSTOMER_ID is env-var only (see _guard_file_secrets) so don't
        # suggest putting it back in variables.yaml — that path warns and
        # aborts on non-TTY.
        env_names = ", ".join(
            "CUSTOMER_ID" if k == "customer_id" else k.upper()
            for k in missing
        )
        if "customer_id" in missing and "project_id" in missing:
            hint = f"Set CUSTOMER_ID as an env var. PROJECT_ID can be an env var or live in {variables_path}."
        elif "customer_id" in missing:
            hint = "Set CUSTOMER_ID as an env var (it is env-var only, not accepted in variables.yaml)."
        else:
            hint = f"Set them as env vars ({env_names}) or in {variables_path}."
        raise ConfigError(f"Missing required variable(s): {missing}. {hint}")

    # ── Set GCP credentials from file if specified ──
    credentials_file = variables.get("credentials_file")
    if credentials_file:
        if not os.path.exists(credentials_file):
            raise ConfigError(f"Credentials file not found: {credentials_file}")
        _warn_if_world_or_group_readable(credentials_file, "credentials_file (SA key)")
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_file
        # Log only the basename — the full path can leak directory structure
        # (e.g. /etc/secrets/sa-PROJECTID-key.json). Full path is at DEBUG.
        logger.info(f"GCP credentials loaded from: {os.path.basename(credentials_file)}")
        logger.debug(f"GCP credentials full path: {credentials_file}")
    elif "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ:
        # On Cloud Run / GCE / GKE the metadata server provides default creds,
        # so this is normal there. Warn for local dev only.
        logger.info(
            "No credentials_file set; relying on Application Default Credentials "
            "(metadata server / gcloud auth / GOOGLE_APPLICATION_CREDENTIALS)."
        )

    return variables


def load_config(config_path=None, variables_path=None, feeds_path=None):
    """Load and return the YAML configuration file with variables injected.

    Feeds are loaded from a separate file (feeds.yaml by default, override
    with the FEEDS_PATH env var) so that the sensitive operational data
    (Chronicle instance UUID, feed UUIDs, source endpoints, team metadata)
    can be stored separately from the git-tracked settings file. If
    feeds.yaml is missing the feeds list is treated as empty.
    """
    config_path = config_path or os.environ.get("CONFIG_PATH", "config.yaml")
    variables_path = variables_path or os.environ.get("VARIABLES_PATH", VARIABLES_FILE)
    feeds_path = feeds_path or os.environ.get("FEEDS_PATH", FEEDS_FILE)

    if not os.path.exists(config_path):
        raise ConfigError(f"Config file not found: {config_path}")
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    # ── Load feeds from separate file (gitignored / Secret Manager / GCS) ──
    if os.path.exists(feeds_path):
        _warn_if_world_or_group_readable(feeds_path, "feeds.yaml")
        with open(feeds_path, "r", encoding="utf-8") as f:
            feeds_doc = yaml.safe_load(f) or {}
        config["feeds"] = feeds_doc.get("feeds", []) or []
    else:
        # Allow legacy config.yaml that still embeds feeds: inline.
        config.setdefault("feeds", [])
        if not config["feeds"]:
            logger.warning(
                f"Feeds file not found: {feeds_path} — running with empty feed "
                f"list. Run `python -m app.sync_feeds` to populate it."
            )

    # ── Load variables from file ──
    variables = _load_variables(variables_path)

    # Inject NON-SENSITIVE variables into config (project_id, customer_id,
    # region, location). These come from env vars first, then variables.yaml.
    # Secrets (Jira API key/url/email, SMTP password) are intentionally NOT
    # placed in `config` — they are loaded on demand by the modules that
    # need them.
    config["project_id"] = variables["project_id"]
    config["customer_id"] = variables["customer_id"]
    config["region"] = variables["region"]
    config["location"] = variables["location"]

    # ── Overlay action-level identifying values ──
    # Things like Jira project key, assignees, and email recipients are
    # environment-specific and often considered sensitive (personal
    # emails, internal project keys). They live in variables.yaml / env
    # vars so config.yaml can be safely committed to git. If a value is
    # also present in config.yaml it is kept as a fallback.
    file_vars = _read_variables_file(variables_path)
    _overlay_action_values(config, file_vars)

    # Stash resolved feeds_path so sync_feeds can write back to the same file
    # without re-resolving the env-var precedence rules.
    config["_feeds_path"] = feeds_path

    logger.info(f"Config loaded — {len(config.get('feeds', []))} feed(s) defined")
    return config
