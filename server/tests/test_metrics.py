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
