"""HYPERUN-USAGE: what each team, person and vendor spent, day by day.

THE ONE THING THAT MAKES THIS HARD, and the reason most of the cases below exist:
a job is not a point in time. The run this was written against took 9 h 44 m and
crossed midnight; several have taken over 24 hours. Charging a whole 25-hour run
to the day it FINISHED puts a $150 spike on a Tuesday and nothing on the Monday
it actually ran, and the daily figure is what an operator reads to notice
something is wrong.

So hours are split at midnight UTC and money follows the hours -- but a JOB is
counted once, on the day it started, because "we ran two jobs" is not true of one
run that happened to cross a date line.
"""
from __future__ import annotations

import datetime

import pytest

from ddpsrun_server import usage

UTC = datetime.timezone.utc


def when(text: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(text).replace(tzinfo=UTC)


NOW = when("2026-09-16T00:00:00")


def job(name, started, finished=None, owner="jglee", vendor="runpod",
        usd_per_hour=6.36, gpus=4):
    """A PacsJob shaped the way the cluster actually returns one."""
    status = {
        "phase": "Succeeded" if finished else "Running",
        "startedAt": started,
        "currentOffering": {"vendor": vendor, "instanceType": "NVIDIA A100-SXM4-80GB",
                            "usdPerHour": usd_per_hour, "gpuCount": gpus},
        "currentOfferingGroups": [{"vendor": vendor, "usdPerHour": usd_per_hour}],
    }
    if finished:
        status["finishedAt"] = finished
    return {
        "metadata": {"name": name,
                     "labels": {"ddpsrun.io/owner": owner, "ddpsrun.io/name": name}},
        "spec": {"parallelism": 1},
        "status": status,
    }


# ------------------------------------------------------------------ splitting


def test_a_run_inside_one_day_is_one_day():
    pieces = usage.split_across_days(when("2026-09-15T01:00:00"),
                                     when("2026-09-15T05:00:00"))
    assert pieces == {"2026-09-15": pytest.approx(4.0)}


def test_the_real_run_that_crossed_midnight_is_split_in_proportion():
    # ★ THE CASE THIS MODULE EXISTS FOR. `c3-job1-fix` started 01:05:15Z and
    # finished 10:49:17Z the same day -- but a run an hour earlier would have
    # crossed, and several have. Charging it all to one date is what makes a
    # daily figure useless.
    pieces = usage.split_across_days(when("2026-09-15T22:00:00"),
                                     when("2026-09-16T07:44:00"))
    assert pieces["2026-09-15"] == pytest.approx(2.0)
    assert pieces["2026-09-16"] == pytest.approx(7.733, abs=0.01)
    assert sum(pieces.values()) == pytest.approx(9.733, abs=0.01)


def test_a_run_longer_than_a_day_touches_every_day_it_covers():
    pieces = usage.split_across_days(when("2026-09-14T12:00:00"),
                                     when("2026-09-16T06:00:00"))
    assert sorted(pieces) == ["2026-09-14", "2026-09-15", "2026-09-16"]
    assert pieces["2026-09-15"] == pytest.approx(24.0), "a whole day in the middle"


def test_a_job_that_died_the_instant_it_started_is_zero_days_not_an_error():
    assert usage.split_across_days(when("2026-09-15T01:00:00"),
                                   when("2026-09-15T01:00:00")) == {}


# ------------------------------------------------------------------ rolling up


def test_money_follows_the_hours_across_the_midnight_it_crossed():
    jobs = {"ddps-a": [job("j", "2026-09-14T22:00:00Z", "2026-09-15T02:00:00Z",
                           usd_per_hour=10.0)]}
    out = usage.summarise(jobs, {"ddps-a": "ddps"}, NOW, days=5)
    by_date = {d.date: d for d in out.days}
    assert by_date["2026-09-14"].estimate_usd == pytest.approx(20.0)
    assert by_date["2026-09-15"].estimate_usd == pytest.approx(20.0)


def test_one_job_is_counted_once_on_the_day_it_started():
    # ★ HOURS SPLIT, JOBS DO NOT. "We ran two jobs" is not true of one run that
    # crossed a date line, and a report that said so would be answering a
    # different question from the one it printed.
    jobs = {"ddps-a": [job("j", "2026-09-14T22:00:00Z", "2026-09-16T02:00:00Z")]}
    out = usage.summarise(jobs, {"ddps-a": "ddps"}, NOW, days=5)
    counts = {d.date: d.jobs for d in out.days}
    assert counts["2026-09-14"] == 1
    assert counts["2026-09-15"] == 0
    assert counts["2026-09-16"] == 0


def test_the_three_rollups_agree_with_each_other():
    jobs = {
        "ddps-a": [job("a", "2026-09-15T00:00:00Z", "2026-09-15T04:00:00Z",
                       owner="alice", vendor="runpod", usd_per_hour=6.0),
                   job("b", "2026-09-15T00:00:00Z", "2026-09-15T02:00:00Z",
                       owner="bob", vendor="aws", usd_per_hour=4.0)],
        "ddps-b": [job("c", "2026-09-15T00:00:00Z", "2026-09-15T01:00:00Z",
                       owner="carol", vendor="aws", usd_per_hour=4.0)],
    }
    out = usage.summarise(jobs, {"ddps-a": "ddps", "ddps-b": "other"}, NOW, days=5)

    total = 6.0 * 4 + 4.0 * 2 + 4.0 * 1        # 24 + 8 + 4
    assert sum(t.estimate_usd for t in out.teams) == pytest.approx(total)
    assert sum(u.estimate_usd for u in out.users) == pytest.approx(total)
    assert sum(v.estimate_usd for v in out.vendors) == pytest.approx(total)
    assert sum(d.estimate_usd for d in out.days) == pytest.approx(total)


def test_a_namespace_maps_to_its_team_and_two_namespaces_can_share_one():
    jobs = {
        "ddps-a": [job("a", "2026-09-15T00:00:00Z", "2026-09-15T01:00:00Z", usd_per_hour=1.0)],
        "ddps-b": [job("b", "2026-09-15T00:00:00Z", "2026-09-15T01:00:00Z", usd_per_hour=1.0)],
    }
    out = usage.summarise(jobs, {"ddps-a": "ddps", "ddps-b": "ddps"}, NOW, days=5)
    assert [t.name for t in out.teams] == ["ddps"]
    assert out.teams[0].estimate_usd == pytest.approx(2.0)


def test_a_namespace_the_directory_does_not_name_lands_under_its_own_name():
    # An operator namespace with nobody registered in it. Dropping it would hide
    # real spend; inventing a team for it would be a lie.
    jobs = {"default": [job("a", "2026-09-15T00:00:00Z", "2026-09-15T01:00:00Z")]}
    out = usage.summarise(jobs, {}, NOW, days=5)
    assert [t.name for t in out.teams] == ["default"]


def test_everything_is_sorted_by_spend_because_that_is_the_reading_order():
    jobs = {"ns": [
        job("small", "2026-09-15T00:00:00Z", "2026-09-15T01:00:00Z",
            owner="small", usd_per_hour=1.0),
        job("big", "2026-09-15T00:00:00Z", "2026-09-15T10:00:00Z",
            owner="big", usd_per_hour=6.0),
    ]}
    out = usage.summarise(jobs, {}, NOW, days=5)
    assert [u.name for u in out.users] == ["big", "small"]


# ------------------------------------------------------------------ honesty


def test_a_job_that_never_started_costs_nothing_and_is_not_counted():
    # Failed during placement: a real job with a real outcome that rented
    # nothing. Counting it would put a zero-dollar row in every table.
    never = {"metadata": {"name": "j", "labels": {}}, "spec": {}, "status": {"phase": "Failed"}}
    out = usage.summarise({"ns": [never]}, {}, NOW, days=5)
    assert out.teams == [] and sum(d.jobs for d in out.days) == 0


def test_an_unpriced_job_is_counted_rather_than_absorbed():
    # ★ A TOTAL THAT SILENTLY SWALLOWED IT WOULD READ AS COMPLETE AND BE LOW.
    unpriced = job("j", "2026-09-15T00:00:00Z", "2026-09-15T02:00:00Z")
    unpriced["status"]["currentOffering"] = {"vendor": "runpod"}
    unpriced["status"].pop("currentOfferingGroups")
    out = usage.summarise({"ns": [unpriced]}, {}, NOW, days=5)
    assert sum(d.unpriced_jobs for d in out.days) >= 1
    assert "LOW by whatever those cost" in out.note
    # The hours are still real and still reported.
    assert sum(d.gpu_hours for d in out.days) == pytest.approx(2.0)


def test_every_answer_says_the_money_is_an_estimate():
    # baseline-c computed to $11.07 and was billed $44.28. A reader who takes
    # these for an invoice loses an afternoon.
    out = usage.summarise({}, {}, NOW, days=5)
    assert "estimate" in out.note


def test_the_window_is_clamped_rather_than_answered_short():
    out = usage.summarise({}, {}, NOW, days=9999)
    assert len(out.days) == usage.MAX_DAYS


def test_yesterdays_figures_are_yesterdays_and_not_todays():
    # ★ TODAY IS HALF FINISHED. A report that called today's partial spend
    # "yesterday" would print a number that grows while somebody reads it.
    jobs = {"ns": [
        job("yday", "2026-09-15T00:00:00Z", "2026-09-15T02:00:00Z",
            owner="alice", usd_per_hour=5.0),
        job("today", "2026-09-16T00:00:00Z", None, owner="alice", usd_per_hour=5.0),
    ]}
    out = usage.summarise(jobs, {}, when("2026-09-16T06:00:00"), days=5)
    alice = out.users[0]
    assert alice.day_estimate_usd == pytest.approx(10.0), "only the 2 h that ran yesterday"
    assert alice.day_jobs == 1


def test_month_to_date_covers_the_month_the_window_ends_in():
    jobs = {"ns": [
        job("last-month", "2026-08-31T00:00:00Z", "2026-08-31T02:00:00Z", usd_per_hour=5.0),
        job("this-month", "2026-09-01T00:00:00Z", "2026-09-01T02:00:00Z", usd_per_hour=5.0),
    ]}
    out = usage.summarise(jobs, {}, when("2026-09-16T00:00:00"), days=40)
    assert out.mtd_estimate_usd == pytest.approx(10.0)
    assert out.mtd_jobs == 1
