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


def test_runpod_multiplies_by_the_card_count_because_that_is_measured():
    """★ 2026-09-09 에 답이 $6.36 에서 $6.388 로 바뀌었다. 곱셈이 틀린 것이 아니다.

    이 테스트가 지키는 사실은 "카드 수를 곱한다" 이고 그것은 그대로다. 다만 4장
    A100 은 실제 청구서를 들고 있는 유일한 모양이라(DDPSRUN-BILLED-RATE) 그 값이
    답이 된다. 곱셈이 여전히 옳다는 것은 두 값이 0.4% 안에서 같다는 것으로 확인하고,
    곱셈 자체는 청구서가 없는 모양(8장)에서 시험한다.
    """
    """이 테스트는 2026-09-08 에 반대로 뒤집혔다. 근거가 생겼기 때문이다.

    옛 판은 "one-card pod 로 낸 값을 곱하는 것은 미실측" 이라며 4장 구성을
    None 으로 답했다. 그런데 baseline-c 가 4 x A100-SXM4-80GB 를
    `4 x $1.59 = $6.36/hr` 로 청구받았고(2026-09-04, `facts/cost-ledger.md` 의
    $44.28 귀속과 일치) RunPod 의 과금 단위가 카드라는 것이 실측됐다.

    거부를 유지하는 비용이 그 사이 드러났다: 21시간 4장 job 을 비용 `unknown`
    으로 결정하게 만든다. **시간은 계속 unknown 이 정답이고**, 시간당 단가는
    published price 라 답할 수 있다 -- 그 둘은 다른 질문이다.
    """
    rate = e.hourly_rate("A100-80GB", 4, 1, ["runpod"], "on-demand")
    assert rate.usd_per_hour_low == 6.388, "청구서를 들고 있으면 그것이 답이다"
    assert abs(rate.usd_per_hour_low - 4 * 1.59) / rate.usd_per_hour_low < 0.005, (
        "그리고 곱셈은 0.4% 안에서 같은 답을 낸다 -- 카드당 과금이라는 사실은 유효하다")
    eight = e.hourly_rate("A100-80GB", 8, 1, ["runpod"], "on-demand")
    assert eight.usd_per_hour_low == round(1.59 * 8, 4), "청구서가 없는 모양은 곱셈"
    assert "per card-hour" in eight.basis


def test_runpod_one_card_is_unchanged():
    rate = e.hourly_rate("A100-80GB", 1, 1, ["runpod"], "on-demand")
    assert rate.usd_per_hour_low == 1.59


def test_runpod_multiplies_pods_and_cards_together():
    """2 pods x 4 cards = 8 카드분. pod 마다 자기 machine 을 빌린다."""
    rate = e.hourly_rate("A100-80GB", 4, 2, ["runpod"], "on-demand")
    # 청구서는 pod 하나에 대한 것이므로 pod 수만 곱한다.
    assert rate.usd_per_hour_low == round(6.388 * 2, 4)


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


# --------------------------------------------------------------------------
# DDPSRUN-PRICES / DDPSRUN-REGIONS. Every region, not one -- and being able to
# ASK for the others, which is what makes looking at them worth anything.
# --------------------------------------------------------------------------


def test_the_table_covers_every_region_the_catalogue_prices():
    """The first version was us-west-2 only: 30 rows, so "what does an H100 cost
    in Seoul" had no answer anywhere in this service."""
    aws = [r for r in m.PRICE_ROWS if r.vendor == "aws"]
    gcp = [r for r in m.PRICE_ROWS if r.vendor == "gcp"]
    assert len({r.region for r in aws}) == 22
    assert len(aws) == 304
    assert len(gcp) == 306
    # Every choosable card is priced somewhere, which was already true for
    # us-west-2 and must not regress as regions are added.
    assert {c.name for c in catalogue.CHOOSABLE} <= {r.card for r in aws}


def test_an_ask_that_names_no_region_gets_the_operators_one_default():
    """★ EMPTY IS NOT "ANYWHERE". PACSrun gives an unqualified AWS ask exactly one
    region -- the operator's own default (placement.go:376, PACSRUN-AWS-ONE-REGION)
    -- so pricing it at the globally cheapest region would be a wrong number
    dressed as a helpful one."""
    default_only = m.aws_machines_for("L40S")
    assert {r.region for r in default_only} == {m.DEFAULT_AWS_REGION}

    everywhere = m.aws_machines_for("L40S", list(m.AWS_REGIONS))
    assert len({r.region for r in everywhere}) > 1


def test_naming_a_region_prices_that_region_and_says_which():
    """A price without its region is not checkable, so the sentence carries it.
    The H100 is 25% dearer in ap-northeast-1 than in us-west-2, which is the kind
    of difference that was invisible while the table held one region."""
    home = e.hourly_rate("H100", 1, 1, ["aws"], "on-demand")
    seoul = e.hourly_rate("H100", 1, 1, ["aws"], "on-demand", ["aws/ap-northeast-1"])
    assert home.usd_per_hour_low == 6.88
    assert seoul.usd_per_hour_low == 8.60
    assert m.DEFAULT_AWS_REGION in home.basis
    assert "ap-northeast-1" in seoul.basis


def test_several_regions_take_the_cheapest_and_name_it():
    """With more than one region allowed the cheapest wins, and the answer says
    where -- otherwise the number cannot be checked against the catalogue."""
    rate = e.hourly_rate("H100", 1, 1, ["aws"], "on-demand",
                         ["aws/ap-northeast-1", "aws/us-west-2", "aws/us-east-1"])
    assert rate.usd_per_hour_low == 6.88
    assert "ap-northeast-1" not in rate.basis


def test_a_bare_vendor_word_names_no_region(monkeypatch):
    """`placement.regions` also takes a bare vendor ("gcp"), which names no
    region. Reading that as one would look up a region called "gcp" and find
    nothing, so the default has to survive it."""
    rate = e.hourly_rate("L40S", 1, 1, ["aws"], "on-demand", ["gcp", "aws"])
    assert rate.usd_per_hour_low == 1.861
    assert m.DEFAULT_AWS_REGION in rate.basis


def test_the_machine_sizes_on_offer_depend_on_the_region():
    """Which is why the region travels with the fill question. Reading the sizes
    from one region and applying them to another would revive the 2026-09-02
    Pending-forever failure in a new place."""
    everywhere = {r: m.aws_counts("H100", [r]) for r in m.AWS_REGIONS}
    distinct = set(everywhere.values())
    assert len(distinct) > 1, everywhere
    assert m.aws_counts("H100") == (1, 8)


def test_the_two_price_bases_are_kept_apart():
    """★ AWS prices a whole machine; GCP prices the accelerators ALONE, because a
    GPU there attaches to a machine type the catalogue prices separately. Ranking
    them together would put GCP on top whenever it is not actually cheaper, so
    nothing that prices a job reads a GCP row."""
    assert {r.basis for r in m.PRICE_ROWS if r.vendor == "aws"} == {"machine"}
    assert {r.basis for r in m.PRICE_ROWS if r.vendor == "gcp"} == {"accelerator"}
    # AWS rows always name their instance; GCP rows never can.
    assert all(r.instance for r in m.PRICE_ROWS if r.vendor == "aws")
    assert not any(r.instance for r in m.PRICE_ROWS if r.vendor == "gcp")
    # And the estimator only ever prices from the AWS half.
    assert all(r.vendor == "aws" for r in m.AWS_MACHINES)


def test_rows_whose_spot_beats_their_own_on_demand_are_flagged_not_dropped():
    """38 GCP rows list a spot price ABOVE their own on-demand price, by 5-17%,
    with both zones of a region agreeing -- A100 x1 asia-northeast1 is 1.70586
    and 1.7915 in both -a and -c. That is the catalogue's content, not a grouping
    mistake (an earlier generator DID have one, pairing a 1-card price with a
    16-card spot). They ship flagged so nothing ranks them and nobody has to
    rediscover it."""
    flagged = [r for r in m.PRICE_ROWS if r.flags == "spot_above_ondemand"]
    assert len(flagged) == 38
    assert {r.vendor for r in flagged} == {"gcp"}
    for row in m.PRICE_ROWS:
        if row.vendor != "aws" or row.usd_per_hour is None or row.spot_high is None:
            continue
        assert row.spot_high <= row.usd_per_hour * 1.001, row


# ---------------------------------------------- 2026-09-09 결정 2번
# 비용은 산술로, 시간 모델은 측정한 모양의 job 에만.


def test_the_billed_rate_beats_the_multiplication_when_we_have_the_invoice():
    """DDPSRUN-BILLED-RATE. 청구서를 들고 있으면 그것을 인용한다.

    곱셈(4 x $1.59 = $6.36)도 옳다 — RunPod 은 카드당 과금이고 그것은 실측이다.
    다만 **유도한 값과 청구서는 표준이 다르다.** baseline-c 의 네 장은
    `myself.currentSpendPerHr` 로 $6.388/hr 였다(2026-09-07). 차이는 0.4% 이고,
    그래서 지금 넣는 것이 맞다 — 두 값이 아직 일치하니 곱셈이 옳다는 것을 읽는
    사람이 볼 수 있고, 나중에 list price 와 청구가 갈라지면 답은 청구 쪽으로 남는다.
    """
    four = e.hourly_rate("A100-80GB", 4, 1, ["runpod"], "on-demand")
    assert four.usd_per_hour_low == 6.388
    assert "BILLED" in four.basis
    assert "currentSpendPerHr" in four.basis, "어느 청구서인지 문장에 있다"
    # 곱셈은 0.4% 안에서 같은 답을 낸다.
    assert abs(4 * 1.59 - 6.388) / 6.388 < 0.005


def test_a_pod_shape_we_have_never_been_billed_for_still_multiplies():
    eight = e.hourly_rate("A100-80GB", 8, 1, ["runpod"], "on-demand")
    assert eight.usd_per_hour_low == round(1.59 * 8, 4)
    assert "per card-hour" in eight.basis, "청구서가 없으면 정직한 차선이 곱셈이다"


def test_two_pods_of_a_billed_shape_multiply_the_pod_rate():
    """청구서는 pod 하나에 대한 것이므로 pod 수만 곱한다."""
    two = e.hourly_rate("A100-80GB", 4, 2, ["runpod"], "on-demand")
    assert two.usd_per_hour_low == round(6.388 * 2, 4)


def test_expected_hours_gives_a_cost_when_our_time_model_cannot():
    """★ 이것이 없던 동안 21시간 4장 job 을 비용 없이 결정하게 했다."""
    result = e.estimate(gpu_name="A100-80GB", cap=12288, pairs=5000, epochs=1,
                        row_tokens=None, mitigations_on=True, vendors=["runpod"],
                        gpu_count=4, expected_hours=21.0)
    assert result.duration.confidence == "unknown", "시간은 여전히 unknown 이 정답이다"
    assert result.rate.usd_per_hour_low == 6.388
    assert result.cost_low_usd == round(21.0 * 6.388, 2)
    assert result.cost_basis == "user-supplied", (
        "누구의 숫자인지가 응답에 있다 — 산술은 우리 것이고 불확실성은 사용자 것이다")
    assert any("YOUR 21 hours" in w for w in result.warnings)


def test_without_expected_hours_the_cost_stays_absent():
    result = e.estimate(gpu_name="A100-80GB", cap=12288, pairs=5000, epochs=1,
                        row_tokens=None, mitigations_on=True, vendors=["runpod"],
                        gpu_count=4)
    assert result.cost_low_usd is None
    assert result.cost_basis == "", "비용이 없으면 basis 도 비어 있다"


def test_a_measured_job_is_labelled_measured_and_ignores_expected_hours():
    """시간 모델이 답할 수 있으면 사용자의 추측이 그것을 덮지 않는다."""
    # aiops-exp1 이 실측한 조합: A100-80GB, cap 12288, 응답 5,600 토큰.
    result = e.estimate(gpu_name="A100-80GB", cap=12288, pairs=5000, epochs=1,
                        row_tokens=5600, mitigations_on=True, vendors=["runpod"],
                        expected_hours=999.0)
    assert result.duration.confidence != "unknown"
    assert result.cost_basis == "measured"
    assert result.cost_low_usd is not None and result.cost_low_usd < 999.0
