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
        self.log_lines = ["line one", "line two"]

    def submit(self, body):
        self.submitted = body
        return self.submit_result

    def status(self, job_id):
        return self.status_result

    def logs(self, job_id, follow=False):
        return iter(self.log_lines)

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


def test_logs_prints_every_line(fake, capsys):
    assert run(["logs", "job-a8acdef80a07"]) == cli.EXIT_OK
    assert capsys.readouterr().out == "line one\nline two\n"


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
    def interrupted(job_id, follow=False):
        raise KeyboardInterrupt

    monkeypatch.setattr(fake, "logs", interrupted)
    assert run(["logs", "job-a8acdef80a07", "--follow"]) == cli.EXIT_OK


def test_no_command_prints_help_and_exits_2(capsys):
    assert run([]) == cli.EXIT_USAGE
    assert "ddpsrun" in capsys.readouterr().out
