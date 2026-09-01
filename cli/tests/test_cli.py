"""Argument parsing, body assembly, and what each command prints.

The body assembly tests are the important ones: `build_submit_body` decides what
the server is asked for, and a flag silently failing to override a file would be
invisible until a job ran with the wrong GPU for eight hours.
"""

import json

import pytest

from ddpsrun import cli, config
from ddpsrun.client import ServerError


@pytest.fixture(autouse=True)
def logged_in(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv(config.SERVER_ENV, "https://run.example")
    monkeypatch.setenv(config.TOKEN_ENV, "s3cret")


class FakeClient:
    """Stands in for the HTTP client. Records calls, returns canned answers."""

    def __init__(self):
        self.submitted = None
        self.submit_result = {
            "job_id": "job-a8acdef80a07",
            "name": "bank-exp2",
            "result_path": "s3://bucket/pacsrun/lab-alice/bank-exp2-a8acdef80a07/",
        }
        self.status_result = {"job_id": "job-a8acdef80a07", "name": "bank-exp2", "phase": "Running"}
        self.log_lines = ["2026-09-01T00:00:01.000Z line one",
                          "2026-09-01T00:00:02.000Z line two"]
        self.estimate_result = {}
        self.validate_result = {"ok": True, "findings": [], "not_checked": []}
        self.metrics_result = {"window_seconds": 3600, "gpu_series": [], "note": ""}
        self.stats_result = {"team": "", "members": [], "jobs": 0, "gpu_hours": 0.0,
                             "cost_usd": 0.0, "unpriced_jobs": 0, "note": ""}

    def estimate(self, body):
        return self.estimate_result

    def validate(self, body):
        return self.validate_result

    def metrics(self, job_id, window_seconds=3600):
        return self.metrics_result

    def stats(self):
        return self.stats_result

    def submit(self, body):
        self.submitted = body
        return self.submit_result

    def status(self, job_id):
        return self.status_result

    def log_window(self, job_id, since=None, window_seconds=30):
        # The fixture behaves like the server: it drops what the caller says it
        # has already seen.
        lines = [l for l in self.log_lines if since is None or l.split(" ", 1)[0] > since]
        return {"lines": lines,
                "last_timestamp": lines[-1].split(" ", 1)[0] if lines else None,
                "window_seconds": window_seconds}

    def explain(self):
        return "ddpsrun — submit a batch job.\n"

    def schema(self):
        return {"properties": {"name": {}}}


@pytest.fixture
def fake(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(cli, "client_from_config", lambda: client)
    return client


def run(argv):
    return cli.main(argv)


# ---------------------------------------------------------------- body building


def args_for(argv):
    return cli.build_parser().parse_args(argv)


def test_a_job_can_be_built_from_flags_alone():
    body = cli.build_submit_body(args_for(["submit", "--name", "x", "--image", "img"]))
    assert body == {"name": "x", "image": "img"}


def test_a_flag_overrides_the_same_field_in_the_file(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: from-file\nimage: from-file\n")
    body = cli.build_submit_body(args_for(["submit", "-f", str(path), "--name", "from-flag"]))
    assert body["name"] == "from-flag"
    assert body["image"] == "from-file"


def test_env_from_a_file_and_from_flags_are_merged_not_replaced(tmp_path):
    # The reason: a file carries the ten stable values, a flag the one that
    # changes between runs. Replacing would silently drop the other nine.
    path = tmp_path / "job.yaml"
    path.write_text("name: x\nimage: img\nenv:\n  ML: '12288'\n  EPOCHS: '4'\n")
    body = cli.build_submit_body(
        args_for(["submit", "-f", str(path), "--env", "EPOCHS=8", "--env", "MP=11264"])
    )
    assert body["env"] == {"ML": "12288", "EPOCHS": "8", "MP": "11264"}


def test_an_env_value_may_contain_equals_signs():
    body = cli.build_submit_body(
        args_for(["submit", "--name", "x", "--image", "i", "--env", "ARGS=--lr=1e-5"])
    )
    assert body["env"] == {"ARGS": "--lr=1e-5"}


def test_an_env_pair_with_no_equals_is_refused():
    with pytest.raises(SystemExit, match="KEY=VALUE"):
        cli.build_submit_body(args_for(["submit", "--name", "x", "--image", "i", "--env", "OOPS"]))


def test_asking_by_vram_clears_a_model_name_from_the_file(tmp_path):
    # Sending both is what the server refuses, and the flag is the newer intent.
    path = tmp_path / "job.yaml"
    path.write_text("name: x\nimage: img\ngpu:\n  name: L40S\n  count: 1\n")
    body = cli.build_submit_body(args_for(["submit", "-f", str(path), "--gpu-vram", "80"]))
    assert body["gpu"] == {"vram_gb": 80, "count": 1}


def test_asking_by_model_clears_a_vram_floor_from_the_file(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: x\nimage: img\ngpu:\n  vram_gb: 48\n")
    body = cli.build_submit_body(args_for(["submit", "-f", str(path), "--gpu-name", "L40S"]))
    assert body["gpu"] == {"name": "L40S"}


def test_both_gpu_flags_at_once_is_refused_rather_than_silently_resolved():
    # Found by running the CLI against the server on 2026-08-31: the two
    # branches erased each other and whichever ran last won, so a contradictory
    # command submitted a job to a GPU the user had not asked for.
    with pytest.raises(SystemExit, match="two different ways"):
        cli.build_submit_body(
            args_for(["submit", "--name", "x", "--image", "i",
                      "--gpu-vram", "48", "--gpu-name", "L40S"])
        )


def test_a_gpu_count_alone_still_applies_to_the_files_style(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: x\nimage: img\ngpu:\n  vram_gb: 48\n")
    body = cli.build_submit_body(args_for(["submit", "-f", str(path), "--gpu-count", "2"]))
    assert body["gpu"] == {"vram_gb": 48, "count": 2}


def test_secrets_are_deduplicated_and_sorted(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: x\nimage: img\nsecrets: [GITHUB_PAT]\n")
    body = cli.build_submit_body(
        args_for(["submit", "-f", str(path), "--secret", "GITHUB_PAT", "--secret", "HF_TOKEN"])
    )
    assert body["secrets"] == ["GITHUB_PAT", "HF_TOKEN"]


def test_a_json_file_works_as_well_as_yaml(tmp_path):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"name": "x", "image": "img"}))
    assert cli.build_submit_body(args_for(["submit", "-f", str(path)]))["image"] == "img"


def test_a_missing_name_or_image_says_which_flags_supply_them():
    with pytest.raises(SystemExit, match="--name"):
        cli.build_submit_body(args_for(["submit", "--image", "img"]))


def test_a_file_that_is_a_list_is_refused(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("- name: x\n")
    with pytest.raises(SystemExit, match="must contain a mapping"):
        cli.build_submit_body(args_for(["submit", "-f", str(path)]))


def test_a_missing_file_is_one_line_not_a_traceback():
    with pytest.raises(SystemExit, match="cannot read"):
        cli.build_submit_body(args_for(["submit", "-f", "/no/such/file.yaml"]))


# ---------------------------------------------------------------------- commands


def test_submit_prints_the_id_the_results_and_how_to_follow(fake, capsys):
    assert run(["submit", "--name", "bank-exp2", "--image", "img"]) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "job-a8acdef80a07" in printed
    assert "s3://bucket/pacsrun/lab-alice/" in printed
    assert "ddpsrun logs job-a8acdef80a07 --follow" in printed


def test_submit_with_json_prints_the_raw_response(fake, capsys):
    assert run(["submit", "--name", "x", "--image", "i", "--json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out) == fake.submit_result


def test_a_job_nobody_has_looked_at_yet_does_not_print_a_blank_phase(fake, capsys):
    fake.status_result = {"job_id": "job-a8acdef80a07", "name": "x", "phase": ""}
    run(["status", "job-a8acdef80a07"])
    assert "accepted, not yet started" in capsys.readouterr().out


def test_status_calls_a_restart_what_it_is(fake, capsys):
    # A user who reads "restarts 3" without the explanation concludes their job
    # is broken. It is not: rented capacity gets reclaimed.
    fake.status_result = {
        "job_id": "job-a8acdef80a07", "name": "x", "phase": "Running",
        "recovery_count": 3, "gpu": "L40S", "vendor": "runpod",
    }
    run(["status", "job-a8acdef80a07"])
    printed = capsys.readouterr().out
    assert "L40S (runpod)" in printed
    assert "the machine was reclaimed" in printed


def test_logs_prints_every_line_without_its_timestamp(fake, capsys):
    # The timestamp is bookkeeping for the next request, not something the user
    # asked to read.
    assert run(["logs", "job-a8acdef80a07"]) == cli.EXIT_OK
    assert capsys.readouterr().out == "line one\nline two\n"


def test_logs_without_follow_asks_once(fake, capsys, monkeypatch):
    calls = []
    original = fake.log_window
    monkeypatch.setattr(fake, "log_window",
                        lambda *a, **k: (calls.append(k), original(*a, **k))[1])
    run(["logs", "job-a8acdef80a07"])
    assert len(calls) == 1


def test_follow_asks_again_and_prints_only_what_is_new(fake, capsys, monkeypatch):
    # The server cannot stream, so this is what --follow actually does.
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    rounds = {"n": 0}
    original = fake.log_window

    def growing(job_id, since=None, window_seconds=30):
        rounds["n"] += 1
        if rounds["n"] == 2:
            fake.log_lines.append("2026-09-01T00:00:03.000Z line three")
        if rounds["n"] > 2:
            raise KeyboardInterrupt
        return original(job_id, since=since, window_seconds=window_seconds)

    monkeypatch.setattr(fake, "log_window", growing)
    assert run(["logs", "job-a8acdef80a07", "--follow"]) == cli.EXIT_OK
    printed = capsys.readouterr().out
    # Three lines total, and none of them twice.
    assert printed.count("line one") == 1
    assert printed.count("line two") == 1
    assert printed.count("line three") == 1


def test_a_server_refusal_becomes_exit_1_and_its_own_message(fake, capsys, monkeypatch):
    def refuse(body):
        raise ServerError("there is no secret called 'NOPE'. Available: GITHUB_PAT")

    monkeypatch.setattr(fake, "submit", refuse)
    assert run(["submit", "--name", "x", "--image", "i"]) == cli.EXIT_SERVER
    assert "GITHUB_PAT" in capsys.readouterr().err


def test_no_credentials_is_exit_2_and_names_the_login_command(capsys, monkeypatch):
    monkeypatch.delenv(config.SERVER_ENV)
    monkeypatch.delenv(config.TOKEN_ENV)
    assert run(["status", "job-a8acdef80a07"]) == cli.EXIT_USAGE
    assert "ddpsrun login" in capsys.readouterr().err


def test_ctrl_c_while_following_is_not_a_failure(fake, capsys, monkeypatch):
    # Stopping the watch does not touch the job, which keeps running.
    def interrupted(job_id, since=None, window_seconds=30):
        raise KeyboardInterrupt

    monkeypatch.setattr(fake, "log_window", interrupted)
    assert run(["logs", "job-a8acdef80a07", "--follow"]) == cli.EXIT_OK


def test_no_command_prints_help_and_exits_2(capsys):
    assert run([]) == cli.EXIT_USAGE
    assert "ddpsrun" in capsys.readouterr().out


# --------------------------------------------------- stage 3: estimate, validate


def test_the_training_facts_go_under_training_not_at_the_top_level():
    body = cli.build_submit_body(
        args_for(["estimate", "--name", "x", "--image", "i",
                  "--pairs", "1110", "--epochs", "4", "--row-tokens", "4100",
                  "--cap", "12288", "--resumable"])
    )
    assert body["training"] == {
        "pairs": 1110, "epochs": 4, "row_tokens": 4100, "cap": 12288, "resumable": True,
    }
    assert "pairs" not in body


def test_training_facts_in_a_file_are_kept_and_a_flag_overrides_one(tmp_path):
    path = tmp_path / "job.yaml"
    path.write_text("name: x\nimage: i\ntraining:\n  pairs: 1110\n  epochs: 4\n  cap: 12288\n")
    body = cli.build_submit_body(args_for(["estimate", "-f", str(path), "--epochs", "8"]))
    assert body["training"] == {"pairs": 1110, "epochs": 8, "cap": 12288}


def test_the_script_flag_sends_the_text_not_the_path(tmp_path):
    # The server cannot read a file on the user's laptop.
    path = tmp_path / "run.sh"
    path.write_text("set -euo pipefail\npython train.py\n")
    body = cli.build_submit_body(
        args_for(["validate", "--name", "x", "--image", "i", "--script", str(path)])
    )
    assert body["script"] == "set -euo pipefail\npython train.py\n"
    assert str(path) not in str(body)


def test_a_missing_script_file_is_one_line_not_a_traceback():
    with pytest.raises(SystemExit, match="cannot read"):
        cli.build_submit_body(
            args_for(["validate", "--name", "x", "--image", "i", "--script", "/no/such.sh"])
        )


def test_estimate_prints_the_range_the_basis_and_the_gpu_reason(fake, capsys):
    fake.estimate_result = {
        "steps": 556,
        "hours": {"low": 6.52, "high": 6.87, "confidence": "measured"},
        "cost_usd": {"low": 6.45, "high": 6.8},
        "basis": "5 run(s) on L40S within 15% of 4,100 tokens per response",
        "gpu": {"recommended": "L40S", "recommended_vram_gb": 48,
                "peak_logits_gib": 6.96, "reason": "the logits buffer reaches 6.96 GiB"},
        "capacity_type": "on-demand",
        "capacity_reason": "RunPod does not sell spot",
        "warnings": ["Vendor prices move."],
    }
    assert run(["estimate", "--name", "x", "--image", "i"]) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "556" in printed
    assert "6.52 ~ 6.87" in printed
    assert "$6.45 ~ $6.8" in printed
    assert "5 run(s) on L40S" in printed
    assert "on-demand" in printed


def test_an_unknown_estimate_says_so_instead_of_printing_a_blank(fake, capsys):
    # Printing nothing where a number belongs is how a reader assumes zero.
    fake.estimate_result = {
        "steps": 556,
        "hours": {"low": None, "high": None, "confidence": "unknown"},
        "cost_usd": {"low": None, "high": None},
        "basis": "outside what we have measured",
        "gpu": {"recommended": None, "recommended_vram_gb": None,
                "peak_logits_gib": 0.0, "reason": "we cannot say without --max-len"},
        "capacity_type": "on-demand", "capacity_reason": "x", "warnings": [],
    }
    run(["estimate", "--name", "x", "--image", "i"])
    printed = capsys.readouterr().out
    assert "모름" in printed
    assert "unknown" in printed


def test_validate_exits_1_when_something_would_actually_stop_the_job(fake, capsys):
    # So a script can gate a submit on it.
    fake.validate_result = {
        "ok": False,
        "findings": [{"level": "error", "code": "gpu-too-small",
                      "message": "this asks for 48 GB", "fix": "ask for 80 GB"}],
        "not_checked": ["your repository's real layout"],
    }
    assert run(["validate", "--name", "x", "--image", "i"]) == cli.EXIT_SERVER
    printed = capsys.readouterr().out
    assert "gpu-too-small" in printed
    assert "ask for 80 GB" in printed
    assert "못 봄" in printed


def test_validate_exits_0_when_nothing_is_an_error(fake):
    fake.validate_result = {"ok": True, "findings": [], "not_checked": []}
    assert run(["validate", "--name", "x", "--image", "i"]) == cli.EXIT_OK


def test_estimate_validate_and_submit_take_the_same_flags():
    # A user must be able to check a job and then submit THAT job with nothing
    # rewritten. Three separately-written argument lists would drift.
    parser = cli.build_parser()
    shared = ["--name", "x", "--image", "i", "--gpu-vram", "48", "--env", "A=1",
              "--secret", "GITHUB_PAT", "--pairs", "10", "--cap", "12288"]
    bodies = [
        cli.build_submit_body(parser.parse_args([command] + shared))
        for command in ("estimate", "validate", "submit")
    ]
    assert bodies[0] == bodies[1] == bodies[2]


# ------------------------------------------------------------ stage 4: watch


def test_the_progress_bar_fills_in_proportion():
    assert cli.bar(0, width=10) == "----------"
    assert cli.bar(50, width=10) == "#####-----"
    assert cli.bar(100, width=10) == "##########"


def test_a_percentage_outside_the_range_does_not_overflow_the_bar():
    # A malformed progress line could produce anything; the bar must stay the
    # width it was asked for.
    assert len(cli.bar(-10, width=10)) == 10
    assert len(cli.bar(1000, width=10)) == 10


def test_watch_prints_progress_and_gpu(fake, capsys):
    fake.metrics_result = {
        "latest_gpu": {"utilization_percent": 94, "memory_used_mib": 38200,
                       "memory_total_mib": 45440, "memory_percent": 84.1,
                       "temperature_c": 71, "power_w": 298.5},
        "gpu_series": [{}] * 120,
        "progress": {"step": 350, "total_steps": 556, "percent": 62.9,
                     "seconds_per_step": 41.57, "elapsed": "4:02:35",
                     "remaining": "2:22:44", "projected_total_hours": 6.42,
                     "steady": True},
        "window_seconds": 3600, "note": "",
    }
    assert run(["watch", "job-a8acdef80a07"]) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "350 / 556" in printed
    assert "6.42 시간" in printed
    assert "38,200 / 45,440 MiB" in printed
    assert "120 개" in printed


def test_an_unsettled_projection_is_labelled_rather_than_stated(fake, capsys):
    fake.metrics_result = {
        "progress": {"step": 5, "total_steps": 556, "percent": 0.9,
                     "seconds_per_step": 45.74, "elapsed": "04:34",
                     "remaining": "6:52:00", "projected_total_hours": 7.06,
                     "steady": False},
        "gpu_series": [], "window_seconds": 3600, "note": "",
    }
    run(["watch", "job-a8acdef80a07"])
    assert "아직 안 정해짐" in capsys.readouterr().out


def test_an_empty_window_prints_the_note_that_explains_it(fake, capsys):
    fake.metrics_result = {
        "gpu_series": [], "window_seconds": 3600,
        "note": "no GPU readings and no progress lines in this window.",
    }
    assert run(["watch", "job-a8acdef80a07"]) == cli.EXIT_OK
    assert "no GPU readings" in capsys.readouterr().out


def test_parallelism_and_gpu_count_are_different_things():
    # --parallelism is pods, --gpu-count is GPUs per pod. Confusing them is how a
    # request for 8 workers becomes a request for one 8-GPU pod.
    body = cli.build_submit_body(
        args_for(["submit", "--name", "x", "--image", "i",
                  "--parallelism", "8", "--gpu-vram", "48", "--gpu-count", "2"])
    )
    assert body["parallelism"] == 8
    assert body["gpu"] == {"vram_gb": 48, "count": 2}


def test_no_parallelism_flag_leaves_the_field_out():
    body = cli.build_submit_body(args_for(["submit", "--name", "x", "--image", "i"]))
    assert "parallelism" not in body


# ------------------------------------------------------------ stage 4b: stats


def test_stats_prints_a_row_per_member_and_a_total(fake, capsys):
    fake.stats_result = {
        "team": "ddps",
        "members": [
            {"user": "alice", "jobs": 3, "succeeded": 2, "failed": 1, "running": 0,
             "gpu_hours": 9.54, "cost_usd": 9.44, "unpriced_jobs": 0},
            {"user": "bob", "jobs": 1, "succeeded": 1, "failed": 0, "running": 0,
             "gpu_hours": 2.0, "cost_usd": 1.98, "unpriced_jobs": 0},
        ],
        "jobs": 4, "gpu_hours": 11.54, "cost_usd": 11.42, "unpriced_jobs": 0, "note": "",
    }
    assert run(["stats"]) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "팀 ddps" in printed
    assert "alice" in printed and "bob" in printed
    assert "$11.42" in printed


def test_stats_prints_the_note_when_the_total_is_incomplete(fake, capsys):
    fake.stats_result = {
        "team": "ddps", "members": [], "jobs": 0, "gpu_hours": 0.0, "cost_usd": 0.0,
        "unpriced_jobs": 2, "note": "2 job(s) ran on a machine we have no measured price for",
    }
    run(["stats"])
    assert "no measured price" in capsys.readouterr().out


def test_the_capacity_type_flag_reaches_the_body():
    # The caller decides this. The server refuses a submit without it rather
    # than choosing, because a wrong value is invisible until the job has
    # already run somewhere the user did not intend.
    body = cli.build_submit_body(
        args_for(["submit", "--name", "x", "--image", "i", "--capacity-type", "spot"])
    )
    assert body["capacity_type"] == "spot"


def test_no_capacity_type_flag_leaves_the_field_out_so_the_server_can_refuse():
    body = cli.build_submit_body(args_for(["submit", "--name", "x", "--image", "i"]))
    assert "capacity_type" not in body


def test_a_capacity_type_the_flag_does_not_know_is_refused_at_the_cli():
    with pytest.raises(SystemExit):
        args_for(["submit", "--name", "x", "--image", "i", "--capacity-type", "reserved"])


def test_estimate_tells_you_the_flag_to_use(fake, capsys):
    fake.estimate_result = {
        "steps": 556, "hours": {"low": 6.5, "high": 6.9, "confidence": "measured"},
        "cost_usd": {"low": 6.4, "high": 6.8}, "basis": "measured",
        "gpu": {"recommended": "L40S", "recommended_vram_gb": 48,
                "peak_logits_gib": 6.96, "reason": "fits"},
        "capacity_type": "on-demand", "capacity_reason": "RunPod does not sell spot",
        "warnings": [],
    }
    run(["estimate", "--name", "x", "--image", "i"])
    assert "--capacity-type on-demand" in capsys.readouterr().out
