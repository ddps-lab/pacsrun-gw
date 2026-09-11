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
