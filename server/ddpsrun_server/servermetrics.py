"""What this server says about itself, for Prometheus to scrape.

END-TO-END FLOW:

  1. `count_request()` is called once per finished request by the middleware in
     `main.py`, with the route's template path, the method and the status.
  2. The counts live in one dict in this process's memory.
  3. `render()` turns them into the Prometheus text format, and `GET /metrics`
     returns that.
  4. Prometheus (already running as `pacsrun-system/prometheus`) scrapes it.

★ WHY THIS EXISTS AT ALL, AND WHY IT IS NEW. On Lambda these numbers arrived
free: CloudWatch records Invocations, Errors, Duration and Throttles for every
function, and reading them is how the 30-day figure of 19,229 requests was
measured on 2026-09-14. A pod gets none of that. Moving the gateway into the
cluster therefore LOSES observability unless it is replaced, and this module is
the replacement for the half that matters most -- how many requests, of what
kind, and how many failed.

WHY NOT prometheus_client, THE OBVIOUS LIBRARY. It is a dependency for four
counters and one text format, in a package whose install size already decides
whether the Lambda zip fits (92.4 MB against a 250 MB limit, measured
2026-09-01). The text format is six lines of code and is frozen by Prometheus's
own exposition spec. If histograms or a registry are ever wanted, take the
dependency then.

THE PATH IS THE ROUTE TEMPLATE, NEVER THE REAL URL. `/v1/jobs/{job_id}/logs`
and not `/v1/jobs/job-66b46719b854/logs`. A label whose value is a job id makes
one time series per job, which is what "cardinality explosion" means in
practice: Prometheus's memory grows with the number of jobs anyone has ever run
and never comes back down.

WHAT IS DELIBERATELY NOT COUNTED. Nothing per user, per namespace or per job.
Those are the same cardinality problem wearing a different label, and the
question they would answer -- who spent what -- is already answered exactly by
`/v1/stats`, from the PacsJob objects themselves.

Grep anchor: HYPERUN-SERVER-METRICS
"""

from __future__ import annotations

import threading
import time

# Counts, keyed by (path template, method, status class). The status CLASS and
# not the code: "2xx" and "5xx" is the question anybody asks of a gateway, and
# keeping all 60-odd codes apart multiplies the series for an answer nobody
# needs. A 404 and a 400 are both "4xx" here; the log has the exact one.
_counts: dict[tuple[str, str, str], int] = {}
_latency_sum: dict[tuple[str, str], float] = {}
_latency_count: dict[tuple[str, str], int] = {}
_lock = threading.Lock()

_STARTED = time.time()


def count_request(path: str, method: str, status: int, seconds: float) -> None:
    """Record one finished request.

    Args:
        path: the ROUTE TEMPLATE, e.g. `/v1/jobs/{job_id}/logs`. See the module
            docstring for why this must not be the real URL.
        method: `GET`, `POST`, ...
        status: the HTTP status code that was returned.
        seconds: how long the request took.
    """
    klass = f"{status // 100}xx"
    with _lock:
        _counts[(path, method, klass)] = _counts.get((path, method, klass), 0) + 1
        _latency_sum[(path, method)] = _latency_sum.get((path, method), 0.0) + seconds
        _latency_count[(path, method)] = _latency_count.get((path, method), 0) + 1


def _escape(value: str) -> str:
    """Make one label value safe for the exposition format.

    Prometheus's text format ends a label value at an unescaped quote, so a path
    containing one would produce a line the scraper rejects and the whole scrape
    fails -- not just that series.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "")


def render() -> str:
    """Every counter, in the Prometheus text exposition format.

    Returns:
        The body of `GET /metrics`. Content type is `text/plain; version=0.0.4`.
    """
    with _lock:
        counts = dict(_counts)
        sums = dict(_latency_sum)
        totals = dict(_latency_count)

    lines: list[str] = []

    lines.append("# HELP hyperun_gateway_uptime_seconds How long this process has been up.")
    lines.append("# TYPE hyperun_gateway_uptime_seconds gauge")
    lines.append(f"hyperun_gateway_uptime_seconds {time.time() - _STARTED:.1f}")

    lines.append("# HELP hyperun_gateway_requests_total Requests this process has finished.")
    lines.append("# TYPE hyperun_gateway_requests_total counter")
    for (path, method, klass), n in sorted(counts.items()):
        lines.append(
            f'hyperun_gateway_requests_total{{path="{_escape(path)}",'
            f'method="{_escape(method)}",status="{klass}"}} {n}'
        )

    # A sum and a count rather than a histogram: dividing them gives the mean,
    # which is the question ("is it slower than it was") a gateway with a handful
    # of users actually gets asked. Buckets can come with prometheus_client.
    lines.append("# HELP hyperun_gateway_request_seconds_sum Time spent in requests.")
    lines.append("# TYPE hyperun_gateway_request_seconds_sum counter")
    for (path, method), total in sorted(sums.items()):
        lines.append(
            f'hyperun_gateway_request_seconds_sum{{path="{_escape(path)}",'
            f'method="{_escape(method)}"}} {total:.4f}'
        )
    lines.append("# HELP hyperun_gateway_request_seconds_count Requests timed.")
    lines.append("# TYPE hyperun_gateway_request_seconds_count counter")
    for (path, method), n in sorted(totals.items()):
        lines.append(
            f'hyperun_gateway_request_seconds_count{{path="{_escape(path)}",'
            f'method="{_escape(method)}"}} {n}'
        )

    return "\n".join(lines) + "\n"
