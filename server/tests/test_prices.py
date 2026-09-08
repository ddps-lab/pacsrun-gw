"""The AWS price table, and the two wrong numbers it replaced.

WHAT THESE TESTS ARE FOR. `measurements.AWS_MACHINES` is generated from a CSV, so
nothing here re-checks arithmetic the generator did. What they check is the three
claims the table is USED for, each of which was false before it existed:

  1. every card a user may choose has a price. Twelve of fourteen had none, and
     `/v1/estimate` answered `unknown` for cost as well as hours.
  2. the price belongs to the vendor that was asked for. Every job used to be
     priced at RunPod's rate, so an AWS L40S job was quoted 47% low.
  3. an ask AWS cannot fill is named before submitting. The old check tested
     `count == 1` only, and 76 of the 82 unfillable asks passed it.

Grep anchor: DDPSRUN-AWS-PRICES
"""
from __future__ import annotations

from ddpsrun_server import catalogue, estimate as e, measurements as m


def test_every_choosable_card_has_a_price():
    """CLAIM 1. The complaint that started this: fourteen cards in the dropdown,
    two with measurements, twelve answering `unknown` about money."""
    unpriced = [c.name for c in catalogue.CHOOSABLE if not m.aws_machines_for(c.name)]
    assert unpriced == []


def test_the_two_rented_cards_still_carry_what_we_actually_paid():
    """The new table must not have replaced the measured prices. `GPUS` is what we
    were CHARGED on RunPod; `AWS_MACHINES` is AWS's published list. Both are real
    and they are not interchangeable -- which is the point of test 2 below."""
    assert m.gpu_by_name("L40S").usd_per_hour == 0.99
    assert m.gpu_by_name("A100-80GB").usd_per_hour == 1.59
    assert m.gpu_by_name("T4") is None


def test_an_aws_job_is_no_longer_priced_at_runpods_rate():
    """CLAIM 2, with the number that made it a defect rather than a gap. The same
    L40S job, same hours, differs by 88% between the two vendors."""
    on_aws = e.hourly_rate("L40S", 1, 1, ["aws"], "on-demand")
    on_runpod = e.hourly_rate("L40S", 1, 1, ["runpod"], "on-demand")
    assert on_aws.usd_per_hour_low == 1.861
    assert on_runpod.usd_per_hour_low == 0.99
    assert on_aws.vendor == "aws" and on_runpod.vendor == "runpod"
    # 1.861 / 0.99 = 1.88. Quoting the RunPod number for an AWS job understated
    # it by 47% (0.99 is 53% of 1.861).
    assert round(on_runpod.usd_per_hour_low / on_aws.usd_per_hour_low, 2) == 0.53


def test_an_unrestricted_ask_takes_the_cheaper_and_names_the_other():
    """`vendors` empty means no restriction, and we cannot know which vendor the
    solve lands on. Quoting one of two candidates silently is how the 47%
    happened, so the other one is in the sentence."""
    rate = e.hourly_rate("L40S", 1, 1, None, "on-demand")
    assert rate.usd_per_hour_low == 0.99
    assert "g6e.xlarge" in rate.basis
    assert "1.8610" in rate.basis


def test_spot_is_a_range_and_on_demand_is_one_number():
    """On-demand is published per region. Spot is per availability zone and moves,
    and both live spot measurements landed inside this range."""
    od = e.hourly_rate("L40S", 1, 1, ["aws"], "on-demand")
    spot = e.hourly_rate("L40S", 1, 1, ["aws"], "spot")
    assert od.usd_per_hour_low == od.usd_per_hour_high
    assert spot.usd_per_hour_low < spot.usd_per_hour_high
    assert spot.usd_per_hour_high < od.usd_per_hour_low


def test_the_rate_covers_every_machine_the_job_rents():
    """A rate for one card is the wrong number for four pods. Four pods of one
    L40S rent four g6e.xlarge, so the rate is 4 x $1.8610."""
    rate = e.hourly_rate("L40S", 1, 4, ["aws"], "on-demand")
    assert rate.machines == 4
    assert rate.usd_per_hour_low == round(1.861 * 4, 4)


def test_eight_pods_sharing_one_eight_gpu_machine_pay_for_one_machine():
    """The packing case. Eight one-card pods fill a p4de.24xlarge exactly, so the
    job rents ONE machine -- not eight -- and PACSrun's seat arithmetic
    (pkg/decider/decider.go:607) is what makes that true."""
    rate = e.hourly_rate("A100-80GB", 1, 8, ["aws"], "on-demand")
    assert rate.machines == 1
    assert rate.usd_per_hour_low == 27.4471


def test_a_partly_filled_machine_carries_its_own_waste():
    """Six pods at four seats a machine needs two machines, and the second runs
    half empty. Whichever machine is cheapest per pod, the number returned is
    what gets billed, not the tidy division."""
    rate = e.hourly_rate("L40S", 1, 6, ["aws"], "on-demand")
    # Six g6e.xlarge at $1.8610 beats two g6e.12xlarge at $10.4926 ($11.166 vs
    # $20.9852), so the cheapest answer happens to have no waste at all.
    assert rate.machines == 6
    assert rate.usd_per_hour_low == round(1.861 * 6, 4)


def test_runpod_refuses_to_multiply_a_price_it_has_only_measured_once():
    """Both RunPod prices we hold were paid for a pod holding ONE card. Charging
    count x that assumes linear per-card billing, which is plausible and
    unmeasured -- and an unmeasured multiplication is the market-exp2 mistake."""
    rate = e.hourly_rate("L40S", 4, 1, ["runpod"], "on-demand")
    assert rate.usd_per_hour_low is None
    assert "one-card pod" in rate.basis


def test_a_card_with_no_measurement_still_answers_a_rate():
    """★ THE WHOLE POINT. An H100 has never been rented, so its hours are
    `unknown` and must stay that way -- but $6.88/hour is a published price and
    saying nothing about it was the defect."""
    result = e.estimate(gpu_name="H100", cap=12288, pairs=5000, epochs=1,
                        row_tokens=4100, mitigations_on=True, vendors=["aws"])
    assert result.duration.confidence == "unknown"
    assert result.cost_low_usd is None
    assert result.rate.usd_per_hour_low == 6.88
    assert "p5.4xlarge" in result.rate.basis


def test_hours_are_never_extrapolated_to_an_unrented_card():
    """WHY 3 IS NOT A SPEC RATIO. We hold exactly one controlled comparison --
    aiops-exp1 and aiops-exp2, same cap and same response length on two
    different cards -- so a per-card model has two points and no third card to
    test it on. Every unrented card keeps answering `unknown` for time."""
    pair = [r for r in m.THROUGHPUT if r.cap == 12288 and r.row_tokens == 5600]
    assert {r.gpu for r in pair} == {"A100-80GB", "L40S"}
    assert len(pair) == 2
    for choice in catalogue.CHOOSABLE:
        if m.gpu_by_name(choice.name) is not None:
            continue
        got = e.seconds_per_step(choice.name, 4100, 1, 8)
        assert got.confidence == "unknown", choice.name
        assert got.seconds_per_step is None


def test_the_machine_sizes_reproduce_the_boolean_they_replaced():
    """`catalogue.Choice` carried `sold_singly`, hand-read from the CSV on
    2026-09-02. The derived form has to agree with that read, card for card, or
    one of the two was wrong."""
    hand_read_2026_09_02 = {
        "T4": True, "T4g": True, "L4": True, "A10G": True, "RTX PRO 4500": True,
        "V100-32GB": False, "L40S": True, "RTXPRO6000": True, "A100": False,
        "A100-80GB": False, "H100": True, "H200": False, "B200": False,
        "B300": False,
    }
    assert {c.name: c.sold_singly for c in catalogue.CHOOSABLE} == hand_read_2026_09_02


def test_no_fractional_gpu_machine_is_in_the_table():
    """AWS is the only vendor publishing a fractional AcceleratorCount, and
    PACSrun rounds 0.5 up to 1 (catalog.go:235), so g6f.4xlarge would enter the
    pool as a one-GPU machine 30% under a whole L4. It is excluded here because
    its 11.18 GiB fails the shipped 16 GiB floor -- an accident PACSrun's own
    aws_test.go:356 refuses to rely on, so this test names the instances."""
    instances = {row.instance for row in m.AWS_MACHINES}
    assert not any(i.startswith(("g6f.", "gr6f.")) for i in instances)
    assert "g6.xlarge" in instances


# --------------------------------------------------------------------------
# The three defects the first end-to-end run through the real routes exposed.
# All three were in code written the same hour, and none of them showed up in a
# unit test -- which is why the routes get exercised, not just the functions.
# --------------------------------------------------------------------------


def test_the_rate_prices_the_capacity_that_was_asked_for():
    """DEFECT A. The rate was built from `capacity_type()`'s RECOMMENDATION, so a
    caller who typed `spot` on a 7.3-hour unresumable run -- which is recommended
    on-demand -- was quoted $1.8610/hour for a machine they were not buying. The
    spot answer is a range because spot is per availability zone."""
    asked_spot = e.estimate(gpu_name="L40S", cap=12288, pairs=5000, epochs=1,
                            row_tokens=4100, mitigations_on=True, vendors=["aws"],
                            asked_capacity="spot")
    assert asked_spot.capacity_type == "on-demand"        # the recommendation
    assert asked_spot.rate.usd_per_hour_low == 1.0555     # the price of what was asked
    assert asked_spot.rate.usd_per_hour_high == 1.2863
    assert any("asked" in w and "recommend" in w for w in asked_spot.warnings)

    undecided = e.estimate(gpu_name="L40S", cap=12288, pairs=5000, epochs=1,
                           row_tokens=4100, mitigations_on=True, vendors=["aws"])
    assert undecided.rate.usd_per_hour_low == 1.861       # falls back to the advice


def test_the_unfillable_finding_counts_the_pods_it_was_given():
    """DEFECT B. `validate()` was never handed `parallelism`, so its message read
    "1 x 1 pods" for an eight-pod job and the ceiling it printed was wrong. The
    check and the estimate have to agree about the same ask."""
    from ddpsrun_server import validate as v
    one_pod = v.check_gpu_is_buyable("A100-80GB", 1, None, 1)
    eight_pods = v.check_gpu_is_buyable("A100-80GB", 1, None, 8)
    assert any(f.code == "gpu-count-unfillable" for f in one_pod)
    assert not any(f.code == "gpu-count-unfillable" for f in eight_pods)
    assert e.hourly_rate("A100-80GB", 1, 8, ["aws"], "on-demand").machines == 1


def test_the_never_rented_warning_no_longer_calls_the_price_a_guess():
    """DEFECT C. It said "any time or cost figure for it is a guess rather than a
    measurement", which was true when there was no price and became false the
    moment there was one. The rate is published; the runtime is what is missing."""
    from ddpsrun_server import validate as v
    finding = [f for f in v.check_gpu_is_buyable("H100", 1, None, 1)
               if f.code == "gpu-never-rented"][0]
    assert "published price" in finding.message
    assert "cost figure for it is a guess" not in finding.message
