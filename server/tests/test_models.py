"""The translation from a submit request into a PacsJob, and back into a response.

This is where the isolation promise is actually kept, so most of these tests
check that something a caller sent did NOT end up in the object.
"""

import pytest
from pydantic import ValidationError

from ddpsrun_server import naming
from ddpsrun_server.auth import Principal
from ddpsrun_server.config import SecretBinding, Settings
from ddpsrun_server.models import (
    KNOWN_VENDORS,
    JobView,
    SubmitRequest,
    to_pacsjob,
)

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


# ---------------------------------------------------------------- DDPSRUN-VENDOR-CHOICE


def test_the_caller_may_name_the_vendors_and_the_mode():
    """PACSrun's CRD has had spec.placement.vendors and .mode all along; this gateway wrote
    neither.

    It sent `placement: {capacityType}` and nothing else, so "run this on RunPod" and "price
    every vendor and buy nothing" could not be said through this API at all -- even though the
    CRD documents both, and one of them (mode: compare) is the only way to reach the four
    vendors that can be priced and never rented.
    """
    request = SubmitRequest(
        name="n", image="img", vendors=["aws", "runpod"], placement_mode="cheapest"
    )
    obj = to_pacsjob(request, ALICE, SETTINGS, JOB_ID, capacity_type="spot")
    assert obj["spec"]["placement"] == {
        "capacityType": "spot",
        "vendors": ["aws", "runpod"],
        "mode": "cheapest",
    }


def test_vendors_alone_still_produce_a_placement_block():
    """Before this change the block existed ONLY when a capacity type did.

    So the three fields cannot be three separate `if`s appending to a dict that may not have
    been created -- which is why to_pacsjob builds one dict and writes it once.
    """
    request = SubmitRequest(name="n", image="img", vendors=["runpod"])
    obj = to_pacsjob(request, ALICE, SETTINGS, JOB_ID, capacity_type=None)
    assert obj["spec"]["placement"] == {"vendors": ["runpod"]}


def test_a_job_naming_nothing_has_no_placement_at_all():
    """Byte-for-byte the old behaviour, which is what every job written before today did."""
    request = SubmitRequest(name="n", image="img")
    obj = to_pacsjob(request, ALICE, SETTINGS, JOB_ID, capacity_type=None)
    assert "placement" not in obj["spec"]


def test_an_unrecognised_vendor_is_refused_and_the_message_lists_the_real_ones():
    """A typo must be an error and not a skip, for the reason PACSrun's own validateVendors gives.

    An unrecognised word matches no placement candidate, so ignoring it would leave the walk
    with nothing that vendor covers -- and the job would then run somewhere the user did not
    name, silently. Refusing here turns a Kubernetes enum violation at submit time into a 400
    that says which field to fix.
    """
    with pytest.raises(ValidationError) as caught:
        SubmitRequest(name="n", image="img", vendors=["oracle"])
    message = str(caught.value)
    assert "oracle" in message
    for name in KNOWN_VENDORS:
        assert name in message


def test_a_vendor_named_twice_is_refused():
    """A list that says aws twice is a user who edited it and lost track, not an intent."""
    with pytest.raises(ValidationError) as caught:
        SubmitRequest(name="n", image="img", vendors=["aws", "runpod", "aws"])
    assert "aws" in str(caught.value)


def test_an_unrecognised_mode_is_refused_rather_than_defaulted():
    """The mirror of the vendor rule, and the stronger case of the two.

    An unrecognised vendor eventually kills the job. An unrecognised MODE has a perfectly good
    default sitting behind it, so reading "cheapets" as "ordered" produces a job that runs,
    succeeds, and never compared anything -- the user gets the old failover walk while believing
    they bought the cheapest answer.
    """
    with pytest.raises(ValidationError):
        SubmitRequest(name="n", image="img", placement_mode="cheapets")

# ---------------------------------------------------------------- DDPSRUN-WORKLOAD-SA


def test_the_default_service_account_is_the_one_the_role_trusts(monkeypatch):
    """The default was an IAM ROLE's name in a ServiceAccount's slot, and it broke every AWS job.

    WHAT spec.serviceAccountName IS FOR, which is where the mistake started. It is put on the
    job's DRIVER POD, and the driver pod's identity is what RENTS THE MACHINE -- not, as
    config/deploy/workload-sa.yaml used to say, merely what lets the workload upload its
    results. So a name no role trusts is not a smaller failure later; it is exit 10 before
    anything is bought:

        configuration error: PACSRUN_AWS_ZONE is unusable: ... AccessDenied ... Not authorized
        to perform sts:AssumeRoleWithWebIdentity

    MEASURED 2026-09-08 on job ddpsrun-24547306294e, submitted from the New job screen. The
    solve was clean (`aws g6.xlarge usw2-az4`) and the driver died at +0.31s in its own
    configuration check. The same request with `pacsjob-writer` reached Running and rented a
    gr6.4xlarge.

    WHY THE NAME CANNOT BE CHOSEN FREELY. The role is assumed through EKS Pod Identity and the
    association is `default/pacsjob-writer -> role/pacsrun-workload`; the role's trust policy
    names exactly one namespace/ServiceAccount pair. PACSrun's own config/deploy/README.md step
    3: "role의 trust policy가 그 namespace/ServiceAccount 조합 하나만 신뢰하므로, 다른 SA로
    돌리면 STS가 거절한다."
    """
    monkeypatch.setenv("DDPSRUN_RESULT_BUCKET", "b")
    monkeypatch.setenv("DDPSRUN_TOKENS_PATH", "/etc/ddpsrun/tokens.json")
    monkeypatch.delenv("DDPSRUN_SERVICE_ACCOUNT", raising=False)

    settings = Settings.from_env()
    assert settings.service_account == "pacsjob-writer", (
        "the default must be the ServiceAccount PACSrun's terraform wired to the role, not the "
        "role's own name"
    )

    # AND IT IS STILL A SETTING. A deployment whose terraform output differs has to be able to
    # say so, which is why this is a default and not a constant.
    monkeypatch.setenv("DDPSRUN_SERVICE_ACCOUNT", "some-other-sa")
    assert Settings.from_env().service_account == "some-other-sa"


# ------------------------------------------------- DDPSRUN-SCRIPTS: it has to RUN


def _submitted(**kwargs):
    """Turn a submit body into the PacsJob the server would create."""
    from ddpsrun_server import naming
    from ddpsrun_server.auth import Principal
    from ddpsrun_server.config import Settings
    from ddpsrun_server.models import JudgementRequest, to_pacsjob

    settings = Settings.from_env({"DDPSRUN_RESULT_BUCKET": "b",
                                  "DDPSRUN_TOKENS_PATH": "t"})
    request = JudgementRequest(name="x", image="i", capacity_type="spot", **kwargs)
    return to_pacsjob(request, Principal(user="u", namespace="default", team="d"),
                      settings, naming.new_job_id(), "spot")["spec"]


def test_a_script_with_nothing_else_is_what_runs():
    """★ THE TRAP THIS CLOSES, and the agent skill walked straight into it.

    `script` was a VALIDATE-ONLY field: four checks read the text and the submit
    path threw it away. So `ddpsrun submit --script run.sh` created a job with no
    command and no args -- the operator then refuses to build the driver pod
    ("nothing to run") AFTER the job has been accepted.

    agent/skills/ddpsrun/SKILL.md said: step 1 write a run.sh, step 3 validate it
    with --script, step 4 submit. Nothing said the script had to reach `submit`
    too. An agent following it wrote a script, checked it, submitted it, and the
    script never ran.
    """
    spec = _submitted(script="python train.py --epochs 4")
    assert spec["args"] == ["bash", "-lc", "python train.py --epochs 4"]
    assert "command" not in spec


def test_the_shape_is_the_one_the_scripts_route_reads_back():
    """['bash','-lc',text] is not an arbitrary choice: it is what the screen sends
    and the only shape GET /v1/scripts recognises. A job submitted with --script
    therefore appears on the Scripts screen, which it would not if this wrapped
    the text any other way."""
    spec = _submitted(script="echo hi")
    args = spec["args"]
    assert len(args) == 3 and args[0] == "bash" and args[1] == "-lc"


def test_an_explicit_args_still_wins_over_the_script():
    """Deliberate: some jobs fetch their script inside the container, so a caller
    who named `args` meant it. The script is then only CHECKED."""
    spec = _submitted(script="python train.py", args=["bash", "-lc", "echo other"])
    assert spec["args"] == ["bash", "-lc", "echo other"]


def test_an_explicit_command_still_wins_too():
    """Same reasoning, and it must not end up with BOTH -- a command and a
    script-derived args on one job would run the command and silently ignore the
    script, which is the confusing half of what was wrong before."""
    spec = _submitted(script="python train.py", command=["/entry.sh"])
    assert spec["command"] == ["/entry.sh"]
    assert "args" not in spec


def test_no_script_and_no_command_still_carries_neither():
    """The image's own entrypoint. Unchanged, and the operator refuses it -- which
    is correct, because a job with nothing to run is a mistake and not a default."""
    spec = _submitted()
    assert "args" not in spec and "command" not in spec


def test_the_script_is_never_written_into_the_spec_as_its_own_field():
    """It goes in as `args` and nowhere else. A `spec.script` would be a second
    copy that the operator does not read and that could disagree with the first."""
    spec = _submitted(script="python train.py")
    assert "script" not in spec


def test_a_named_gpu_resolves_its_memory_from_the_choosable_list():
    """★ IT LOOKED IN THE WRONG TABLE, and two checks turned themselves off.

    `vram_gb_for` resolved a named GPU through `measurements.gpu_by_name`, which
    knows only the cards we have RENTED -- two of them. So the other twelve
    answered None, and `validate.check_memory` opens with
    `if cap is None or vram_gb is None: return []`.

    The consequence was silent and absurd: asking for an L4 BY NAME skipped the
    PYTORCH_CUDA_ALLOC_CONF check and the TRL-patch check, while asking for the
    same card by `vram_gb: 24` ran them. Two of validate's checks depended on
    which of two equivalent spellings you used.
    """
    from ddpsrun_server.models import JudgementRequest, vram_gb_for

    def asked(**gpu):
        return vram_gb_for(JudgementRequest(name="x", image="i",
                                            capacity_type="spot", gpu=gpu))

    # All fourteen choosable cards resolve, not just the two rented ones.
    assert asked(name="L4") == 24
    assert asked(name="H100") == 80
    assert asked(name="B300") == 288
    # The two that are in both tables are unchanged: both spell `vram_gb` as
    # "the number printed on the card".
    assert asked(name="L40S") == 48
    assert asked(name="A100-80GB") == 80
    # An explicit floor still wins over any name.
    assert asked(vram_gb=40) == 40
    # And a name nothing knows still declines to invent a figure --
    # `check_gpu_is_buyable` is what refuses it.
    assert asked(name="NoSuchCard") is None
    assert vram_gb_for(JudgementRequest(name="x", image="i",
                                        capacity_type="spot")) is None


def test_the_memory_checks_run_for_a_named_gpu_too():
    """The behaviour the fix above exists for, asserted through validate rather
    than through the helper: the same card named two ways gets the same checks."""
    from ddpsrun_server import estimate as estimator
    from ddpsrun_server import validate as validator
    from ddpsrun_server.models import JudgementRequest, cap_from, vram_gb_for

    def codes(**gpu):
        request = JudgementRequest(name="x", image="i", capacity_type="spot",
                                    gpu=gpu, script="python train.py\n",
                                    training={"cap": 12288})
        result = validator.validate(
            env={}, script=request.script, cap=cap_from(request),
            vram_gb=vram_gb_for(request),
            job_estimate=estimator.estimate(gpu_name="L4", cap=12288, pairs=None,
                                            epochs=None, row_tokens=None),
            gpu_name="L4", gpu_count=1, capacity_type="spot")
        return {f.code for f in result.findings}

    by_name = codes(name="L4")
    by_memory = codes(vram_gb=24)
    assert "alloc-conf-missing" in by_name
    assert "trl-patch-missing" in by_name
    assert by_name == by_memory


def test_a_script_too_big_for_the_job_object_is_refused_before_anything_is_rented():
    # DDPSRUN-SCRIPT-SIZE. The script travels inside spec.args, so it is stored
    # in etcd with the PacsJob and shares etcd's 1.5 MiB request limit. Without
    # a cap here the refusal arrives from the apiserver as "etcdserver: request
    # is too large", which names nothing the submitter can act on.
    import pydantic
    from ddpsrun_server.models import SCRIPT_MAX_CHARS, JudgementRequest

    ok = JudgementRequest(name="x", image="i", capacity_type="spot",
                          script="#" * SCRIPT_MAX_CHARS)
    assert ok.script is not None

    with pytest.raises(pydantic.ValidationError):
        JudgementRequest(name="x", image="i", capacity_type="spot",
                         script="#" * (SCRIPT_MAX_CHARS + 1))


def test_the_cap_leaves_room_for_the_scripts_people_actually_write():
    # baseline-c's real training script is 19,655 bytes (measured on the live
    # cluster 2026-09-08) and it does not even travel this way — it sits in S3
    # behind a 302-byte bootstrap. The cap is thirteen times that script.
    from ddpsrun_server.models import SCRIPT_MAX_CHARS

    assert SCRIPT_MAX_CHARS > 13 * 19_655
    # And a sixth of etcd's own limit, so the rest of the object still fits.
    assert SCRIPT_MAX_CHARS < 1.5 * 1024 * 1024 / 5
