# Security Policy

## Reporting a Vulnerability

If you believe you've found a security vulnerability in this project,
**please do not open a public GitHub issue**.

Instead, report it privately using GitHub's
[Security Advisories](../../security/advisories/new) ("Report a
vulnerability" button on the **Security** tab of this repository).

Please include:

- A description of the issue and the impact you've assessed.
- Steps to reproduce (proof-of-concept code is welcome but not required).
- Affected version / commit SHA.
- Any suggested mitigation, if you have one.

You'll get an acknowledgement within **5 business days**. We'll work
with you on a fix and coordinate public disclosure once a patched
version is available.

## Scope

In scope:

- Code in this repository (`app/`, `tests/`, `Dockerfile`,
  `requirements.txt`).
- Default configuration in `config.yaml` and the example files.
- Documented deployment patterns in `REFERENCE.md`.

Out of scope:

- Vulnerabilities in third-party dependencies — please report those
  upstream (Dependabot already opens PRs for known CVEs in this repo).
- Misconfiguration of a user's own GCP project, Jira tenant, or SMTP
  relay.
- Findings that require attacker-controlled `config.yaml`,
  `variables.yaml`, `feeds.yaml`, or environment variables (those are
  trust boundaries — anyone who can write them already controls the
  app).

## Supported Versions

Only the `main` branch receives security fixes. There are no LTS
branches at this time.

## Hardening Notes for Operators

Even in the absence of a vulnerability, please follow these practices
when running this app against production data:

- Never commit `variables.yaml` or `feeds.yaml`. Both are gitignored;
  store them in Secret Manager (variables) and a locked-down GCS
  bucket (feeds) for production.
- On Cloud Run, pass secrets via `--set-secrets` (Secret Manager),
  **never** `--set-env-vars`. Env-var values appear in
  `gcloud run jobs describe` output and shell history.
- Grant the runtime service account least-privilege IAM roles — see
  the **SERVICE ACCOUNT — MINIMUM ROLES** section in `REFERENCE.md`. If
  you don't use auto-restart, downgrade `roles/chronicle.editor` to
  `roles/chronicle.viewer`.
- Enable bucket versioning + Data Access audit logs on the GCS bucket
  holding `feeds.yaml`.
- Set `log_level: PROD` for production runs — it suppresses
  potentially sensitive INFO/WARNING output and emits only a
  sanitized `COMPLETED:` summary line.
- Review `REFERENCE.md` → **DATA SENT TO THIRD PARTIES** before enabling
  the `jira` or `llm` actions if your feed names, namespaces, or
  queries contain regulated data.
