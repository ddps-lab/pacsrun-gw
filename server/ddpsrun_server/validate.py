"""Everything we had to fix by hand, turned into a check that runs before the job.

END-TO-END FLOW of one `/v1/validate`:

  1. The caller sends the same body they would submit, optionally with the text
     of their `run.sh` attached.
  2. Each `check_*` function below looks at that body and returns zero or more
     `Finding`s.
  3. `validate()` runs them all and sorts the findings so errors come first.
  4. Nothing is submitted. The caller decides what to do.

WHERE THESE CHECKS COME FROM. Every one of them is something that actually went
wrong across the eight jobs we ran between 2026-08-20 and 2026-08-31, and every
one was invisible to the person submitting. Two of them cost real money: the
AIOps out-of-memory burned four steps on a rented card, and a job submitted
without `capacityType: on-demand` silently loses RunPod from the candidate list.

WHAT A CHECK MAY NOT DO. It may not say "this might be a problem". A finding
either names what will happen and what to change, or it does not exist. A user
who learns to skim these has lost the value of all of them.

WHAT THIS CANNOT SEE. The user's repository. Three of the seven problems in
`docs/03-api.md` were mismatches between a document and a repository's real
layout, and no check here can catch those without cloning. They are listed at
the end of the findings as "not checked" rather than passed over in silence.

Grep anchor: DDPSRUN-VALIDATE
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import estimate as estimator
from .measurements import INCIDENTS

ERROR = "error"
WARNING = "warning"
INFO = "info"

# The environment variable that stops CUDA's allocator from fragmenting the
# free memory into pieces too small to serve a large request.
ALLOC_CONF = "PYTORCH_CUDA_ALLOC_CONF"
ALLOC_CONF_VALUE = "expandable_segments:True"

# The script that makes the DPO trainer build logits for the answer span only
# instead of the whole sequence.
TRL_PATCH = "patch_trl_liger_slice.py"

# `--out adapter_x` in the training command has to be the same directory the
# inference command reads with `--lora`. They are two separate commands in the
# same script and nothing connects them.
OUT_PATTERN = re.compile(r"--out[= ]+(\S+)")
LORA_PATTERN = re.compile(r"--lora[= ]+(\S+)")
MAX_LEN_PATTERN = re.compile(r"--max-len[= ]+(\d+)")
MAX_PROMPT_PATTERN = re.compile(r"--max-prompt-len[= ]+(\d+)")


@dataclass
class Finding:
    """One thing worth saying about a job before it runs.

    Attributes:
        level: `error` stops a sensible person, `warning` should change what
            they do, `info` is worth knowing.
        code: a short stable identifier, so a script can act on it.
        message: what is wrong or notable, with the evidence.
        fix: what to change. None when there is nothing to change, only
            something to know.
    """

    level: str
    code: str
    message: str
    fix: str | None = None


@dataclass
class Validation:
    """The whole answer `/v1/validate` returns."""

    ok: bool
    findings: list[Finding] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)


def mitigations_from(env: dict[str, str], script: str | None) -> tuple[bool, bool]:
    """Work out whether each of the two memory mitigations is in use.

    Args:
        env: the job's environment variables.
        script: the text of the job's script, when it was supplied.

    Returns:
        `(alloc_conf_on, trl_patch_on)`.

    The allocator setting may arrive as an environment variable or as a prefix
    on the training command inside the script, and we have used both. The patch
    can only appear in the script, because it is a command that has to run
    before training starts.
    """
    haystack = script or ""
    alloc_on = ALLOC_CONF in env or ALLOC_CONF in haystack
    patch_on = TRL_PATCH in haystack
    return alloc_on, patch_on


def check_memory(cap: int | None, vram_gb: int | None, alloc_on: bool, patch_on: bool) -> list[Finding]:
    """Will the largest allocation fit on the card that was asked for.

    Args:
        cap: `--max-len`. None when we could not find it.
        vram_gb: the memory the job asked for. None for a CPU-only job.
        alloc_on: `PYTORCH_CUDA_ALLOC_CONF` is set.
        patch_on: the TRL patch runs.

    Returns:
        Findings.
    """
    if cap is None or vram_gb is None:
        return []

    peak = estimator.peak_logits_gib(cap)
    findings: list[Finding] = []

    if not alloc_on:
        findings.append(
            Finding(
                WARNING, "alloc-conf-missing",
                f"{ALLOC_CONF} is not set. The logits buffer at cap {cap:,} is "
                f"{peak:.2f} GiB, and it has to be one contiguous block. "
                f"{INCIDENTS['aiops-oom'].what_happened}",
                f"add {ALLOC_CONF}={ALLOC_CONF_VALUE} to env, or prefix the "
                f"training command with it.",
            )
        )
    if not patch_on:
        findings.append(
            Finding(
                WARNING, "trl-patch-missing",
                f"{TRL_PATCH} does not appear in the script. Without it the "
                f"trainer builds logits across the whole sequence rather than the "
                f"answer span, which is the {peak:.2f} GiB above.",
                f"run `python {TRL_PATCH} $(python -c \"import "
                f"trl.trainer.dpo_trainer as m; print(m.__file__)\")` before training.",
            )
        )

    advice = estimator.recommend_gpu(cap, alloc_on and patch_on)
    if advice.recommended_vram_gb and vram_gb < advice.recommended_vram_gb:
        findings.append(
            Finding(
                ERROR, "gpu-too-small",
                f"this asks for {vram_gb} GB. {advice.reason}",
                f"ask for {advice.recommended_vram_gb} GB, or turn the two "
                f"mitigations on and ask again.",
            )
        )
    return findings


def check_caps(script: str | None, env: dict[str, str]) -> list[Finding]:
    """Is the prompt cap below the sequence cap.

    `--max-prompt-len` has to leave room for the answer inside `--max-len`. When
    it does not, training stops seconds after it starts with a message about
    dropped samples, which is a cheap failure but a confusing one.
    """
    text = (script or "") + " " + " ".join(f"{k}={v}" for k, v in env.items())
    max_len = MAX_LEN_PATTERN.search(text)
    max_prompt = MAX_PROMPT_PATTERN.search(text)
    if not max_len or not max_prompt:
        return []

    sequence, prompt = int(max_len.group(1)), int(max_prompt.group(1))
    if prompt >= sequence:
        return [
            Finding(
                ERROR, "prompt-cap-too-high",
                f"--max-prompt-len is {prompt:,} and --max-len is {sequence:,}. "
                f"That leaves {sequence - prompt} tokens for the answer.",
                f"our runs used a gap of 1,024 tokens: 12288 with 11264, "
                f"18432 with 17408.",
            )
        ]
    return []


def check_adapter_paths(script: str | None) -> list[Finding]:
    """Does inference read the adapter that training wrote.

    They are two separate commands and nothing links them. When they disagree,
    training runs to completion, and only then does inference fail with a
    missing path. On AIOps that would have been 31 hours of GPU time before the
    mistake surfaced.
    """
    if not script:
        return []
    written = {match.group(1).rstrip("/") for match in OUT_PATTERN.finditer(script)}
    read = {match.group(1).rstrip("/").split("/")[-1] for match in LORA_PATTERN.finditer(script)}
    if not written or not read:
        return []

    written_names = {path.split("/")[-1] for path in written}
    unmatched = read - written_names
    if unmatched:
        return [
            Finding(
                ERROR, "adapter-path-mismatch",
                f"inference reads {', '.join(sorted(unmatched))} but training writes "
                f"{', '.join(sorted(written_names))}. Training would finish first and "
                f"only then would inference fail.",
                "use one shell variable for both --out and --lora.",
            )
        ]
    return []


def check_partial_results(script: str | None) -> list[Finding]:
    """Does the script upload what it has if it dies halfway.

    A job that fails at hour 20 with nothing uploaded has cost the money and
    produced nothing. `trap ... EXIT` runs the upload whichever step killed it.
    """
    if not script:
        return []
    if "trap" in script and "EXIT" in script:
        return []
    return [
        Finding(
            WARNING, "no-exit-trap",
            "the script has no `trap ... EXIT`. If it dies partway, whatever it "
            "had already produced is lost with the machine.",
            "add `trap upload_everything EXIT` so the upload runs on any exit.",
        )
    ]


def check_runtime(job_estimate: estimator.Estimate) -> list[Finding]:
    """Is this long enough to need special handling.

    Two thresholds matter. Past about 11 hours the job's own credentials expire
    before it finishes, so the driver has to collect the results instead. Past
    about 4 hours without a checkpoint, losing the machine is expensive enough
    that spot is not defensible.
    """
    findings: list[Finding] = []
    hours = job_estimate.duration.high_hours

    if job_estimate.duration.confidence == estimator.Confidence.UNKNOWN:
        findings.append(
            Finding(
                INFO, "runtime-unknown",
                f"we cannot say how long this will take. {job_estimate.duration.basis}",
                "submit it anyway. The answer appears on /v1/jobs/{id} once "
                "about 50 steps have run, which took 35 minutes on our shortest job.",
            )
        )
        return findings

    if hours and hours > estimator.FETCH_MODE_HOURS:
        findings.append(
            Finding(
                INFO, "fetch-mode-needed",
                f"up to about {hours:.1f} hours. "
                f"{INCIDENTS['sts-12h'].what_happened}",
                "nothing for you to do: the server turns fetch mode on for this job.",
            )
        )
    return findings


def check_secrets_as_literals(env: dict[str, str]) -> list[Finding]:
    """Is something that looks like a credential sitting in `env`.

    A literal in `env` is stored in the clear and appears in `kubectl get -o
    yaml`, in events, in controller logs, in backups, and in audit logs. The
    submit route already refuses reserved names, but it cannot know that
    `MY_KEY` holds a token.
    """
    suspicious = []
    for name, value in env.items():
        upper = name.upper()
        looks_like_a_name = any(
            word in upper for word in ("TOKEN", "SECRET", "PASSWORD", "KEY", "PAT", "CREDENTIAL")
        )
        if looks_like_a_name and value:
            suspicious.append(name)
    if not suspicious:
        return []
    return [
        Finding(
            ERROR, "secret-in-env",
            f"{', '.join(sorted(suspicious))} looks like a credential and is in `env` "
            f"as a literal. That value is stored in the clear and appears in logs, "
            f"events, backups and audit records.",
            "move the NAME into `secrets` and ask an operator to store the value. "
            "The value then never travels through this API.",
        )
    ]


# What no check here can see, because it would need the user's repository.
NOT_CHECKED = (
    "whether the paths in your script match your repository's real layout. Our "
    "own recipe said `runs/xxx/` and the repository had `dpo-training/runs/xxx/`.",
    "whether your commands actually produce every file you expect back. Three of "
    "the five outputs we needed had to be produced by the wrapper script.",
    "whether the training data is where the script looks for it.",
)


def validate(
    *,
    env: dict[str, str],
    script: str | None,
    cap: int | None,
    vram_gb: int | None,
    job_estimate: estimator.Estimate,
) -> Validation:
    """Run every check and sort what comes back.

    Args:
        env: the job's environment variables.
        script: the text of the job's script, when supplied. Several checks are
            skipped without it and say so.
        cap: `--max-len`.
        vram_gb: the memory the job asked for.
        job_estimate: the result of `estimate.estimate` for the same job.

    Returns:
        A `Validation`. `ok` is False when any finding is an error.
    """
    alloc_on, patch_on = mitigations_from(env, script)

    findings: list[Finding] = []
    findings += check_secrets_as_literals(env)
    findings += check_memory(cap, vram_gb, alloc_on, patch_on)
    findings += check_caps(script, env)
    findings += check_adapter_paths(script)
    findings += check_partial_results(script)
    findings += check_runtime(job_estimate)

    order = {ERROR: 0, WARNING: 1, INFO: 2}
    findings.sort(key=lambda finding: order.get(finding.level, 3))

    not_checked = list(NOT_CHECKED)
    if not script:
        not_checked.insert(
            0,
            "anything inside your script: you did not send one. Send the text of "
            "your run.sh as `script` and four more checks become available.",
        )

    return Validation(
        ok=not any(finding.level == ERROR for finding in findings),
        findings=findings,
        not_checked=not_checked,
    )
