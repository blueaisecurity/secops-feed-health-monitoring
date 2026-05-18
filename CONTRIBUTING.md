# Contributing

Thanks for taking the time to look! This is a small project — quick
guidelines below.

## Reporting bugs / requesting features

Use the issue templates under **Issues → New issue**. Please include
your Python version, OS, and the relevant log output (with
`log_level: DEBUG` if possible). Redact `project_id`, `customer_id`,
and any feed UUIDs before posting.

## Reporting security vulnerabilities

**Do not open a public issue.** See [SECURITY.md](SECURITY.md) for the
private disclosure process.

## Pull requests

1. Fork, branch, and open a PR against `main`.
2. Keep changes focused — one logical change per PR.
3. Match the existing code style (4-space indent, no extra
   reformatting of unrelated lines).
4. Run the smoke checks locally before pushing:
   ```powershell
   python -m compileall -q app tests
   cp config.yaml.example    config.yaml      # if you don't already have one
   cp variables.yaml.example variables.yaml   # ditto
   cp feeds.yaml.example     feeds.yaml
   python -c "from app.config import load_config; load_config()"
   python .\tests\test_connection.py          # probes Chronicle + Monitoring
   ```
5. CI (`.github/workflows/smoke.yml`) must be green before merge.

## Adding a new check

Health checks live in [app/checks.py](app/checks.py). A check is a
function with the signature:

```python
def check_my_thing(config, feed_config, feeds_cache=None):
    # return True / False, a string, or a dict (see existing checks)
```

Wire it into `app/main.py` `CHECK_REGISTRY` and document it in
README → **AVAILABLE CHECKS**.

## Adding a new action

Action handlers live in [app/actions.py](app/actions.py). Follow the
existing `jira` / `email` pattern: gate on `config["actions"][name]["enabled"]`,
read credentials from `variables.yaml` / env vars (never `config.yaml`),
and degrade gracefully when the action is disabled.

## What I'm unlikely to merge

- Bare reformatting / linter-only changes (open an issue first so we
  can agree on a tool before churning the diff).
- New runtime dependencies without a clear justification — the
  install footprint matters for Cloud Run cold-start.
- Anything that requires `variables.yaml` to be committed, or that
  reads secrets from `config.yaml`.

## Use of AI tools

AI coding assistants (e.g., GitHub Copilot) may be used to help draft
or refine contributions. If you use them, please review and test the
output yourself before opening a PR — you are responsible for the
code you submit.
