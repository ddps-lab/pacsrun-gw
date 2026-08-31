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
    assert "AIOps 실험1" in message_for(result, "alloc-conf-missing")


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
    # This is exactly the AIOps 실험1 submission, and it burned four steps of
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
