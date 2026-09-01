"""The arithmetic, checked against the eight runs it came from.

Two kinds of test here. The first kind reproduces a number we have in a log,
which is what makes the estimator trustworthy at all. The second kind checks
that it REFUSES to answer where it should, which is what stops it repeating the
market-exp2 mistake.
"""

import pytest

from ddpsrun_server import estimate as e
from ddpsrun_server.measurements import THROUGHPUT


# --------------------------------------------------------------- step counting


@pytest.mark.parametrize(
    "job, pairs, epochs, logged",
    [
        ("telecom-exp1", 564, 4, 284),
        ("telecom-exp2", 675, 4, 340),
        ("bank-exp1", 973, 4, 488),
        ("bank-exp1v2", 1061, 4, 532),
        ("market-exp2", 742, 4, 372),
        ("aiops-exp1", 3311, 4, 1656),
        ("bank-exp2v2", 1110, 4, 556),
        ("aiops-exp2", 3546, 4, 1776),
    ],
)
def test_the_step_formula_matches_every_log_we_have(job, pairs, epochs, logged):
    # All eight. This part of the estimate has no error in it, which is worth
    # knowing because everything downstream does.
    assert e.steps(pairs, epochs, 1, 8) == logged, job


def test_a_zero_anywhere_is_refused_rather_than_dividing():
    for bad in [(0, 4, 1, 8), (100, 0, 1, 8), (100, 4, 0, 8), (100, 4, 1, 0)]:
        with pytest.raises(ValueError):
            e.steps(*bad)


# ------------------------------------------------------- seconds per step


@pytest.mark.parametrize(
    "job, gpu, tokens_per_second, row, logged_seconds",
    [
        ("bank-exp2v2", "L40S", 1555, 4100, 42.32),
        ("aiops-exp2", "L40S", 1357, 5600, 66.44),
        ("market-exp2", "L40S", 1000, 10619, 172.9),
        ("aiops-exp1", "A100-SXM4-80GB", 1682, 5600, 53.49),
    ],
)
def test_feeding_a_run_its_own_throughput_reproduces_its_step_time(
    job, gpu, tokens_per_second, row, logged_seconds
):
    # This is the derivation itself: tokens_per_step / tokens_per_second. If it
    # ever stops landing within a couple of percent, the factor of 2 for DPO's
    # chosen and rejected responses is the first thing to suspect.
    predicted = e.tokens_per_step(row, 1, 8) / tokens_per_second
    assert abs(predicted - logged_seconds) / logged_seconds < 0.02, job


def test_a_length_we_have_run_is_called_measured():
    duration = e.seconds_per_step("L40S", 5600, 1, 8)
    assert duration.confidence == e.Confidence.MEASURED
    assert "aiops-exp2" in duration.basis


def test_the_point_estimate_sits_between_the_runs_that_produced_it():
    # Five L40S runs sat between 1,474 and 1,555 tokens/s around 4,100 tokens.
    # The answer must not be whichever row happens to be listed first.
    duration = e.seconds_per_step("L40S", 4100, 1, 8)
    per_step = e.tokens_per_step(4100, 1, 8)
    assert per_step / 1555 < duration.seconds_per_step < per_step / 1474


def test_a_gap_between_two_runs_is_interpolated_not_claimed_as_measured():
    # 7,500 tokens is between our 5,600 and 10,619 L40S runs and outside the
    # 15% band around either.
    duration = e.seconds_per_step("L40S", 7500, 1, 8)
    assert duration.confidence == e.Confidence.INTERPOLATED
    assert "Nothing was measured at 7,500" in duration.basis


def test_past_the_ends_of_the_measured_range_the_answer_is_unknown():
    # This is the market-exp2 lesson made mechanical: that job's length was
    # far outside anything measured, it was estimated anyway, and the estimate
    # was 96% out.
    duration = e.seconds_per_step("L40S", 30000, 1, 8)
    assert duration.confidence == e.Confidence.UNKNOWN
    assert "96%" in duration.basis


def test_a_gpu_we_have_never_rented_is_unknown_and_says_so():
    duration = e.seconds_per_step("H200", 4100, 1, 8)
    assert duration.confidence == e.Confidence.UNKNOWN
    assert "never rented" in duration.basis


def test_one_measurement_cannot_be_interpolated_from():
    # We have exactly one A100 run. A line needs two points, so anything away
    # from that one length is unknown rather than a guess off a single point.
    a100_rows = [row for row in THROUGHPUT if row.gpu == "A100-SXM4-80GB"]
    assert len(a100_rows) == 1, "this test's premise changed; revisit it"
    assert e.seconds_per_step("A100-SXM4-80GB", 9000, 1, 8).confidence == e.Confidence.UNKNOWN


def test_the_fit_reproduces_the_hand_worked_coefficients_on_the_same_two_points():
    # docs/04-estimate.md section 2 fitted a line through exactly two rows by
    # hand and got a = 4.7215e-04, b = 4.9738e-08. Given the same two rows the
    # code has to land on the same line.
    two = [
        row for row in THROUGHPUT
        if row.gpu == "L40S" and (row.job, row.row_tokens) in
        {("bank-exp1v2", 4144), ("market-exp2", 10619)}
    ]
    assert len(two) == 2, "the two rows the doc fitted are no longer in the table"
    intercept, slope = e._fit_seconds_per_token(two)
    assert abs(intercept - 4.7215e-04) / 4.7215e-04 < 0.01
    assert abs(slope - 4.9738e-08) / 4.9738e-08 < 0.01


def test_the_fit_over_every_l40s_run_lands_near_the_two_point_line():
    # The code actually fits ALL the rows for a GPU, not two. More points is
    # better, but it must not wander off: five of the six L40S rows sit at one
    # end of the range, so a bad fit would be pulled hard toward them.
    intercept, slope = e._fit_seconds_per_token(
        [row for row in THROUGHPUT if row.gpu == "L40S"]
    )
    assert abs(intercept - 4.7215e-04) / 4.7215e-04 < 0.10
    assert abs(slope - 4.9738e-08) / 4.9738e-08 < 0.10


# ------------------------------------------------------------------ memory


def test_the_logits_buffer_matches_the_allocation_that_killed_aiops():
    # The traceback asked for a buffer for 11,926 tokens and it was 6.75 GiB.
    assert abs(e.peak_logits_gib(11926) - 6.75) < 0.02


def test_the_cap_and_not_the_average_length_is_what_decides_memory():
    # The longest sample grows until it meets the cap. aiops-exp1's average
    # row was about 5,600 tokens and it died on a buffer for 11,926.
    assert e.peak_logits_gib(12288) > e.peak_logits_gib(5600) * 2


def test_a_bigger_vocabulary_costs_proportionally_more():
    # This term dominates, which is why a different model needs its own number.
    assert e.peak_logits_gib(12288, vocab=303_872) == pytest.approx(
        e.peak_logits_gib(12288, vocab=151_936) * 2
    )


# --------------------------------------------------------- GPU recommendation


def test_without_the_mitigations_the_answer_is_80gb_and_cites_the_incident():
    advice = e.recommend_gpu(12288, mitigations_on=False)
    assert advice.recommended_vram_gb == 80
    assert "aiops-exp1" in advice.reason


def test_with_the_mitigations_48gb_is_enough_at_every_cap_we_have_run():
    for cap in (12288, 18432):
        advice = e.recommend_gpu(cap, mitigations_on=True)
        assert advice.recommended_vram_gb == 48, cap


def test_a_cap_longer_than_anything_we_have_run_gets_80gb():
    advice = e.recommend_gpu(24576, mitigations_on=True)
    assert advice.recommended_vram_gb == 80
    assert "longer than anything we have run" in advice.reason


# ------------------------------------------------------------- capacity type


def test_every_answer_is_on_demand_because_runpod_sells_nothing_else():
    for hours, resumable in [(1.0, True), (30.0, False), (None, False)]:
        kind, reason = e.capacity_type(hours, resumable)
        assert kind == "on-demand", (hours, resumable)
        assert "RunPod does not sell spot" in reason


def test_a_long_job_with_no_checkpoint_says_what_it_would_lose():
    _, reason = e.capacity_type(30.0, resumable=False)
    assert "throw all of it away" in reason


def test_an_unestimatable_job_is_not_bet_on_reclaimable_capacity():
    _, reason = e.capacity_type(None, resumable=True)
    assert "could not estimate" in reason


# ------------------------------------------------------------ the whole answer


def test_a_job_we_have_run_before_is_priced_close_to_what_it_cost():
    # bank-exp2v2: 1,110 pairs, 4 epochs, ~4,100 tokens, L40S, and it took
    # 6.54 hours at $0.99/hour, so about $6.47.
    result = e.estimate(
        gpu_name="L40S", cap=12288, pairs=1110, epochs=4, row_tokens=4100,
        mitigations_on=True,
    )
    assert result.steps == 556
    assert result.duration.confidence == e.Confidence.MEASURED
    assert result.duration.low_hours < 6.54 < result.duration.high_hours
    assert result.cost_low_usd < 6.47 < result.cost_high_usd


def test_a_long_job_is_warned_about_drift_and_about_credentials_expiring():
    # aiops-exp2: 3,546 pairs, 1,776 steps, and it ran 33.5 hours.
    result = e.estimate(
        gpu_name="L40S", cap=12288, pairs=3546, epochs=4, row_tokens=5600,
        mitigations_on=True,
    )
    assert result.steps == 1776
    joined = " ".join(result.warnings)
    assert "28.80 and 38.21" in joined
    assert "12 hours" in joined


def test_without_the_dataset_size_there_is_no_step_count_and_no_cost():
    result = e.estimate(
        gpu_name="L40S", cap=12288, pairs=None, epochs=None, row_tokens=4100,
    )
    assert result.steps is None
    assert result.cost_low_usd is None
    # The memory advice does not need the dataset, so it is still answered.
    assert result.gpu.recommended_vram_gb is not None


def test_without_a_row_length_the_answer_says_what_would_be_needed():
    result = e.estimate(
        gpu_name="L40S", cap=12288, pairs=1110, epochs=4, row_tokens=None,
    )
    assert result.steps == 556
    assert result.duration.confidence == e.Confidence.UNKNOWN
    assert "row_tokens" in result.duration.basis


def test_a_gpu_we_cannot_price_produces_no_cost_rather_than_a_wrong_one():
    result = e.estimate(
        gpu_name="H200", cap=12288, pairs=1110, epochs=4, row_tokens=4100,
    )
    assert result.cost_low_usd is None


def test_the_price_is_stamped_with_when_we_paid_it():
    result = e.estimate(
        gpu_name="L40S", cap=12288, pairs=1110, epochs=4, row_tokens=4100,
        mitigations_on=True,
    )
    assert any("Vendor prices move" in warning for warning in result.warnings)


# --------------------------------------------- the two defects found on 2026-08-31


def test_a_missing_cap_produces_no_gpu_advice_rather_than_a_zero():
    # It used to say "the logits buffer reaches 0.00 GiB at cap 0" and recommend
    # 80 GB off the back of it. A number is believable; that one was wrong.
    advice = e.recommend_gpu(None, mitigations_on=True)
    assert advice.recommended is None
    assert advice.peak_logits_gib == 0.0
    assert "--max-len" in advice.reason


def test_a_missing_cap_still_lets_the_runtime_be_estimated():
    # The cap decides memory. It does not decide speed, so the rest of the
    # answer must survive without it.
    result = e.estimate(gpu_name="L40S", cap=None, pairs=1110, epochs=4, row_tokens=4100)
    assert result.steps == 556
    assert result.duration.confidence == e.Confidence.MEASURED
    assert result.gpu.recommended is None


def test_the_cost_uses_the_gpu_the_job_asked_for_not_the_one_we_recommend():
    # Timing an L40S run and pricing it at A100 rates overstated a job by 57%.
    # bank-exp2v2 cost $6.47 on an L40S at $0.99/hour.
    result = e.estimate(
        gpu_name="L40S", cap=12288, pairs=1110, epochs=4, row_tokens=4100,
        mitigations_on=False,   # so the recommendation is 80 GB, not the L40S
    )
    assert result.gpu.recommended == "A100-SXM4-80GB"
    assert result.cost_low_usd < 6.47 < result.cost_high_usd


def test_a_disagreement_between_the_price_and_the_recommendation_is_said_out_loud():
    result = e.estimate(
        gpu_name="L40S", cap=12288, pairs=1110, epochs=4, row_tokens=4100,
        mitigations_on=False,
    )
    assert any("does not reflect that" in warning for warning in result.warnings)
