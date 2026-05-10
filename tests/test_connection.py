import os
import sys
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# Allow `python .\tests\test_connection.py` from the repo root by
# making the project root importable as `app.*`.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from secops import SecOpsClient
from google.cloud import monitoring_v3
from google.protobuf.timestamp_pb2 import Timestamp

# Load PROJECT_ID / CUSTOMER_ID / REGION the same way the app does:
# env vars first, then variables.yaml. This makes the script work both
# locally (variables.yaml) and in any env-only deployment.
from app.config import _load_variables

_vars = _load_variables()
PROJECT_ID  = _vars["project_id"]
CUSTOMER_ID = _vars["customer_id"]
REGION      = _vars.get("region") or "us"


def _mask(value, keep=4):
    """Mask a sensitive identifier so terminal/CI logs don't leak it."""
    if not value:
        return "—"
    s = str(value)
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "…" + "*" * max(0, len(s) - keep)


def _explain(e, *, project_id=None):
    """Turn a raw GCP / Chronicle exception into a short, actionable hint.

    Returns (one_line_summary, suggested_fix). Both are strings;
    suggested_fix may be empty if the error doesn't match a known
    pattern (in which case the caller still has the raw exception).
    Project IDs in the message are masked so screenshots are safe.
    """
    import re
    msg = str(e)
    if project_id:
        msg = msg.replace(str(project_id), _mask(project_id))
    cust = _vars.get("customer_id")
    if cust:
        msg = msg.replace(str(cust), _mask(cust))

    # Pull status code + the API's own 'message' field if present
    # (much more useful than a 200-char URL).
    status_match = re.search(r"status[=:]\s*(\d+)", msg)
    msg_match = re.search(r"'message':\s*'([^']+)'", msg)
    reason_match = re.search(r"'reason':\s*'([A-Z_]+)'", msg)
    parts = []
    if status_match:
        parts.append(f"HTTP {status_match.group(1)}")
    if reason_match:
        parts.append(reason_match.group(1))
    if msg_match:
        parts.append(msg_match.group(1))
    summary = " — ".join(parts) if parts else msg.splitlines()[0]
    if len(summary) > 200:
        summary = summary[:200] + "…"

    low = msg.lower()
    fix = ""

    if "service_disabled" in low or "has not been used in project" in low:
        fix = (
            f"The Chronicle / Monitoring API is disabled for project {_mask(project_id)}.\n"
            f"     → Verify project_id in variables.yaml is correct.\n"
            f"     → Or enable it: gcloud services enable chronicle.googleapis.com "
            f"monitoring.googleapis.com --project=<your-project-id>"
        )
    elif "consumer_invalid" in low:
        fix = (
            f"Project {_mask(project_id)} is not registered with this Chronicle tenant.\n"
            "     → Verify project_id matches the GCP project bound to your Chronicle customer_id."
        )
    elif "not found" in low or "404" in msg or ("instance" in low and "not exist" in low):
        fix = (
            f"Resource not found. Most common cause: customer_id ({_mask(cust)}) is wrong.\n"
            f"     → Verify customer_id and region ({_vars.get('region', 'us')}) in variables.yaml."
        )
    elif "permission denied" in low or "403" in msg:
        fix = (
            "Permission denied. Most likely cause:\n"
            f"     → project_id in variables.yaml ({_mask(project_id)}) is wrong, OR\n"
            "     → the caller is missing roles/chronicle.viewer + roles/monitoring.viewer, OR\n"
            "     → the API is not enabled in this project (gcloud services enable ...).\n"
            "     → If running locally: gcloud auth application-default login"
        )
    elif "could not automatically determine credentials" in low or "default credentials" in low:
        fix = (
            "No GCP credentials found.\n"
            "     → Run: gcloud auth application-default login\n"
            "     → Or set GOOGLE_APPLICATION_CREDENTIALS to a service account JSON key."
        )
    elif "unauthenticated" in low or "401" in msg:
        fix = (
            "Authentication failed.\n"
            "     → Run: gcloud auth application-default login\n"
            "     → Confirm the credentials are not expired."
        )
    elif "name or service not known" in low or "failed to resolve" in low or "timeout" in low:
        fix = (
            "Network error reaching the API.\n"
            "     → Check internet connectivity and any corporate proxy."
        )
    elif "invalid_argument" in low or "400" in msg or "invalid argument" in low:
        fix = (
            f"Invalid request. Most common cause: customer_id ({_mask(cust)}) is malformed.\n"
            "     → customer_id must be the full 36-character UUID of your Chronicle instance.\n"
            "     → Verify it in variables.yaml (it's the value labeled"
            " 'Chronicle customer ID' in your tenant settings)."
        )

    return summary, fix


def _record_failure(failures, label, e, *, project_id=None):
    """Print a one-line FAIL marker now; stash details for the final summary."""
    summary, fix = _explain(e, project_id=project_id)
    print(f"  ❌ {label} FAILED — see summary below")
    failures.append((label, summary, fix))


def main():
    failures = []

    print("=" * 60)
    print("  Connection Test")
    print(f"  PROJECT_ID  : {_mask(PROJECT_ID)}")
    print(f"  CUSTOMER_ID : {_mask(CUSTOMER_ID)}")
    print(f"  REGION      : {REGION}")
    creds = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    print(f"  CREDENTIALS : {'SET' if creds else 'NOT SET (using ADC)'}")
    print("=" * 60)

    # ---------------------------------------------------------
    # Test 1: Chronicle client creation
    # ---------------------------------------------------------
    print("\n[1/4] Creating Chronicle client...")
    try:
        client = SecOpsClient()
        chronicle = client.chronicle(
            customer_id=CUSTOMER_ID,
            project_id=PROJECT_ID,
            region=REGION,
        )
        print("  ✅ Chronicle client created OK")
    except Exception as e:
        _record_failure(failures, "Chronicle client", e, project_id=PROJECT_ID)
        _print_summary(failures)
        return

    # ---------------------------------------------------------
    # Test 2: List feeds
    # ---------------------------------------------------------
    print("\n[2/4] Listing Chronicle feeds...")
    try:
        feeds = chronicle.list_feeds()
        if not feeds:
            print("  ⚠️ No feeds found")
        else:
            print(f"  ✅ Found {len(feeds)} feed(s)")
            for feed in feeds[:5]:  # Show first 5
                feed_id = feed.get("name", "").split("/")[-1]
                display = feed.get("displayName", "—")
                state   = feed.get("state", "unknown")
                print(f"     - {display} | {_mask(feed_id)} | State: {state}")
            if len(feeds) > 5:
                print(f"     ... and {len(feeds) - 5} more")
    except Exception as e:
        _record_failure(failures, "List feeds", e, project_id=PROJECT_ID)

    # ---------------------------------------------------------
    # Test 3: UDM search
    # ---------------------------------------------------------
    print("\n[3/4] Running UDM search (last 24h, max 1 event)...")
    # Override with TEST_NAMESPACE env var to query a real namespace.
    # Default is generic and will likely return 0 events — that's fine
    # for a connectivity check; only an API error is treated as failure.
    test_namespace = os.environ.get("TEST_NAMESPACE", "demo")
    try:
        end_time   = datetime.now(timezone.utc)
        start_time = end_time - timedelta(hours=24)
        result = chronicle.search_udm(
            query=f'namespace = "{test_namespace}"',
            start_time=start_time,
            end_time=end_time,
            max_events=1,
        )
        count = result.get("total_events", 0)
        print(f"  ✅ UDM search OK — {count} event(s) for namespace={test_namespace!r}")
    except Exception as e:
        _record_failure(failures, "UDM search", e, project_id=PROJECT_ID)

    # ---------------------------------------------------------
    # Test 4: GCP Cloud Monitoring metrics
    # ---------------------------------------------------------
    print("\n[4/4] Checking GCP Cloud Monitoring metrics (last 24h)...")
    try:
        mon_client = monitoring_v3.MetricServiceClient()
        now   = datetime.now(timezone.utc)
        start = now - timedelta(hours=24)

        interval = monitoring_v3.TimeInterval(
            end_time=Timestamp(seconds=int(now.timestamp())),
            start_time=Timestamp(seconds=int(start.timestamp())),
        )

        aggregation = monitoring_v3.Aggregation(
            alignment_period={"seconds": 86400},
            per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
        )

        req = monitoring_v3.ListTimeSeriesRequest(
            name=f"projects/{PROJECT_ID}",
            filter='metric.type = "chronicle.googleapis.com/ingestion/log/record_count"',
            interval=interval,
            aggregation=aggregation,
            view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        )

        series = list(mon_client.list_time_series(request=req))

        if not series:
            print("  ⚠️ No ingestion metrics found")
        else:
            namespace_totals = defaultdict(int)
            for ts in series:
                ns = ts.metric.labels.get("namespace", "unknown")
                total = sum(p.value.int64_value for p in ts.points)
                namespace_totals[ns] += total

            print(f"  ✅ Found {len(namespace_totals)} namespace(s)")
            print(f"     {'Namespace':<35} {'Records':>12}")
            print(f"     {'-'*35} {'-'*12}")
            for ns, count in sorted(namespace_totals.items(), key=lambda x: x[1], reverse=True)[:10]:
                print(f"     {ns:<35} {count:>12,}")
    except Exception as e:
        _record_failure(failures, "Cloud Monitoring", e, project_id=PROJECT_ID)

    _print_summary(failures)


def _print_summary(failures):
    print("\n" + "=" * 60)
    if not failures:
        print("  ✅ Connection test complete — all checks passed")
        print("=" * 60)
        return

    print(f"  ❌ Connection test complete — {len(failures)} failure(s)")
    print("=" * 60)
    for i, (label, summary, fix) in enumerate(failures, 1):
        print(f"\n[{i}] {label}")
        print(f"    {summary}")
        if fix:
            print(f"    💡 {fix}")
    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
