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

```powershell
git clone https://github.com/blueaisecurity/secops-feed-health-monitoring.git feed-health-monitoring
cd feed-health-monitoring
python -m venv venv
.\venv\Scripts\Activate.ps1            # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

cp variables.yaml.example variables.yaml   # fill in project_id, customer_id, region
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
ingestion guardrail once credentials are in `variables.yaml`.

## Configuration files

| File | What | Committed? |
|---|---|---|
| `config.yaml` | Tuning knobs + action toggles | yes |
| `variables.yaml` | project_id, customer_id, secrets | **gitignored** |
| `feeds.yaml` | Per-feed settings | **gitignored** |

## Production

Deploy as a Cloud Run **Job** triggered by Cloud Scheduler. Secrets via
Secret Manager (`--set-secrets`), `feeds.yaml` mounted from a hardened
GCS bucket, no JSON keys on disk. Full walkthrough in
[REFERENCE.md → SETUP — CLOUD RUN](REFERENCE.md#setup--cloud-run-production).

## Operator env vars

- `FEEDHEALTH_UNMASK=1` — show raw IDs in terminal output (terminal only,
  never on Cloud Run).
- `FEEDHEALTH_NO_CONFIRM=1` — skip the DEBUG confirmation prompt.
 
## License

MIT — see [LICENSE](LICENSE).