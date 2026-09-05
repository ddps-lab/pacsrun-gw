"""What a team has spent, computed from the jobs themselves.

END-TO-END FLOW of one `/v1/stats`:

  1. The caller's token names a team. `TokenStore.namespaces_in_team` turns that
     into the list of namespaces belonging to it — from the server's own token
     file, so this costs no Kubernetes permission.
  2. Every PacsJob in each of those namespaces is listed.
  3. `summarise()` turns them into per-person and whole-team totals: how many
     jobs, how they ended, how many GPU-hours, and what that cost.

WHY THIS IS POSSIBLE ONLY SINCE 2026-09-01. A PacsJob's status carried no
timestamps, so a finished job's own record said when it was accepted and nothing
about when it ran. PACSrun now writes status.startedAt and status.finishedAt
(PACSRUN-JOB-CLOCK), and those two are what make a duration, and a duration
times the offering's hourly price is what makes a cost.

WHY startedAt AND NOT creationTimestamp. Between the two sits waiting for a
solve, waiting for a machine, and pulling the image: minutes usually, once 1,800
seconds. Charging that to a person's total would overstate everyone, and by
different amounts.

WHAT IS DELIBERATELY NOT HERE. No per-job detail. A team total is a sum, and a
caller asking for their team's figures is not thereby entitled to read another
member's job names or results — that stays namespace-scoped, which is where the
isolation actually lives.

Grep anchor: DDPSRUN-STATS
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .measurements import gpu_by_name

# Phases that mean the job is over. Anything else is still spending.
TERMINAL_PHASES = {"Succeeded", "Failed", "Compared"}


@dataclass
class MemberTotals:
    """One person's figures inside a team."""

    user: str
    jobs: int = 0
    succeeded: int = 0
    failed: int = 0
    running: int = 0
    gpu_hours: float = 0.0
    cost_usd: float = 0.0
    unpriced_jobs: int = 0


@dataclass
class TeamTotals:
    """The whole team, plus each member."""

    team: str
    namespaces: list[str] = field(default_factory=list)
    members: list[MemberTotals] = field(default_factory=list)
    jobs: int = 0
    gpu_hours: float = 0.0
    cost_usd: float = 0.0
    unpriced_jobs: int = 0
    note: str = ""


def _parse_time(value: str | None) -> datetime | None:
    """Read a Kubernetes RFC 3339 timestamp.

    Args:
        value: e.g. "2026-09-01T00:12:45Z", or None.

    Returns:
        An aware datetime, or None when there is nothing to read.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def job_hours(job: dict[str, Any], now: datetime) -> float | None:
    """How long this job has been computing, in hours.

    Args:
        job: a PacsJob as a plain dict.
        now: what to measure a still-running job against.

    Returns:
        Hours, or None when the job never reached Running and therefore never
        spent anything on a machine. A job that failed during placement is the
        common case: it is a real job with a real outcome and zero cost.
    """
    status = job.get("status") or {}
    started = _parse_time(status.get("startedAt"))
    if started is None:
        return None
    finished = _parse_time(status.get("finishedAt")) or now
    seconds = (finished - started).total_seconds()
    return max(0.0, seconds / 3600)


def job_cost(job: dict[str, Any], hours: float) -> float | None:
    """What those hours cost.

    Args:
        job: a PacsJob as a plain dict.
        hours: from `job_hours`.

    Returns:
        Dollars, or None when we cannot price the machine it ran on — either
        status.currentOffering names no instance type, or it names one we have
        never rented and therefore have no measured price for. Returning None
        rather than zero is the point: a total that silently absorbed unpriced
        jobs would read as complete and be low.
    """
    offering = (job.get("status") or {}).get("currentOffering") or {}
    gpu = gpu_by_name(offering.get("instanceType") or "")
    if gpu is None:
        return None
    # parallelism pods each hold their own machine, so the bill is that many.
    slots = int((job.get("spec") or {}).get("parallelism", 1) or 1)
    return hours * gpu.usd_per_hour * slots


def summarise(
    team: str,
    namespaces: list[str],
    jobs_by_namespace: dict[str, Iterable[dict[str, Any]]],
    now: datetime | None = None,
) -> TeamTotals:
    """Turn a team's jobs into figures.

    Args:
        team: the team name.
        namespaces: its namespaces, in the order to report them.
        jobs_by_namespace: the PacsJobs found in each.
        now: what to measure still-running jobs against. Defaults to the current
            time; tests pass a fixed one.

    Returns:
        A `TeamTotals`. `unpriced_jobs` is how many jobs contributed hours but
        no cost, and `note` says so in words when it is not zero.
    """
    now = now or datetime.now(timezone.utc)
    totals = TeamTotals(team=team, namespaces=list(namespaces))

    for namespace in namespaces:
        # The convention is "<team>-<user>", so the member's name is the rest of
        # it. This is presentation only — nothing is decided by it — which is
        # why splitting on a dash is acceptable here and was not acceptable for
        # the S3 prefix.
        member = MemberTotals(
            user=namespace[len(team) + 1:] if namespace.startswith(team + "-") else namespace
        )

        for job in jobs_by_namespace.get(namespace, []):
            member.jobs += 1
            phase = ((job.get("status") or {}).get("phase")) or ""
            if phase == "Succeeded":
                member.succeeded += 1
            elif phase == "Failed":
                member.failed += 1
            elif phase not in TERMINAL_PHASES:
                member.running += 1

            hours = job_hours(job, now)
            if hours is None:
                continue
            member.gpu_hours += hours
            cost = job_cost(job, hours)
            if cost is None:
                member.unpriced_jobs += 1
            else:
                member.cost_usd += cost

        member.gpu_hours = round(member.gpu_hours, 2)
        member.cost_usd = round(member.cost_usd, 2)
        totals.members.append(member)

        totals.jobs += member.jobs
        totals.gpu_hours += member.gpu_hours
        totals.cost_usd += member.cost_usd
        totals.unpriced_jobs += member.unpriced_jobs

    totals.gpu_hours = round(totals.gpu_hours, 2)
    totals.cost_usd = round(totals.cost_usd, 2)

    if not namespaces:
        totals.note = (
            "this token names no team, so there is nothing to add together. An "
            "operator sets `team` on a token to put its owner in one."
        )
    elif totals.unpriced_jobs:
        totals.note = (
            f"{totals.unpriced_jobs} job(s) ran on a machine we have no measured price "
            f"for, so their hours are counted and their cost is not. The total below is "
            f"therefore a floor, not the bill."
        )
    return totals
