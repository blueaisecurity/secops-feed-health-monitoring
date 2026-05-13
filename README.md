# Feed Health Monitoring

Auto-discovers Google SecOps (Chronicle) feeds, runs configurable health
checks, and triggers actions when feeds go unhealthy — locally or as a
Cloud Run Job.

> **Looking for the full docs?** See [REFERENCE.md](REFERENCE.md) for the
> complete reference (setup, IAM, all checks/actions, ingestion guardrail,
> observability).

---

## What it does

- **Discovers** every feed visible to your service account (`sync_feeds`).
- **Checks** each enabled feed:
  - `feed_state` — Chronicle's reported state
  - `gcp_metrics` — anomaly detection on ingestion volume (median + MAD)
  - `udm_search` — confirms events actually arrive
- **Acts** on failures: optional auto-restart, Jira ticket, email alert,
  Vertex AI Gemini investigation summary.
- **Guards** project-wide ingestion volume (e.g. "alert at 1 TB/day").

Identifiers (project_id, customer_id, feed UUIDs, source endpoints) are
masked in terminal output by default. Vertex AI prompts always have
project/customer/UUIDs and high-signal infra values redacted.

## Quick start

> **Where to run these commands.** The examples below are for a **local
> terminal** (PowerShell on Windows, bash/zsh on macOS/Linux). For
> Cloud Run deployment you can also run the gcloud commands from
> **Cloud Shell** in the browser — see
> [REFERENCE.md → Where to run setup commands](REFERENCE.md#where-to-run-setup-commands)
> for the differences (line continuation, multi-line paste, etc.).

```powershell
git clone https://github.com/blueaisecurity/secops-feed-health-monitoring.git feed-health-monitoring
cd feed-health-monitoring
python -m venv venv
.\venv\Scripts\Activate.ps1            # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

cp config.yaml.example     config.yaml       # tuning knobs + action toggles
cp variables.yaml.example  variables.yaml    # fill in project_id, region (NOT customer_id — see below)

# Required env var for the very first run. CUSTOMER_ID is env-var only
# (the app will refuse to read it from variables.yaml). PROJECT_ID can
# also be set here instead of in variables.yaml.
$env:CUSTOMER_ID="<chronicle-customer-uuid>"   # PowerShell
# export CUSTOMER_ID="<chronicle-customer-uuid>"   # bash/zsh

gcloud auth application-default login
# Alternatives: impersonate a service account, or set
# GOOGLE_APPLICATION_CREDENTIALS / credentials_file to a SA JSON key.
# See REFERENCE.md (Provide GCP credentials) for options and the
# IAM roles / granular permissions the service account needs.

python .\tests\test_connection.py          # smoke test
python -m app.sync_feeds                   # discover feeds
python -m app.main                         # one monitoring pass
```

By default all outbound actions ship **disabled** — the first run is safe
and only logs results. Edit `config.yaml` to enable Jira / email / the
ingestion guardrail once credentials are set.

**Env-var-only secrets.** Four values must be set as environment
variables and are never read from `variables.yaml`:

| Env var               | Required when                   |
| --------------------- | ------------------------------- |
| `CUSTOMER_ID`         | always (first run included)     |
| `JIRA_API_KEY`        | `actions.jira.enabled = true`   |
| `EMAIL_SMTP_USERNAME` | SMTP relay requires auth        |
| `EMAIL_SMTP_PASSWORD` | SMTP relay requires auth        |

If any of them appear in `variables.yaml` the app warns and prompts on
every run, and refuses to start on Cloud Run / cron. See
[REFERENCE.md](REFERENCE.md) for details.

## Configuration files

| File              | What                              | Committed?      |
| ----------------- | --------------------------------- | --------------- |
| `config.yaml`     | Tuning knobs + action toggles     | **gitignored**  |
| `variables.yaml`  | project_id, customer_id, secrets  | **gitignored**  |
| `feeds.yaml`      | Per-feed settings                 | **gitignored**  |

Only the `*.example` templates are committed. Copy each one to its
unsuffixed name and edit. In production, all three files live outside
the container (GCS bucket for `config.yaml` + `feeds.yaml`, Secret
Manager for the values inside `variables.yaml`).

## Production

Deploy as a Cloud Run **Job** triggered by Cloud Scheduler. Secrets via
Secret Manager (`--set-secrets`), `config.yaml` + `feeds.yaml` mounted
from a hardened GCS bucket, no JSON keys on disk. Full walkthrough in
[REFERENCE.md → Setup — Cloud Run](REFERENCE.md#setup--cloud-run-production).

## Operator env vars

- `FEEDHEALTH_UNMASK=1` — show raw IDs in terminal output (terminal only,
  never on Cloud Run).
- `FEEDHEALTH_NO_CONFIRM=1` — skip the DEBUG confirmation prompt.
- `FEEDHEALTH_ALLOW_FILE_SECRETS=1` — bypass the "secret in `variables.yaml`"
  warning without prompting. On Cloud Run / cron (non-TTY) this is the only
  way to allow file-resident secrets to load. Discouraged; intended for
  one-off migrations only.

## License

MIT — see [LICENSE](LICENSE).
