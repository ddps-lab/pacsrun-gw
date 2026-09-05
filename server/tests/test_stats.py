"""Team figures, and what they refuse to claim.

The arithmetic is simple; what these tests defend is the honesty of the totals.
A number that silently absorbed the jobs it could not price would read as the
bill and be low, and a team's total that included another team's namespaces
would be worse than useless.
"""

from datetime import datetime, timedelta, timezone

from ddpsrun_server import auth
from ddpsrun_server import stats

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def job(*, phase="Succeeded", started=None, hours=None, instance="L40S", parallelism=1):
    """Build a PacsJob the way the cluster would return it."""
    status = {"phase": phase}
    if started is not None:
        status["startedAt"] = started.isoformat().replace("+00:00", "Z")
        if hours is not None:
            finished = started + timedelta(hours=hours)
            status["finishedAt"] = finished.isoformat().replace("+00:00", "Z")
    if instance:
        status["currentOffering"] = {"vendor": "runpod", "instanceType": instance}
    return {"spec": {"parallelism": parallelism}, "status": status}


# ------------------------------------------------------------------ duration


def test_a_job_that_never_ran_contributes_no_hours():
    # Failing during placement is a real outcome with a real message and zero
    # cost: no machine was ever bought.
    assert stats.job_hours(job(phase="Failed", started=None), NOW) is None


def test_a_finished_job_is_measured_between_its_two_stamps():
    started = NOW - timedelta(hours=6, minutes=32)
    assert abs(stats.job_hours(job(started=started, hours=6.54), NOW) - 6.54) < 0.01


def test_a_still_running_job_is_measured_up_to_now():
    started = NOW - timedelta(hours=3)
    assert abs(stats.job_hours(job(phase="Running", started=started), NOW) - 3.0) < 0.01


# ---------------------------------------------------------------------- cost


def test_the_cost_of_a_job_we_have_priced_matches_what_it_actually_cost():
    # bank-exp2v2: 6.54 hours on an L40S at $0.99/hour, $6.47.
    assert abs(stats.job_cost(job(instance="L40S"), 6.54) - 6.47) < 0.02


def test_parallel_pods_each_hold_their_own_machine():
    one = stats.job_cost(job(instance="L40S", parallelism=1), 1.0)
    eight = stats.job_cost(job(instance="L40S", parallelism=8), 1.0)
    assert abs(eight - one * 8) < 0.001


def test_a_machine_we_have_never_rented_has_no_price_rather_than_zero():
    # Zero would be absorbed silently and the total would read as complete.
    assert stats.job_cost(job(instance="H200"), 5.0) is None
    assert stats.job_cost(job(instance=None), 5.0) is None


# -------------------------------------------------------------------- totals


def test_a_team_is_the_sum_of_its_members():
    started = NOW - timedelta(hours=10)
    totals = stats.summarise(
        "ddps", ["ddps-alice", "ddps-bob"],
        {
            "ddps-alice": [job(started=started, hours=6.54), job(started=started, hours=1.0)],
            "ddps-bob": [job(started=started, hours=2.0)],
        },
        now=NOW,
    )
    assert totals.jobs == 3
    assert [m.user for m in totals.members] == ["alice", "bob"]
    assert abs(totals.gpu_hours - 9.54) < 0.02
    assert abs(totals.cost_usd - 9.54 * 0.99) < 0.05


def test_outcomes_are_counted_separately():
    started = NOW - timedelta(hours=2)
    totals = stats.summarise(
        "ddps", ["ddps-alice"],
        {"ddps-alice": [
            job(phase="Succeeded", started=started, hours=1),
            job(phase="Failed", started=started, hours=1),
            job(phase="Failed", started=None),
            job(phase="Running", started=started),
        ]},
        now=NOW,
    )
    member = totals.members[0]
    assert (member.jobs, member.succeeded, member.failed, member.running) == (4, 1, 2, 1)


def test_an_unpriced_job_is_counted_and_said_out_loud():
    started = NOW - timedelta(hours=5)
    totals = stats.summarise(
        "ddps", ["ddps-alice"],
        {"ddps-alice": [job(started=started, hours=5, instance="H200")]},
        now=NOW,
    )
    assert totals.gpu_hours == 5.0
    assert totals.cost_usd == 0.0
    assert totals.unpriced_jobs == 1
    assert "a floor, not the bill" in totals.note


def test_a_token_with_no_team_gets_nothing_rather_than_everything():
    # Falling back to "all namespaces" would hand one person the whole lab's
    # figures because an operator forgot a field.
    totals = stats.summarise("", [], {}, now=NOW)
    assert totals.jobs == 0
    assert "names no team" in totals.note


def test_a_namespace_that_does_not_follow_the_convention_still_reports():
    # The "<team>-<user>" split is presentation only, so an odd namespace name
    # must not lose its jobs.
    totals = stats.summarise(
        "ddps", ["legacy-shared"],
        {"legacy-shared": [job(started=NOW - timedelta(hours=1), hours=1)]},
        now=NOW,
    )
    assert totals.members[0].user == "legacy-shared"
    assert totals.jobs == 1


def test_a_clean_team_has_nothing_to_note():
    totals = stats.summarise(
        "ddps", ["ddps-alice"],
        {"ddps-alice": [job(started=NOW - timedelta(hours=1), hours=1)]},
        now=NOW,
    )
    assert totals.note == ""


# ------------------------------------------------------- who is in the team


def test_team_membership_comes_from_the_token_file_not_from_the_cluster():
    store = auth.TokenStore.from_document(({"tokens": [
        {"sha256": auth.hash_token("a"), "user": "alice", "namespace": "ddps-alice", "team": "ddps"},
        {"sha256": auth.hash_token("b"), "user": "bob", "namespace": "ddps-bob", "team": "ddps"},
        {"sha256": auth.hash_token("c"), "user": "carol", "namespace": "other-carol", "team": "other"},
    ]}))
    assert store.namespaces_in_team("ddps") == ["ddps-alice", "ddps-bob"]
    assert store.namespaces_in_team("other") == ["other-carol"]


def test_an_empty_team_name_matches_nobody():
    store = auth.TokenStore.from_document(({"tokens": [
        {"sha256": auth.hash_token("a"), "user": "alice", "namespace": "ddps-alice"},
    ]}))
    assert store.namespaces_in_team("") == []


def test_a_token_without_a_team_still_works_for_everything_else():
    store = auth.TokenStore.from_document(({"tokens": [
        {"sha256": auth.hash_token("a"), "user": "alice", "namespace": "ddps-alice"},
    ]}))
    principal = store.principal_for("a")
    assert principal.namespace == "ddps-alice"
    assert principal.team == ""
