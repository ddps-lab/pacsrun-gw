"""The translation from a submit request into a PacsJob, and back into a response.

This is where the isolation promise is actually kept, so most of these tests
check that something a caller sent did NOT end up in the object.
"""

import pytest
from pydantic import ValidationError

from ddpsrun_server import naming
from ddpsrun_server.auth import Principal
from ddpsrun_server.config import SecretBinding, Settings
from ddpsrun_server.models import JobView, SubmitRequest, to_pacsjob

ALICE = Principal(user="alice", namespace="lab-alice")

SETTINGS = Settings(
    result_bucket="<RESULT_BUCKET>",
    result_prefix="pacsrun/",
    service_account="pacsrun-workload",
    tokens_path="/etc/ddpsrun/tokens.json",
    secret_bindings={"GITHUB_PAT": SecretBinding("slm-rca-clone", "token")},
    log_tail_lines=2000,
)

JOB_ID = "job-a8acdef80a07"


def minimal(**overrides):
    body = {"name": "bank-exp2", "image": "runpod/pytorch:1.1.0"}
    body.update(overrides)
    return SubmitRequest(**body)


def test_the_server_fills_the_four_fields_the_user_cannot_send():
    obj = to_pacsjob(minimal(), ALICE, SETTINGS, JOB_ID)
    assert obj["metadata"]["namespace"] == "lab-alice"
    assert obj["spec"]["serviceAccountName"] == "pacsrun-workload"
    assert obj["spec"]["parallelism"] == 1
    assert obj["spec"]["resultPath"] == (
        "s3://<RESULT_BUCKET>/pacsrun/lab-alice/bank-exp2-a8acdef80a07/"
    )


def test_the_result_path_carries_the_namespace_from_the_token_only():
    # PACSrun's own guard requires the prefix s3://<bucket>/pacsrun/<namespace>/
    # (PACSRUN-RESULT-TENANCY). If this ever stops matching, every job a tenant
    # submits is refused on the cluster side.
    bob = Principal(user="bob", namespace="lab-bob")
    obj = to_pacsjob(minimal(), bob, SETTINGS, JOB_ID)
    assert obj["spec"]["resultPath"].startswith("s3://<RESULT_BUCKET>/pacsrun/lab-bob/")


def test_two_jobs_with_the_same_name_do_not_share_a_folder():
    first = to_pacsjob(minimal(), ALICE, SETTINGS, "job-aaaaaaaaaaaa")
    second = to_pacsjob(minimal(), ALICE, SETTINGS, "job-bbbbbbbbbbbb")
    assert first["spec"]["resultPath"] != second["spec"]["resultPath"]


def test_the_object_name_comes_from_the_job_id():
    obj = to_pacsjob(minimal(), ALICE, SETTINGS, JOB_ID)
    assert obj["metadata"]["name"] == naming.object_name(JOB_ID)


def test_env_is_sorted_and_shaped_the_way_the_crd_wants():
    obj = to_pacsjob(minimal(env={"ML": "12288", "EPOCHS": "4"}), ALICE, SETTINGS, JOB_ID)
    assert obj["spec"]["env"] == [
        {"name": "EPOCHS", "value": "4"},
        {"name": "ML", "value": "12288"},
    ]


def test_a_secret_becomes_a_reference_and_never_a_literal():
    obj = to_pacsjob(minimal(secrets=["GITHUB_PAT"]), ALICE, SETTINGS, JOB_ID)
    entry = [e for e in obj["spec"]["env"] if e["name"] == "GITHUB_PAT"][0]
    assert "value" not in entry
    assert entry["valueFrom"]["secretKeyRef"] == {"name": "slm-rca-clone", "key": "token"}


def test_an_unbound_secret_name_is_refused_and_the_message_lists_what_exists():
    with pytest.raises(ValueError, match="GITHUB_PAT"):
        to_pacsjob(minimal(secrets=["NO_SUCH_SECRET"]), ALICE, SETTINGS, JOB_ID)


def test_a_reserved_env_name_is_refused_at_the_edge():
    # PACSrun's controller refuses these too, but its error never reaches a user
    # who has no kubectl.
    with pytest.raises(ValidationError, match="PACSRUN_"):
        minimal(env={"PACSRUN_EXIT": "0"})


def test_the_same_name_in_env_and_secrets_is_refused():
    with pytest.raises(ValidationError, match="both env and secrets"):
        minimal(env={"GITHUB_PAT": "literal"}, secrets=["GITHUB_PAT"])


def test_a_gpu_ask_by_vram_matches_the_crd_shape():
    obj = to_pacsjob(minimal(gpu={"vram_gb": 48, "count": 1}), ALICE, SETTINGS, JOB_ID)
    assert obj["spec"]["resources"]["gpus"] == {"count": 1, "vramGB": 48}


def test_a_gpu_ask_by_model_name_matches_the_crd_shape():
    obj = to_pacsjob(minimal(gpu={"name": "L40S", "count": 2}), ALICE, SETTINGS, JOB_ID)
    assert obj["spec"]["resources"]["gpus"] == {"count": 2, "name": "L40S"}


def test_asking_both_ways_or_neither_is_refused():
    # Mirrors the CRD's own CEL rule at config/crd/pacsrun.io_pacsjobs.yaml:197.
    with pytest.raises(ValidationError, match="exactly one"):
        minimal(gpu={"vram_gb": 48, "name": "L40S"})
    with pytest.raises(ValidationError, match="exactly one"):
        minimal(gpu={"count": 1})


def test_no_capacity_type_writes_no_placement_at_all():
    # The stage-1 shape, still reachable: with no capacity_type the object
    # carries no placement and PACSrun applies its own defaults.
    obj = to_pacsjob(minimal(gpu={"vram_gb": 48}), ALICE, SETTINGS, JOB_ID)
    assert "placement" not in obj["spec"]


def test_a_capacity_type_is_written_into_placement():
    # Why this matters: an empty capacityType means spot, and RunPod's decider
    # declines anything that is not on-demand before it reads the catalogue.
    obj = to_pacsjob(minimal(gpu={"vram_gb": 48}), ALICE, SETTINGS, JOB_ID, "on-demand")
    assert obj["spec"]["placement"] == {"capacityType": "on-demand"}


def test_a_cpu_only_job_asks_for_no_gpu():
    obj = to_pacsjob(minimal(cpus="4", memory="16Gi"), ALICE, SETTINGS, JOB_ID)
    assert obj["spec"]["resources"] == {"cpus": "4", "memory": "16Gi"}


def test_expected_hours_is_recorded_but_not_acted_on():
    obj = to_pacsjob(minimal(expected_hours=8.0), ALICE, SETTINGS, JOB_ID)
    assert obj["metadata"]["annotations"]["ddpsrun.io/expected-hours"] == "8.0"
    assert "placement" not in obj["spec"]


def test_a_job_the_controller_has_not_touched_yet_still_renders():
    # The first second of every job's life: metadata exists, status does not.
    view = JobView.from_pacsjob(
        {"metadata": {"name": "ddpsrun-a8acdef80a07", "labels": {}}, "spec": {}}
    )
    assert view.job_id == JOB_ID
    assert view.phase == ""
    assert view.recovery_count == 0


def test_the_response_drops_the_internal_fields():
    view = JobView.from_pacsjob(
        {
            "metadata": {
                "name": "ddpsrun-a8acdef80a07",
                "namespace": "lab-alice",
                "labels": {naming.JOB_ID_LABEL: JOB_ID, naming.DISPLAY_NAME_LABEL: "bank-exp2"},
            },
            "spec": {
                "serviceAccountName": "pacsrun-workload",
                "resultPath": "s3://<RESULT_BUCKET>/pacsrun/lab-alice/bank-exp2-a8acdef80a07/",
            },
            "status": {
                "phase": "Running",
                "recoveryCount": 2,
                "currentOffering": {
                    "vendor": "runpod",
                    "instanceType": "L40S",
                    "region": "US-KS-2",
                    "zone": "US-KS-2a",
                },
                "blamedNodes": ["ip-10-0-1-5.us-west-2.compute.internal"],
                "excludedOfferings": [{"vendor": "aws", "instanceType": "g6.2xlarge"}],
            },
        }
    )
    rendered = view.model_dump()
    assert rendered["gpu"] == "L40S"
    assert rendered["vendor"] == "runpod"
    assert rendered["recovery_count"] == 2
    # Nothing that names our own infrastructure survives.
    flattened = str(rendered)
    for internal in ("pacsrun-workload", "US-KS-2a", "blamedNodes", "ip-10-0", "g6.2xlarge"):
        assert internal not in flattened

    # The ONE documented exception. result_path is the only way a stage-1 caller
    # can collect their output, and the namespace is part of that path. It must
    # appear there and nowhere else; when /v1/jobs/{id}/artifacts lands and hands
    # out download URLs instead, this field goes away and the exception with it.
    assert rendered["result_path"].count("lab-alice") == 1
    without_path = dict(rendered)
    without_path.pop("result_path")
    assert "lab-alice" not in str(without_path)


def test_a_korean_job_name_survives_the_round_trip():
    # A label value may hold only [A-Za-z0-9._-], so "은행 실험2" sanitises to "2".
    # The annotation is what carries the real name back to the user.
    obj = to_pacsjob(minimal(name="은행 실험2"), ALICE, SETTINGS, JOB_ID)
    assert obj["metadata"]["labels"][naming.DISPLAY_NAME_LABEL] == "2"
    assert obj["metadata"]["annotations"][naming.DISPLAY_NAME_ANNOTATION] == "은행 실험2"
    assert JobView.from_pacsjob(obj).name == "은행 실험2"


def test_the_name_falls_back_to_the_label_then_to_the_object_name():
    # An object written by an older server has no annotation; one applied with
    # kubectl by hand has neither.
    only_label = {"metadata": {"name": "ddpsrun-a8acdef80a07",
                               "labels": {naming.DISPLAY_NAME_LABEL: "bank-exp2"}}}
    assert JobView.from_pacsjob(only_label).name == "bank-exp2"
    neither = {"metadata": {"name": "hand-written-job"}}
    assert JobView.from_pacsjob(neither).name == "hand-written-job"


def test_parallelism_is_the_users_and_not_pinned_at_one():
    # It WAS pinned at 1, which silently removed the way a job fills a multi-GPU
    # machine: parallelism is independent worker pods, gpus.count is GPUs per
    # pod. A user asking for 8 workers got 1.
    obj = to_pacsjob(minimal(parallelism=8, gpu={"vram_gb": 48, "count": 2}), ALICE, SETTINGS, JOB_ID)
    assert obj["spec"]["parallelism"] == 8
    assert obj["spec"]["resources"]["gpus"] == {"count": 2, "vramGB": 48}


def test_parallelism_defaults_to_one():
    assert to_pacsjob(minimal(), ALICE, SETTINGS, JOB_ID)["spec"]["parallelism"] == 1


def test_parallelism_is_capped_at_what_the_crd_can_record():
    # status.completedSlots has maxItems 256, so a job with more slots than that
    # cannot record which of them finished.
    with pytest.raises(ValidationError):
        minimal(parallelism=257)
    with pytest.raises(ValidationError):
        minimal(parallelism=0)
