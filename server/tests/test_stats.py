"""Team figures, and what they refuse to claim.

The arithmetic is simple; what these tests defend is the honesty of the totals.
A number that silently absorbed the jobs it could not price would read as the
bill and be low, and a team's total that included another team's namespaces
would be worse than useless.
"""

from datetime import datetime, timedelta, timezone

from ddpsrun_server import auth
from ddpsrun_server import naming
from ddpsrun_server import stats

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)


def job(*, phase="Succeeded", started=None, hours=None, instance="L40S",
        parallelism=1, owner=None):
    """Build a PacsJob the way the cluster would return it.

    `owner` fills the ddpsrun.io/owner label the server writes at submit time;
    None builds a kubectl-shaped job, which carries no such label.
    """
    status = {"phase": phase}
    if started is not None:
        status["startedAt"] = started.isoformat().replace("+00:00", "Z")
        if hours is not None:
            finished = started + timedelta(hours=hours)
            status["finishedAt"] = finished.isoformat().replace("+00:00", "Z")
    if instance:
        status["currentOffering"] = {"vendor": "runpod", "instanceType": instance}
    body = {"spec": {"parallelism": parallelism}, "status": status}
    if owner:
        body["metadata"] = {"labels": {naming.OWNER_LABEL: owner}}
    return body


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


def test_every_card_on_the_machine_is_billed():
    # baseline-c, the run that exposed this: 4 x A100-SXM4-80GB for 6.97 hours.
    # RunPod's ledger says $44.28 for it (facts/cost-ledger.md, 2026-09-07);
    # at the measured $1.59/card/hour this arithmetic gives $44.32. Before the
    # card count existed the screen said $11.07 to the person holding that bill.
    # The shape is the CRD's own: resources.gpus.count, read off the live
    # baseline-c object — not a flat spec.gpus, which a first version of the
    # code reached for and silently priced every job as one card.
    four_cards = job(instance="NVIDIA A100-SXM4-80GB")
    four_cards["spec"]["resources"] = {"gpus": {"name": "A100", "count": 4}}
    assert abs(stats.job_cost(four_cards, 6.97) - 44.32) < 0.05


def test_a_machine_we_have_never_rented_has_no_price_rather_than_zero():
    # Zero would be absorbed silently and the total would read as complete.
    assert stats.job_cost(job(instance="H200"), 5.0) is None
    assert stats.job_cost(job(instance=None), 5.0) is None


def test_a_group_priced_job_needs_no_price_table():
    # PACSRUN-GROUP-PRICE: PACSrun stamps each offering group with the price
    # its solve stated, machine count already inside. Any vendor works — this
    # instance type appears in no local table, and the sum is still exact.
    j = job(instance="g6.2xlarge")
    j["status"]["currentOfferingGroups"] = [
        {"instanceType": "g6.2xlarge", "nodes": 2, "usdPerHour": "1.9758"},
        {"instanceType": "g6.xlarge", "nodes": 1, "usdPerHour": "0.8048"},
    ]
    assert abs(stats.job_cost(j, 2.0) - 2 * (1.9758 + 0.8048)) < 0.001


def test_a_partly_priced_job_falls_back_to_the_estimate():
    # One unpriced group (an alternates-path buy) would make the group sum
    # understate, so the whole group path is refused and the measured-card
    # estimate answers instead — same all-or-nothing rule as answerPrice.
    j = job(instance="NVIDIA L40S")
    j["status"]["currentOfferingGroups"] = [
        {"usdPerHour": "0.9879"},
        {"usdPerHour": ""},
    ]
    assert abs(stats.job_cost(j, 1.0) - 0.99) < 0.001


def test_nvidia_smi_spellings_price_the_same_card():
    # status.currentOffering carries nvidia-smi's names, not the catalogue's.
    # Until 2026-09-07 these two returned None and every real job was unpriced:
    # Cost "-" on each row, Team spend $0.00.
    assert abs(stats.job_cost(job(instance="NVIDIA L40S"), 1.0) - 0.99) < 0.001
    assert abs(
        stats.job_cost(job(instance="NVIDIA A100-SXM4-80GB"), 1.0) - 1.59
    ) < 0.001


# -------------------------------------------------------------------- totals


def test_a_team_is_the_sum_of_its_members():
    started = NOW - timedelta(hours=10)
    totals = stats.summarise(
        "ddps", ["ddps-alice", "ddps-bob"],
        {
            "ddps-alice": [job(started=started, hours=6.54, owner="alice"),
                           job(started=started, hours=1.0, owner="alice")],
            "ddps-bob": [job(started=started, hours=2.0, owner="bob")],
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
            job(phase="Succeeded", started=started, hours=1, owner="alice"),
            job(phase="Failed", started=started, hours=1, owner="alice"),
            job(phase="Failed", started=None, owner="alice"),
            job(phase="Running", started=started, owner="alice"),
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


def test_the_same_jobs_add_up_by_vendor_too():
    started = NOW - timedelta(hours=2)
    aws = job(started=started, hours=1, instance="g6.2xlarge")
    aws["status"]["currentOffering"]["vendor"] = "aws"
    runpod = job(started=started, hours=2, instance="L40S")
    runpod["status"]["currentOffering"]["vendor"] = "runpod"
    # Ran for an hour on a machine whose record does not name the seller. This
    # is what `unknown` is for, and its hours are real.
    ancient = job(started=started, hours=1)
    ancient["status"]["currentOffering"].pop("vendor", None)

    totals = stats.summarise(
        "ddps", ["default"], {"default": [aws, runpod, ancient]}, now=NOW,
    )
    by = {v.vendor: v for v in totals.vendors}
    assert set(by) == {"aws", "runpod", "unknown"}
    # The AWS machine is in no price table: hours counted, dollars refused.
    assert by["aws"].unpriced_jobs == 1 and by["aws"].gpu_hours == 1.0
    assert abs(by["runpod"].cost_usd - 2 * 0.99) < 0.01
    assert by["unknown"].jobs == 1 and by["unknown"].gpu_hours == 1.0


def test_every_vendor_in_the_catalogue_gets_a_row_even_with_no_jobs():
    # ★ THE ROWS ARE THE CATALOGUE, NOT WHOEVER HAPPENS TO HAVE SOLD TO THIS
    # TEAM. Derived from the jobs, the Per vendor table showed exactly one row
    # on 2026-09-11 -- `runpod` -- because aws, gcp and shadeform had only sold
    # into the `default` namespace, which is not in this team. A brand new
    # account with no jobs would have seen an empty table and no way to tell
    # "never used" from "the screen forgot".
    totals = stats.summarise(
        "ddps", ["ddps-alice"],
        {"ddps-alice": [job(started=NOW - timedelta(hours=1), hours=1)]},
        now=NOW, known_vendors=("aws", "runpod", "gcp", "azure"),
    )
    by = {v.vendor: v for v in totals.vendors}
    assert set(by) == {"aws", "runpod", "gcp", "azure"}
    assert by["gcp"].jobs == 0 and by["gcp"].gpu_hours == 0.0 and by["gcp"].cost_usd == 0.0
    assert by["runpod"].jobs == 1
    # Spend first, so the vendor actually used is not buried under the zeroes.
    assert totals.vendors[0].vendor == "runpod"
    assert [v.vendor for v in totals.vendors[1:]] == ["aws", "azure", "gcp"]


def test_a_vendor_the_catalogue_does_not_list_still_gets_a_row():
    # A name the gateway has not heard of must be visible, not swallowed. This
    # is how `shadeform` behaved before 2026-09-10, when the CRD accepted the
    # word and the gateway did not know it.
    surprise = job(started=NOW - timedelta(hours=1), hours=1)
    surprise["status"]["currentOffering"]["vendor"] = "brandnew"

    totals = stats.summarise(
        "ddps", ["ddps-alice"], {"ddps-alice": [surprise]},
        now=NOW, known_vendors=("aws", "runpod"),
    )
    assert {v.vendor for v in totals.vendors} == {"aws", "runpod", "brandnew"}


def test_a_job_with_no_vendor_that_never_ran_is_in_no_row_at_all():
    # ★ NOTHING WAS BOUGHT AND NOBODY IS NAMED. A Pending job and a
    # compare-mode job both reach here with no currentOffering and no
    # startedAt, and until 2026-09-11 the bucket was made before either was
    # checked, so they showed as a seller called `unknown` with $0.00 beside
    # `runpod`. On the live cluster that was 17 compare-mode jobs and one
    # Pending seed marker. They stay in the team's job count.
    pending = job(phase="Pending", started=None, instance=None)
    compared = job(phase="Compared", started=None, instance=None)
    ran = job(started=NOW - timedelta(hours=1), hours=1)

    totals = stats.summarise(
        "ddps", ["default"], {"default": [pending, compared, ran]}, now=NOW,
    )
    assert [v.vendor for v in totals.vendors] == ["runpod"]
    assert totals.vendors[0].jobs == 1
    # The skipped jobs are not lost -- Team still counts them, and the Vendors
    # screen subtracts these two numbers to say how many it left out.
    assert totals.jobs == 3


def test_a_named_vendor_keeps_its_row_even_with_no_clock():
    # ★ THE FIRST ATTEMPT AT THE RULE ABOVE DROPPED THESE TOO, and that deleted
    # `gcp` from the Per vendor table outright: its one job finished before
    # PACSRUN-JOB-CLOCK (2026-09-01) and carries a vendor with no startedAt,
    # as do eleven runpod jobs. We know exactly who sold those machines, so a
    # row reading 0.0 hours is the true answer and no row is not.
    old_gcp = job(phase="Succeeded", started=None)
    old_gcp["status"]["currentOffering"]["vendor"] = "gcp"
    ran = job(started=NOW - timedelta(hours=1), hours=1)

    totals = stats.summarise(
        "ddps", ["default"], {"default": [old_gcp, ran]}, now=NOW,
    )
    by = {v.vendor: v for v in totals.vendors}
    assert set(by) == {"gcp", "runpod"}
    assert by["gcp"].jobs == 1 and by["gcp"].gpu_hours == 0.0


def test_a_job_that_ran_but_names_no_vendor_is_still_unknown():
    # The third case: `unknown` is not gone, it just means what it always said
    # -- a machine WAS rented for real hours and the record does not say by
    # whom. No such job exists on this cluster today, and the day one appears
    # its hours must not vanish.
    ran = job(started=NOW - timedelta(hours=2), hours=2)
    ran["status"]["currentOffering"].pop("vendor", None)

    totals = stats.summarise("ddps", ["default"], {"default": [ran]}, now=NOW)
    assert [v.vendor for v in totals.vendors] == ["unknown"]
    assert totals.vendors[0].gpu_hours == 2.0


def test_a_job_with_no_owner_label_is_in_no_member_row():
    # ★ THE COLUMN HAS CARRIED THREE NON-PEOPLE AND A READER TOOK EACH FOR A
    # COLLEAGUE: `default` (a namespace) until 2026-09-07, `admin` (a role)
    # until 2026-09-10, `kubectl` (a tool) until 2026-09-11. Every column in
    # that table is a fact about a person, so the fix is not a fourth name --
    # the row is gone and the count is a sentence. The jobs stay in every
    # total, because the money was spent either way.
    totals = stats.summarise(
        "ddps", ["default"],
        {"default": [
            job(started=NOW - timedelta(hours=1), hours=1),
            job(started=NOW - timedelta(hours=1), hours=1, owner="alice"),
        ]},
        now=NOW,
    )
    assert [m.user for m in totals.members] == ["alice"]
    assert totals.unowned_jobs == 1
    assert totals.jobs == 2
    assert abs(totals.cost_usd - 2 * 0.99) < 0.01


def test_a_team_whose_every_job_was_applied_by_hand_has_no_member_rows():
    # The degenerate case the screen has to word carefully: real spend, and
    # nobody to attribute it to.
    totals = stats.summarise(
        "ddps", ["default"],
        {"default": [job(started=NOW - timedelta(hours=1), hours=1)]},
        now=NOW,
    )
    assert totals.members == []
    assert totals.jobs == 1 and totals.unowned_jobs == 1
    assert abs(totals.cost_usd - 0.99) < 0.01


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


def test_a_member_who_only_signs_in_through_the_screen_is_still_in_the_team():
    # THE BUG THIS PINS. A person with no static token has an `email` and no
    # `sha256`, so they exist only in `_by_email`. `namespaces_in_team` scanned
    # `_by_hash` alone and skipped them -- and on the live deployment
    # (2026-09-11) that was every member of team `ddps`, so Team and Vendors
    # both drew 0 jobs, 0.0 GPU hours and $0.00. Every test above this one uses
    # `sha256` entries, which is why nothing caught it.
    store = auth.TokenStore.from_document(({"tokens": [
        {"email": "alice@example.com", "user": "alice", "namespace": "ddps-alice", "team": "ddps"},
        {"sha256": auth.hash_token("b"), "user": "bob", "namespace": "ddps-bob", "team": "ddps"},
        {"sha256": auth.hash_token("p"), "user": "probe", "namespace": "default", "team": "probe"},
    ]}))
    assert store.namespaces_in_team("ddps") == ["ddps-alice", "ddps-bob"]
    assert store.namespaces_in_team("probe") == ["default"]


def test_one_person_with_both_credentials_contributes_one_namespace():
    # The same Principal is in both maps, and the union must not report its
    # namespace twice -- `sorted({...})` already deduplicates, and this says so.
    store = auth.TokenStore.from_document(({"tokens": [
        {"sha256": auth.hash_token("a"), "email": "alice@example.com",
         "user": "alice", "namespace": "ddps-alice", "team": "ddps"},
    ]}))
    assert store.namespaces_in_team("ddps") == ["ddps-alice"]


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
