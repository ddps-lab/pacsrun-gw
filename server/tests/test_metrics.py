"""Reading GPU usage and progress out of a log.

The parsing tests use lines in the exact shape our runs actually produced. The
rest check that an empty answer explains itself, because "no data" and "your
script does not print it" look identical to a user and need different fixes.
"""

import pytest

from ddpsrun_server import metrics as m

# What section 8 of the script contract tells a run.sh to print.
GPU = "PACSRUN_GPU=94,38200,45440,71,298.5"
# A real line shape from a training log.
PROGRESS = " 63%|######3   | 350/556 [4:02:35<2:22:44, 41.57s/it]"


def test_a_gpu_line_is_read_field_by_field():
    sample = m.parse_gpu(GPU)
    assert sample.utilization_percent == 94
    assert sample.memory_used_mib == 38200
    assert sample.memory_total_mib == 45440
    assert sample.temperature_c == 71
    assert sample.power_w == 298.5
    assert sample.memory_percent == 84.1


def test_a_gpu_line_is_found_even_with_the_relay_prefix_around_it():
    # The driver relays remote output, so a line can arrive with text in front.
    assert m.parse_gpu("[remote] PACSRUN_GPU=10,100,200,40,50.0") is not None


def test_the_apiserver_stamp_becomes_the_samples_time():
    # recent_log_lines reads with timestamps=True, so a line arrives as
    # "<RFC 3339> PACSRUN_GPU=..." — the stamp is the chart's x axis.
    stamped = m.parse_gpu("2026-09-07T11:49:33.199910919Z PACSRUN_GPU=93,77209,81920,48,229.86")
    assert stamped.time == "2026-09-07T11:49:33.199910919Z"
    assert m.parse_gpu(GPU).time == ""   # a bare line has no stamp to harvest


def test_scan_reports_the_peak_and_the_average():
    # The peak is what a post-mortem asks for: a finished job's LAST reading is
    # the idle card just before teardown (0%, 0 MiB) and says nothing.
    reading = m.scan([
        "PACSRUN_GPU=100,45669,81920,62,370.0",
        "PACSRUN_GPU=98,77209,81920,51,381.1",
        "PACSRUN_GPU=0,0,81920,36,67.8",
    ], 3600)
    assert reading.peak_gpu.memory_used_mib == 77209
    assert reading.latest_gpu.memory_used_mib == 0
    assert abs(reading.avg_utilization_percent - 66.0) < 0.1


@pytest.mark.parametrize(
    "line",
    ["", "PACSRUN_KEEPALIVE", "{'loss': 0.42}", "PACSRUN_GPU=", "PACSRUN_GPU=1,2,3"],
)
def test_a_line_that_is_not_a_gpu_reading_is_left_alone(line):
    assert m.parse_gpu(line) is None


def test_a_progress_line_gives_the_step_and_the_projection():
    progress = m.parse_progress(PROGRESS)
    assert (progress.step, progress.total_steps) == (350, 556)
    assert progress.seconds_per_step == 41.57
    assert progress.elapsed == "4:02:35"
    assert progress.remaining == "2:22:44"
    assert round(progress.projected_total_hours, 2) == 6.42
    assert progress.percent == 62.9


def test_the_bare_progress_shape_is_read_too():
    # tqdm writes the bar; the plain form appears in captured output.
    assert m.parse_progress("350/556 [4:02:35<2:22:44, 41.57s/it]").step == 350


def test_a_projection_from_too_few_steps_is_marked_unsettled():
    # bank-exp2v2 was 32% out at step 1 and within 4% by step 50.
    assert m.parse_progress("1/556 [00:56<8:39:00, 56.00s/it]").steady is False
    assert m.parse_progress("50/556 [33:51<5:42:23, 40.61s/it]").steady is True


def test_the_projection_at_step_50_is_close_to_what_the_job_actually_took():
    # bank-exp2v2 finished in 6.54 hours.
    projected = m.parse_progress("50/556 [33:51<5:42:23, 40.61s/it]").projected_total_hours
    assert abs(projected - 6.54) / 6.54 < 0.05


def test_the_last_progress_line_wins():
    # The library overwrites this line thousands of times a run.
    reading = m.scan(
        ["10/556 [00:07<01:00, 40.00s/it]", "350/556 [4:02:35<2:22:44, 41.57s/it]"], 3600
    )
    assert reading.progress.step == 350


def test_a_run_faster_than_one_step_a_second_still_has_a_progress_bar():
    # ★ WHY SOME JOBS SHOWED NO PROGRESS PANEL AT ALL. tqdm writes seconds per
    # iteration only while a step takes more than a second; below that the same
    # bar flips to iterations per second. The pattern ended at `s/it`, so every
    # fast run matched nothing and the screen drew no Progress panel -- which
    # reads as "this job has no progress" rather than "we cannot parse it"
    # (reported 2026-09-11, against market64-exp0 at 179.09s/it which did have
    # one).
    fast = m.parse_progress("100%|##########| 128/128 [00:13<00:00,  9.52it/s]")

    assert fast is not None
    assert (fast.step, fast.total_steps) == (128, 128)
    # 9.52 steps a second is 0.105 seconds a step, and everything downstream
    # works in seconds a step.
    assert round(fast.seconds_per_step, 4) == 0.1050
    assert round(fast.projected_total_hours, 5) == round(128 * 0.1050 / 3600, 5)


def test_the_slow_unit_is_unchanged():
    # market64-exp0's real line, read from its log on 2026-09-11.
    slow = m.parse_progress("  8%|3   | 10/128 [30:41<5:52:12, 179.09s/it]")

    assert (slow.step, slow.total_steps) == (10, 128)
    assert slow.seconds_per_step == 179.09
    assert round(slow.projected_total_hours, 2) == 6.37


def test_a_bar_that_has_not_measured_a_rate_yet_is_not_progress():
    # tqdm's first frame carries 0.00it/s, which has no reciprocal and says
    # nothing about pace. Inverting it would divide by zero.
    assert m.parse_progress(" 0%| | 0/128 [00:00<?,  0.00it/s]") is None


def test_a_long_series_is_thinned_but_still_ends_where_the_job_is():
    # A 25-hour job prints about 3,000 readings and no chart can show them all.
    lines = [f"PACSRUN_GPU={i % 100},{i},45440,70,300.0" for i in range(3000)]
    reading = m.scan(lines, 86400)
    assert len(reading.gpu_series) <= m.MAX_SAMPLES
    assert reading.gpu_series[-1].memory_used_mib == 2999
    assert reading.latest_gpu.memory_used_mib == 2999


def test_the_sample_count_is_the_readings_taken_and_not_the_points_kept():
    # ★ THE SCREEN PRINTED THE WRONG ONE UNDER THE WORD "samples".
    # job-66b46719b854 printed 785 readings per card; downsample kept 393 of
    # them so a chart could draw the line, and both the panel note and the
    # per-card table counted the 393 (reported 2026-09-11). The mean and the
    # peak are computed over all 785, so the count next to them has to be 785.
    lines = [f"PACSRUN_GPU_CARD=0,{i % 100},{i},81920,70,300.0" for i in range(785)]
    reading = m.scan(lines, 86400)

    assert reading.sample_count == 785
    assert reading.cards[0].sample_count == 785
    assert len(reading.cards[0].series) < 785, "the series itself is still thinned"
    assert len(reading.cards[0].series) <= m.MAX_SAMPLES


def test_every_card_counts_its_own_readings():
    # Four cards, and one of them printed fewer -- a card that joined late, or
    # a line the relay dropped. Each count is that card's own.
    lines = [f"PACSRUN_GPU_CARD={c},90,{i},81920,70,300.0"
             for c in range(4) for i in range(500 if c < 3 else 120)]
    cards = m.scan(lines, 86400).cards

    assert [c.sample_count for c in cards] == [500, 500, 500, 120]


def test_a_short_series_is_not_thinned():
    lines = [f"PACSRUN_GPU=50,{i},45440,70,300.0" for i in range(10)]
    assert len(m.scan(lines, 3600).gpu_series) == 10


def test_a_window_with_nothing_in_it_points_at_the_watcher_line_not_the_users_script():
    # Both of these notes used to tell the reader to go and edit their run.sh, which was
    # right while printing the GPU line was the script's job. PACSrun's drivers print it
    # now (driver/common/gpu-watch.sh, grep PACSRUN-GPU-WATCH), so that advice would send
    # someone to change a file that was never the cause. What IS diagnostic is the
    # PACSRUN_GPU_WATCH line the watcher itself prints, so the notes name that instead.
    reading = m.scan(["installing packages", "downloading model"], 3600)
    assert reading.latest_gpu is None
    assert reading.progress is None
    assert "PACSRUN_GPU_WATCH" in reading.note
    assert "run.sh" not in reading.note


def test_progress_without_gpu_readings_names_the_watcher_line_and_the_two_usual_causes():
    reading = m.scan([PROGRESS], 3600)
    assert "PACSRUN_GPU_WATCH" in reading.note
    assert "nvidia-smi" in reading.note
    assert "run.sh" not in reading.note


def test_gpu_readings_without_progress_suggests_the_run_is_still_setting_up():
    reading = m.scan([GPU], 3600)
    assert "installing or downloading" in reading.note


def test_a_healthy_window_past_the_settling_point_has_nothing_to_note():
    reading = m.scan([GPU, PROGRESS], 3600)
    assert reading.note == ""
    assert reading.latest_gpu is not None
    assert reading.progress.steady is True


def test_an_early_window_says_the_projection_is_not_settled():
    reading = m.scan([GPU, "5/556 [04:34<6:52:00, 45.74s/it]"], 3600)
    assert "not settled yet" in reading.note


def test_the_window_asked_for_is_reported_back():
    assert m.scan([], 7200).window_seconds == 7200


# ------------------------------------------------------ several cards, one pod


def test_each_card_gets_its_own_series():
    # baseline-c rents four A100s in ONE pod. The watcher used to keep
    # `head -1` and this screen drew card 0 alone, so three quarters of a $44
    # run was invisible (2026-09-08).
    lines = []
    for _ in range(2):          # two intervals, so every card has a series
        for index, (util, used) in enumerate(
            ((100, 45669), (98, 44100), (97, 43900), (99, 44950))
        ):
            lines.append(f"PACSRUN_GPU_CARD={index},{util},{used},81920,62,370.32")
    reading = m.scan(lines, 3600)

    assert [c.gpu_index for c in reading.cards] == [0, 1, 2, 3]
    assert all(len(c.series) == 2 for c in reading.cards)
    assert reading.cards[3].latest.memory_used_mib == 44950
    # The single-card fields keep describing the LOWEST card, so a screen that
    # has not learned cards[] sees what it saw before.
    assert reading.latest_gpu.memory_used_mib == 45669
    assert reading.latest_gpu.gpu_index == 0


def test_card_zero_is_not_counted_twice():
    # The watcher sends card 0 on both its per-card line and the old
    # five-field one. Counting both would give that card two samples per
    # interval and halve every rate read off the chart.
    lines = [
        "PACSRUN_GPU_CARD=0,100,45669,81920,62,370.32",
        "PACSRUN_GPU=100,45669,81920,62,370.32",
        "PACSRUN_GPU_CARD=1,98,44100,81920,61,355.10",
    ]
    reading = m.scan(lines, 3600)
    assert [len(c.series) for c in reading.cards] == [1, 1]


def test_a_log_from_before_the_per_card_change_still_reads():
    # No per-card line anywhere: the old readings ARE the answer and become
    # card 0, which is what every log written before 2026-09-08 contains.
    reading = m.scan(["PACSRUN_GPU=93,77209,81920,48,229.86"], 3600)
    assert [c.gpu_index for c in reading.cards] == [0]
    assert reading.cards[0].latest.memory_used_mib == 77209


def test_the_index_of_a_per_card_line_is_not_read_as_utilisation():
    reading = m.scan(["PACSRUN_GPU_CARD=3,99,44950,81920,63,366.04"], 3600)
    assert reading.cards[0].gpu_index == 3
    assert reading.cards[0].latest.utilization_percent == 99


def test_the_peak_utilisation_is_not_read_off_the_highest_memory_sample():
    """★ THE 0% A FINISHED RUN REPORTED. job-66b46719b854 rented four A100s and
    printed 785 readings per card. Its single highest-memory sample, 77,631 MiB,
    happened to fall between steps and read utilisation 0 -- while the card's
    real maximum was 100 and its mean 84.6. The screen labelled that sample's
    utilisation "Utilisation at peak", so a run that had been busy for six hours
    was reported as idle. `peak` stays memory-chosen, because memory is what
    kills runs; the utilisation peak is now its own number.
    """
    # Three readings shaped like that run: busy, busier, and one fat quiet one.
    lines = [
        "PACSRUN_GPU=100,60000,81920,70,300",
        "PACSRUN_GPU=90,70000,81920,71,310",
        "PACSRUN_GPU=0,77631,81920,32,62",     # the memory peak, between steps
    ]
    reading = m.scan(lines, 3600)
    assert reading.peak_gpu.memory_used_mib == 77631, "peak 은 여전히 메모리로 고른다"
    assert reading.peak_gpu.utilization_percent == 0
    assert reading.peak_utilization_percent == 100, (
        "이 값이 화면의 'Peak utilisation' 이고, 위의 0 이 아니다")
    assert reading.avg_utilization_percent == round((100 + 90 + 0) / 3, 1)


def test_every_card_carries_its_own_utilisation_peak():
    lines = [
        "PACSRUN_GPU_CARD=0,100,60000,81920,70,300",
        "PACSRUN_GPU_CARD=1,40,60000,81920,70,300",
        "PACSRUN_GPU_CARD=0,0,77000,81920,32,62",
        "PACSRUN_GPU_CARD=1,0,77000,81920,32,62",
    ]
    cards = m.scan(lines, 3600).cards
    assert [c.gpu_index for c in cards] == [0, 1]
    assert [c.peak_utilization_percent for c in cards] == [100, 40]
    # And the single-card fields still describe card 0, unchanged.
    assert m.scan(lines, 3600).peak_utilization_percent == 100


# ------------------------------------------------- PACSRUN-METRIC-WATCH: the training's own numbers
#
# ★ WHY THESE EXIST, in one measurement. On 2026-09-15 a job finished `Succeeded`
# after 9 h 44 m on four A100s at $6.36/hour with a perfect progress bar, and one
# of its nine trainings had run its objective 25% downhill. Every log that job
# uploaded contained the word `loss` exactly zero times -- the numbers were in a
# file. `progress` answers "how far has it got"; only these answer "is it
# learning", and the two are different questions.
#
# The rows below are REAL, copied out of that run's `ppo_stats.jsonl`. Invented
# data would let the arithmetic be wrong in exactly the way that matters: this
# series rises for a while before it falls, which is the shape that fooled a
# language model asked the same question.

REAL_SCORES = [2.4413, 2.7344, 3.6300, 3.0712, 2.3713, 2.4700, 2.2800, 3.1000,
               2.4000, 3.3800, 2.9400, 3.4100, 3.0800, 2.8100, 3.4400, 1.5100,
               3.0000, 1.5400, 2.2800, 2.3700]


def metric_lines(series="bank/adapters/AD/iter_1", scores=None, key="step", extra=None):
    """Log lines exactly as metric-watch.sh prints them, timestamp and all."""
    import json as _json
    scores = REAL_SCORES if scores is None else scores
    out = []
    for i, score in enumerate(scores, start=1):
        row = {"_series": series, key: i, "score": score}
        if extra:
            row.update(extra(i))
        out.append("2026-09-15T00:00:00.000Z PACSRUN_METRIC=" +
                   _json.dumps(row, separators=(",", ":")))
    return out


def test_the_arithmetic_catches_the_run_that_went_backwards():
    # The numbers this whole feature was built for. A language model shown this
    # same series answered "learning and improving": it found the peak at step 3
    # and called it the end. Least squares does not do that.
    reading = m.scan(metric_lines(), 3600)
    assert len(reading.metric_series) == 1
    trend = reading.metric_series[0].trends["score"]
    assert trend.slope < 0
    # The scores above are the real ones rounded to four places, so the slope
    # lands a hair off the -0.0269 computed from full precision. The tolerance is
    # that rounding and nothing else.
    assert trend.slope == pytest.approx(-0.0269, abs=0.0005)
    assert trend.head == pytest.approx(2.850, abs=0.001)
    assert trend.tail == pytest.approx(2.140, abs=0.001)
    assert trend.change_ratio == pytest.approx(-0.249, abs=0.001)


def test_nine_trainings_stay_nine_series_and_do_not_merge():
    # ★ WITHOUT THIS THE READING IS WORSE THAN NOTHING. The measured job trained
    # nine times and every one numbered its steps from 1. Merged, step 1 appears
    # nine times with nine different scores and the average of the pile means
    # nothing about any of them.
    lines = []
    for domain in ("bank", "market", "telecom"):
        lines += metric_lines(series=f"{domain}/AD/iter_1", scores=REAL_SCORES[:6])
    reading = m.scan(lines, 3600)
    assert sorted(s.name for s in reading.metric_series) == [
        "bank/AD/iter_1", "market/AD/iter_1", "telecom/AD/iter_1"]
    for series in reading.metric_series:
        assert series.row_count == 6
        assert (series.first_step, series.last_step) == (1, 6)


def test_a_repeated_step_is_the_same_row_and_not_a_second_point():
    # metric-watch.sh re-prints what it already printed when it could not write
    # its state file, so duplicates are expected. Counting them twice would put
    # a doubled point into every trend.
    lines = metric_lines(scores=REAL_SCORES[:5])
    reading = m.scan(lines + lines, 3600)
    assert reading.metric_series[0].row_count == 5


def test_a_nan_is_reported_rather_than_propagated_into_a_meaningless_slope():
    # Arithmetic on NaN gives NaN, and a slope of nan reads downstream as "no
    # answer" when the honest answer is "this training is broken".
    lines = metric_lines(scores=[1.0, 2.0, float("nan"), 4.0])
    reading = m.scan(lines, 3600)
    trend = reading.metric_series[0].trends["score"]
    assert trend.has_nan is True
    assert trend.slope == 0.0


def test_every_numeric_field_gets_a_trend_because_the_server_picks_no_objective():
    # `loss` for one run, `score` and `kl` for another. Deciding here which field
    # matters would be the server deciding what a researcher may measure.
    lines = metric_lines(extra=lambda i: {"kl": -0.3 * i, "entropy": 33.0 + i})
    series = m.scan(lines, 3600).metric_series[0]
    assert series.fields == ["entropy", "kl", "score"]
    assert set(series.trends) == {"entropy", "kl", "score"}
    assert series.trends["kl"].slope < 0
    assert series.trends["entropy"].slope > 0


def test_a_boolean_field_is_not_measured_as_a_number():
    # `isinstance(True, int)` is True in python, so a flag would arrive as a
    # metric that is always 0 or 1 and could be picked as the objective.
    lines = metric_lines(scores=[1.0, 2.0, 3.0], extra=lambda i: {"should_stop": i == 3})
    series = m.scan(lines, 3600).metric_series[0]
    assert "should_stop" not in series.fields


def test_a_trainer_style_global_step_orders_the_rows():
    lines = metric_lines(scores=[2.0, 1.5, 1.2], key="global_step")
    series = m.scan(lines, 3600).metric_series[0]
    assert series.step_key == "global_step"
    assert series.last_step == 3


def test_a_line_that_is_not_ours_or_will_not_parse_is_ignored():
    lines = ["2026-09-15T00:00:00Z some ordinary training output",
             "2026-09-15T00:00:00Z PACSRUN_METRIC={not json at all}",
             "2026-09-15T00:00:00Z PACSRUN_GPU=94,38200,45440,71,298"]
    reading = m.scan(lines + metric_lines(scores=[1.0, 2.0]), 3600)
    assert len(reading.metric_series) == 1
    assert reading.metric_series[0].row_count == 2
    # And the GPU line on the same stdout is still read as a GPU line.
    assert reading.latest_gpu is not None


def test_a_long_run_is_thinned_but_keeps_its_first_and_last_row():
    # The head and tail are what a trend is read from. Thinning that dropped the
    # first row would move the baseline a reader compares against.
    lines = metric_lines(scores=[float(i) for i in range(1, 1001)])
    series = m.scan(lines, 3600).metric_series[0]
    assert series.row_count == 1000
    assert len(series.rows) <= m.MAX_METRIC_ROWS
    assert series.rows[0]["step"] == 1
    assert series.rows[-1]["step"] == 1000
    # The trend is measured on all 1,000, not on what survived the thinning.
    assert series.trends["score"].slope == pytest.approx(1.0)


def test_a_head_tail_window_never_overlaps_on_a_short_run():
    # Six rows with a window of five would compare rows 1-5 against 2-6 and call
    # four rows of overlap a trend.
    series = m.scan(metric_lines(scores=[1.0, 2.0, 3.0, 4.0, 5.0, 6.0]),
                          3600).metric_series[0]
    assert series.trends["score"].window == 3


def test_a_job_that_prints_no_metric_line_reports_an_empty_list_and_not_an_error():
    # A CPU image with no python3, and a training that keeps its numbers in
    # memory until the end, both land here. Neither is a failure.
    reading = m.scan(["2026-09-15T00:00:00Z PACSRUN_GPU=94,38200,45440,71,298"], 3600)
    assert reading.metric_series == []


# ------------------------------------------------- which tqdm bar is the run


def test_the_training_bar_wins_over_the_one_that_came_last():
    """★ MEASURED ON job-a72cfb29b593 (2026-09-16), AND THE SCREEN WAS WRONG BECAUSE OF IT.

    A Hugging Face training prints several tqdm bars: the training loop, the dataset `map`,
    and one per checkpoint write. "The last one wins" then picks whichever finished most
    recently, which at the end of a run is `Writing model shards: 1/1`. The panel read
    Elapsed 00:00, Remaining 00:00, Projected total 0.00 h on a job that had just trained
    for six minutes.
    """
    lines = [
        "2026-09-16T05:00:00Z 4000/4000 [05:54<00:00, 11.29it/s]",
        "2026-09-16T05:00:01Z 1/1 [00:00<00:00,  4.21it/s]",
    ]
    progress = m.scan(lines, 3600).progress
    assert progress is not None
    assert progress.total_steps == 4000, "the model-shard writer was read as the training"
    assert progress.elapsed == "05:54"


def test_a_long_dataset_map_does_not_beat_a_longer_training():
    # ★ ELAPSED AND NOT THE STEP TOTAL. A `map` over 8,000 rows has a bigger total than a
    # 4,000-step training and is over in seconds; "which of these IS the run" is a question
    # about time.
    lines = [
        "2026-09-16T05:00:00Z 8000/8000 [00:12<00:00, 654.0it/s]",
        "2026-09-16T05:00:01Z 4000/4000 [05:54<00:00, 11.29it/s]",
    ]
    assert m.scan(lines, 3600).progress.total_steps == 4000


def test_the_hour_long_form_is_compared_correctly():
    # tqdm writes H:MM:SS past an hour and MM:SS before it, and both turn up in one log.
    # Comparing them as strings would make "59:00" beat "4:02:35".
    lines = [
        "2026-09-16T05:00:00Z 350/556 [4:02:35<2:22:44, 41.57s/it]",
        "2026-09-16T05:00:01Z 100/100 [59:00<00:00, 35.40s/it]",
    ]
    assert m.scan(lines, 3600).progress.total_steps == 556


def test_a_zero_rate_is_not_a_reading_in_either_unit():
    # tqdm prints `0.00s/it` on the first line of a bar, before any item has finished. Only
    # the it/s half was guarded, so that line produced a projected total of 0.00 h.
    assert m.parse_progress("0/4000 [00:00<00:00,  0.00s/it]") is None
    assert m.parse_progress("0/4000 [00:00<00:00,  0.00it/s]") is None

