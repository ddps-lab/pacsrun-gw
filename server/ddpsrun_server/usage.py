"""What each team, person and vendor spent, day by day.

END-TO-END FLOW of one `GET /v1/usage`:

  1. Every PacsJob in every namespace the directory names is listed.
  2. `job_hours` and `job_cost` (stats.py, unchanged) say how long each ran and
     what that cost. This module does NOT re-price anything.
  3. `split_across_days` cuts each job's hours at midnight UTC, so a run that
     started at 01:05 and ended at 10:49 the next day contributes to both days
     in proportion.
  4. The pieces are added up three ways -- by team, by person, by vendor -- and
     once more into a per-day total and a month-to-date figure.

★ WHY A JOB IS SPLIT AT ALL, rather than charged to the day it finished. The run
this was written against took 9 h 44 m; several have taken over 24. Charging the
whole of a 25-hour job to one date puts a $150 spike on a Tuesday and nothing on
the Monday it actually ran, and the daily figure is the number an operator uses
to notice something is wrong. Splitting is arithmetic on two timestamps the CR
already carries.

★★ EVERY NUMBER HERE IS AN ESTIMATE AND SAYS SO. It is the catalogue rate times
the hours, which is what the screen and `hyperun stats` have always shown. The
vendor's own bill is a different number and has been measured to differ by a lot:
`baseline-c` computed to $11.07 and was billed $44.28 (a missed card count). So
every field carrying money here is named `*_estimate_usd`, and anything that
wants the truth has to ask the vendor. Naming it `cost_usd` would invite a reader
to reconcile against an invoice and lose an afternoon.

WHAT IT DOES NOT DO. It does not read the vendors' billing APIs. RunPod's
`myself.billing(granularity: DAILY)` answers for the WHOLE ACCOUNT, which
includes another researcher's pods (`khlee-*`), so it cannot be split per job and
cannot go in a per-person table. It belongs beside these numbers as a
reconciliation line, not inside them.

Grep anchor: HYPERUN-USAGE
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any

from . import stats

# How many days back a caller may ask for. Sixty because a month-to-date figure
# needs the whole month and a comparison needs the one before it; beyond that the
# PacsJob objects themselves have usually been deleted and the answer would be
# quietly short rather than wrong.
MAX_DAYS = 60


@dataclass
class DayTotals:
    """One calendar day, UTC."""

    date: str
    jobs: int = 0
    gpu_hours: float = 0.0
    estimate_usd: float = 0.0
    # Jobs whose machine we could not price. Counted rather than absorbed: a
    # total that silently swallowed them would read as complete and be low.
    unpriced_jobs: int = 0


@dataclass
class Bucket:
    """One team, person or vendor, over the whole window."""

    name: str
    jobs: int = 0
    gpu_hours: float = 0.0
    estimate_usd: float = 0.0
    unpriced_jobs: int = 0
    # The same figures for TODAY. Today is half-finished on purpose: a person
    # watching a job wants the number that is still moving.
    day_gpu_hours: float = 0.0
    day_estimate_usd: float = 0.0
    day_jobs: int = 0


@dataclass
class Usage:
    """Everything `GET /v1/usage` answers."""

    days: list[DayTotals] = field(default_factory=list)
    teams: list[Bucket] = field(default_factory=list)
    users: list[Bucket] = field(default_factory=list)
    vendors: list[Bucket] = field(default_factory=list)
    # Month to date, in UTC, of the day the window ends on.
    mtd_estimate_usd: float = 0.0
    mtd_gpu_hours: float = 0.0
    mtd_jobs: int = 0
    note: str = ""


def split_across_days(started: datetime.datetime, finished: datetime.datetime
                      ) -> dict[str, float]:
    """How many hours of one run fall on each UTC date.

    Args:
        started: when the job began computing.
        finished: when it stopped, or now for a job still running.

    Returns:
        date string -> hours. A run inside one day gives one entry; the
        9 h 44 m run measured on 2026-09-15 gives one, and a 25-hour run gives
        two or three.

        An empty dict when `finished` is at or before `started`, which happens
        for a job that failed the instant it started -- a real outcome with zero
        hours, not an error.
    """
    if finished <= started:
        return {}
    out: dict[str, float] = {}
    cursor = started
    while cursor < finished:
        # Midnight after `cursor`, or the end, whichever comes first.
        next_midnight = datetime.datetime.combine(
            cursor.date() + datetime.timedelta(days=1),
            datetime.time.min, tzinfo=datetime.timezone.utc)
        edge = min(next_midnight, finished)
        hours = (edge - cursor).total_seconds() / 3600
        key = cursor.date().isoformat()
        out[key] = out.get(key, 0.0) + hours
        cursor = edge
    return out


def _vendor_of(job: dict[str, Any]) -> str:
    """Which vendor sold the machine, or "unknown".

    Read off `status.currentOffering`, the same field `stats.py` prices from, so
    the two cannot disagree about who a job belongs to.
    """
    offering = (job.get("status") or {}).get("currentOffering") or {}
    return str(offering.get("vendor") or "unknown")


def summarise(jobs_by_namespace: dict[str, list[dict[str, Any]]],
              team_of: dict[str, str],
              now: datetime.datetime,
              days: int = 30) -> Usage:
    """Roll every job up by day, team, person and vendor.

    Args:
        jobs_by_namespace: namespace -> the PacsJobs in it.
        team_of: namespace -> team name, from the token directory. A namespace
            missing from it lands under its own name, which is what an operator
            namespace with nobody registered in it should look like.
        now: what an unfinished job is measured against, and the day the window
            ends on.
        days: how far back to report. Clamped to MAX_DAYS.

    Returns:
        A `Usage`. Every list is sorted by estimate, largest first -- which is
        the order a reader scans, and the order the Slack report prints.
    """
    days = max(1, min(days, MAX_DAYS))
    end = now.date()
    start = end - datetime.timedelta(days=days - 1)
    window = {(start + datetime.timedelta(days=i)).isoformat(): DayTotals(
        date=(start + datetime.timedelta(days=i)).isoformat()) for i in range(days)}

    # ★ TODAY, NOT YESTERDAY, AND THAT IS A DECISION. Today's date is
    # half-finished, so this number grows while somebody reads it -- which is
    # exactly what a person watching a job wants. The question the daily report
    # answers is "what is running now and what has it cost", not "what did we
    # spend on a day that is over". A finished day is still in `days` for anyone
    # who wants it. (Changed 2026-09-16 at the user's request; the first version
    # showed the last complete day for the opposite reason.)
    today = end.isoformat()
    month_prefix = end.isoformat()[:7]

    teams: dict[str, Bucket] = {}
    users: dict[str, Bucket] = {}
    vendors: dict[str, Bucket] = {}
    result = Usage()

    def bucket(table: dict[str, Bucket], name: str) -> Bucket:
        if name not in table:
            table[name] = Bucket(name=name)
        return table[name]

    for namespace, jobs in jobs_by_namespace.items():
        team = team_of.get(namespace, namespace)
        for job in jobs:
            hours = stats.job_hours(job, now)
            if hours is None:
                # Never reached Running, so it rented nothing. A real job with a
                # real outcome and no cost.
                continue
            cost = stats.job_cost(job, hours)
            status = job.get("status") or {}
            started = stats._parse_time(status.get("startedAt"))
            if started is None:
                continue
            finished = stats._parse_time(status.get("finishedAt")) or now
            pieces = split_across_days(started, finished)
            if not pieces:
                continue

            labels = (job.get("metadata") or {}).get("labels") or {}
            owner = str(labels.get("ddpsrun.io/owner") or "unknown")
            vendor = _vendor_of(job)
            # The hourly rate, so each day's slice can be priced on its own
            # hours rather than on the whole job's.
            rate = None if cost is None or hours <= 0 else cost / hours

            touched_window = False
            for date_key, slice_hours in pieces.items():
                slice_cost = None if rate is None else rate * slice_hours

                if date_key in window:
                    touched_window = True
                    day = window[date_key]
                    day.gpu_hours += slice_hours
                    if slice_cost is None:
                        day.unpriced_jobs += 1
                    else:
                        day.estimate_usd += slice_cost

                if date_key[:7] == month_prefix:
                    result.mtd_gpu_hours += slice_hours
                    if slice_cost is not None:
                        result.mtd_estimate_usd += slice_cost

                # Today's figures are kept per bucket so a report can print
                # "who is spending what today" without a second pass.
                if date_key == today:
                    for table, key in ((teams, team), (users, owner), (vendors, vendor)):
                        b = bucket(table, key)
                        b.day_gpu_hours += slice_hours
                        if slice_cost is not None:
                            b.day_estimate_usd += slice_cost

            # ONE JOB COUNTS ONCE, on the day it STARTED, however many days it
            # spans. Counting it per day would report a 25-hour run as two jobs
            # and make "how many did we run" unanswerable.
            first_day = min(pieces)
            if first_day in window:
                window[first_day].jobs += 1
            if first_day[:7] == month_prefix:
                result.mtd_jobs += 1
            if first_day == today:
                for table, key in ((teams, team), (users, owner), (vendors, vendor)):
                    bucket(table, key).day_jobs += 1

            if touched_window:
                for table, key in ((teams, team), (users, owner), (vendors, vendor)):
                    b = bucket(table, key)
                    b.jobs += 1
                    b.gpu_hours += hours
                    if cost is None:
                        b.unpriced_jobs += 1
                    else:
                        b.estimate_usd += cost

    def finish(table: dict[str, Bucket]) -> list[Bucket]:
        out = []
        for b in table.values():
            b.gpu_hours = round(b.gpu_hours, 2)
            b.estimate_usd = round(b.estimate_usd, 2)
            b.day_gpu_hours = round(b.day_gpu_hours, 2)
            b.day_estimate_usd = round(b.day_estimate_usd, 2)
            out.append(b)
        return sorted(out, key=lambda b: (-b.estimate_usd, b.name))

    for day in window.values():
        day.gpu_hours = round(day.gpu_hours, 2)
        day.estimate_usd = round(day.estimate_usd, 2)

    result.days = [window[k] for k in sorted(window)]
    result.teams = finish(teams)
    result.users = finish(users)
    result.vendors = finish(vendors)
    result.mtd_estimate_usd = round(result.mtd_estimate_usd, 2)
    result.mtd_gpu_hours = round(result.mtd_gpu_hours, 2)

    unpriced = sum(d.unpriced_jobs for d in result.days)
    if unpriced:
        result.note = (
            f"{unpriced} job(s) ran on a machine with no price we know, so the totals are "
            f"LOW by whatever those cost. Everything here is an estimate in any case: it is "
            f"the catalogue rate times the hours, and a measured run computed to $11.07 "
            f"against a $44.28 bill."
        )
    else:
        result.note = (
            "Every figure is an estimate -- the catalogue rate times the hours. The vendor's "
            "bill is a different number and has been measured to differ by a lot."
        )
    return result
