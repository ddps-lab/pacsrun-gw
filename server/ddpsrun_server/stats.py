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
from .naming import OWNER_LABEL

# What the Member column says for a job with no owner label. NOT a person's
# name and deliberately lowercase, so it cannot be mistaken for an account —
# see the bucket comment in `summarise`.
NO_OWNER = "kubectl"

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
class VendorTotals:
    """One vendor's share of the team's figures.

    The vendor is status.currentOffering.vendor — "aws", "runpod", "gcp".

    ★ A JOB THAT NAMES NO VENDOR AND NEVER RAN IS IN NO ROW AT ALL. Nothing was
    bought and nobody is named, so there is nothing to file. Until 2026-09-11
    the bucket was made before the duration was known, so 17 compare-mode jobs
    and one Pending seed marker showed up as a seller called `unknown` with 0.0
    hours and $0.00 next to `runpod`, which reads as a vendor we had failed to
    identify. Such a job stays in the team's job count and in `members`; the
    Vendors screen subtracts the two and says how many it left out.

    "unknown" itself is not gone and keeps its original meaning: a job that DID
    rent a machine for real hours whose record does not name the seller. Those
    hours must land somewhere rather than disappear.

    A NAMED VENDOR KEEPS ITS ROW EVEN WITH NO CLOCK. Jobs that finished before
    PACSRUN-JOB-CLOCK (2026-09-01) carry a vendor and no startedAt -- one gcp
    and eleven runpod on this cluster. A row saying 0.0 hours is a true and
    useful answer; deleting `gcp` from the table because we cannot time it is
    not. See the loop in `summarise` for the two-fact rule.
    """

    vendor: str
    jobs: int = 0
    gpu_hours: float = 0.0
    cost_usd: float = 0.0
    unpriced_jobs: int = 0


@dataclass
class TeamTotals:
    """The whole team, plus each member and each vendor."""

    team: str
    namespaces: list[str] = field(default_factory=list)
    members: list[MemberTotals] = field(default_factory=list)
    vendors: list[VendorTotals] = field(default_factory=list)
    jobs: int = 0
    gpu_hours: float = 0.0
    cost_usd: float = 0.0
    unpriced_jobs: int = 0
    # How many of `jobs` carry no ddpsrun.io/owner label, and so appear in no
    # row of `members`. Their hours and dollars ARE in the totals above; only
    # the row is gone. See the rollup in `summarise`.
    unowned_jobs: int = 0
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
    status = job.get("status") or {}

    # PACSRUN-GROUP-PRICE, the vendor-neutral first choice. Since 2026-09-07
    # PACSrun stamps every offering group with the price the winning answer
    # stated (usdPerHour — machine count already inside, so nothing here
    # multiplies). When EVERY group carries one, the job's hourly rate is
    # simply their sum: any vendor the solver can price, AWS included, with no
    # price table in this server at all. A partly priced job falls through to
    # the measured-card estimate below — the same all-or-nothing rule
    # answerPrice applies in PACSrun, because a partial sum can only ever
    # understate the bill.
    groups = status.get("currentOfferingGroups") or []
    rates = [g.get("usdPerHour") for g in groups]
    if groups and all(rates):
        try:
            return hours * sum(float(rate) for rate in rates)
        except (TypeError, ValueError):
            pass  # a malformed stamp is a bug upstream; fall back to the estimate

    offering = status.get("currentOffering") or {}
    gpu = gpu_by_name(offering.get("instanceType") or "")
    if gpu is None:
        return None
    spec = job.get("spec") or {}
    # parallelism pods each hold their own machine, so the bill is that many —
    # and each machine holds spec.gpus.count CARDS, each billed separately,
    # because the measured usd_per_hour is PER CARD (RunPod bills 4 x
    # A100-SXM4-80GB at $1.59/GPU/hr = $6.36/hr, read 2026-09-04). Forgetting
    # the card count is how baseline-c (4 cards, 6.97 h) showed $11.07 on the
    # screen while RunPod's ledger said $44.28 (facts/cost-ledger.md,
    # 2026-09-07): the missing factor was exactly the 4.
    slots = int(spec.get("parallelism", 1) or 1)
    # The card count lives at spec.resources.gpus.count on the CRD (checked on
    # the live baseline-c object, 2026-09-07) — NOT at spec.gpus, which was the
    # first version of this line and quietly read 1 for every job.
    gpus = ((spec.get("resources") or {}).get("gpus")) or {}
    cards = int(gpus.get("count", 1) or 1)
    return hours * gpu.usd_per_hour * slots * cards


def summarise(
    team: str,
    namespaces: list[str],
    jobs_by_namespace: dict[str, Iterable[dict[str, Any]]],
    now: datetime | None = None,
    known_vendors: Iterable[str] = (),
) -> TeamTotals:
    """Turn a team's jobs into figures.

    Args:
        team: the team name.
        namespaces: its namespaces, in the order to report them.
        jobs_by_namespace: the PacsJobs found in each.
        now: what to measure still-running jobs against. Defaults to the current
            time; tests pass a fixed one.
        known_vendors: every vendor this deployment can buy from, which the
            caller supplies because `models.KNOWN_VENDORS` cannot be imported
            here -- models imports this module. Each gets a row whether or not
            this team has used it. See the seeding below for why.

    Returns:
        A `TeamTotals`. `unpriced_jobs` is how many jobs contributed hours but
        no cost, and `note` says so in words when it is not zero.
    """
    now = now or datetime.now(timezone.utc)
    totals = TeamTotals(team=team, namespaces=list(namespaces))

    # A member is a PERSON, and the person is on the job itself: the
    # ddpsrun.io/owner label the server writes at submit time — the same value
    # the jobs screen prints under "Submitted by". A job with no owner label was
    # applied straight to the cluster with kubectl, which only an operator can
    # do.
    #
    # ★ THAT BUCKET IS CALLED `kubectl`, AND TWO EARLIER NAMES WERE WRONG for
    # the same reason: they put something that is not a person in the Member
    # column, and readers took it for one.
    #
    #   "default"   the NAMESPACE, until 2026-09-07. A namespace pretending to
    #               be a person.
    #   "admin"     a ROLE, until 2026-09-10. On this cluster that row read 36
    #               jobs and $62.57 against the caller's own 3 jobs and $42.61,
    #               and the caller asked why the only member was "admin" and
    #               where their own spend had gone. It had not gone anywhere; it
    #               was the second row, under something that looked like an
    #               account.
    #
    # `kubectl` is what actually applied them — a fact about the job rather than
    # a guess about who is answerable for it, and nobody reads it as a colleague.
    members: dict[str, MemberTotals] = {}
    # The same jobs, added up a second way: by WHO SOLD the machine. Same loop,
    # same hours, same prices — the two tables must never disagree about a job.
    #
    # ★ THE ROWS ARE THE CATALOGUE, NOT A BY-PRODUCT OF THE JOBS. Every vendor
    # this deployment can buy from is seeded here with zeroes, so the table is
    # the same seven rows for every team, every namespace and every account,
    # including one created a minute ago with no jobs at all. Deriving the rows
    # from the jobs instead made the list mean "who happens to have sold to
    # THESE namespaces": on 2026-09-11 the Per vendor table showed one row,
    # `runpod`, because aws, gcp and shadeform had only ever sold into the
    # `default` namespace, which is not in this team. A vendor that sold
    # nothing is a real and useful answer -- 0 jobs, 0.0 hours, $0.00 -- and an
    # absent row is not, because the reader cannot tell "never used" from
    # "we forgot about this one".
    #
    # A job naming a vendor outside this list still gets its own row appended
    # by the loop below, so a name the gateway has not heard of is visible
    # rather than swallowed.
    vendors: dict[str, VendorTotals] = {
        name: VendorTotals(vendor=name) for name in known_vendors
    }

    for namespace in namespaces:
        for job in jobs_by_namespace.get(namespace, []):
            labels = ((job.get("metadata") or {}).get("labels")) or {}
            owner = labels.get(OWNER_LABEL) or NO_OWNER
            member = members.setdefault(owner, MemberTotals(user=owner))

            member.jobs += 1
            phase = ((job.get("status") or {}).get("phase")) or ""
            if phase == "Succeeded":
                member.succeeded += 1
            elif phase == "Failed":
                member.failed += 1
            elif phase not in TERMINAL_PHASES:
                member.running += 1

            hours = job_hours(job, now)

            # ★ A ROW PER SELLER, AND NO ROW AT ALL FOR A JOB NOBODY SOLD.
            # Two independent facts decide this, and conflating them is how the
            # screen went wrong twice on 2026-09-11:
            #
            #   does status.currentOffering.vendor name somebody?
            #   does status.startedAt exist, i.e. did a machine ever run?
            #
            # A NAMED VENDOR ALWAYS GETS ITS ROW, clock or no clock. On this
            # cluster one gcp job and eleven runpod jobs finished before
            # PACSRUN-JOB-CLOCK existed (2026-09-01), so they carry a vendor and
            # no duration. Dropping them for want of a clock deleted `gcp` from
            # the Per vendor table entirely, which is a worse answer than a row
            # reading 0.0 hours: we know perfectly well who sold those.
            #
            # NO VENDOR AND NO CLOCK GETS NO ROW. Nothing was bought and nobody
            # is named, so there is nothing to file. That is the 18 jobs -- 17
            # compare-mode and one Pending seed marker -- that used to appear as
            # a seller called `unknown` with $0.00 beside `runpod`, reading as a
            # vendor we had failed to identify.
            #
            # NO VENDOR BUT A CLOCK IS STILL `unknown`, and that bucket keeps
            # its original meaning: a machine was rented for real hours and the
            # record does not say by whom. There is no such job on this cluster
            # today, and the day one appears its hours must not vanish.
            sold_by = (((job.get("status") or {}).get("currentOffering")) or {}).get(
                "vendor"
            ) or ""
            vendor = None
            if sold_by or hours is not None:
                name = sold_by or "unknown"
                vendor = vendors.setdefault(name, VendorTotals(vendor=name))
                vendor.jobs += 1

            if hours is None:
                continue
            member.gpu_hours += hours
            vendor.gpu_hours += hours
            cost = job_cost(job, hours)
            if cost is None:
                member.unpriced_jobs += 1
                vendor.unpriced_jobs += 1
            else:
                member.cost_usd += cost
                vendor.cost_usd += cost

    for user in sorted(members):
        member = members[user]
        member.gpu_hours = round(member.gpu_hours, 2)
        member.cost_usd = round(member.cost_usd, 2)

        # The team's figures count every job, owned or not. Only the ROW is at
        # stake below.
        totals.jobs += member.jobs
        totals.gpu_hours += member.gpu_hours
        totals.cost_usd += member.cost_usd
        totals.unpriced_jobs += member.unpriced_jobs

        # ★ THE OWNERLESS BUCKET IS NOT A MEMBER ROW. Every column in that
        # table is a fact about a PERSON, and `kubectl` is not one -- it is how
        # the job was applied. The column has now carried three non-people in a
        # row and a reader took each of them for a colleague: `default` (a
        # namespace) until 2026-09-07, `admin` (a role) until 2026-09-10, and
        # `kubectl` until today. Renaming it a fourth time does not fix the
        # kind of thing it is, so the row is gone and the count is stated as a
        # sentence instead.
        if user == NO_OWNER:
            totals.unowned_jobs = member.jobs
            continue
        totals.members.append(member)

    # Spend first, then hours, then name. Seeding the catalogue means most rows
    # are zeroes on a young team, and plain alphabetical order buried the one
    # vendor that had actually been used between five that had not.
    for sold_by in sorted(
        vendors, key=lambda name: (-vendors[name].cost_usd,
                                   -vendors[name].gpu_hours, name)
    ):
        vendor = vendors[sold_by]
        vendor.gpu_hours = round(vendor.gpu_hours, 2)
        vendor.cost_usd = round(vendor.cost_usd, 2)
        totals.vendors.append(vendor)

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
