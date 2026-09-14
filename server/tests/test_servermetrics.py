"""What the gateway says about itself once it is a pod and not a Lambda.

HYPERUN-SERVER-METRICS. CloudWatch counted Invocations, Errors and Duration for
the function for free; a pod gets none of that, so moving into the cluster loses
observability unless it is replaced. These tests defend the two things that make
the replacement usable rather than harmful:

  * the label is the ROUTE TEMPLATE, so the number of time series is the number
    of routes and not the number of jobs anybody has ever run;
  * nothing identifying is in a label, because per-user and per-job questions
    are answered by /v1/stats from the PacsJob objects themselves.
"""

from __future__ import annotations

import pytest

from ddpsrun_server import servermetrics


@pytest.fixture(autouse=True)
def empty_counters(monkeypatch):
    monkeypatch.setattr(servermetrics, "_counts", {})
    monkeypatch.setattr(servermetrics, "_latency_sum", {})
    monkeypatch.setattr(servermetrics, "_latency_count", {})


def test_a_request_is_counted_by_route_method_and_status_class():
    servermetrics.count_request("/v1/jobs", "GET", 200, 0.25)
    body = servermetrics.render()
    assert 'hyperun_gateway_requests_total{path="/v1/jobs",method="GET",status="2xx"} 1' in body


def test_the_status_is_a_class_and_not_the_code():
    # "how many 5xx" is the question a gateway is asked. Keeping all sixty-odd
    # codes apart multiplies the series for an answer nobody wants; the exact
    # code is in the log.
    servermetrics.count_request("/v1/jobs", "GET", 404, 0.01)
    servermetrics.count_request("/v1/jobs", "GET", 400, 0.01)
    body = servermetrics.render()
    assert 'status="4xx"} 2' in body
    assert "404" not in body and "400" not in body


def test_two_calls_to_the_same_route_are_one_series():
    # ★ THE WHOLE POINT. The caller passes `/v1/jobs/{job_id}/logs`, so two
    # different jobs land on ONE series. Passing the real URL instead would make
    # Prometheus's memory grow with every job ever run and never come back down.
    for _ in range(3):
        servermetrics.count_request("/v1/jobs/{job_id}/logs", "GET", 200, 0.1)
    body = servermetrics.render()
    assert body.count('path="/v1/jobs/{job_id}/logs"') == 3   # counter, sum, count
    assert "job-66b46719b854" not in body


def test_the_mean_can_be_computed_from_sum_and_count():
    servermetrics.count_request("/v1/stats", "GET", 200, 1.0)
    servermetrics.count_request("/v1/stats", "GET", 200, 3.0)
    body = servermetrics.render()
    assert 'hyperun_gateway_request_seconds_sum{path="/v1/stats",method="GET"} 4.0000' in body
    assert 'hyperun_gateway_request_seconds_count{path="/v1/stats",method="GET"} 2' in body


def test_a_quote_in_a_path_cannot_break_the_whole_scrape():
    # Prometheus ends a label value at an unescaped quote, and a malformed line
    # fails the ENTIRE scrape rather than one series. An unmatched request can
    # carry anything.
    servermetrics.count_request('/v1/"odd"', "GET", 404, 0.0)
    body = servermetrics.render()
    assert '\\"odd\\"' in body
    for line in body.splitlines():
        if line.startswith("#") or not line:
            continue
        assert line.count('"') % 2 == 0, line


def test_every_series_is_named_hyperun():
    # The product's name. Nothing new carries the old one (TASK.md, the rename).
    servermetrics.count_request("/v1/jobs", "GET", 200, 0.1)
    for line in servermetrics.render().splitlines():
        if line.startswith("#") or not line:
            continue
        assert line.startswith("hyperun_"), line


def test_uptime_is_reported_even_before_any_request():
    # A freshly restarted pod answers /metrics with something, so "did it just
    # restart" is answerable without waiting for traffic.
    assert "hyperun_gateway_uptime_seconds" in servermetrics.render()
