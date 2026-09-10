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

from . import catalogue
from . import estimate as estimator
from . import secret_expiry
from . import models          # DDPSRUN-VENDOR-CHOICE: the two vendor lists live there
from . import measurements
from .measurements import INCIDENTS

ERROR = "error"
WARNING = "warning"
INFO = "info"

# The environment variable that stops CUDA's allocator from fragmenting the
# free memory into pieces too small to serve a large request.
# ---------------------------------------------------------------------------
# ★ TWO TIERS, AND WHICH TIER A CHECK IS IN IS A DECISION, NOT AN ACCIDENT.
#
# DDPSRUN-CHECK-TIERS. Decided 2026-09-09 after the user read the finding list
# and said it was biased: "학습을 dpo와 같은 걸로 진행하지 않아, 추론이 학습이
# 쓴 경로와 같은 지 이런 거는 너무 그 실험에 한정된거야."
#
#   PLATFORM   True for every job this server can submit, because it is a fact
#              about PACSrun, the vendors, or the credentials -- not about what
#              the job computes. Can a machine of that shape be bought. Does a
#              secret name exist. Does anything leave the container. Does a
#              group have a rendezvous. These run always.
#
#   RECIPE     True only for one way of training, and stated in one lab's own
#              flag names. `--max-prompt-len` inside `--max-len`, an adapter
#              directory written by `--out` and read by `--lora`, the logits
#              buffer a DPO trainer builds. These run ONLY when the script
#              shows that recipe, and the finding says which recipe it
#              recognised -- so a pretraining job, a PPO job, an inference
#              sweep or a distributed run never hears about them.
#
# WHY THE RECIPE TIER IS KEPT AT ALL RATHER THAN DELETED. Each of those caught a
# real loss: an adapter path mismatch would have surfaced after 31 hours of GPU
# time. The defect was never that the checks are wrong; it is that they were
# presented as general. A gated check costs a reader nothing and still catches
# the run it was written for.
#
# WHAT NEITHER TIER CAN DO, said here so nobody looks for it: judge whether the
# training is CORRECT. Nothing here reads a loss curve or a hyperparameter.
# ---------------------------------------------------------------------------

ALLOC_CONF = "PYTORCH_CUDA_ALLOC_CONF"
ALLOC_CONF_VALUE = "expandable_segments:True"

# The script that makes the DPO trainer build logits for the answer span only
# instead of the whole sequence.
TRL_PATCH = "patch_trl_liger_slice.py"

# WHICH TRAINER THE SCRIPT USES, because TRL_PATCH only edits the DPO one.
# The value on the right is what the reader would call it; the keys are the
# spellings that actually occur in a script or a command line. Order matters:
# "dpo" is checked last because "grpo" and "cpo" contain no "dpo" but a script
# may well mention DPO in a comment while training with PPO.
TRAINER_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PPO", ("PPOTrainer", "ppo_trainer", "trl.trainer.ppo", "--ppo", "ppo.py")),
    ("GRPO", ("GRPOTrainer", "grpo_trainer", "trl.trainer.grpo", "--grpo", "grpo.py")),
    ("SFT", ("SFTTrainer", "sft_trainer", "trl.trainer.sft", "--sft", "sft.py")),
    ("DPO", ("DPOTrainer", "dpo_trainer", "trl.trainer.dpo", "--dpo", "dpo.py")),
)


def trainer_in(script: str | None) -> str | None:
    """Which TRL trainer this script trains with, as far as the text shows.

    DDPSRUN-TRAINER. This exists because of one wrong sentence. On 2026-09-08 a
    PPO job was told to `python patch_trl_liger_slice.py $(python -c "import
    trl.trainer.dpo_trainer ...")` -- a patch that edits the DPO trainer, on a
    run that never imports it. Following that advice patches a file nobody
    reads and the reader is left believing a memory mitigation is in place.

    Args:
        script: the job's script text, or None when it was not supplied.

    Returns:
        "PPO", "GRPO", "SFT", "DPO", or None when nothing in the text says.
        None is a real answer and the caller must treat it as "unknown", not as
        "not DPO": a script that calls its own wrapper reveals no trainer at
        all, and guessing either way would put a false sentence in front of a
        reader.
    """
    if not script:
        return None
    for label, markers in TRAINER_MARKERS:
        if any(marker in script for marker in markers):
            return label
    return None


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


# A line that hands the real work to ANOTHER script: `bash runs/run_C.sh`,
# `sh ./train.sh`, `./wrapper.sh`, `source setup.sh`. The submitted text is then a
# launcher and the settings we look for live in a file we were never sent.
CALLS_ANOTHER_SCRIPT = re.compile(
    r'^\s*(?:exec\s+)?(?:(?:bash|sh|zsh|source|\.)\s+\S*\.sh\b|\./\S*\.sh\b)',
    re.MULTILINE)


def defers_to_another_script(script: str | None) -> str | None:
    """The first line that hands the work to a script we were not sent, or None.

    DDPSRUN-DEFERRED-SCRIPT. Decided 2026-09-09 after `alloc-conf-missing` fired
    on a job whose `run_C_wrapper.sh` exported the setting three lines into the
    file it calls. The check was not wrong about what it could see -- the
    submitted text really has no `PYTORCH_CUDA_ALLOC_CONF` -- and it was wrong
    about what that means. A warning that cannot be acted on (the fix is already
    there, one file away) is worse than no warning: it teaches a reader that
    these warnings are noise, and the next one they skip will be real.

    Args:
        script: the submitted text, or None.

    Returns:
        The offending line, stripped, so the message can quote it. None when the
        script does its own work and the checks below can be trusted.
    """
    if not script:
        return None
    hit = CALLS_ANOTHER_SCRIPT.search(script)
    if hit is None:
        return None
    line_start = script.rfind('\n', 0, hit.start()) + 1
    line_end = script.find('\n', hit.start())
    line = script[line_start:line_end if line_end != -1 else len(script)]
    return line.strip()


def check_memory(cap: int | None, vram_gb: int | None, alloc_on: bool, patch_on: bool,
                 trainer: str | None = None,
                 deferred: str | None = None) -> list[Finding]:
    """Will the largest allocation fit on the card that was asked for.

    Args:
        cap: `--max-len`. None when we could not find it.
        vram_gb: the memory the job asked for. None for a CPU-only job.
        alloc_on: `PYTORCH_CUDA_ALLOC_CONF` is set.
        patch_on: the TRL patch runs.
        deferred: the line from `defers_to_another_script`, or None. When set,
            `alloc-conf-missing` is NOT reported: the setting may well be in the
            file this script calls, and we were not sent that file.
        trainer: what `trainer_in` found, or None for unknown. The TRL patch
            edits `trl.trainer.dpo_trainer`, so telling a PPO run to apply it
            is advice that cannot work -- see DDPSRUN-TRAINER.

    Returns:
        Findings.
    """
    findings: list[Finding] = []

    # A CPU-only job has no CUDA allocator to configure, so nothing below
    # applies to it. `vram_gb is None` IS "no GPU was asked for".
    if vram_gb is None:
        return findings

    # ── PLATFORM. The allocator setting is not about DPO and not about any
    # recipe: it stops CUDA's caching allocator fragmenting free memory into
    # pieces too small to serve one large contiguous request. ANY job that
    # makes a big allocation near the card's ceiling is exposed -- a long
    # sequence, a big batch, a fused kernel's workspace, an inference server's
    # KV cache. So it is reported whether or not we can compute a peak, and its
    # message no longer quotes a DPO figure it cannot justify for other jobs.
    if not alloc_on and deferred is None:
        findings.append(
            Finding(
                WARNING, "alloc-conf-missing",
                f"{ALLOC_CONF} is not set. CUDA's allocator fragments free "
                f"memory into pieces, and one large contiguous request can then "
                f"fail on a card with plenty free. "
                f"{INCIDENTS['aiops-oom'].what_happened}",
                f"add {ALLOC_CONF}={ALLOC_CONF_VALUE} to env, or prefix the "
                f"training command with it. It costs nothing when nothing needs "
                f"it.",
            )
        )
    # DDPSRUN-DEFERRED-SCRIPT: when `deferred` is set the setting may be in the
    # file this script calls, and the caller gets a `not_checked` line instead.

    # ── RECIPE from here down. Everything below is the DPO logits arithmetic:
    # `peak_logits_gib` is `cap x vocab x bytes`, measured on ONE trainer, and
    # `recommend_gpu` reads from the same table. A pretraining run, an RL
    # pipeline, a distributed job or an inference sweep has a different largest
    # allocation entirely, and applying this to them would be a confident wrong
    # number -- which docs/04-estimate.md says is worse than `unknown`.
    if cap is None:
        return findings
    if deferred is not None:
        # ★ WE CANNOT SEE THE TRAINER, SO WE CANNOT KNOW THE RECIPE APPLIES.
        # Reported 2026-09-09 as D5 by a session submitting an OpsAgent PPO
        # pipeline: the submitted text was a launcher (`bash jobs/run_C.sh`), so
        # `trainer_in` found nothing, "unknown" was treated as "assume DPO", and
        # the job was told to patch `trl.trainer.dpo_trainer` -- for a run that
        # uses `utils/trl_ppo_lowmem.py` and measured 44.9 GB per card.
        #
        # The same response's `not_checked` already said our figures came from
        # ONE recipe, so the finding contradicted our own caveat in the same
        # payload. Assuming DPO is defensible when the script IS the training
        # command and simply does not name its trainer; it is not defensible
        # when the script hands the work to a file we were never sent.
        return findings
    if trainer not in (None, "DPO"):
        # A trainer we recognised and it is not the one this arithmetic is for.
        # Nothing further is said: the honest answer is that we have not
        # measured this shape, and `not_checked` carries that.
        return findings

    peak = estimator.peak_logits_gib(cap)
    patch_applies = True
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
                (f"ask for {advice.recommended_vram_gb} GB, or turn the two "
                 f"mitigations on and ask again."
                 if patch_applies else
                 f"ask for {advice.recommended_vram_gb} GB. Only one of the two "
                 f"mitigations is available to a {trainer} run ({ALLOC_CONF}), "
                 f"so the smaller card this figure assumes is not reachable by "
                 f"turning things on."),
            )
        )
    return findings


def check_caps(script: str | None, env: dict[str, str]) -> list[Finding]:
    """RECIPE TIER. Is the prompt cap below the sequence cap.

    `--max-prompt-len` has to leave room for the answer inside `--max-len`. When
    it does not, training stops seconds after it starts with a message about
    dropped samples, which is a cheap failure but a confusing one.

    SELF-GATING BY CONSTRUCTION, and that is why it stays. Both patterns are one
    repository's flag spellings, so a job that does not use them matches
    nothing and this returns []. It is in the recipe tier because the ADVICE is
    that lab's too -- "our runs used a gap of 1,024 tokens" is a fact about two
    of our jobs, not about sequence models.
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
    """RECIPE TIER. Does inference read the adapter that training wrote.

    ONLY MEANINGFUL FOR A TRAIN-THEN-INFER LoRA SCRIPT, and only when both
    commands spell their paths `--out` and `--lora`, which is one repository's
    convention. Anything else matches nothing and this returns []. Kept because
    a mismatch here cost 31 hours of GPU time once; gated because a pretraining
    or PPO job has no adapter and must not be told about one.

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


# The launchers that start more than one process and expect a rendezvous. Any of
# them in a script means the author is doing distributed work; none of them, in a
# job with a group, means N single-process runs that will never meet.
DISTRIBUTED_LAUNCHERS = (
    "torchrun", "torch.distributed.run", "torch.distributed.launch",
    "accelerate launch", "deepspeed", "mpirun", "horovodrun", "srun",
)

# What PACSrun tells a pod in a distributed group. Exact names, from
# `internal/controller/pacsjob_controller.go` (PACSRUN-GROUP-COORDS) and
# `driver/aws/driver.py` (PACSRUN-GROUP-HOSTNET). A script that reads NONE of
# these cannot know where its rendezvous is.
GROUP_COORDS = (
    "PACSRUN_MASTER_ADDR", "PACSRUN_MASTER_PORT",
    "PACSRUN_GROUP_RANK", "PACSRUN_GROUP_SIZE", "PACSRUN_GROUP_INDEX",
)

NPROC_PATTERN = re.compile(r"--nproc[-_]per[-_]node[= ]+(\d+)")
NNODES_PATTERN = re.compile(r"--nnodes[= ]+(\d+)")


def check_distributed(script: str | None, group_size: int, group_mode: str,
                      parallelism: int, gpu_count: int) -> list[Finding]:
    """PLATFORM TIER. Can the pods of a group actually find each other.

    DDPSRUN-GROUP. The failure this exists for is SILENT, which is why it is
    worth a check rather than a paragraph. `driver/common/remotek8s.py` records
    it: "NEITHER RANK PRINTED ANYTHING. Both sat in dist.init_process_group with
    no error and no output." Every rank waits for a rendezvous nobody is
    hosting, the cards read busy, and the run bills until the stall detector
    fires at 3,600 s -- or until the lifetime ceiling, on a driver too old for
    that detector.

    THREE THINGS ARE CHECKED AND ALL THREE ARE ABOUT WIRING, NOT ABOUT THE
    MODEL. Nothing here knows whether the parallelism strategy is a good one.

      the script never reads the coordinates   PACSrun hands them over as
                                               PACSRUN_MASTER_ADDR and friends
                                               and translates nothing: torchrun
                                               wants --master_addr, another
                                               launcher wants something else,
                                               and that translation is the
                                               script's line to write.
      there is no launcher at all              `python train.py` under a group
                                               of four is four independent
                                               single-process runs. They will
                                               finish, cost four machines, and
                                               produce four unrelated results.
      the launcher's world size disagrees      `--nproc_per_node 8` on a pod
                                               given 4 GPUs, or `--nnodes 2` in
                                               a group of 4. NCCL then waits for
                                               ranks that do not exist.

    Args:
        script: the submitted text, or None.
        group_size: `group.size`. 1 means no group.
        group_mode: `group.mode`. Only "distributed" makes a rendezvous.
        parallelism: how many pods the job asks for in total.
        gpu_count: GPUs per pod.

    Returns:
        Findings. Nothing for a job with no group, which is every job written
        before `spec.group` existed.
    """
    findings: list[Finding] = []
    distributed = group_mode == "distributed" and group_size > 1

    # A group that divides badly is refused by the CRD, but saying so here
    # costs nothing and saves a round trip through a rejected submit.
    if group_size > 1 and parallelism % group_size != 0:
        findings.append(Finding(
            ERROR, "group-does-not-divide",
            f"parallelism {parallelism} is not a multiple of group.size "
            f"{group_size}, so the last group would be short. The number of "
            f"groups is derived as parallelism / size.",
            f"use a parallelism that divides by {group_size} "
            f"({group_size * max(1, parallelism // group_size)}, for instance), "
            f"or change the size.",
        ))

    if not distributed or not script:
        return findings

    if not any(coord in script for coord in GROUP_COORDS):
        findings.append(Finding(
            ERROR, "group-coords-unread",
            "this job asks for a distributed group and the script reads none of "
            + ", ".join(GROUP_COORDS[:2])
            + " or the rank variables. PACSrun hands the rendezvous over in "
              "those and translates nothing, so every rank would start alone and "
              "wait for a peer that is not coming -- with no error and no output.",
            "read them and pass them to your launcher, e.g. `torchrun "
            "--nnodes $PACSRUN_GROUP_SIZE --node_rank $PACSRUN_GROUP_RANK "
            "--master_addr $PACSRUN_MASTER_ADDR --master_port $PACSRUN_MASTER_PORT "
            "--nproc_per_node <GPUs per pod> train.py`.",
        ))

    if not any(launcher in script for launcher in DISTRIBUTED_LAUNCHERS):
        findings.append(Finding(
            WARNING, "no-distributed-launcher",
            f"this job asks for groups of {group_size} and the script starts no "
            f"launcher ({', '.join(DISTRIBUTED_LAUNCHERS[:4])}, ...). If each pod "
            f"runs one process that never joins a process group, the group buys "
            f"{group_size} machines to do {group_size} unrelated single-GPU runs.",
            "if the script starts its own processes (a framework that calls "
            "init_process_group itself, or an MPI job launched inside), this is "
            "nothing to act on -- say so and move on.",
        ))

    nproc = NPROC_PATTERN.search(script)
    if nproc and gpu_count and int(nproc.group(1)) != gpu_count:
        findings.append(Finding(
            ERROR, "world-size-mismatch",
            f"the launcher asks for {nproc.group(1)} processes per node and this "
            f"job asks for {gpu_count} GPU(s) per pod. NCCL would wait for ranks "
            f"that have no card, or leave cards idle that you are paying for.",
            f"make them agree: either --nproc_per_node {gpu_count}, or ask for "
            f"gpu.count {nproc.group(1)}.",
        ))

    nnodes = NNODES_PATTERN.search(script)
    if nnodes and int(nnodes.group(1)) != group_size:
        findings.append(Finding(
            ERROR, "nnodes-mismatch",
            f"the launcher asks for {nnodes.group(1)} node(s) and one group is "
            f"{group_size} pod(s). Every rank has to agree on the world size or "
            f"the rendezvous never completes.",
            f"use --nnodes $PACSRUN_GROUP_SIZE so the two cannot drift.",
        ))
    return findings


def check_results_leave(script: str | None) -> list[Finding]:
    """PLATFORM TIER. Does anything at all leave the container.

    THE MOST EXPENSIVE MISTAKE THERE IS, and until 2026-09-09 nothing checked
    for it: a job that computes for 21 hours, exits 0, and is marked Succeeded
    with an empty result prefix. `no-exit-trap` catches the narrower case
    (results exist and a mid-run death loses them); this catches the script that
    never had a way out in the first place.

    THREE WAYS OUT COUNT, because all three are real:

      `PACSRUN_ARTIFACT=`   the contract (script-contract 13). Works on every
                            vendor.
      `aws s3 cp` and kin   the workload uploading with the credential it was
                            given. Still works, and is what AWS/GCP jobs do
                            until the k3s fetch is deployed.
      `$PACSRUN_RESULT_PATH` a script that reads the destination and does
                            something with it that we cannot name -- boto3, a
                            framework's own writer. Its presence is enough:
                            guessing further would produce false alarms.

    Args:
        script: the submitted text, or None -- then nothing is claimed.

    Returns:
        One WARNING when none of the three appears. Not an ERROR: an inference
        sweep whose whole output is its log, or a job whose point is a metric in
        Prometheus, is legitimate and must not be blocked.
    """
    if not script:
        return []
    ways_out = ("PACSRUN_ARTIFACT", "PACSRUN_RESULT_PATH", "aws s3 ", "gsutil ",
                "s3.upload", "upload_file", "boto3")
    if any(way in script for way in ways_out):
        return []
    return [
        Finding(
            WARNING, "nothing-leaves-the-container",
            "the script mentions no way of getting anything out: no "
            "`PACSRUN_ARTIFACT=` line, no `$PACSRUN_RESULT_PATH`, no upload. The "
            "machine is deleted seconds after the workload exits, and its disk "
            "goes with it, so a job like this can spend its whole runtime and be "
            "marked Succeeded with an empty result prefix.",
            "announce each finished file with "
            "`echo \"PACSRUN_ARTIFACT=/path/to/file\"` (script-contract 13). If "
            "the output really is only the log, that is fine -- the log is "
            "relayed and kept, and this warning is one to dismiss out loud.",
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


def check_vendors_can_run(vendors: list[str], placement_mode: str | None) -> list[Finding]:
    """Can the vendors that were named actually rent a machine.

    DDPSRUN-VENDOR-CHOICE. Six vendor names are accepted and only two of them can
    run anything. aws and runpod have an execution path; gcp, azure, lambda and
    nebius are answered from the SkyPilot catalogue CSVs, which is enough to
    state a price and nothing like enough to rent a machine -- no actuator in
    PACSrun understands their machine names.

    SO THE MODE DECIDES WHETHER NAMING ONE IS SENSIBLE OR WASTEFUL.

      compare   ranks every candidate and STOPS. Nothing is bought, so a
                price-only vendor is exactly what this mode is for.
      cheapest  buys the cheapest answer. A price-only vendor that WINS reaches
                the actuator and fails there, and the comparison is thrown away
                with it.
      ordered   stops at the first candidate that answers. A price-only vendor
                that answers first fails the same way, and sooner.

    THIS WARNS AND DOES NOT REFUSE, which is deliberate. PACSrun's CRD documents
    the same trap and still allows it (`spec.placement.vendors`: "Listing one
    under mode: cheapest and having it WIN reaches the actuator and fails there,
    which is the right answer to 'buy me a thing nobody can buy'"). Refusing here
    would make this service stricter than the thing it submits to, over a
    judgement the layer below has already made. Saying so before the money is
    spent is the part that was missing.

    Args:
        vendors: the names the caller listed. Empty means no restriction.
        placement_mode: "ordered", "cheapest", "compare", or None for the
            default, which is "ordered".

    Returns:
        Findings. Nothing when no vendor was named.
    """
    if not vendors:
        return []

    findings: list[Finding] = []
    mode = placement_mode or "ordered"
    priced_only = [v for v in vendors if v in models.PRICE_ONLY_VENDORS]

    if priced_only and mode != "compare":
        findings.append(
            Finding(
                level=WARNING,
                code="vendor-cannot-run",
                message=(
                    f"{', '.join(priced_only)} can be PRICED but not rented: "
                    f"the answer comes from a catalogue CSV and no actuator here "
                    f"understands those machine names. Under mode {mode!r} one of "
                    f"them can win the walk, and then the job fails at the "
                    f"actuator with the comparison thrown away."
                ),
                fix=(
                    "Either set placement_mode to 'compare', which ranks every "
                    "candidate and stops without buying anything, or list only "
                    f"{' and '.join(models.RUNNABLE_VENDORS)}."
                ),
            )
        )

    if mode == "compare":
        findings.append(
            Finding(
                level=INFO,
                code="compare-buys-nothing",
                message=(
                    "mode 'compare' prices every candidate and then stops. No "
                    "machine is rented, no pod is created and the workload does "
                    "not run: the job ends in the terminal phase Compared with "
                    "the winner, the runner-up and the margin in its message."
                ),
                fix="Submit again with 'cheapest' or 'ordered' when you want it to run.",
            )
        )

    if len(vendors) == 1 and mode in ("cheapest", "compare"):
        findings.append(
            Finding(
                level=INFO,
                code="nothing-to-compare",
                message=(
                    f"mode {mode!r} ranks the candidates against each other, and "
                    f"only {vendors[0]} was named. There is one answer, so the "
                    f"ranking has nothing to rank it against."
                ),
                fix="Name a second vendor, or use 'ordered'.",
            )
        )

    return findings


def _unfillable_remedy(card: str, gpu_count: int, pods: int,
                       counts: tuple[int, ...], aws_only: bool) -> str:
    """What to change so the ask can be filled, with the arithmetic shown.

    Args:
        card: the catalogue's spelling, for naming it in the sentence.
        gpu_count: `gpu.count` as asked.
        pods: `parallelism`, already floored at 1.
        counts: the machine sizes AWS offers for this card.
        aws_only: True when spot was asked for, which leaves no other vendor.

    Returns:
        One sentence naming a concrete count or parallelism that WOULD fit. The
        two levers are real and independent: raising parallelism raises the
        ceiling (`gpusPerPod * podCount`), and choosing a count AWS actually
        sells satisfies the floor.
    """
    per_pod = max(1, gpu_count)
    sizes = ", ".join(str(n) for n in counts)
    # LEVER ONE: a count AWS sells outright, so one machine holds exactly one pod.
    # Which of the sizes to pick is the caller's call and not ours -- going down
    # is cheaper, going up is more cards -- so they are all named rather than one
    # being guessed at.
    # "one of 1, 4, 8" reads wrong when there is only one size to name.
    offer = f"one of {sizes}" if len(counts) > 1 else sizes
    by_count = (f"set gpu.count to {offer} instead of {per_pod}"
                if per_pod not in counts else "")
    # LEVER TWO: keep the count and add pods until the ceiling `gpusPerPod *
    # podCount` reaches the smallest machine that can host one pod.
    reachable = [n for n in counts if n >= per_pod]
    by_pods = ""
    if reachable:
        smallest = min(reachable)
        need = -(-smallest // per_pod)                # ceil, in whole pods
        if need > pods:
            article = "an" if str(smallest)[0] == "8" else "a"
            by_pods = (f"raise parallelism to {need}, which lifts the ceiling to "
                       f"{per_pod * need} and lets {article} {smallest}-card "
                       f"machine through")
    parts = [x for x in (by_count, by_pods) if x]
    # ★ THE RUNPOD HALF USED TO BE A HEDGE AND IS NOW AN ANSWER. Until
    # 2026-09-09 this sentence read "RunPod sells some cards singly, though we
    # hold no table of its machine sizes and cannot promise it fits" -- a remedy
    # that told the reader to try something and could not say what would happen.
    # prices.csv now carries 105 RunPod rows read from the endpoint PACSrun's
    # own decider calls, so the pod and its price can be named, and so can the
    # two ways RunPod fails: a count it will not build, or a NAME its catalogue
    # has no entry for (`A100-80GB` and `RTXPRO6000` are both that second case).
    tail = ""
    if not aws_only:
        pod = measurements.runpod_cheapest(card, per_pod)
        rp_counts = measurements.runpod_counts(card)
        if pod is not None:
            tail = (f" Or submit on-demand, which keeps RunPod as a candidate -- "
                    f"and RunPod DOES fill this shape: one {pod.instance} pod of "
                    f"{pod.gpus} card(s) at ${pod.usd_per_hour:.2f} per pod-hour, "
                    f"its published price read on {measurements.RUNPOD_PRICED_ON}.")
        elif rp_counts:
            tail = (f" On-demand would keep RunPod as a candidate but not help "
                    f"here: RunPod attaches at most {max(rp_counts)} of this card "
                    f"to one pod.")
        else:
            tail = (f" On-demand would keep RunPod as a candidate but not help "
                    f"here: nothing in RunPod's catalogue matches the name "
                    f"{card!r} (read {measurements.RUNPOD_PRICED_ON}), so that "
                    f"vendor cannot answer this ask under any count.")
    if not parts:
        return f"No count fits this card on AWS.{tail}"
    joined = ", or ".join(parts)
    return joined[0].upper() + joined[1:] + "." + tail


def check_gpu_is_buyable(gpu_name: str | None, gpu_count: int,
                        capacity_type: str | None,
                        parallelism: int = 1,
                        regions: list[str] | None = None,
                        vendors: list[str] | None = None) -> list[Finding]:
    """Can the GPU that was asked for actually be bought.

    DDPSRUN-CATALOGUE. Three ways an ask can be unfillable, and none of them was
    visible before submitting until this existed. On 2026-09-02 a job asked for
    "NVIDIA L40S" on spot and sat in Pending forever, retrying the same failure
    every eleven minutes:

      1. the name is nvidia-smi's, not the catalogue's. us-west-2 has 32 L40S
         rows and every one was refused on an exact-match name comparison.
      2. the machine sizes AWS offers cannot make up the ask. This was written
         here as "the card is only sold as a whole eight-GPU machine, so a count
         of 1 cannot be filled", which is BOTH too narrow and conditional --
         see THE RULE below.
      3. spot was asked for. RunPod does not sell spot and its decider refuses
         before reading the catalogue, so only AWS is left.

    ★ THE RULE, WHICH IS PACSrun's AND HAS TWO HALVES.
    `PACSrun/pkg/decider/skycatalog/aws.go:333` keeps a catalogue row only when

        AcceleratorCount >= gpusPerPod  AND  AcceleratorCount <= gpusPerPod * podCount

    The first half is the one this check knew about. The second half -- a machine
    carrying more cards than the WHOLE job needs is refused -- is why the old
    version was wrong in both directions:

        L40S count 2, parallelism 1   AWS offers 1, 4, 8. 1 is too small and 4
                                      exceeds the ceiling of 2. NOTHING fits, and
                                      the old check passed it, because the count
                                      was not 1.
        A100-80GB count 1, par 8      ceiling 8, so p4de.24xlarge (8 cards) fits
                                      and the eight pods fill it. The old check
                                      called this an error.

    MEASURED over all fourteen cards at counts 1-8: with one pod, 82 of the 112
    asks cannot be filled and the old check caught 6. With eight pods, 6 cannot.
    The screen only started offering counts above 1 on 2026-09-08, so this is a
    hazard that arrived with that box: an unfillable ask is not refused by the
    operator, it sits in Pending and retries, which is exactly the 2026-09-02
    incident above.

    ★ AND THE MACHINE SIZES ARE AWS'S, SO THE VENDOR LIST DECIDES WHETHER THEY
    ARE EVIDENCE AT ALL. Everything under THE RULE above is read out of the AWS
    catalogue. A job that names `vendors: ["runpod"]` never reaches
    `pkg/decider/skycatalog/aws.go:333`, so an AWS row proves nothing about it.
    Until 2026-09-08 vendors did not arrive here and the check judged every ask
    against AWS anyway: a RunPod-only A100 count-1 job was told "AWS us-west-2
    sells the A100 in machines of 8 cards", which is true, irrelevant, and
    impossible to act on -- the remedy it suggested was already what the job
    did. A warning that survives its own fix teaches a reader to skip warnings.

    Args:
        gpu_name: what the caller asked for, or None for a job with no GPU.
        gpu_count: how many.
        capacity_type: "spot", "on-demand", or None.
        parallelism: how many pods, because the ceiling is per JOB not per pod.
        regions: `["aws/us-west-2", ...]`. The sizes on offer differ by region.
        vendors: the vendor names the job named. Empty means no restriction, so
            both AWS and RunPod are candidates. When AWS is NOT among them the
            AWS size check is skipped rather than softened, because an AWS row
            proves nothing about a RunPod purchase -- and since 2026-09-09 the
            RunPod side is not silence either: prices.csv carries that vendor's
            own machine sizes, so `_runpod_capacity` answers the same question
            against the same evidence PACSrun's decider will use.

    Returns:
        Findings. Nothing when no GPU was asked for.
    """
    if not gpu_name:
        return []

    findings: list[Finding] = []
    choice = catalogue.choice_for(gpu_name)

    if choice is None:
        suggestion = catalogue.nvidia_smi_spelling(gpu_name)
        if suggestion:
            findings.append(Finding(
                ERROR, "gpu-name-vocabulary",
                f"{gpu_name!r} is the name nvidia-smi prints. Capacity is asked "
                f"for by the catalogue's name, and nothing will match this one.",
                f"Ask for {suggestion!r}. The nvidia-smi spelling is the right one "
                f"for reading your own PACSRUN_GPU= lines, and the wrong one here.",
            ))
        else:
            known = ", ".join(c.name for c in catalogue.CHOOSABLE)
            findings.append(Finding(
                ERROR, "gpu-name-unknown",
                f"no GPU called {gpu_name!r} is on offer.",
                f"Choose one of: {known}",
            ))
        return findings

    pods = max(1, parallelism)
    # THE MACHINE SIZES ON OFFER DEPEND ON THE REGION -- the H100 comes as 1 or 8
    # in us-west-2 and as 8 only elsewhere -- so the region has to travel with
    # the question. An ask that names none gets the operator's one default.
    aws_regions = [entry.split("/", 1)[1] for entry in (regions or [])
                   if entry.startswith("aws/") and "/" in entry]
    # No vendor named means no restriction, so AWS is one of the candidates.
    aws_is_candidate = not vendors or "aws" in vendors
    counts = measurements.aws_counts(choice.name, aws_regions) if aws_is_candidate else []
    if counts and not measurements.aws_fillable(
            choice.name, gpu_count, pods, aws_regions):
        sizes = ", ".join(str(n) for n in counts)
        ceiling = max(1, gpu_count) * pods
        # RunPod is only a candidate on on-demand, so the severity says how
        # much room is left: on spot there is no other vendor and this is an
        # ERROR, otherwise RunPod may still answer and the remedy now says
        # whether it does, out of that vendor's own rows.
        aws_only = capacity_type == "spot"
        where = ", ".join(aws_regions) if aws_regions else measurements.AWS_PRICE_REGION
        findings.append(Finding(
            ERROR if aws_only else WARNING, "gpu-count-unfillable",
            f"AWS {where} sells the {choice.name} in machines of {sizes} cards. "
            + (f"A machine must carry exactly {ceiling} for this ask -- one pod "
               f"needs {max(1, gpu_count)} and the whole job needs no more than "
               f"{max(1, gpu_count)} x {pods} pods -- and no machine does."
               if ceiling == max(1, gpu_count) else
               f"A machine must carry between {max(1, gpu_count)} (one pod's "
               f"worth) and {ceiling} (the whole job's worth, "
               f"{max(1, gpu_count)} x {pods} pods), and none does.")
            + f" PACSrun refuses a machine on either side of that range "
              f"(pkg/decider/skycatalog/aws.go:333), so this job would sit in "
              f"Pending and retry rather than fail."
            + (f" {choice.note}" if choice.note else ""),
            _unfillable_remedy(choice.name, gpu_count, pods, counts, aws_only),
        ))

    # ★ DDPSRUN-RUNPOD-CAPACITY, new on 2026-09-09 and only possible now. This
    # asks RunPod the question the AWS block above asks AWS -- "can this vendor
    # be sold this shape at all" -- and until prices.csv carried RunPod rows
    # there was nothing to ask it with. It matters for the same reason: an ask
    # no vendor can fill is not refused, it sits in Pending and retries, which
    # is the 2026-09-02 L40S incident this whole function exists for.
    #
    # THE TWO WAYS RUNPOD REFUSES, and they need opposite fixes:
    #   the count  it will not attach that many cards to one pod (an L4 caps at
    #              9, an RTX A4500 at 4). Over the cap the pod-create call
    #              returns the same HTTP 400 body as out-of-stock, so PACSrun
    #              records a capacity failure and retries forever.
    #   the NAME   its catalogue has no entry matching the spelling. `A100-80GB`
    #              and `RTXPRO6000` are both this case, and neither is a typo --
    #              they are AWS's spellings, which is what this catalogue
    #              follows, and RunPod writes the same silicon as "A100 SXM" and
    #              "RTX PRO 6000". `choice.note` carries the reachable spelling.
    #
    # SPOT IS EXCLUDED FIRST because RunPod is not a candidate there at all, and
    # the two findings below already say so; adding a second refusal for the same
    # job would be noise.
    runpod_is_candidate = (not vendors or "runpod" in vendors) and capacity_type != "spot"
    if runpod_is_candidate and measurements.runpod_cheapest(
            choice.name, gpu_count) is None:
        rp_counts = measurements.runpod_counts(choice.name)
        # RunPod ALONE means Pending forever; with AWS still in the list the
        # solve has somewhere else to land, so this is information, not a fault.
        rp_alone = bool(vendors) and "aws" not in vendors
        if rp_counts:
            what = (f"RunPod sells the {choice.name} but attaches at most "
                    f"{max(rp_counts)} of them to one pod, and this ask wants "
                    f"{max(1, gpu_count)} per pod. Over that cap RunPod answers the "
                    f"pod-create call with the same HTTP 400 body as out-of-stock, so "
                    f"PACSrun records a capacity failure and retries.")
            fix = f"Set gpu.count to {max(rp_counts)} or below"
        else:
            what = (f"nothing in RunPod's Secure Cloud catalogue matches the name "
                    f"{choice.name!r} (read {measurements.RUNPOD_PRICED_ON}). Its "
                    f"decider matches a family name plus a variant "
                    f"(pkg/decider/runpod/decider.go:661), so the names it can answer "
                    f"are {', '.join(measurements.RUNPOD_CARDS)}.")
            fix = "Ask for one of those names"
        findings.append(Finding(
            ERROR if rp_alone else INFO, "runpod-cannot-fill",
            what + (f" {choice.note}" if choice.note else ""),
            fix + ("." if rp_alone else
                   ", or leave this as it is -- AWS is still a candidate and the "
                   "solve can land there instead."),
        ))

    if capacity_type == "spot" and vendors and "aws" not in vendors:
        # Naming spot with AWS excluded is not a hint, it is a contradiction:
        # `pkg/decider/runpod/decider.go` refuses spot before it reads any
        # catalogue, so there is no candidate left and the job cannot start.
        findings.append(Finding(
            ERROR, "spot-has-no-vendor",
            f"spot was asked for and AWS is not among the vendors named "
            f"({', '.join(vendors)}). RunPod does not sell spot and its decider "
            f"refuses before it reads the catalogue, so no vendor is left to ask.",
            "Either add aws to vendors, or submit on-demand.",
        ))
    elif capacity_type == "spot":
        findings.append(Finding(
            INFO, "spot-excludes-runpod",
            "spot leaves AWS as the only vendor. RunPod does not sell spot, and "
            "its decider refuses before it reads the catalogue.",
            "Submit with --capacity-type on-demand to keep RunPod as a candidate.",
        ))

    if not catalogue.has_been_measured(choice.name):
        findings.append(Finding(
            WARNING, "gpu-never-rented",
            f"we have never rented a {choice.name}, so how LONG a job takes on it "
            f"cannot be answered -- the estimate says `unknown` for time and for "
            f"the total rather than guessing. What an hour of it costs is a "
            f"published price and is answered.",
            "Run something short on it first and the estimate gains a measured "
            "runtime. Until then, the hourly rate is the number to plan with.",
        ))

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


def check_secret_expiry(secrets: list[str],
                        expiries: dict[str, str | None]) -> list[Finding]:
    """Is any name this job asks for past the date it was stored with.

    DDPSRUN-SECRET-EXPIRY. On 2026-09-08 a judge credential expired at 14:27Z
    and nothing said so: the name was still there, the submit was accepted, and
    the run failed at the Bedrock call an hour in on a rented GPU. The date was
    known the whole time -- `GetFederationToken` returns it and the operator
    stored it. Nobody looked.

    Args:
        secrets: what the job asks for.
        expiries: name -> stored expiry, from the caller's own namespace. An
            operator binding has no date here: it points at a Secret whose
            lifetime is the operator's business and this server is not told.

    Returns:
        One ERROR naming every expired one. An error and not a warning because
        the failure is certain and expensive: the value is injected, the job
        starts, and the call that needs it is refused hours later.
    """
    stale = [(name, expiries.get(name)) for name in secrets
             if secret_expiry.is_past(expiries.get(name))]
    if not stale:
        return []
    listed = ", ".join(f"{name} (expired {when})" for name, when in stale)
    return [
        Finding(
            ERROR, "secret-expired",
            f"{listed}. The value is still stored and would still be injected, "
            f"so this job would start, rent a machine, and fail at the call that "
            f"needs the credential.",
            "re-mint it and store it again with `hyperun secret-set <NAME> "
            "--from-file <path> --expires-at <when>`. Until then nothing that "
            "uses that name can succeed.",
        )
    ]


def check_secret_names(secrets: list[str], known: dict[str, object]) -> list[Finding]:
    """Are the names in `secrets` ones this deployment actually holds.

    DDPSRUN-SECRET-NAMES. `secrets: ["GITHUB_PAT"]` is a word that opens the
    server's vault; submit refuses a word the vault does not have. Until
    2026-09-08 validate did not look at these AT ALL, so a wrong name passed
    with `Nothing blocking. EXIT=0` and the only way to learn the right one was
    to try a submit and read the refusal — which is exactly backwards for a
    tool whose promise is "check it before it costs anything".

    Args:
        secrets: the names the caller asked for.
        known: the deployment's bindings, `Settings.secret_bindings`.

    Returns:
        One ERROR naming the unknown words and listing what does exist. An
        error, not a warning: the submit WILL be refused, so this is a
        certainty rather than a risk.
    """
    if not secrets:
        return []
    missing = [name for name in secrets if name not in known]
    if not missing:
        return []
    available = ", ".join(sorted(known)) or "(none is stored for this deployment)"
    return [
        Finding(
            ERROR, "secret-name-unknown",
            f"{', '.join(missing)} is not a secret this deployment holds, so the "
            f"submit would be refused. Stored names: {available}.",
            "run `hyperun secrets` for the list. If the one you need is not "
            "there, an operator has to store it — the value never travels "
            "through this API, so nobody can add it from here.",
        )
    ]


# The three variables PACSrun injects for the result upload. A job that needs a
# DIFFERENT AWS account for its own work collides with them.
_RESULT_CREDENTIAL_VARS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


def check_aws_credential_collision(env: dict[str, str], secrets: list[str],
                                   script: str | None) -> list[Finding]:
    """Does this job want a second AWS identity in the same three variables.

    DDPSRUN-AWS-COLLISION. PACSrun hands the container credentials for writing
    results as AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN.
    A job that also calls another AWS account — Bedrock for an LLM judge, say —
    reaches for the same three names, and boto3 reads the environment BEFORE
    any profile, so `AWS_PROFILE` is ignored in silence. Whichever identity
    wins, the other half of the job fails: either the judge is refused, or the
    results cannot be collected at the end of a run that already cost money.

    This was learned the hard way (2026-09-08): the fact appeared in no
    document, and getting it wrong shows up an hour into a 21-hour job.

    Args:
        env, secrets: what the job asks to have set.
        script: its text, when supplied.

    Returns:
        A WARNING when a second identity is visible, with the pattern that
        works. Not an error: a job MAY legitimately override these — for
        instance when it does not need the result upload at all.
    """
    asked = {name.upper() for name in list(env) + list(secrets)}
    # A judge-shaped second identity: any AWS credential name that is NOT one
    # of the three, e.g. JUDGE_AWS_ACCESS_KEY_ID.
    second = sorted(
        name for name in asked
        if "AWS" in name and ("ACCESS_KEY" in name or "SECRET_ACCESS" in name)
        and name not in _RESULT_CREDENTIAL_VARS
    )
    overwrites = sorted(name for name in asked if name in _RESULT_CREDENTIAL_VARS)
    in_script = bool(script) and any(
        f"export {var}" in script for var in _RESULT_CREDENTIAL_VARS
    )

    if not second and not overwrites and not in_script:
        return []

    what = []
    if overwrites:
        what.append(f"{', '.join(overwrites)} in the job's own variables")
    if in_script:
        what.append("an `export` of them inside the script")
    if second:
        what.append(f"a second identity in {', '.join(second)}")

    return [
        Finding(
            WARNING, "aws-credential-collision",
            "this job carries " + "; ".join(what) + ". PACSrun injects the "
            "result-upload credentials as AWS_ACCESS_KEY_ID, "
            "AWS_SECRET_ACCESS_KEY and AWS_SESSION_TOKEN, and boto3 reads the "
            "environment before any profile — so AWS_PROFILE will not separate "
            "them and whichever wins breaks the other half of the run.",
            "keep the second identity under its own names and pass it "
            "explicitly where it is used: "
            "boto3.session.Session(aws_access_key_id=os.environ['JUDGE_AWS_ACCESS_KEY_ID'], "
            "...). Do not overwrite the three: results are collected at the END "
            "of the run, so a broken upload costs the whole job.",
        )
    ]


# What no check here can see, because it would need the user's repository.
NOT_CHECKED = (
    "whether this job's own arithmetic fits the card. Our memory and time "
    "figures were measured on ONE recipe (a TRL DPO run at two sequence caps), "
    "so for a pretraining run, an RL pipeline, an inference sweep or a "
    "distributed job we can price an hour and say nothing about how many. "
    "`expected_hours` is where your own figure goes.",
    "whether the paths in your script match your repository's real layout. Our "
    "own recipe said `runs/xxx/` and the repository had `dpo-training/runs/xxx/`.",
    "whether your commands actually produce every file you expect back. Three of "
    "the five outputs we needed had to be produced by the wrapper script.",
    "whether the training data is where the script looks for it, or whether a file "
    "that cloned fine is really a Git LFS pointer.",
    "whether the model can be downloaded. A gated model answers 401 to a job with "
    "no HF_TOKEN, and the run has already rented a GPU by then. Rule 5 of the "
    "script contract puts a check for all of this ahead of the training command.",
)


def validate(
    *,
    env: dict[str, str],
    script: str | None,
    cap: int | None,
    vram_gb: int | None,
    job_estimate: estimator.Estimate,
    gpu_name: str | None = None,
    gpu_count: int = 1,
    capacity_type: str | None = None,
    parallelism: int = 1,
    regions: list[str] | None = None,
    vendors: list[str] | None = None,
    placement_mode: str | None = None,
    secrets: list[str] | None = None,
    known_secrets: dict[str, object] | None = None,
    secret_expiries: dict[str, str | None] | None = None,
    group_size: int = 1,
    group_mode: str = "independent",
) -> Validation:
    """Run every check and sort what comes back.

    Args:
        env: the job's environment variables.
        script: the text of the job's script, when supplied. Several checks are
            skipped without it and say so.
        cap: `--max-len`.
        vram_gb: the memory the job asked for.
        job_estimate: the result of `estimate.estimate` for the same job.
        vendors: the vendor names the caller listed, or None for no restriction.
        placement_mode: "ordered", "cheapest" or "compare", or None for the
            default. Both are needed together: whether naming a price-only
            vendor is sensible depends entirely on the mode.
        secrets: the vault words the job asks for.
        group_size: `group.size`, and `group_mode` its mode. Both together:
            whether the pods need a rendezvous is the two of them, and a size
            without a mode says nothing. DDPSRUN-GROUP.
        secret_expiries: name -> the date it was stored with, for this
            namespace's own registrations. DDPSRUN-SECRET-EXPIRY.
        known_secrets: what the deployment holds (`Settings.secret_bindings`).
            Both are needed together, and passing secrets without this would
            make every name look unknown — so a caller that cannot supply the
            bindings passes neither and the check simply does not run.

    Returns:
        A `Validation`. `ok` is False when any finding is an error.
    """
    alloc_on, patch_on = mitigations_from(env, script)

    findings: list[Finding] = []
    # ── PLATFORM TIER (DDPSRUN-CHECK-TIERS). True for every job, because each is
    # a fact about PACSrun, a vendor or a credential -- not about what the job
    # computes.
    findings += check_vendors_can_run(vendors or [], placement_mode)
    # vendors GOES IN, and until 2026-09-08 it did not: the check judged every
    # ask against AWS's catalogue, so `vendors: ["runpod"]` left an AWS-shaped
    # warning standing. A warning that survives the fix it asks for teaches a
    # reader to ignore warnings.
    findings += check_gpu_is_buyable(gpu_name, gpu_count, capacity_type, parallelism,
                                         regions, vendors or [])
    findings += check_secrets_as_literals(env)
    if known_secrets is not None:
        findings += check_secret_names(secrets or [], known_secrets)
    findings += check_secret_expiry(secrets or [], secret_expiries or {})
    findings += check_aws_credential_collision(env, secrets or [], script)
    findings += check_distributed(script, group_size, group_mode, parallelism,
                                  gpu_count)
    findings += check_results_leave(script)
    findings += check_partial_results(script)
    findings += check_runtime(job_estimate)
    # ── RECIPE TIER. Each of these self-gates on the recipe it was written for
    # and returns nothing for anything else, so a pretraining run, an RL
    # pipeline, an inference sweep or a distributed job hears none of it.
    deferred = defers_to_another_script(script)
    findings += check_memory(cap, vram_gb, alloc_on, patch_on, trainer_in(script),
                             deferred=deferred)
    findings += check_caps(script, env)
    findings += check_adapter_paths(script)

    order = {ERROR: 0, WARNING: 1, INFO: 2}
    findings.sort(key=lambda finding: order.get(finding.level, 3))

    not_checked = list(NOT_CHECKED)
    if deferred is not None:
        not_checked.insert(
            0,
            f"anything that depends on WHICH trainer this is: your script hands "
            f"the work to another one ({deferred!r}) and we were not sent that "
            f"file, so the memory arithmetic and the card recommendation are "
            f"withheld rather than guessed. They were measured on a TRL "
            f"preference-tuning run and would be a confident wrong number for "
            f"anything else.",
        )
    if deferred is not None and not alloc_on:
        not_checked.insert(
            0,
            f"whether {ALLOC_CONF} is set: your script hands the work to another "
            f"one ({deferred!r}) and we were not sent that file. It may well be "
            f"exported in there -- `run_C_wrapper.sh` does it three lines in -- so "
            f"this is not reported as missing. Check it yourself, or send the "
            f"script that actually runs the training as `script`.",
        )
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
