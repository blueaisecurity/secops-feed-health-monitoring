# tests/

A single smoke / integration script — **not** a pytest suite. Running
`pytest` from the repo root collects zero tests by design.

| Script                | What it does                                                  |
| --------------------- | ------------------------------------------------------------- |
| `test_connection.py`  | Probes Chronicle + Cloud Monitoring end-to-end: lists feeds, runs a UDM query, prints metric series. Sensitive identifiers (project ID, customer ID, feed UUIDs) are masked so the output is safe to paste into a bug report. |

Run it like a normal script (the venv must be active and credentials
configured per the root README):

```powershell
python .\tests\test_connection.py
```