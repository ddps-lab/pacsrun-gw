"""Every check, and the incident it exists because of.

A test here that passes without asserting on the MESSAGE is worth little: a
finding that says "possible memory issue" is one a user learns to skim, and the
whole point is that each one names what happened last time.
"""

import pytest

from ddpsrun_server import estimate as e
from ddpsrun_server import validate as v

# A run we could estimate, used where the check under test does not care.
KNOWN = e.estimate(
    gpu_name="L40S", cap=12288, pairs=1110, epochs=4, row_tokens=4100, mitigations_on=True
)

SCRIPT_WITH_EVERYTHING = """\
set -euo pipefail
trap upload_everything EXIT
python patch_trl_liger_slice.py $(python -c "import trl.trainer.dpo_trainer as m; print(m.__file__)")
PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \\
  python train_dpo_m3.py --pairs $PAIRS --out adapter_$JOB --max-len 12288 --max-prompt-len 11264
python gen_openrca_tasks_fast.py --in $EVAL --out out_$JOB.jsonl --lora /root/ab/adapter_$JOB
"""


def run(env=None, script=None, cap=12288, vram_gb=48, job_estimate=KNOWN):
    return v.validate(
        env=env or {}, script=script, cap=cap, vram_gb=vram_gb, job_estimate=job_estimate
    )


def codes(result):
    return {finding.code for finding in result.findings}


def message_for(result, code):
    return next(f.message for f in result.findings if f.code == code)


# ------------------------------------------------------------------- memory


def test_a_missing_allocator_setting_names_the_run_that_died_without_it():
    result = run(env={}, script="python train.py")
    assert "alloc-conf-missing" in codes(result)
    assert "aiops-exp1" in message_for(result, "alloc-conf-missing")


def test_the_allocator_setting_counts_whether_it_is_env_or_a_command_prefix():
    # We have written it both ways across eight jobs.
    from_env = run(env={"PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"})
    from_script = run(script=SCRIPT_WITH_EVERYTHING)
    assert "alloc-conf-missing" not in codes(from_env)
    assert "alloc-conf-missing" not in codes(from_script)


def test_a_missing_trl_patch_gives_the_command_that_applies_it():
    result = run(script="python train_dpo_m3.py --max-len 12288")
    fix = next(f.fix for f in result.findings if f.code == "trl-patch-missing")
    assert "patch_trl_liger_slice.py" in fix


def test_48gb_without_the_mitigations_is_an_error_not_a_warning():
    # This is exactly the aiops-exp1 submission, and it burned four steps of
    # rented A100 time before it failed.
    result = run(env={}, script="python train.py --max-len 12288", vram_gb=48)
    assert "gpu-too-small" in codes(result)
    assert result.ok is False


def test_48gb_with_both_mitigations_is_accepted():
    result = run(script=SCRIPT_WITH_EVERYTHING, vram_gb=48)
    assert "gpu-too-small" not in codes(result)
    assert result.ok is True


def test_a_cpu_job_is_not_asked_about_gpu_memory():
    result = run(cap=None, vram_gb=None)
    assert "gpu-too-small" not in codes(result)
    assert "alloc-conf-missing" not in codes(result)


# --------------------------------------------------------------------- caps


def test_a_prompt_cap_at_or_above_the_sequence_cap_is_an_error():
    result = run(script="python train.py --max-len 12288 --max-prompt-len 12288")
    assert "prompt-cap-too-high" in codes(result)
    assert "0 tokens for the answer" in message_for(result, "prompt-cap-too-high")


def test_the_caps_we_actually_used_pass():
    for sequence, prompt in [(12288, 11264), (18432, 17408)]:
        result = run(script=f"python train.py --max-len {sequence} --max-prompt-len {prompt}")
        assert "prompt-cap-too-high" not in codes(result), (sequence, prompt)


# --------------------------------------------------------- adapter paths


def test_inference_reading_a_different_adapter_than_training_wrote_is_an_error():
    # The failure this prevents is expensive rather than confusing: training
    # runs to completion first, and on AIOps that is 31 hours.
    script = (
        "python train_dpo_m3.py --out adapter_bank_v2\n"
        "python gen_openrca_tasks_fast.py --lora /root/ab/adapter_bank\n"
    )
    result = run(script=script)
    assert "adapter-path-mismatch" in codes(result)
    assert "adapter_bank" in message_for(result, "adapter-path-mismatch")


def test_the_same_adapter_through_a_variable_passes():
    assert "adapter-path-mismatch" not in codes(run(script=SCRIPT_WITH_EVERYTHING))


def test_a_script_with_only_training_in_it_is_not_accused():
    assert "adapter-path-mismatch" not in codes(run(script="python train.py --out adapter_x"))


# ------------------------------------------------------------ partial results


def test_a_script_with_no_exit_trap_is_warned():
    result = run(script="python train.py")
    assert "no-exit-trap" in codes(result)


def test_a_script_with_a_trap_is_not():
    assert "no-exit-trap" not in codes(run(script=SCRIPT_WITH_EVERYTHING))


# ------------------------------------------------------------------ secrets


def test_something_that_looks_like_a_credential_in_env_is_an_error():
    result = run(env={"GITHUB_TOKEN": "somevalue"})
    assert "secret-in-env" in codes(result)
    assert result.ok is False
    fix = next(f.fix for f in result.findings if f.code == "secret-in-env")
    assert "`secrets`" in fix


def test_an_ordinary_variable_is_left_alone():
    assert "secret-in-env" not in codes(run(env={"EPOCHS": "4", "ML": "12288"}))


def test_an_empty_value_is_not_accused():
    # A name declared with no value is not a leaked credential.
    assert "secret-in-env" not in codes(run(env={"HF_TOKEN": ""}))


# ------------------------------------------------------------------ runtime


def test_a_job_past_twelve_hours_is_told_why_that_matters():
    long_run = e.estimate(
        gpu_name="L40S", cap=12288, pairs=3546, epochs=4, row_tokens=5600, mitigations_on=True
    )
    result = run(script=SCRIPT_WITH_EVERYTHING, job_estimate=long_run)
    assert "fetch-mode-needed" in codes(result)
    assert "12 hours" in message_for(result, "fetch-mode-needed")


def test_an_unestimatable_job_is_told_to_submit_anyway():
    unknown = e.estimate(gpu_name="H200", cap=12288, pairs=1110, epochs=4, row_tokens=4100)
    result = run(script=SCRIPT_WITH_EVERYTHING, job_estimate=unknown)
    fix = next(f.fix for f in result.findings if f.code == "runtime-unknown")
    assert "submit it anyway" in fix


# ---------------------------------------------------------------- reporting


def test_errors_come_before_warnings_and_warnings_before_notes():
    result = run(env={"HF_TOKEN": "leaked"}, script="python train.py --max-len 12288")
    levels = [finding.level for finding in result.findings]
    assert levels == sorted(levels, key=lambda level: {"error": 0, "warning": 1, "info": 2}[level])


def test_without_a_script_the_answer_says_what_it_could_not_look_at():
    result = run(script=None)
    assert any("you did not send one" in line for line in result.not_checked)


def test_a_clean_result_still_lists_what_no_check_can_see():
    # Three of the seven problems we hit were mismatches between a document and
    # a repository's real layout. A clean pass must not read as a complete one.
    result = run(script=SCRIPT_WITH_EVERYTHING)
    assert result.ok is True
    assert any("repository's real layout" in line for line in result.not_checked)


def test_every_finding_says_what_to_change_or_deliberately_does_not():
    result = run(env={"HF_TOKEN": "leaked"}, script="python train.py --max-len 12288")
    for finding in result.findings:
        assert finding.message
        # `info` may have nothing to change; anything stronger must.
        if finding.level != v.INFO:
            assert finding.fix, finding.code


# ---------------------------------------------------------------- DDPSRUN-VENDOR-CHOICE


def test_a_price_only_vendor_under_the_default_mode_is_warned_about():
    """Six vendor names are accepted and only two of them can run anything.

    gcp, azure, lambda and nebius are answered from the SkyPilot catalogue CSVs -- enough to
    state a price, nothing like enough to rent a machine, because no actuator in PACSrun
    understands their machine names. Under `ordered` (the default) such a vendor answering FIRST
    reaches the actuator and fails there; under `cheapest` it fails if it WINS, and takes the
    comparison down with it.
    """
    findings = v.check_vendors_can_run(["gcp"], None)
    codes = [f.code for f in findings]
    assert "vendor-cannot-run" in codes
    warned = next(f for f in findings if f.code == "vendor-cannot-run")
    assert warned.level == v.WARNING
    assert "gcp" in warned.message
    assert "compare" in warned.fix


def test_it_warns_and_does_not_refuse():
    """PACSrun's CRD allows the combination on purpose, so this service must not be stricter.

    The CRD's own words on spec.placement.vendors: "Listing one under mode: cheapest and having
    it WIN reaches the actuator and fails there, which is the right answer to 'buy me a thing
    nobody can buy'". Refusing here would overrule a judgement the layer below has already made;
    saying so before the money is spent is the part that was missing.
    """
    findings = v.check_vendors_can_run(["gcp", "azure"], "cheapest")
    assert all(f.level != v.ERROR for f in findings)


def test_compare_says_out_loud_that_nothing_is_bought():
    """The one mode where the job ends without the workload ever running.

    A user who picks it expecting a run gets a job in the terminal phase Compared, no machine and
    no output -- so the finding is what stands between "I got my prices" and "why did nothing
    happen".
    """
    findings = v.check_vendors_can_run(["aws", "runpod"], "compare")
    told = next(f for f in findings if f.code == "compare-buys-nothing")
    assert told.level == v.INFO
    assert "Compared" in told.message


def test_ranking_one_candidate_against_itself_is_pointed_out():
    """`cheapest` and `compare` rank the candidates against each other, and one is not a ranking."""
    codes = [f.code for f in v.check_vendors_can_run(["aws"], "cheapest")]
    assert "nothing-to-compare" in codes
    assert "nothing-to-compare" not in [
        f.code for f in v.check_vendors_can_run(["aws", "runpod"], "cheapest")
    ]


def test_the_runnable_two_under_the_default_mode_say_nothing_at_all():
    """The common case has to stay silent, or the findings list becomes something people skim."""
    assert v.check_vendors_can_run(["aws", "runpod"], "ordered") == []
    assert v.check_vendors_can_run(["aws"], None) == []
    assert v.check_vendors_can_run([], None) == []


def test_the_findings_reach_the_aggregator():
    """A check nothing calls is a check that never runs.

    validate() gained two arguments for this and the route threads both -- whether naming a
    price-only vendor is sensible depends entirely on the mode, so neither is useful alone.
    """
    result = v.validate(
        env={}, script=None, cap=12288, vram_gb=48, job_estimate=KNOWN,
        vendors=["nebius"], placement_mode="ordered",
    )
    assert "vendor-cannot-run" in [f.code for f in result.findings]


# ---------------------------------------------------------- E2E 1회차(2026-09-08)
# 아래 네 뭉치는 slm-rca 세션이 실제로 부딪힌 결함이다. 판정 전문은
# skypilot 루트의 `E2E-FINDINGS-2026-09-08.md`.


def test_a_secret_name_the_deployment_does_not_hold_is_blocking():
    """DDPSRUN-SECRET-NAMES. 없는 이름은 submit 이 거부하므로 여기서 error 다.

    그 세션은 `secrets: ["GITHUB_PAT"]` 로 validate 를 통과했고(EXIT=0),
    submit 에서야 거부를 봤다. 검사가 볼 수 있는 것을 안 봤다는 뜻이다.
    """
    result = v.validate(
        env={}, script=None, cap=None, vram_gb=None, job_estimate=KNOWN,
        secrets=["GITHUB_PAT"], known_secrets={},
    )
    finding = [f for f in result.findings if f.code == "secret-name-unknown"]
    assert len(finding) == 1
    assert finding[0].level == v.ERROR
    assert "GITHUB_PAT" in finding[0].message
    assert result.ok is False


def test_a_secret_name_the_deployment_holds_is_silent():
    result = v.validate(
        env={}, script=None, cap=None, vram_gb=None, job_estimate=KNOWN,
        secrets=["GITHUB_PAT"], known_secrets={"GITHUB_PAT": object()},
    )
    assert "secret-name-unknown" not in {f.code for f in result.findings}


def test_secret_names_are_not_judged_without_the_bindings():
    """known_secrets 를 못 주는 caller 는 검사를 아예 안 돌린다.

    둘 중 하나만 주면 모든 이름이 없는 이름으로 보이고, 그것은 검사가 아니라
    거짓말이다.
    """
    result = v.validate(
        env={}, script=None, cap=None, vram_gb=None, job_estimate=KNOWN,
        secrets=["GITHUB_PAT"],
    )
    assert "secret-name-unknown" not in {f.code for f in result.findings}


def test_runpod_only_job_is_not_judged_against_the_aws_catalogue():
    """A100 은 AWS 가 8장 machine 으로만 파는데, 그 사실이 RunPod job 과 무관하다."""
    aws_shaped = v.check_gpu_is_buyable("A100-80GB", 1, "on-demand", 1, None, [])
    runpod_only = v.check_gpu_is_buyable("A100-80GB", 1, "on-demand", 1, None, ["runpod"])
    assert "gpu-count-unfillable" in {f.code for f in aws_shaped}
    assert "gpu-count-unfillable" not in {f.code for f in runpod_only}


def test_naming_aws_explicitly_keeps_the_size_check():
    named = v.check_gpu_is_buyable("A100-80GB", 1, "on-demand", 1, None, ["aws", "runpod"])
    assert "gpu-count-unfillable" in {f.code for f in named}


def test_spot_without_aws_has_no_vendor_left():
    """spot + vendors=[runpod] 는 힌트가 아니라 모순이다. RunPod 은 spot 을 안 판다."""
    result = v.check_gpu_is_buyable("A100-80GB", 1, "spot", 1, None, ["runpod"])
    finding = [f for f in result if f.code == "spot-has-no-vendor"]
    assert len(finding) == 1
    assert finding[0].level == v.ERROR
    assert "spot-excludes-runpod" not in {f.code for f in result}


def test_a_ppo_script_hears_nothing_about_the_dpo_arithmetic():
    """★ 2026-09-09 에 답이 바뀌었다. INFO 한 줄에서 침묵으로.

    옛 판은 "이 patch 는 DPO 것이라 안 맞는다" 를 INFO 로 알려 줬다. 그것도
    개선이었지만 여전히 남의 recipe 를 이 job 의 화면에 올리는 것이다.
    DDPSRUN-CHECK-TIERS: `peak_logits_gib` 는 **한 trainer 에서 측정한 산술**이고,
    PPO 의 가장 큰 allocation 은 그것이 아니다. 그래서 recipe 층 전체가 꺼지고,
    못 봤다는 사실은 `not_checked` 가 말한다.
    """
    script = "python -m trl.trainer.ppo --max-len 2048 --max-prompt-len 1024"
    result = v.check_memory(2048, 40, alloc_on=True, patch_on=False,
                            trainer=v.trainer_in(script))
    codes = {f.code for f in result}
    assert "trl-patch-missing" not in codes, "DPO patch 를 권하지 않는다"
    assert "gpu-too-small" not in codes, (
        "DPO 표에서 나온 카드 권고도 하지 않는다 — 그 표는 PPO 를 재 본 적이 없다")
    assert codes == set(), f"recipe 층은 통째로 침묵한다: {codes}"


def test_the_allocator_warning_is_platform_and_survives_a_ppo_script():
    """allocator 설정은 recipe 와 무관하다 — 어떤 job 이든 큰 연속 할당을 한다."""
    script = "python -m trl.trainer.ppo --max-len 2048"
    result = v.check_memory(2048, 40, alloc_on=False, patch_on=False,
                            trainer=v.trainer_in(script))
    finding = [f for f in result if f.code == "alloc-conf-missing"]
    assert len(finding) == 1
    assert "fragments free memory" in finding[0].message, (
        "이유가 allocator 의 성질로 적혀 있다 — 어느 job 에나 참인 문장")
    assert "at cap" not in finding[0].message, (
        "★ **이 job 의** peak 를 계산해 내밀지 않는다. 옛 메시지는 "
        "'The logits buffer at cap 2,048 is N GiB' 였고, 그 산술은 한 trainer 를 "
        "재 본 것이라 PPO job 에 대해 정당화할 수 없다")
    assert "aiops-exp1" in finding[0].message, (
        "대신 실측 incident 을 출처와 함께 인용한다 — 그것이 이 설정이 중요한 이유의 근거이고, "
        "읽는 사람에게 '당신 job 에 logits buffer 가 있다' 고 말하지는 않는다")


def test_a_cpu_job_gets_no_allocator_warning_either():
    assert v.check_memory(2048, None, alloc_on=False, patch_on=False) == [], (
        "CPU job 에는 설정할 CUDA allocator 가 없다")


def test_a_dpo_script_still_gets_the_patch_warning():
    script = "python -m trl.trainer.dpo --max-len 2048"
    result = v.check_memory(2048, 40, alloc_on=True, patch_on=False,
                            trainer=v.trainer_in(script))
    assert "trl-patch-missing" in {f.code for f in result}


def test_an_unknown_trainer_still_gets_the_patch_warning():
    """trainer 를 못 읽으면 DPO 로 가정한다 -- 경고를 지우는 쪽이 더 위험하다."""
    result = v.check_memory(2048, 40, alloc_on=True, patch_on=False, trainer=None)
    assert "trl-patch-missing" in {f.code for f in result}


def test_a_second_aws_identity_is_named_before_the_run_starts():
    """DDPSRUN-AWS-COLLISION. Bedrock judge 와 결과 upload 가 같은 세 이름을 쓴다."""
    result = v.check_aws_credential_collision(
        {"JUDGE_AWS_ACCESS_KEY_ID": "AKIA..."}, [], None)
    assert len(result) == 1
    assert result[0].code == "aws-credential-collision"
    assert "AWS_SESSION_TOKEN" in result[0].message


def test_overwriting_the_result_credentials_is_named():
    result = v.check_aws_credential_collision({"AWS_ACCESS_KEY_ID": "AKIA..."}, [], None)
    assert result and result[0].code == "aws-credential-collision"


def test_an_export_inside_the_script_is_named_too():
    result = v.check_aws_credential_collision(
        {}, [], "export AWS_SECRET_ACCESS_KEY=$SOMETHING\npython train.py")
    assert result and result[0].code == "aws-credential-collision"


def test_a_job_with_no_aws_identity_of_its_own_is_silent():
    assert v.check_aws_credential_collision({"HF_TOKEN": "x"}, ["GITHUB_PAT"], "python x.py") == []


# ------------------------------------------- DDPSRUN-DEFERRED-SCRIPT
# 2026-09-09 결정 4번. `alloc-conf-missing` 이 오탐은 아니었지만 사용법이 갈렸다.


def test_a_launcher_script_gets_not_checked_instead_of_a_warning():
    """★ 조치할 수 없는 경고는 없는 경고보다 나쁘다.

    `run_C_wrapper.sh` 는 자기가 부르는 파일 세 줄 안에서 그 설정을 export 한다.
    제출된 텍스트만 보면 정말 없고, 검사는 자기가 본 것에 대해 틀리지 않았다 —
    그것이 무슨 뜻인지에 대해 틀렸다. 조치할 수 없는 경고를 읽은 사람은 이런
    경고를 잡음으로 배우고, 다음에 넘기는 경고는 진짜다.
    """
    script = "set -euo pipefail\nbash runs/run_C_wrapper.sh 2 2\n"
    result = run(script=script, env={})
    assert "alloc-conf-missing" not in codes(result)
    deferred = [line for line in result.not_checked if "PYTORCH_CUDA_ALLOC_CONF" in line]
    assert len(deferred) == 1, "대신 not_checked 에 한 줄이 온다"
    assert "run_C_wrapper.sh" in deferred[0], "어느 줄 때문인지 인용한다"
    assert "not reported as missing" in deferred[0]


def test_a_script_that_does_its_own_work_still_gets_the_warning():
    result = run(script="python -m trl.trainer.dpo --max-len 12288\n", env={})
    assert "alloc-conf-missing" in codes(result), (
        "스스로 학습을 돌리는 script 는 그 설정이 정말 없는 것이고, 그것은 실측된 OOM 이다")


def test_a_launcher_that_sets_it_anyway_is_silent_either_way():
    script = ("export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True\n"
              "bash runs/run_C.sh\n")
    result = run(script=script, env={})
    assert "alloc-conf-missing" not in codes(result)
    assert not any("PYTORCH_CUDA_ALLOC_CONF" in line for line in result.not_checked), (
        "보였고 켜져 있으므로 못 봤다고 말할 이유가 없다")


def test_the_launcher_rule_does_not_fire_on_a_mention():
    for text in ('echo "then run bash setup.sh"\npython train.py\n',
                 '# bash old_run.sh -- 옛 방식\npython train.py\n'):
        assert v.defers_to_another_script(text) is None, text


# ------------------------------------------- DDPSRUN-GROUP / DDPSRUN-CHECK-TIERS
# 2026-09-09. validate 가 한 recipe 에 편향돼 있었고, 정작 분산학습은 판단할
# 대상조차 없었다 -- `spec.group` 이 CRD 에는 있고 이 API 에는 없었다.


def test_a_job_with_no_group_hears_nothing_about_groups():
    """2026-09-05 이전에 쓰인 모든 job 이 이 모양이다. 회귀가 곧 전면 회귀다."""
    assert v.check_distributed("python train.py", 1, "independent", 1, 1) == []
    assert v.check_distributed("python train.py", 1, "independent", 8, 1) == []


def test_a_distributed_group_that_reads_none_of_the_coordinates_is_blocking():
    """★ 이 실패는 조용하다. 그래서 문장이 아니라 검사여야 한다.

    `remotek8s.py` 가 적어 둔 실측: "NEITHER RANK PRINTED ANYTHING. Both sat in
    dist.init_process_group with no error and no output." 모든 rank 가 아무도
    열지 않은 rendezvous 를 기다리고, 카드는 busy 로 읽히고, 3,600초 뒤
    stall detector 가 뜰 때까지 과금된다.
    """
    result = v.check_distributed(
        "torchrun --nproc_per_node 4 train.py", 2, "distributed", 4, 4)
    finding = [f for f in result if f.code == "group-coords-unread"]
    assert len(finding) == 1
    assert finding[0].level == v.ERROR
    assert "PACSRUN_MASTER_ADDR" in finding[0].message
    assert "torchrun --nnodes $PACSRUN_GROUP_SIZE" in finding[0].fix


def test_reading_the_coordinates_is_enough_to_pass():
    script = ("torchrun --nnodes $PACSRUN_GROUP_SIZE --node_rank $PACSRUN_GROUP_RANK "
              "--master_addr $PACSRUN_MASTER_ADDR --master_port $PACSRUN_MASTER_PORT "
              "--nproc_per_node 4 train.py")
    assert v.check_distributed(script, 2, "distributed", 4, 4) == []


def test_a_group_with_no_launcher_is_a_warning_and_not_an_error():
    """framework 가 스스로 init_process_group 을 부르는 경우가 있어 error 가 아니다."""
    script = "python train.py  # PACSRUN_MASTER_ADDR is read inside\n"
    result = v.check_distributed(script, 4, "distributed", 8, 1)
    finding = [f for f in result if f.code == "no-distributed-launcher"]
    assert len(finding) == 1
    assert finding[0].level == v.WARNING
    assert "4 machines to do 4 unrelated single-GPU runs" in finding[0].message


def test_the_launchers_world_size_must_match_what_was_bought():
    script = ("torchrun --nproc_per_node 8 --nnodes 4 --master_addr "
              "$PACSRUN_MASTER_ADDR train.py")
    codes = {f.code for f in v.check_distributed(script, 2, "distributed", 4, 4)}
    assert "world-size-mismatch" in codes, "8 프로세스인데 카드는 4장"
    assert "nnodes-mismatch" in codes, "4 node 인데 group 은 2 pod"


def test_a_group_that_does_not_divide_the_parallelism_is_refused_before_the_crd():
    result = v.check_distributed(None, 3, "distributed", 8, 1)
    finding = [f for f in result if f.code == "group-does-not-divide"]
    assert len(finding) == 1
    assert finding[0].level == v.ERROR
    assert "8" in finding[0].message and "3" in finding[0].message


def test_a_script_with_no_way_out_is_named():
    """가장 비싼 실수이고 2026-09-09 까지 아무도 검사하지 않았다."""
    result = v.check_results_leave("python train.py --epochs 3\n")
    assert len(result) == 1
    assert result[0].code == "nothing-leaves-the-container"
    assert result[0].level == v.WARNING, (
        "error 가 아니다 -- 출력이 로그뿐인 추론 실험이나 metric 만 남기는 job 은 정당하다")


def test_any_of_the_three_ways_out_is_enough():
    for script in ('echo "PACSRUN_ARTIFACT=/root/out.tar"\n',
                   'aws s3 cp out.tar "$RESULT_PATH"\n',
                   'python - <<PY\nimport os; print(os.environ["PACSRUN_RESULT_PATH"])\nPY\n',
                   'boto3.client("s3").upload_file(...)\n'):
        assert v.check_results_leave(script) == [], script


def test_the_recipe_tier_is_silent_for_a_job_that_is_not_that_recipe():
    """★ 사용자의 지적 그 자체. 분산 pretraining job 이 DPO 얘기를 듣지 않는다."""
    script = ("torchrun --nnodes $PACSRUN_GROUP_SIZE --node_rank $PACSRUN_GROUP_RANK "
              "--master_addr $PACSRUN_MASTER_ADDR --nproc_per_node 4 pretrain.py\n"
              "export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True\n"
              "trap 'echo PACSRUN_ARTIFACT=/root/ckpt.pt' EXIT\n")
    result = v.validate(env={}, script=script, cap=None, vram_gb=80,
                        job_estimate=KNOWN, gpu_name="A100-80GB", gpu_count=4,
                        capacity_type="on-demand", parallelism=4,
                        group_size=2, group_mode="distributed")
    codes = {f.code for f in result.findings}
    for recipe_code in ("trl-patch-missing", "prompt-cap-too-high",
                        "adapter-path-mismatch", "gpu-too-small"):
        assert recipe_code not in codes, f"{recipe_code} 가 이 job 에 나왔다: {codes}"
    assert "alloc-conf-missing" not in codes, "그 job 은 스스로 켰다"
    assert "nothing-leaves-the-container" not in codes
    assert "no-exit-trap" not in codes


def test_not_checked_says_the_time_model_is_one_recipe():
    result = v.validate(env={}, script="python pretrain.py", cap=None, vram_gb=80,
                        job_estimate=KNOWN)
    assert any("measured on ONE recipe" in line for line in result.not_checked), (
        "무엇을 못 보는지가 문장으로 있어야 한다 -- 통과가 보장이 아니다")


def test_a_launcher_script_hears_no_recipe_finding_at_all():
    """★ D5, 2026-09-09. launcher script 는 trainer 를 보여 주지 않는다.

    옛 판은 "trainer 를 못 읽으면 DPO 로 가정한다 — 경고를 지우는 쪽이 더
    위험하다" 였다. script 가 **학습 명령 자체**일 때는 그것이 맞다. `bash
    jobs/run_C.sh` 처럼 **우리가 못 받은 파일로 넘길** 때는 아니다: 그 job 은
    `utils/trl_ppo_lowmem.py` 로 같은 일을 하고 카드당 44.9GB 실측인데
    `trl.trainer.dpo_trainer` 를 패치하라는 말을 들었다. 같은 응답의
    `not_checked` 가 "measured on ONE recipe" 라고 스스로 적고 있었다.
    """
    script = "set -u\nbash jobs/run_C.sh 2>&1 | tee run.log\n"
    result = v.validate(env={}, script=script, cap=12288, vram_gb=80,
                        job_estimate=KNOWN, gpu_name="A100-80GB", gpu_count=4,
                        capacity_type="on-demand")
    codes = {f.code for f in result.findings}
    for recipe_code in ("trl-patch-missing", "gpu-too-small", "prompt-cap-too-high"):
        assert recipe_code not in codes, f"{recipe_code} 가 나왔다: {codes}"
    assert any("WHICH trainer this is" in line for line in result.not_checked), (
        "대신 무엇을 못 봤는지가 not_checked 에 문장으로 있다")


def test_a_script_that_is_the_training_command_still_assumes_dpo():
    """가정을 없앤 것이 아니다 — 볼 수 있을 때는 그대로 본다."""
    result = v.validate(env={}, script="python train.py --max-len 12288\n",
                        cap=12288, vram_gb=40, job_estimate=KNOWN,
                        gpu_name="L40S", gpu_count=1, capacity_type="on-demand")
    assert "trl-patch-missing" in {f.code for f in result.findings}
