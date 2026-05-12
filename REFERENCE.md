# Feed Health Monitoring — Reference

> The short landing page is in [README.md](README.md). This file is the
> full reference: setup, IAM, every check and action, the ingestion
> guardrail, and observability.

## Contents

- [Overview](#overview)
- [Where to run setup commands](#where-to-run-setup-commands)
- [Quick start (local)](#quick-start-local)
- [Project layout](#project-layout)
- [Execution flow](#execution-flow)
- [Setup — local development](#setup--local-development)
- [Setup — Cloud Run (production)](#setup--cloud-run-production)
- [Environment variables](#environment-variables)
- [Data sent to third parties](#data-sent-to-third-parties)
- [Commands](#commands)
- [Available checks](#available-checks)
- [Available actions](#available-actions)
- [Global ingestion-volume guardrail](#global-ingestion-volume-guardrail)
- [Health checks — deep dive](#health-checks--deep-dive)
- [Service account — minimum roles](#service-account--minimum-roles)
- [Granular permissions](#granular-permissions-for-custom-roles--least-privilege)
- [Auto-restart](#auto-restart)
- [Auto-sync](#auto-sync)
- [Debug notes](#debug-notes)

## Overview

A monitoring app that auto-discovers Chronicle (Google SecOps) feeds, runs
configurable health checks against them, and executes failure actions
(logging, alerting, auto-restart, LLM investigation, etc.).

Runs identically on a local system and as a Cloud Run Job. Secrets are
loaded from env vars first (Cloud Run / Secret Manager) and fall back
to `variables.yaml` on disk (local dev).

License: MIT — see [LICENSE](LICENSE).

**Status / scope**

- **Implemented:** `feed_state`, `gcp_metrics`, `udm_search` checks; `jira`,
  `email`, and `llm` actions; auto-restart; auto-sync.
- **Not yet shipped:** Slack notifications. The hooks were in earlier drafts
  but were removed for v1 to avoid shipping non-functional code. Tracked
  as an open issue.

## Where to run setup commands

The commands in this document fall into two groups:

1. **Local-only commands** — anything that touches files in this repo
   (`git clone`, `python -m venv`, editing `variables.yaml`, etc.).
   Run these on your own machine in a terminal (PowerShell on Windows,
   bash/zsh on macOS/Linux). They do **not** work in Cloud Shell because
   the repo is not checked out there.
2. **GCP setup commands** — anything starting with `gcloud`, `gsutil`,
   or `gcloud storage`. These work the same in any environment that has
   the SDK installed:
   - Local terminal (after `gcloud auth login`).
   - **Cloud Shell** in the browser (`https://shell.cloud.google.com`).
     Cloud Shell is bash and already has gcloud authenticated.

### Cloud Shell quirks

If you choose to run the GCP setup from Cloud Shell, three things to
keep in mind:

1. **Multi-line commands.** The deploy command in this doc uses
   shell line-continuation (`\` at the end of a line). Cloud Shell is
   bash, so `\` works. **PowerShell** uses a backtick (`` ` ``)
   instead — if you copy bash commands into PowerShell, replace each
   trailing `\` with `` ` ``, *or* join the command onto one line.
2. **Multi-line paste.** Pasting a multi-line command into the Cloud
   Shell textarea sometimes executes each line as a separate command
   (the terminal treats Enter as "submit"). Two workarounds:
   - Join the command onto a single line (remove the `\` line-continuations).
   - Wrap it in a `bash <<'EOF' ... EOF` here-doc and paste the whole block.
3. **Shell history hygiene.** Anything you paste into Cloud Shell
   lands in the in-memory history of that tab. Closing the tab
   discards it. See [Storing the Jira secret](#3-store-jira-secrets-in-secret-manager)
   below for how to avoid putting the secret in history in the first
   place.

## Quick start (local)

For first-time setup, follow these seven steps. Full details are in
[Setup — local development](#setup--local-development) further down.

1. Create a venv and install deps:
   ```bash
   python -m venv venv
   .\venv\Scripts\Activate.ps1            # Windows PowerShell
   source venv/bin/activate               # macOS/Linux
   pip install -r requirements.txt
   ```

2. Copy the templates and fill in your values:
   ```bash
   cp config.yaml.example     config.yaml
   cp variables.yaml.example  variables.yaml
   cp feeds.yaml.example      feeds.yaml
   ```
   Required in `variables.yaml`: `project_id`, `customer_id`, `region`.

3. Authenticate to GCP (no JSON key on disk):
   ```bash
   gcloud auth application-default login
   ```

4. Verify everything is wired up (recommended):
   ```bash
   python .\tests\test_connection.py
   ```
   Probes Chronicle + Cloud Monitoring with your credentials and
   prints a masked summary. If this fails, fix it before continuing —
   the rest of the app will fail too. See `tests/README.md` for details.

5. Discover feeds from Chronicle:
   ```bash
   python -m app.sync_feeds
   ```
   Then open `feeds.yaml` and set `enabled: true` on the feeds you
   want monitored.

6. Run a single monitoring pass:
   ```bash
   python -m app.main
   ```
   By default, all outbound actions (Jira, email) and the
   project-wide ingestion-volume guardrail ship DISABLED so the
   first run is safe — it logs results and nothing else.

7. (Optional) Enable outbound actions in `config.yaml` once you've
   filled in the relevant credentials in `variables.yaml`:
   ```yaml
   actions.jira.enabled: true
   actions.email.enabled: true
   global_settings.ingestion_volume_monitor.enabled: true
   ```
   Re-run `python -m app.main`.

**Configuration files at a glance**

| File                | What                            | Committed?      |
| ------------------- | ------------------------------- | --------------- |
| `config.yaml`       | Tuning knobs + action toggles   | **gitignored**  |
| `variables.yaml`    | Secrets + IDs                   | **gitignored**  |
| `feeds.yaml`        | Per-feed settings               | **gitignored**  |

Only the `*.example` templates are committed.

## Project layout

```
config.yaml              Settings (LOCAL ONLY — gitignored) — global, actions, LLM
config.yaml.example      Template / structure reference for config.yaml
feeds.yaml               Feed list (gitignored) — SENSITIVE
feeds.yaml.example       Template / structure reference for feeds.yaml
variables.yaml           Sensitive vars (gitignored) — project, customer, creds
variables.yaml.example   Template for variables.yaml
requirements.txt         Python dependencies (pinned)
Dockerfile               Container image for Cloud Run (code only — no YAML baked in)
.dockerignore            Keeps secrets/dev artifacts out of the build context
LICENSE                  MIT
app/
  main.py                Orchestrator — runs checks, triggers actions
  config.py              Loads config.yaml + feeds.yaml + variables.yaml
  chronicle_client.py    Cached Chronicle SecOps client + restart helpers
  checks.py              Health check implementations
  actions.py             Failure action handlers
  sync_feeds.py          Auto-discover feeds from Chronicle into feeds.yaml
  llm.py                 Gemini investigation (via google-genai / Vertex AI)
tests/                   Smoke / integration script (NOT a pytest suite — see tests/README.md)
```

### Why `config.yaml`, `variables.yaml`, and `feeds.yaml` are split

- **`config.yaml`** holds tuning knobs (thresholds, timeouts, log level,
  action toggles). It contains no secrets, but the values are
  *environment-specific* — your thresholds aren't anyone else's defaults.
  It's gitignored so you can have a real config without pushing it to a
  public repo. The committed template is `config.yaml.example`.
- **`variables.yaml`** holds identifiers + secrets (`project_id`,
  `customer_id`, Jira credentials). Always gitignored. In production
  it isn't on disk at all — values come from env vars and Secret Manager.
- **`feeds.yaml`** holds operational data — Chronicle instance UUID, feed
  UUIDs, Azure storage URIs, Event Hub endpoints, team/customer labels.
  Gitignored; stored in Secret Manager or a locked-down GCS bucket for
  production. Override its location with the `FEEDS_PATH` env var.

In production, both `config.yaml` and `feeds.yaml` live in the **same
GCS bucket** and are mounted into the container at `/etc/feed-health/`
(see [Cloud Run deploy](#5-deploy-the-cloud-run-job) below). The image
itself contains only code.

## Execution flow

```text
┌─────────────────────────────────────────────────────────┐
│  python -m app.main  (one shot — runs once, then exits) │
└─────────────────────────────────────────────────────────┘
                           │
                           ▼
          ┌──────────────────────────────────┐
          │  load_config()                   │
          │  - reads config.yaml             │
          │  - loads project/region/etc.     │
          │    from env vars OR              │
          │    variables.yaml                │
          └──────────────────────────────────┘
                           │
                           ▼
          ┌──────────────────────────────────┐
          │  (optional) auto_sync ────────►  │  sync_feeds()
          │  pulls feed list from Chronicle  │   - merges new feeds in
          └──────────────────────────────────┘   - preserves user edits
                           │                     - writes atomically
                           ▼
          ┌──────────────────────────────────┐
          │  for each enabled feed:          │
          │    for each configured check:    │
          │      - feed_state                │  ← Chronicle API
          │      - gcp_metrics  (anomaly)    │  ← Cloud Monitoring
          │      - udm_search                │  ← Chronicle UDM
          └──────────────────────────────────┘
                           │
                ┌──────────┴──────────┐
                ▼                     ▼
         HEALTHY (all pass)    UNHEALTHY (any fail)
                                     │
                                     ▼
                ┌────────────────────────────────────┐
                │  optional auto_restart:            │
                │    disable → wait → enable → wait  │  ← Chronicle API
                │  then re-run all checks once       │
                └────────────────────────────────────┘
                                     │
                                     ▼
                ┌────────────────────────────────────┐
                │  execute_actions(feed):            │
                │    - llm   → Vertex AI investigation│
                │    - jira  → create ticket          │
                │    - email → SMTP alert             │
                └────────────────────────────────────┘
```

## Setup — local development

### 1. Clone the repo and create a venv

```bash
git clone https://github.com/blueaisecurity/secops-feed-health-monitoring.git feed-health-monitoring
cd feed-health-monitoring
python -m venv venv
.\venv\Scripts\Activate.ps1            # Windows PowerShell
source venv/bin/activate               # macOS/Linux
pip install -r requirements.txt
```

### 2. Copy the YAML templates

```bash
cp config.yaml.example     config.yaml
cp variables.yaml.example  variables.yaml
cp feeds.yaml.example      feeds.yaml
```

`config.yaml`, `variables.yaml`, and `feeds.yaml` are all gitignored.
The `*.example` versions are the only files committed to the repo.

### 3. Configure runtime variables

You have two options:

**Option A — file (easiest for local dev).** Edit `variables.yaml`:

```yaml
project_id:       my-secops-project
customer_id:      <chronicle customer UUID>
region:           us
location:         us-central1               # Vertex AI location
credentials_file: /path/to/sa-key.json      # only if using a JSON key

# Jira (only if actions.jira.enabled = true)
jira_api_url:     https://your.atlassian.net/rest/api/3
jira_user_email:  you@example.com
jira_api_key:     <Atlassian API token>
```

**Option B — env vars (matches the Cloud Run shape):**

```powershell
# Replace each <PLACEHOLDER> with your value before running.
$env:PROJECT_ID="<your-gcp-project-id>"
$env:CUSTOMER_ID="<chronicle-customer-uuid>"
$env:REGION="us"
$env:LOCATION="us-central1"
$env:JIRA_API_URL="https://<your-site>.atlassian.net/rest/api/3"
$env:JIRA_USER_EMAIL="<your-atlassian-email>"
$env:JIRA_API_KEY="<atlassian-api-token>"
```

Env vars always win over `variables.yaml`. You can mix the two.

### 4. Provide GCP credentials (pick ONE)

**a) RECOMMENDED — impersonate the production service account, no JSON
key on disk:**

```bash
gcloud auth application-default login \
  --impersonate-service-account=feed-health-sa@PROJECT.iam.gserviceaccount.com
```

Then leave `credentials_file` and `CREDENTIALS_FILE` unset.

**b) JSON key file (legacy, less secure):**

Set `credentials_file:` in `variables.yaml`, **OR**

```powershell
$env:GOOGLE_APPLICATION_CREDENTIALS="/path/to/sa-key.json"
```

Treat the JSON key like a password. Never commit it.

### 5. Auto-discover feeds from Chronicle into feeds.yaml

```bash
python -m app.sync_feeds
```

This creates / updates `feeds.yaml` with every feed visible to your
service account. New feeds are added with `enabled: false` by default.
`feeds.yaml` is gitignored — never commit it.

### 6. Edit `feeds.yaml`

Set `enabled: true` for the feeds you want monitored, pick `checks:`
and `actions_on_failure:` per feed.

### 7. Run a single monitoring pass

```bash
python -m app.main
```

To schedule it locally, use Task Scheduler (Windows) or cron (Linux):

```cron
*/15 * * * *  cd /opt/feed-health-monitoring && ./venv/bin/python -m app.main
```

## Setup — Cloud Run (production)

This app is a scheduled batch workload — deploy it as a Cloud Run **Job**
(not a Service) and trigger it with Cloud Scheduler.

The deploy uses NO local files for secrets:

- **config.yaml** → mounted from GCS at `/etc/feed-health/config.yaml`
- **feeds.yaml** → mounted from GCS at `/etc/feed-health/feeds.yaml`
- **non-secret env** → `--set-env-vars`
- **secrets and identifying IDs** → Secret Manager bound via `--set-secrets`
- **GCP auth** → runtime service account (no JSON key)

### What the gcloud flags do

If you're new to Cloud Run jobs, these are the three flags you'll see
below. Knowing what each one does makes it obvious where to put each
value.

| Flag                | What it does                                                                                                                                            | Visible where                                                                                  |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `--set-env-vars`    | Plain `KEY=VALUE` pairs passed straight into the container as environment variables. Stored on the Cloud Run revision.                                  | `gcloud run jobs describe`, Cloud Console UI, your shell history when you ran `gcloud deploy`. |
| `--set-secrets`     | References to Secret Manager secrets (`KEY=secret-name:latest`). Cloud Run resolves the value at container start. The plaintext **never appears** in the Cloud Run config. | `describe` only shows the reference (`secret-name:latest`), never the value.                   |
| `--add-volume` + `--add-volume-mount` | Mounts a GCS bucket as a path inside the container filesystem. Used here to surface `config.yaml` + `feeds.yaml` without rebuilding the image. | Object contents appear at the mount path inside the container only. |

Rule of thumb: **identifiers go in `--set-env-vars`; anything you'd
rotate goes in `--set-secrets`.** The Chronicle `customer_id` is an
edge case — it's not a secret in the cryptographic sense, but it
uniquely identifies your tenant, so this guide stores it in Secret
Manager too, to keep it out of `describe` output.

### 1. Create the service account and grant roles

```bash
# Set these once for the rest of this section.
PROJECT=<your-gcp-project-id>                # e.g. my-secops-project
SA=feed-health-sa@${PROJECT}.iam.gserviceaccount.com

gcloud iam service-accounts create feed-health-sa \
  --project=$PROJECT \
  --display-name="Feed Health Monitor"

# Chronicle SecOps — read feeds + restart (disable/enable)
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" --role="roles/chronicle.editor"

# Cloud Monitoring — read ingestion metrics
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" --role="roles/monitoring.viewer"

# Vertex AI — Gemini investigation (only if investigation.llm.enabled)
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:$SA" --role="roles/aiplatform.user"
```

If you do NOT use auto-restart, replace `chronicle.editor` with
`roles/chronicle.viewer` (read-only).
If you do NOT use the LLM action, omit `roles/aiplatform.user`.

For the granular permissions behind each role (e.g. to build a custom
least-privilege role), see [Granular permissions](#granular-permissions-for-custom-roles--least-privilege)
near the bottom of this file.

The runtime service account also needs:

- **read access to the GCS bucket** holding `config.yaml` + `feeds.yaml`
  (added in [step 4b](#4b-create-the-gcs-bucket-for-configyaml--feedsyaml-hardened)):
  `roles/storage.objectViewer` on `gs://$BUCKET`
  (or `storage.objects.get` + `storage.objects.list` as a custom role).
- if you read any credentials from Secret Manager (step 3 below):
  `roles/secretmanager.secretAccessor` on each secret
  (or `secretmanager.versions.access` as a custom role).

### 2. Enable the required APIs

```bash
gcloud services enable \
  chronicle.googleapis.com \
  monitoring.googleapis.com \
  aiplatform.googleapis.com \
  secretmanager.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com \
  --project=$PROJECT
```

### 3. Store secrets in Secret Manager

You'll create one secret per sensitive value. The **Jira API token** is
always a secret; the **Chronicle customer ID** is stored as a secret in
this guide too, even though it isn't cryptographically sensitive — so
it doesn't show up in `gcloud run jobs describe` output. The **SMTP
password** is only needed if your relay requires authentication.

#### Avoiding your shell history

The naive `echo "<token>" | gcloud secrets create ...` pattern lands
the secret verbatim in `~/.bash_history` (bash) or the in-memory
PowerShell history. **Use `read -s` instead** — it reads a value into a
shell variable without echoing it, then pipes it into gcloud:

```bash
# Bash / Cloud Shell — token is prompted for, not typed visibly.
read -s -p "Paste Atlassian API token: " JIRA_TOKEN; echo
printf '%s' "$JIRA_TOKEN" | gcloud secrets create jira-api-key \
  --data-file=- --project=$PROJECT
unset JIRA_TOKEN

# Same flow for the Chronicle customer ID:
read -s -p "Paste Chronicle customer ID: " CHRONICLE_CUSTOMER; echo
printf '%s' "$CHRONICLE_CUSTOMER" | gcloud secrets create chronicle-customer-id \
  --data-file=- --project=$PROJECT
unset CHRONICLE_CUSTOMER
```

PowerShell equivalent:

```powershell
$JiraToken = Read-Host -AsSecureString "Paste Atlassian API token"
$plain = [System.Net.NetworkCredential]::new('', $JiraToken).Password
$plain | gcloud secrets create jira-api-key --data-file=- --project=$PROJECT
Remove-Variable JiraToken, plain
```

If you do paste a secret into a regular shell prompt by accident, clear
the history afterwards:

- **bash:** `history -d $(history 1 | awk '{print $1}')` (deletes the
  last command), or `history -c && history -w` to wipe the whole file.
- **PowerShell:** `Clear-History` (current session) and delete
  `(Get-PSReadlineOption).HistorySavePath` to clear persisted history.
- **Cloud Shell:** just close the browser tab — the in-memory history
  is discarded with the session. (Anything persisted to
  `~/.bash_history` survives the tab close, so still prefer `read -s`.)

#### Grant the runtime SA access to each secret

```bash
for SECRET in jira-api-key chronicle-customer-id; do
  gcloud secrets add-iam-policy-binding $SECRET \
    --member="serviceAccount:$SA" \
    --role="roles/secretmanager.secretAccessor" --project=$PROJECT
done
```

`jira_api_url` and `jira_user_email` aren't secret — pass them as
`--set-env-vars` instead (shown in step 5). Create them as secrets too if
you prefer; the precedence rules in `app/config.py` accept either source.

### 4. Build and push the container image

The repo ships a production-ready Dockerfile (`python:3.12-slim`,
non-root user, layered for cache reuse). It does NOT copy
`config.yaml`, `variables.yaml`, or `feeds.yaml` into the image — those
are provided at runtime via env vars / Secret Manager / GCS volume
mount (step 4b). The `.dockerignore` enforces this even if you forget.

Build with Cloud Build into Artifact Registry:

```bash
gcloud artifacts repositories create feed-health \
  --repository-format=docker --location=us-central1 --project=$PROJECT

gcloud builds submit --tag \
  us-central1-docker.pkg.dev/$PROJECT/feed-health/monitor:latest \
  --project=$PROJECT
```

### 4b. Create the GCS bucket for `config.yaml` + `feeds.yaml` (hardened)

Both files live in the same bucket and are read-only at runtime.
`feeds.yaml` is sensitive (Chronicle instance UUID, feed UUIDs, source
endpoints, team metadata). `config.yaml` is less sensitive but still
environment-specific. Treat the bucket like a secret store.

```bash
BUCKET=feed-health-config-$PROJECT

gcloud storage buckets create gs://$BUCKET \
  --project=$PROJECT \
  --location=us-central1 \
  --uniform-bucket-level-access \
  --public-access-prevention

# Versioning so a bad sync or config push can be rolled back
gcloud storage buckets update gs://$BUCKET --versioning

# Read-only access for the runtime service account
gcloud storage buckets add-iam-policy-binding gs://$BUCKET \
  --member="serviceAccount:$SA" \
  --role="roles/storage.objectViewer"

# Upload your locally-tuned config.yaml and the locally-synced feeds.yaml
gcloud storage cp config.yaml gs://$BUCKET/config.yaml
gcloud storage cp feeds.yaml  gs://$BUCKET/feeds.yaml
```

Optional but recommended:

- Enable Data Access audit logs for `storage.googleapis.com` on the
  project so reads of `config.yaml` / `feeds.yaml` are logged.
- Encrypt with a CMEK from Cloud KMS
  (`--default-kms-key=projects/.../cryptoKeys/...`).
- Add a lifecycle rule deleting noncurrent versions after N days.

### 5. Deploy the Cloud Run Job

The values you supply are tagged inline with `# ← REPLACE ME` so it's
obvious which ones come from your environment. The placeholders are
also shown one more time below the command.

```bash
gcloud run jobs deploy feed-health-monitor \
  --project=$PROJECT \
  --region=us-central1 \
  --image=us-central1-docker.pkg.dev/$PROJECT/feed-health/monitor:latest \
  --service-account=$SA \
  --task-timeout=900 \
  --max-retries=0 \
  --add-volume=name=feed-health-config,type=cloud-storage,bucket=$BUCKET,readonly=true \
  --add-volume-mount=volume=feed-health-config,mount-path=/etc/feed-health \
  --set-env-vars=\
PROJECT_ID=$PROJECT,\
REGION=us,\
LOCATION=us-central1,\
CONFIG_PATH=/etc/feed-health/config.yaml,\
FEEDS_PATH=/etc/feed-health/feeds.yaml,\
JIRA_API_URL=https://<your-site>.atlassian.net/rest/api/3,\
JIRA_USER_EMAIL=alerts@<your-domain> \
  --set-secrets=\
CUSTOMER_ID=chronicle-customer-id:latest,\
JIRA_API_KEY=jira-api-key:latest
```

Placeholders you must replace before running:

| Placeholder                              | Where it appears                                            |
| ---------------------------------------- | ----------------------------------------------------------- |
| `$PROJECT` (`<your-gcp-project-id>`)     | shell variable set in step 1                                |
| `$SA`                                    | shell variable set in step 1                                |
| `$BUCKET`                                | shell variable set in step 4b                               |
| `<your-site>` (in `JIRA_API_URL`)        | your Atlassian subdomain, e.g. `acme.atlassian.net`         |
| `<your-domain>` (in `JIRA_USER_EMAIL`)   | the Atlassian account that owns the Jira API token          |

**Notes:**

- `--max-retries=0` — the app has its own retry/backoff; an outer retry
  would amplify auto-restart effects.
- `--task-timeout=900` — cap each run at 15 minutes (raise as needed).

**Secrets — important.** Pass every secret value via `--set-secrets`
(Secret Manager), **never** via `--set-env-vars`. Values supplied with
`--set-env-vars` are visible in `gcloud run jobs describe`, the Cloud
Console UI, and the shell history of whoever ran the deploy. The list
of values that MUST come from Secret Manager:

- `JIRA_API_KEY` — always.
- `EMAIL_SMTP_PASSWORD` — if the email action is enabled with auth.
- `CUSTOMER_ID` — Chronicle customer UUID. Not cryptographically
  secret, but is a tenant identifier and shouldn't appear in
  `describe` output. Stored here as a secret for that reason.

Non-secret identifiers (`PROJECT_ID`, `REGION`, `LOCATION`,
`JIRA_API_URL`, `JIRA_USER_EMAIL`, `EMAIL_FROM_ADDRESS`, etc.) are fine
in `--set-env-vars`.

Test it once:

```bash
gcloud run jobs execute feed-health-monitor \
  --region=us-central1 --project=$PROJECT --wait
```

### 6. Schedule with Cloud Scheduler

```bash
gcloud scheduler jobs create http feed-health-cron \
  --project=$PROJECT \
  --location=us-central1 \
  --schedule="*/30 * * * *" \
  --time-zone="Etc/UTC" \
  --uri="https://us-central1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$PROJECT/jobs/feed-health-monitor:run" \
  --http-method=POST \
  --oauth-service-account-email=$SA
```

The Scheduler SA also needs `roles/run.invoker` on the job.

Until per-feed auto-restart cooldown is implemented, keep the schedule
sparse (every 30–60 min). A permanently broken feed will otherwise be
disable→re-enabled on every tick.

### 7. Updating config or the feed list in production

Neither `config.yaml` nor `feeds.yaml` is baked into the container —
both live in GCS so they can be updated without rebuilding.

**Updating tuning knobs / action toggles (`config.yaml`):**

```bash
# Pull current, edit locally, push back.
gcloud storage cp gs://$BUCKET/config.yaml ./config.yaml
# edit config.yaml
gcloud storage cp ./config.yaml gs://$BUCKET/config.yaml
# The next scheduled run picks up the new config automatically.
```

**Updating the feed list (`feeds.yaml`):**

```bash
gcloud storage cp gs://$BUCKET/feeds.yaml ./feeds.yaml
python -m app.sync_feeds
git diff --no-index ./feeds.yaml.bak ./feeds.yaml   # review
gcloud storage cp ./feeds.yaml gs://$BUCKET/feeds.yaml
```

Bucket versioning (step 4b) lets you roll back a bad push of either file.

Do NOT enable `global_settings.auto_sync` in production unless the
service account has `roles/storage.objectAdmin` on the bucket and the
GCS volume is mounted read-write — by default it is mounted read-only
to prevent the running job from rewriting `feeds.yaml` mid-flight.

**Code changes** (anything in `app/`, dependency bumps) still require a
rebuild + redeploy:

```bash
gcloud builds submit ...
gcloud run jobs update feed-health-monitor --image=...
```

### 8. Observability

- All app logs go to stdout / stderr — Cloud Run streams them to
  Cloud Logging automatically. Filter by resource type
  `cloud_run_job`.
- Set `log_level: INFO` in `config.yaml` for normal use, `DEBUG` when
  troubleshooting.
- Set `log_level: PROD` for production deployments. PROD suppresses
  all INFO/WARNING noise (logger is raised to ERROR) and emits one
  sanitized line at the end of each run:
  ```text
  COMPLETED: healthy=10 unhealthy=0 skipped=0 total=10
  ```
  The line contains counts only — no project/customer IDs, feed names,
  or feed UUIDs — and is written to stdout at Cloud Logging
  severity=DEFAULT, so it will not trigger ERROR-based alerts.
  Anything that matters still flows through `logger.error` and lands
  at severity>=ERROR.
- Recommended Cloud Logging alert policies (pair them):
  1. **Run produced an error**
     ```
     resource.type="cloud_run_job"
     severity>=ERROR
     ```
     Triggers on any check crash, Chronicle/GCP API failure, Jira POST
     failure, restart failure, or unhandled exception.
  2. **Run did not complete** (absence detector)
     ```
     resource.type="cloud_run_job"
     textPayload=~"^COMPLETED:"
     ```
     Alert when this log line is absent for longer than your schedule
     interval. Catches OOM, timeout, deadlock, or the process being
     killed before it could log an error.
- Legacy alternative (still valid): Cloud Monitoring alert on the
  Cloud Run Job's `run.googleapis.com/job/completed_execution_count`
  metric with `result="failed"`.

## Environment variables

| Variable               | Description                                                       | Default          | When required                          |
| ---------------------- | ----------------------------------------------------------------- | ---------------- | -------------------------------------- |
| `PROJECT_ID`           | GCP project ID                                                    | —                | required                               |
| `CUSTOMER_ID`          | Chronicle customer UUID (store in Secret Manager in prod)         | —                | required                               |
| `REGION`               | Chronicle region (`us`, `eu`, …)                                  | `us`             | —                                      |
| `LOCATION`             | Vertex AI location                                                | `us-central1`    | only if llm enabled                    |
| `CREDENTIALS_FILE`     | Path to SA JSON key                                               | —                | local-dev legacy auth                  |
| `CONFIG_PATH`          | Override path to `config.yaml`                                    | `./config.yaml`  | —                                      |
| `FEEDS_PATH`           | Override path to `feeds.yaml`                                     | `./feeds.yaml`   | —                                      |
| `VARIABLES_PATH`       | Override path to `variables.yaml`                                 | `./variables.yaml` | —                                    |
| `JIRA_API_URL`         | Jira REST URL (must be HTTPS)                                     | —                | if jira enabled                        |
| `JIRA_USER_EMAIL`      | Atlassian account email                                           | —                | if jira enabled                        |
| `JIRA_API_KEY`         | Atlassian API token (Secret Manager)                              | —                | if jira enabled                        |
| `JIRA_PROJECT_KEY`     | Jira project key (e.g. `SECOPS`)                                  | —                | if jira enabled                        |
| `JIRA_ASSIGNEES`       | Auto-assign list (CSV; emails or accountIds)                      | —                | if jira assign enabled                 |
| `EMAIL_FROM_ADDRESS`   | "From" header for SMTP alerts                                     | —                | if email enabled                       |
| `EMAIL_RECIPIENTS`     | Recipient list (comma-separated)                                  | —                | if email enabled                       |
| `EMAIL_SMTP_USERNAME`  | SMTP auth username                                                | —                | optional (email action)                |
| `EMAIL_SMTP_PASSWORD`  | SMTP auth password (Secret Manager)                               | —                | optional (email action)                |
| `TEST_NAMESPACE`       | Namespace used by `tests/test_connection.py` UDM probe            | `demo`           | —                                      |

**Operator toggles (terminal/debug only — do NOT set on Cloud Run):**

| Variable                 | Description                                                                  |
| ------------------------ | ---------------------------------------------------------------------------- |
| `FEEDHEALTH_UNMASK`      | Set to `1` to disable identifier masking in terminal output. By default `project_id`, `customer_id`, feed UUIDs, log types and source settings are masked so logs are safe to share. |
| `FEEDHEALTH_NO_CONFIRM`  | Set to `1` to skip the interactive confirmation prompt that fires when `log_level` is `DEBUG`. Already auto-skipped when stdin is not a TTY (cron, Cloud Run Job, CI). |

Each env var, if set, takes precedence over the corresponding value in
`variables.yaml`. On Cloud Run you can run with NO `variables.yaml` at
all.

## Data sent to third parties

When `actions.jira.enabled = true`, the feed name, namespace, last
failure message and (if `llm` is also enabled) the Gemini-generated
PROBLEM/FIX summary are sent to Atlassian Jira as a ticket body.

When `investigation.llm.enabled = true`, the following is sent to
Vertex AI Gemini in your own GCP project (location = `LOCATION` env
var, default `us-central1`). Per Google's Vertex AI terms,
request/response content is NOT used to train foundation models and is
not retained beyond the request:

**Sent verbatim:**

- Feed display name and data type (e.g. `AZURE_AD`)
- Feed source type (e.g. `AZURE_EVENT_HUB`)
- Feed state (`FAILED` / `ACTIVE` / …)
- Per-check failure detail strings (e.g. `No records ingested in last 1h`,
  or the failure message returned by Chronicle)
- Source-settings keys + low-signal values (e.g. `consumer_group`,
  hub name, region)
- Chronicle "labels" (whatever you've put there)

**Always redacted before send (cannot be disabled):**

- `project_id`, `customer_id`, feed UUIDs
- High-signal infrastructure values: `event_hub_namespace`, `s3_uri`,
  `sqs_queue`, `endpoint_url`, `webhook_url`, hostname/host, and any
  URI/URL key in `source_settings` (replaced with the literal string
  `<redacted>`)

**Never sent:**

- Credentials, tokens, API keys, SMTP passwords
- UDM event content (only event counts are returned by the check)
- GCP metric time-series values (only failure summary text)
- `variables.yaml` content

If your feed names or labels themselves contain regulated data that
must not leave your project, disable the LLM action by setting
`investigation.llm.enabled: false`. To keep inference in-region for
data-residency requirements, set the `LOCATION` env var to a regional
Vertex endpoint (e.g. `europe-west1`, `australia-southeast1`).

## Commands

```bash
python -m app.sync_feeds              # Sync feeds from Chronicle into feeds.yaml
python -m app.main                    # Run all monitoring checks (one shot)
python .\tests\test_connection.py     # Quick Chronicle connectivity test
```

## Available checks

A feed is reported HEALTHY only if EVERY configured check passes. If any
one check fails, the feed is marked UNHEALTHY and the configured
`actions_on_failure` run (after auto-restart, if enabled).

### `feed_state`

Calls the Chronicle SecOps API and verifies the feed's reported state
is `ACTIVE` or `SUCCEEDED`. Surfaces feed errors raised by the platform.

**Required permissions:**

- IAM role: `roles/chronicle.viewer` (read feeds)
- API enabled: `chronicle.googleapis.com`

### `gcp_metrics`

Anomaly detection on the Cloud Monitoring metric
`chronicle.googleapis.com/ingestion/log/record_count`.

**How it works:**

1. Pulls the last `gcp_metrics_baseline_hours` of data, bucketed in
   `gcp_metrics_hours`-sized windows.
2. The most recent bucket is the "current" value being judged.
3. The baseline is built from prior buckets, preferring
   same-time-of-day matches (e.g., today's 9-10am vs prior 9-10am
   buckets) to respect diurnal patterns. Falls back to all prior
   buckets if too few same-tod samples are available.
4. Computes median + MAD (Median Absolute Deviation — robust to
   outliers) and a modified Z-score: `z = 0.6745 * (current - median) / MAD`.

A feed FAILS this check when any of these is true:

- No data ingested anywhere in the baseline window (feed is silent).
- The current bucket has 0 records while the baseline is non-zero
  (treated as silence / anomaly).
- The current bucket is below the hard floor `min_expected_records`.
- The modified Z-score is below `-gcp_metrics_anomaly_threshold` (i.e.,
  the current bucket is abnormally low vs the baseline).

If baseline samples are below `gcp_metrics_min_baseline_samples`, only
the silence and floor checks apply (anomaly detection is skipped).

**Tuning knobs (set in `global_settings` or per-feed):**

| Knob                                 | Description                                                       |
| ------------------------------------ | ----------------------------------------------------------------- |
| `gcp_metrics_hours`                  | Bucket size for current + baseline.                               |
| `gcp_metrics_baseline_hours`         | History pulled to build baseline (e.g. 720 = 30d).                |
| `gcp_metrics_anomaly_threshold`      | Z-score cutoff. 3.0 = conservative, 2.0 = sensitive.              |
| `gcp_metrics_min_baseline_samples`   | Min baseline samples before anomaly check runs.                   |
| `min_expected_records`               | Hard floor for the current bucket (per-feed).                     |

**Required permissions:**

- IAM role: `roles/monitoring.viewer`
- API enabled: `monitoring.googleapis.com`

### `udm_search`

Runs a UDM query in Chronicle over the last `udm_search_hours` to
validate events exist for this feed.

**Required permissions:**

- IAM role: `roles/chronicle.viewer` (UDM search)
- API enabled: `chronicle.googleapis.com`

## Available actions

### `log_only`

Logs failure to console. No external calls.
**Required permissions:** none.

### `restart_feed` (also `auto_restart` in `global_settings`)

Disables the feed, waits, re-enables it, then waits again.
Timing controlled by:

- `global_settings.auto_restart.wait_after_disable_seconds`
- `global_settings.auto_restart.wait_after_enable_seconds`

**Required permissions:**

- IAM role: `roles/chronicle.editor` (or `roles/chronicle.admin`)
  needed to call `disable_feed` / `enable_feed`
- API enabled: `chronicle.googleapis.com`

### `llm`

Runs a Gemini investigation and prints a short PROBLEM/FIX summary.
Uses the google-genai SDK against Vertex AI.

**Required permissions:**

- IAM role: `roles/aiplatform.user` (invoke Vertex AI models)
- API enabled: `aiplatform.googleapis.com`
- `location` in `config.yaml` must be a region where the model is
  served (e.g., `us-central1` for `gemini-2.5-flash`).

### `jira`

Creates a real Jira issue via the Jira Cloud REST API v3 using API
token + Basic auth (headless, Cloud-Run friendly).
Triggered ONLY for feeds that are still unhealthy after auto-restart
(or immediately, if `auto_restart` is disabled).

**Issue layout (ADF, top → bottom):**

1. **RED panel** — Unhealthy feed banner with:
   - Feed name
   - Reported at `<UTC timestamp>`
   - Last time ingested
   - Health Check (feed state)
   - Failure Message
2. **YELLOW panel** — "⚙️ Auto-restart completed" (only shown if a
   restart was attempted)
3. **GREEN panel** — "🤖 AI Investigation — Suggested Fix" (LLM
   PROBLEM/FIX summary, always visible; auto-included when `jira` AND
   `llm` are both in `actions_on_failure`, or when only `jira` is
   listed but `investigation.llm.enabled`)
4. ▸ Failure Details (collapsed, code block)
5. ▸ 📡 Feed Info — `<name>` (collapsed, key/value list)
6. ▸ 🔍 Health Scan Results (collapsed, per-check pass/fail)

**API token sourcing:**

- `actions.jira.api_key` in `config.yaml`, OR
- `jira_api_key` in `variables.yaml` (preferred — gitignored), OR
- `JIRA_API_KEY` env var (preferred for Cloud Run / Secret Manager).

Same applies to `actions.jira.api_url` ↔ `jira_api_url` in
`variables.yaml`.

**Required permissions:**

- GCP: none.
- Jira: the account named in `user_email` must have at minimum
  Browse + Create + Edit Issue permission in the target project, and
  no role in any other project (this is how scoping is enforced —
  there are no OAuth scopes in this flow). If
  `actions.jira.assign.enabled = true`, the same account also needs
  Assign Issues permission, plus the Browse Users global permission
  so `/user/search` can resolve assignee emails.

**Required config:**

- `actions.jira.enabled: true`
- `actions.jira.api_url`: `https://<your-site>.atlassian.net/rest/api/3`
  (or set `jira_api_url` in `variables.yaml`)
- `actions.jira.user_email`: Atlassian account email
- `actions.jira.api_key`: Atlassian API token (or set `jira_api_key` in
  `variables.yaml`, or `JIRA_API_KEY` env var on Cloud Run)
- `actions.jira.project_key`: e.g. `SECOPS`
- `actions.jira.issue_type`: e.g. `Task`, `Bug` (must match exactly an
  issue type that exists in the target project)

**Optional config:**

- `actions.jira.dedupe.enabled` (default: `true`). Before creating a
  ticket, search the project for an unresolved issue whose summary
  equals `[Feed Health] Unhealthy feed: <feed name>`. If one exists,
  the new ticket is skipped and the existing issue key is logged.
  Set to `false` to always create a new ticket on every unhealthy
  run.
- `actions.jira.assign.enabled` (default: `false`).
- `actions.jira.assign.assignees`: list of user identifiers. After a
  ticket is created, the app tries each entry in order and assigns
  the issue to the first one that resolves to a Jira account (Jira
  allows only one assignee per issue — remaining entries act as
  fallbacks). Each entry can be either an email address (e.g.
  `alice@example.com`) or a raw Atlassian accountId (e.g.
  `5b10a2844c20165700ede21g`). Email entries are looked up via
  `/user/search`, but most Atlassian Cloud sites hide email
  addresses by default (Profile Visibility ≠ "Anyone") and that
  lookup will return no match. On those sites, configure assignees
  as accountIds — find a user's accountId by visiting
  `https://<your-site>.atlassian.net/rest/api/3/myself` while logged
  in as that user, or via the admin user directory. Assignment
  failures never block ticket creation.

### `email`

Sends an SMTP alert (text + HTML) to a list of recipients via any
SMTP relay you control — Google Workspace (`smtp-relay.gmail.com`),
Microsoft 365 (`smtp.office365.com`), Exchange, Postfix, or an
internal corporate relay reachable via Serverless VPC connector.
Stdlib `smtplib` only, no external mail providers.

**Required permissions:**

- GCP: none. Cloud Run egress on port 587 / 465 is allowed by default
  (port 25 is blocked — use a relay on 587 instead).
- Internal relay: must be reachable from the Cloud Run revision.
  Either expose the relay publicly (with auth + TLS), or attach a
  Serverless VPC connector to reach a private relay.

**Required config:**

- `actions.email.enabled: true`
- `actions.email.smtp_server`, `smtp_port`
- `actions.email.from_address`: visible "From" header
- `actions.email.recipients`: list of email addresses

**Optional config:**

- `actions.email.use_starttls` (default: `true`) — STARTTLS on 587
- `actions.email.use_ssl` (default: `false`) — implicit TLS on 465
- `actions.email.timeout_seconds` (default: `30`)
- `actions.email.dedupe_via_jira` (default: `true`) — when both Jira
  and email actions are enabled, suppress the email if an unresolved
  Jira ticket already exists for the same feed (prevents inbox spam
  during a stuck-broken outage). Has no effect if Jira is disabled —
  every unhealthy run sends an email in that case (no separate state
  store, by design).

**Credential sourcing (NEVER read from `config.yaml`):**

- `EMAIL_SMTP_USERNAME` / `EMAIL_SMTP_PASSWORD` env vars (preferred
  for Cloud Run + Secret Manager), OR
- `email_smtp_username` / `email_smtp_password` in `variables.yaml`.

Many internal relays accept allow-listed source IPs without auth —
leave both unset in that case.

**Security:**

- If a username/password is configured but neither `use_starttls` nor
  `use_ssl` is enabled, the app refuses to send (would transmit auth
  in cleartext). Same hard-fail rationale as the Jira HTTPS check.
- Outgoing mail sets `Auto-Submitted: auto-generated` to suppress
  out-of-office bounces during incidents.

## Global ingestion-volume guardrail

A project-wide guardrail (NOT per-feed) that sums a Cloud Monitoring
metric across the trailing window and fires an alert when the total
exceeds a configured threshold. Designed for license / quota
guardrails: "alert me when we hit 800 GB/day so we don't blow through
the contract."

### Behavior

- One Cloud Monitoring `timeSeries.list` call per run (regardless of
  feed count). Negligible cost — same line item as the per-feed
  `gcp_metrics` check.
- Trailing window — every run looks back exactly N hours from "now",
  not the calendar UTC day. Avoids midnight-boundary weirdness.
- Reuses the same actions vocabulary (`jira`, `email`, `log_only`) and
  the same Jira dedup gate as per-feed failures, so a sustained
  breach produces ONE ticket per outage, not one per run.
- Runs even if no feeds are defined in `feeds.yaml` (it's
  project-wide).

### Configuration

Under `global_settings.ingestion_volume_monitor` in `config.yaml`:

```yaml
enabled:           true | false        # default: false
window_hours:      24                  # trailing window in hours
metric_type:       chronicle.googleapis.com/ingestion/log/bytes_count
actions_on_breach: [jira]              # actions to fire when threshold exceeded
```

**Threshold** — pick exactly ONE of these keys:

- `threshold` — raw number in the metric's native unit (pair with
  `unit_label` for display, e.g. `"events"`)
- `threshold_kb` / `threshold_mb` / `threshold_gb` / `threshold_tb` —
  friendly decimal (1000-based) shortcuts — converted to bytes; output
  auto-renders as TB/GB/MB. Use these with a byte-count metric.
- `threshold_kib` / `threshold_mib` / `threshold_gib` / `threshold_tib`
  — binary (1024-based) variants if you ever need them.

`unit_label` is only used with raw `threshold` (ignored when a friendly
`*_gb` / `*_tb` / etc. key is set — those force `unit_label` to
`"bytes"` so the auto-scaling formatter kicks in).

### Choosing a metric

Six Chronicle ingestion metrics are exposed by Cloud Monitoring. Pick
the one that matches your alert intent:

| Metric (`chronicle.googleapis.com/...`)         | Unit   | Use for                                                                              |
| ----------------------------------------------- | ------ | ------------------------------------------------------------------------------------ |
| `ingestion/log/bytes_count`                     | bytes  | **Recommended.** License / volume cap (matches Chronicle billing). Pair with `threshold_gb` / `_tb`. |
| `ingestion/log/record_count`                    | events | Event-count cap. Pair with raw `threshold:` + `unit_label: events`.                  |
| `ingestion/log/quota_rejected_bytes_count`      | bytes  | Bytes dropped due to quota — set `threshold_mb: 1` to alert on ANY rejection.        |
| `log_processing_pipeline/ingested_bytes_count`  | bytes  | Pipeline-level byte view (alt to `ingestion/log/bytes_count`).                       |
| `log_processing_pipeline/ingested_log_count`    | events | Pipeline-level event view.                                                           |
| `normalizer/log/record_count`                   | events | Records that reached the normalizer (post-parsing).                                  |

List the descriptors actually present in your project:

```bash
gcloud monitoring metric-descriptors list \
  --filter="metric.type:chronicle.googleapis.com/ingestion" \
  --format="value(type,unit)" \
  --project=YOUR_PROJECT_ID
```

Note on windows: Cloud Monitoring retains Chronicle ingestion metrics
for ~7 days. If `window_hours: 24` returns 0 series, the project
genuinely had no ingestion in the last 24h — try `window_hours: 168`
to confirm the metric has any data at all.

### Example: alert at 1 TB / day (decimal, matches Chronicle pricing)

```yaml
global_settings:
  ingestion_volume_monitor:
    enabled: true
    window_hours: 24
    metric_type: chronicle.googleapis.com/ingestion/log/bytes_count
    threshold_tb: 1                # equivalent to threshold: 1000000000000
    actions_on_breach: [jira]
```

### Example: alert on ANY quota-rejected bytes

```yaml
global_settings:
  ingestion_volume_monitor:
    enabled: true
    window_hours: 24
    metric_type: chronicle.googleapis.com/ingestion/log/quota_rejected_bytes_count
    threshold_mb: 1
    actions_on_breach: [jira, email]
```

### Example: event-count cap (100M events / day)

```yaml
global_settings:
  ingestion_volume_monitor:
    enabled: true
    window_hours: 24
    metric_type: chronicle.googleapis.com/ingestion/log/record_count
    threshold: 100000000
    unit_label: events
    actions_on_breach: [jira]
```

The Jira ticket renders the value with auto-scaled units, e.g.:

```text
Last 24h ingestion: 1.45 TB (OVER threshold 1.00 TB)
```

The ticket summary embeds today's UTC date, e.g.:

```text
[Feed Health] Daily ingestion over threshold (2026-05-01)
```

**Dedup behavior:**

- Multiple runs on the SAME UTC day → one ticket (Jira dedup matches
  the date-stamped summary).
- A sustained breach that crosses UTC midnight → a fresh ticket each
  new day, giving you a clear daily audit trail in Jira.
- Resolve / close yesterday's ticket whenever you've acknowledged it;
  today's ticket is independent.

### Cloud Run / PROD mode

The PROD-mode COMPLETED line includes a counter:

```text
COMPLETED: healthy=10 unhealthy=0 skipped=0 total=10 ingestion_over_threshold=0|1
```

You can extend the recommended Cloud Logging alert with a metric
filter on `textPayload=~"ingestion_over_threshold=1"` if you want a
dedicated alert separate from the Jira ticket (the Jira ticket alone
is usually enough).

## Health checks — deep dive

### Overall health verdict

A feed is reported HEALTHY only if EVERY check listed in its `checks:`
array passes. The aggregation is a strict AND:

```python
feed_healthy = all(check.passed for check in configured_checks)
```

If ANY single check fails the feed is marked UNHEALTHY for the run, and
the actions in `actions_on_failure:` fire (after `auto_restart`, if
enabled).

A check that cannot run because the feed is missing required config
(e.g., `gcp_metrics` with no `metric_identifier` AND no `dataType`) is
SKIPPED gracefully — counted as healthy with a "skipping" message — so
misconfiguration alone never raises a false alarm.

### 1. `feed_state` (Chronicle SecOps API)

**What it does.** Calls the Chronicle SecOps API and reads the feed's
reported `state`.

**When it FAILS.**

- The API reports any state other than `ACTIVE` or `SUCCEEDED` (e.g.,
  `FAILED`, `INACTIVE`, `ARCHIVED`, `ERROR`).
- The API call itself errors out after `retry_count` retries.

**Tuning knobs (`global_settings`).**

| Knob                          | Description                                                                 |
| ----------------------------- | --------------------------------------------------------------------------- |
| `retry_count`                 | Retries per failed API call.                                                |
| `retry_delay_seconds`         | Initial backoff delay; doubles each retry, capped at 60s.                   |
| `chronicle_timeout_seconds`   | Per-call timeout for Chronicle/GCP SDK calls (default 60).                  |

**Required IAM / API.** `roles/chronicle.viewer` + `chronicle.googleapis.com`

### 2. `gcp_metrics` (Cloud Monitoring anomaly detection)

**What it does.** Pulls the Cloud Monitoring metric
`chronicle.googleapis.com/ingestion/log/record_count` over the last
`gcp_metrics_baseline_hours` and judges the most recent
`gcp_metrics_hours`-sized bucket against the historical baseline.

**Algorithm (modified Z-score with median + MAD).**

1. Aggregate raw metric points into fixed-size buckets of length
   `gcp_metrics_hours` (sum of records in each bucket).
2. The most recent bucket is the "current" value.
3. The remaining buckets form the baseline. Same-time-of-day buckets
   are preferred (today 09-10am compared against prior 09-10am buckets)
   so diurnal patterns aren't flagged as anomalies. If fewer than
   `gcp_metrics_min_baseline_samples` same-tod samples are available,
   the baseline falls back to ALL prior buckets.
4. Compute `median` and `MAD = median(|x_i - median|)` of the baseline
   (robust against outliers — one bad day cannot poison the baseline
   the way mean+stddev would).
5. Compute the modified Z-score:
   ```
   z = 0.6745 * (current - median) / MAD
   ```
   (0.6745 makes z comparable to a normal-distribution z-score.)
6. Edge case — if `MAD == 0` (perfectly flat baseline), `z` is set to
   `-inf`, `0`, or `+inf` depending on whether the current bucket is
   below, equal to, or above the median.

**When it FAILS (any one of these).**

- **SILENT FEED** — zero records anywhere in the last
  `gcp_metrics_baseline_hours`.
- **CURRENT BUCKET EMPTY** — the most recent bucket has 0 records
  while older buckets have data (treated as silence/anomaly).
- **BELOW HARD FLOOR** — current bucket < `min_expected_records`.
- **STATISTICAL ANOMALY** — `z < -gcp_metrics_anomaly_threshold`
  (current bucket abnormally LOW vs the baseline). Spikes ABOVE the
  baseline do NOT fail the check; only drops do.

**When the anomaly check is SKIPPED.** If `baseline_samples <
gcp_metrics_min_baseline_samples` (typical for brand-new feeds with
little history), only the silence and floor checks apply. The feed is
reported healthy with an "insufficient baseline" note in the details.

**Tuning knobs** (set in `global_settings`, override per-feed by
setting the same key on a feed entry):

| Knob                                 | Description                                                                                         |
| ------------------------------------ | --------------------------------------------------------------------------------------------------- |
| `gcp_metrics_hours`                  | Bucket size for current + baseline. Default: 1 hour.                                                |
| `gcp_metrics_baseline_hours`         | Total history pulled to build the baseline. Default: 720 (= 30 days).                               |
| `gcp_metrics_anomaly_threshold`      | Modified-Z-score cutoff. Lower = stricter. Default: 3.0 (~outlier). Try 2.0 for noisy feeds where you want earlier detection, or 4.0 for spiky feeds with many false alarms. |
| `gcp_metrics_min_baseline_samples`   | Minimum baseline samples required to enable anomaly detection. Default: 5.                          |
| `min_expected_records`               | Per-feed hard floor for the current bucket. Default: 1.                                             |

**How to interpret the z-score in the Jira ticket.**

| z-score        | Meaning                                                          |
| -------------- | ---------------------------------------------------------------- |
| `0`            | Exactly at the median, healthy.                                  |
| `-1 < z < 1`   | Ordinary fluctuation.                                            |
| `-2`           | Moderately low (10× more anomalous than normal).                 |
| `-3`           | Strong anomaly (default fail threshold).                         |
| `-10`          | Very strong drop (current bucket far below median).              |
| `-inf`         | `current = 0` with a flat non-zero baseline.                     |

**Required IAM / API.** `roles/monitoring.viewer` + `monitoring.googleapis.com`

### 3. `udm_search` (Chronicle UDM query)

**What it does.** Runs a UDM query in Chronicle over the last
`udm_search_hours` and counts matching events.

**Query resolution order (per feed):**

1. `udm_query` — explicit query string in the feed config.
2. `namespace` — auto-built as `namespace = "<value>"`.
3. `dataType` — auto-built as `metadata.log_type = "<value>"`.
4. None of the above → check is skipped gracefully.

**When it FAILS.**

- The query returns 0 events in the window.
- The Chronicle UDM search API errors out after `retry_count` retries.

**Tuning knobs.**

| Knob                  | Description                                                                  |
| --------------------- | ---------------------------------------------------------------------------- |
| `udm_search_hours`    | Lookback window. Default: 1 hour.                                            |
| `retry_count`         | Retries per failed API call.                                                 |
| `retry_delay_seconds` | Delay between retries.                                                       |

**Required IAM / API.** `roles/chronicle.viewer` + `chronicle.googleapis.com`

### Per-feed overrides

Every tuning knob in `global_settings` can be overridden on an
individual feed by writing the same key directly under the feed entry.
Example:

```yaml
- enabled: true
  name: HighVolumeFeed
  chronicle_feed_id: ...
  # This feed is bursty — relax the anomaly threshold and require
  # at least 100 records per hour.
  gcp_metrics_anomaly_threshold: 4.0
  min_expected_records: 100
  udm_search_hours: 2
  checks:
    - feed_state
    - gcp_metrics
    - udm_search
```

Per-feed values always win over `global_settings` values. The
`sync_feeds` writer preserves these overrides on every re-sync.

### What's recorded for each run

Every check returns a structured result that is:

- logged to the terminal at the configured `log_level`,
- included in the LLM investigation context AND the Jira ticket
  "Health Scan Results" section.

The Jira "Health Scan Results" line for a failed check shows the exact
reason — for `gcp_metrics` that includes the current count, baseline
median, MAD, z-score, sample count, and which baseline strategy was
used (same-time-of-day vs all-prior-buckets).

## Service account — minimum roles

For a single service account that can run the full app with all
features enabled, grant these roles on the project:

| Role                                       | Why                                                                            |
| ------------------------------------------ | ------------------------------------------------------------------------------ |
| `roles/chronicle.editor`                   | `feed_state` + `restart_feed` (disable/enable)                                 |
| `roles/monitoring.viewer`                  | `gcp_metrics` check                                                            |
| `roles/aiplatform.user`                    | `llm` action (Vertex AI Gemini)                                                |
| `roles/storage.objectViewer`               | Read `config.yaml` + `feeds.yaml` from GCS bucket (Cloud Run only)             |
| `roles/secretmanager.secretAccessor`       | Read Jira API key + Chronicle customer ID from Secret Manager (Cloud Run only) |

If you do NOT use `restart_feed` / `auto_restart`, downgrade
`chronicle.editor` to `roles/chronicle.viewer`.

If you do NOT use the `llm` action, omit `roles/aiplatform.user`.

If you run locally with `variables.yaml`, omit `storage.objectViewer`
and `secretmanager.secretAccessor`.

**APIs to enable on the project:**

- `chronicle.googleapis.com`
- `monitoring.googleapis.com`
- `aiplatform.googleapis.com` (only if `llm` action used)
- `secretmanager.googleapis.com` (only if any creds in Secret Manager)
- `storage.googleapis.com` (config + feeds in GCS)

## Granular permissions (for custom roles / least-privilege)

If your org forbids predefined roles, build a custom role with exactly
what the app calls. Each line below is one IAM permission string.

**`feed_state` check** (`chronicle.feeds.list`, `chronicle.feeds.get`):

```
chronicle.feeds.list
chronicle.feeds.get
```

**`restart_feed` / `auto_restart`** (additional, on top of `feed_state`):

```
chronicle.feeds.update
chronicle.feeds.enable
chronicle.feeds.disable
```

(`chronicle.feeds.update` is what the SDK calls; `.enable` / `.disable`
may be required depending on the Chronicle API surface in your
tenant.)

**`udm_search` check:**

```
chronicle.legacies.legacyRunUdmQuery
```

(This permission is bundled in `roles/chronicle.viewer`.)

**`gcp_metrics` check:**

```
monitoring.timeSeries.list
monitoring.metricDescriptors.list   (used during initial discovery)
```

**`llm` action** (Vertex AI Gemini via google-genai):

```
aiplatform.endpoints.predict
```

(And the model itself must be enabled in your location.)

**`config.yaml` + `feeds.yaml` from GCS** (Cloud Run with `--add-volume`):

```
storage.objects.get
storage.objects.list
```

**Secrets from Secret Manager** (Cloud Run with `--set-secrets`):

```
secretmanager.versions.access
```

The Cloud Scheduler service account that triggers the Cloud Run Job
needs (separate SA, not the runtime SA):

```
run.jobs.run
```

## Auto-restart

When `global_settings.auto_restart.enabled` is `true` and a feed's
`feed_state` check fails, the app will:

1. Disable the feed.
2. Wait `wait_after_disable_seconds`.
3. Re-enable the feed.
4. Wait `wait_after_enable_seconds`.
5. Re-run all checks once.

If the retry passes, no failure actions fire. If it still fails, the
configured `actions_on_failure` run as normal.

## Auto-sync

When `global_settings.auto_sync.enabled` is `true`, the app runs
`sync_feeds()` at the start of every `python -m app.main` to pull the
latest feed list from Chronicle into `feeds.yaml` before checks run.

- `global_settings.auto_sync.enabled`
  - `true` → sync feeds from Chronicle on every app start
  - `false` → only sync when you run `python -m app.sync_feeds` manually
- `global_settings.auto_sync.new_feeds_enabled`
  - `'enabled'` value applied to NEWLY discovered feeds.
  - `false` (default) → new feeds appear with `enabled: false`; user
    must flip them to `true` to monitor.
  - `true` → new feeds are monitored immediately on first sync.

**IMPORTANT:** A sync NEVER overrides the `'enabled'` value of a feed
that already exists in `feeds.yaml`. The `new_feeds_enabled` setting
only affects feeds discovered for the first time.

## Debug notes

- `checks.py` currently dumps the full raw feed JSON from the
  Chronicle API for EVERY feed (healthy at INFO, unhealthy at
  WARNING). Search for `TEMP/DEBUG` in `checks.py` to revert to
  unhealthy-only dumping.
- LLM output is written to stderr with line-wrapping at 70 chars.
- `variables.yaml`, `config.yaml`, `feeds.yaml`, and `creds/` are
  gitignored — never commit credentials.
