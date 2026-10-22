"""Prometheus metrics."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

jobs_total = Counter(
    "civision_jobs_total", "Video jobs accepted", ["model", "status"]
)
jobs_succeeded_total = Counter("civision_jobs_succeeded_total", "Video jobs that completed")
jobs_failed_total = Counter("civision_jobs_failed_total", "Video jobs that failed")
moli_spent_total = Counter("civision_moli_spent_total", "Magicubes consumed")
requests_total = Counter("civision_requests_total", "API requests", ["path", "status"])
queue_depth_gauge = Gauge("civision_queue_depth", "Jobs waiting to run")
active_accounts_gauge = Gauge("civision_active_accounts", "Accounts currently usable")
upstream_errors_total = Counter(
    "civision_upstream_errors_total", "Upstream failures", ["kind"]
)
job_duration_seconds = Histogram(
    "civision_job_duration_seconds", "Wall-clock time to finish a job"
)
