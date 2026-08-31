"""How long a job will take, what it will cost, and which GPU to put it on.

END-TO-END FLOW of one `/v1/estimate`:

  1. `steps()` computes how many optimiser steps the run will take. This is
     exact arithmetic on numbers the user supplies, and it matched the log on
     all eight jobs we have run.
  2. `seconds_per_step()` asks `measurements.py` how fast this GPU was at this
     sequence length. It answers with a `confidence`: `measured` when we have
     run something close, `interpolated` when we can fit between two runs, and
     `unknown` when we would be guessing.
  3. `peak_logits_gib()` computes the largest memory allocation the run will
     attempt. This is not the average; it is the longest sample, which grows
     until it reaches the cap.
  4. `recommend_gpu()` puts 3 next to what each GPU really gives and picks one.
  5. `capacity_type()` decides on-demand versus spot, which is not a preference
     but a constraint: RunPod does not sell spot at all.
  6. `estimate()` assembles all of it into one answer with its reasons attached.

THE ONE RULE THIS FILE IS BUILT AROUND. A wrong number is worse than no number.
market 실험2 was estimated at 9.14 hours and took 17.87 — 96% wrong — because
nothing at that sequence length had ever been measured and the estimate was made
anyway. So `unknown` is a first-class answer here, and extrapolating past the
ends of the measured range produces it rather than a number.

NO NUMBERS LIVE IN THIS FILE. Every measurement is in `measurements.py`. What is
here is arithmetic.

Grep anchor: DDPSRUN-ESTIMATE
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .measurements import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_GRAD_ACCUM,
    DPO_RESPONSES_PER_PAIR,
    INCIDENTS,
    LOGIT_BYTES,
    QWEN3_4B_VOCAB,
    THROUGHPUT,
    Gpu,
    Throughput,
    gpu_by_name,
    gpu_by_vram,
)

# How close a measured sequence length has to be before we call an estimate
# `measured` rather than `interpolated`. The five L40S runs at cap 12288 sat
# between 4,000 and 5,600 tokens and their throughput spread was 2.1%, so a
# band of this width does not hide much.
SIMILAR_LENGTH_BAND = 0.15

# How far past the ends of the measured range we are willing to go before
# answering `unknown`. market 실험2 is why this is small: its sequence length
# was 2.6 times the longest we had measured, and the estimate was 96% wrong.
EXTRAPOLATION_ALLOWANCE = 0.20

# How much wider an `interpolated` range is than its point estimate. Taken from
# the one long job whose projection we watched all the way through: aiops-exp2's
# own projected total swung between 28.80 and 38.21 hours while it ran, which is
# 33% about its midpoint. We have no reason to believe a fitted estimate is
# steadier than the job's own live projection.
DRIFT_FACTOR = 1.33

# Above this many steps, the run is long enough that the drift above is worth
# telling the user about even when the estimate is `measured`.
LONG_RUN_STEPS = 1000

# Longest a job can be before it cannot carry its own results home. STS
# temporary credentials cap at 12 hours; one hour of margin is left for the
# upload itself.
STS_HOURS = 12
FETCH_MODE_HOURS = 11

# A job longer than this that cannot resume from a checkpoint must not run on
# spot: losing the machine near the end throws away everything.
SPOT_SAFE_HOURS = 4


class Confidence:
    """How much the hours figure is worth. Strings because they cross the API."""

    MEASURED = "measured"
    INTERPOLATED = "interpolated"
    UNKNOWN = "unknown"


@dataclass
class Duration:
    """A runtime answer, with its range and where it came from."""

    low_hours: float | None
    high_hours: float | None
    confidence: str
    basis: str
    seconds_per_step: float | None = None


@dataclass
class GpuAdvice:
    """Which GPU to use and why."""

    recommended: str | None
    recommended_vram_gb: int | None
    peak_logits_gib: float
    reason: str


@dataclass
class Estimate:
    """The whole answer `/v1/estimate` returns."""

    steps: int | None
    duration: Duration
    cost_low_usd: float | None
    cost_high_usd: float | None
    gpu: GpuAdvice
    capacity_type: str
    capacity_reason: str
    warnings: list[str] = field(default_factory=list)


def steps(pairs: int, epochs: int, batch_size: int, grad_accum: int) -> int:
    """How many optimiser steps the run will take.

    One step is one weight update. With gradient accumulation the trainer runs
    `grad_accum` forward and backward passes before updating, so the number of
    samples consumed per step is `batch_size * grad_accum`.

    Args:
        pairs: how many training pairs the dataset holds.
        epochs: how many times the dataset is traversed.
        batch_size: `per_device_train_batch_size`.
        grad_accum: `gradient_accumulation_steps`.

    Returns:
        The step count.

    Raises:
        ValueError: a non-positive argument, which would make the division
            meaningless rather than merely wrong.

    Example:
        AIOps 실험2 had 3,546 pairs, 4 epochs, and 1 x 8, and its training log
        said 1776.

        >>> steps(3546, 4, 1, 8)
        1776
    """
    if pairs <= 0 or epochs <= 0 or batch_size <= 0 or grad_accum <= 0:
        raise ValueError("pairs, epochs, batch_size and grad_accum must all be positive")
    return math.ceil(pairs / (batch_size * grad_accum)) * epochs


def tokens_per_step(row_tokens: int, batch_size: int, grad_accum: int) -> int:
    """How many tokens one step processes.

    DPO trains on a preferred answer and a rejected one for every pair, so each
    sample contributes twice its own length.

    Args:
        row_tokens: the average length of ONE response, in tokens.
        batch_size: `per_device_train_batch_size`.
        grad_accum: `gradient_accumulation_steps`.

    Returns:
        Tokens per step.
    """
    return batch_size * grad_accum * row_tokens * DPO_RESPONSES_PER_PAIR


def _rows_for(gpu_name: str) -> list[Throughput]:
    """Every measurement we have for one GPU."""
    lowered = gpu_name.strip().lower()
    return [row for row in THROUGHPUT if row.gpu.lower() == lowered]


def _fit_seconds_per_token(rows: list[Throughput]) -> tuple[float, float] | None:
    """Least-squares fit of `seconds per token = a + b * length`.

    WHY THIS SHAPE. A transformer's cost per token has a part that does not
    depend on the sequence (the feed-forward layers, the projections) and a part
    that grows with it (attention compares every token with every other). Cost
    per token is therefore roughly linear in length, which makes total cost
    quadratic. Fitting the per-token form keeps the arithmetic linear.

    Args:
        rows: measurements for one GPU. At least two distinct lengths.

    Returns:
        `(a, b)`, or None when the rows do not pin a line down.

    Example:
        The two exact L40S rows (4,144 tokens at 1,474 tokens/s and 10,619 at
        1,000) give a = 4.73e-04 and b = 4.97e-08, matching the hand-worked
        figures in `docs/04-estimate.md` section 2.
    """
    points = [(float(row.row_tokens), 1.0 / row.tokens_per_second) for row in rows]
    if len({length for length, _ in points}) < 2:
        return None

    count = len(points)
    sum_x = sum(length for length, _ in points)
    sum_y = sum(seconds for _, seconds in points)
    sum_xx = sum(length * length for length, _ in points)
    sum_xy = sum(length * seconds for length, seconds in points)

    denominator = count * sum_xx - sum_x * sum_x
    if denominator == 0:
        return None
    slope = (count * sum_xy - sum_x * sum_y) / denominator
    intercept = (sum_y - slope * sum_x) / count
    return intercept, slope


def seconds_per_step(
    gpu_name: str, row_tokens: int, batch_size: int, grad_accum: int
) -> Duration:
    """How long one step takes on this GPU at this sequence length.

    THE DERIVATION, AND THE FOUR CHECKS IT PASSED. We store tokens per second
    rather than seconds per step, because seconds per step swings with sequence
    length and tokens per second mostly does not. Turning one into the other is
    `tokens_per_step / tokens_per_second`. Feeding each run its OWN measured
    throughput back through that arithmetic reproduces its logged step time:

        bank-exp2v2   1,555 tok/s -> 42.19 s/step,  log said 42.32   (0.3% out)
        aiops-exp2    1,357 tok/s -> 66.03 s/step,  log said 66.44   (0.6% out)
        market-exp2   1,000 tok/s -> 169.9 s/step,  log said 172.9   (1.7% out)
        aiops-exp1    1,682 tok/s -> 53.27 s/step,  log said 53.49   (0.4% out)

    What this function returns for a NEW job is not any one of those. It takes
    the midpoint of every run within the length band, so an answer for a
    4,100-token job lands between the 1,474 and 1,555 tokens/s we saw there
    rather than on whichever run happened to be listed first.

    Args:
        gpu_name: the GPU model.
        row_tokens: average length of one response.
        batch_size: `per_device_train_batch_size`.
        grad_accum: `gradient_accumulation_steps`.

    Returns:
        A `Duration` whose `seconds_per_step` is the point estimate and whose
        `confidence` says how much to believe it.
    """
    rows = _rows_for(gpu_name)
    if not rows:
        return Duration(
            None, None, Confidence.UNKNOWN,
            f"we have never rented a {gpu_name}, so there is nothing to base an "
            f"estimate on. Run it once and the estimate becomes available.",
        )

    per_step_tokens = tokens_per_step(row_tokens, batch_size, grad_accum)

    # Close enough to something we ran: use the spread of those runs directly.
    close = [
        row for row in rows
        if abs(row.row_tokens - row_tokens) <= SIMILAR_LENGTH_BAND * row_tokens
    ]
    if close:
        fastest = max(row.tokens_per_second for row in close)
        slowest = min(row.tokens_per_second for row in close)
        return Duration(
            low_hours=None, high_hours=None, confidence=Confidence.MEASURED,
            basis=(
                f"{len(close)} run(s) on {gpu_name} within {int(SIMILAR_LENGTH_BAND * 100)}% "
                f"of {row_tokens:,} tokens per response: "
                f"{slowest:,}-{fastest:,} tokens/s "
                f"({', '.join(row.job for row in close)})"
            ),
            seconds_per_step=per_step_tokens / ((fastest + slowest) / 2),
        )

    # Not close to anything, but between two things: fit.
    measured_lengths = [row.row_tokens for row in rows]
    low_end = min(measured_lengths) * (1 - EXTRAPOLATION_ALLOWANCE)
    high_end = max(measured_lengths) * (1 + EXTRAPOLATION_ALLOWANCE)
    fit = _fit_seconds_per_token(rows)

    if fit is None or not (low_end <= row_tokens <= high_end):
        return Duration(
            None, None, Confidence.UNKNOWN,
            f"{row_tokens:,} tokens per response is outside what we have measured "
            f"on {gpu_name} ({min(measured_lengths):,}-{max(measured_lengths):,}). "
            f"{INCIDENTS['market-mis-estimate'].what_happened}",
        )

    intercept, slope = fit
    per_token = intercept + slope * row_tokens
    if per_token <= 0:
        return Duration(
            None, None, Confidence.UNKNOWN,
            f"the fit over our {gpu_name} runs does not produce a usable value at "
            f"{row_tokens:,} tokens.",
        )

    return Duration(
        low_hours=None, high_hours=None, confidence=Confidence.INTERPOLATED,
        basis=(
            f"fitted between {len(rows)} runs on {gpu_name} spanning "
            f"{min(measured_lengths):,}-{max(measured_lengths):,} tokens per response. "
            f"Nothing was measured at {row_tokens:,}."
        ),
        seconds_per_step=per_step_tokens * per_token,
    )


def peak_logits_gib(cap: int, vocab: int = QWEN3_4B_VOCAB) -> float:
    """The largest single allocation the run will attempt, in GiB.

    WHY THE CAP AND NOT THE AVERAGE LENGTH. The allocation is made for the
    LONGEST sample in the batch, and the longest sample in a dataset grows until
    it hits `--max-len`. AIOps 실험1 died on a buffer for 11,926 tokens with a
    cap of 12,288: the longest sample reached 97.1% of the cap.

    WHY LOGITS AND NOT ATTENTION. The traceback named
    `empty_strided_cuda((2, s87, 151936), ..., torch.bfloat16)`. That shape is
    the logits: two responses, the sequence, and one score per vocabulary entry.
    With a vocabulary of 151,936 a single token costs 297 KiB.

    Args:
        cap: `--max-len`.
        vocab: the model's vocabulary size. Qwen3-4B by default; a different
            model needs its own number and this term dominates the result.

    Returns:
        GiB.

    Example:
        AIOps 실험1's failed allocation was 6.75 GiB, for a sample 3% shorter
        than the cap.

        >>> round(peak_logits_gib(12288), 2)
        6.96
    """
    return (DPO_RESPONSES_PER_PAIR * cap * vocab * LOGIT_BYTES) / (1024 ** 3)


def recommend_gpu(
    cap: int | None, mitigations_on: bool, vocab: int = QWEN3_4B_VOCAB
) -> GpuAdvice:
    """Which GPU this cap needs.

    WHAT WE ACTUALLY KNOW, which is narrower than a formula would suggest:

      - cap 12288 with NO mitigations died on an L40S (aiops-exp1).
      - cap 12288 with BOTH mitigations finished on an L40S (aiops-exp2), and
        each step got 22.7% faster.
      - cap 18432 with the TRL patch finished on an L40S (market-exp2).

    So with both mitigations on, every cap we have tried fits in 48 GB. Without
    them, the one cap we tried did not. There is no measured factor for how much
    the patch saves, so this does not pretend to compute one — it reports the
    peak buffer and applies the rule those three runs support.

    Args:
        cap: `--max-len`.
        mitigations_on: whether both `PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True`
            and `patch_trl_liger_slice.py` are in use.
        vocab: the model's vocabulary size.

    Returns:
        A `GpuAdvice`. `recommended` is None when nothing we have rented fits.
    """
    # A missing cap must not fall through as zero. It did once, and the answer
    # read "the logits buffer reaches 0.00 GiB at cap 0", which is a number and
    # is therefore believable, and is wrong. Found 2026-08-31 running the CLI
    # against the server.
    if not cap:
        return GpuAdvice(
            recommended=None,
            recommended_vram_gb=None,
            peak_logits_gib=0.0,
            reason=(
                "we cannot say which GPU this needs without --max-len. That value "
                "decides the largest allocation the run attempts, because the "
                "longest sample in a dataset grows until it meets the cap. Send it "
                "as training.cap, as an ML environment variable, or in the script."
            ),
        )

    peak = peak_logits_gib(cap, vocab)
    largest_tried = max(row.cap for row in THROUGHPUT if row.gpu == "L40S")

    if not mitigations_on:
        gpu = gpu_by_vram(80)
        return GpuAdvice(
            recommended=gpu.name if gpu else None,
            recommended_vram_gb=gpu.vram_gb if gpu else None,
            peak_logits_gib=round(peak, 2),
            reason=(
                f"the logits buffer alone reaches {peak:.2f} GiB at cap {cap:,}, and "
                f"without the two mitigations that is what killed AIOps 실험1 on a "
                f"48 GB card. Either turn both on and ask again, or take 80 GB. "
                f"{INCIDENTS['aiops-oom'].what_happened}"
            ),
        )

    if cap <= largest_tried:
        gpu = gpu_by_vram(48)
        return GpuAdvice(
            recommended=gpu.name if gpu else None,
            recommended_vram_gb=gpu.vram_gb if gpu else None,
            peak_logits_gib=round(peak, 2),
            reason=(
                f"the logits buffer reaches {peak:.2f} GiB at cap {cap:,}. With both "
                f"mitigations on, we have finished runs at caps up to {largest_tried:,} "
                f"on a 48 GB card, and 48 GB is cheaper per hour than 80 GB by enough "
                f"to beat its own speed advantage."
            ),
        )

    gpu = gpu_by_vram(80)
    return GpuAdvice(
        recommended=gpu.name if gpu else None,
        recommended_vram_gb=gpu.vram_gb if gpu else None,
        peak_logits_gib=round(peak, 2),
        reason=(
            f"cap {cap:,} is longer than anything we have run on a 48 GB card "
            f"(the longest was {largest_tried:,}), and the logits buffer reaches "
            f"{peak:.2f} GiB. 80 GB, because being wrong here costs the whole run."
        ),
    )


def capacity_type(hours: float | None, resumable: bool) -> tuple[str, str]:
    """on-demand or spot, and why.

    THIS IS NOT A PREFERENCE. Two facts decide it.

    First, RunPod does not sell spot at all, and PACSrun's own default IS spot,
    so a job that does not say on-demand loses RunPod from the candidate list
    before the catalogue is even read. AWS sells both, so asking for on-demand
    narrows nothing that we can still reach.

    Second, spot capacity is taken back without warning. A four-hour job that
    cannot resume loses four hours; a thirty-hour one loses thirty.

    Args:
        hours: the estimated runtime, or None when we could not estimate.
        resumable: whether the job writes checkpoints it can restart from.

    Returns:
        `(capacity_type, reason)`.
    """
    vendor_note = INCIDENTS["runpod-no-spot"].what_happened

    if hours is None:
        return "on-demand", (
            f"we could not estimate how long this will take, so we are not "
            f"betting it on capacity that can be taken back. {vendor_note}"
        )
    if hours >= SPOT_SAFE_HOURS and not resumable:
        return "on-demand", (
            f"about {hours:.1f} hours with no checkpoint to resume from. Losing the "
            f"machine at hour {hours * 0.8:.0f} would throw all of it away. {vendor_note}"
        )
    # Do not splice the note into the middle of a sentence: lowercasing its
    # first character to make the grammar work turns "RunPod" into "runPod".
    return "on-demand", (
        f"spot would be defensible here ({hours:.1f} hours"
        f"{', resumable' if resumable else ''}), but we are asking for on-demand "
        f"anyway. {vendor_note}"
    )


def estimate(
    *,
    gpu_name: str,
    cap: int | None,
    pairs: int | None,
    epochs: int | None,
    row_tokens: int | None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    grad_accum: int = DEFAULT_GRAD_ACCUM,
    mitigations_on: bool = False,
    resumable: bool = False,
    vocab: int = QWEN3_4B_VOCAB,
) -> Estimate:
    """Answer everything `/v1/estimate` is asked, or say why we cannot.

    Args:
        gpu_name: the GPU the job would run on.
        cap: `--max-len`, which decides peak memory.
        pairs: dataset size. Without it there is no step count and no runtime.
        epochs: how many passes.
        row_tokens: average response length. Without it there is no runtime.
        batch_size: `per_device_train_batch_size`.
        grad_accum: `gradient_accumulation_steps`.
        mitigations_on: both memory mitigations in use.
        resumable: the job can restart from a checkpoint.
        vocab: the model's vocabulary size.

    Returns:
        An `Estimate`. Fields we could not compute are None and `basis` says
        what would be needed to compute them.
    """
    warnings: list[str] = []
    advice = recommend_gpu(cap, mitigations_on, vocab)

    step_count: int | None = None
    if pairs and epochs:
        step_count = steps(pairs, epochs, batch_size, grad_accum)

    if row_tokens:
        duration = seconds_per_step(gpu_name, row_tokens, batch_size, grad_accum)
    else:
        duration = Duration(
            None, None, Confidence.UNKNOWN,
            "no average response length was given, so there is nothing to look up. "
            "Send `training.row_tokens`, or submit the job and read the estimate "
            "back from /v1/jobs/{id} once about 50 steps have run.",
        )

    hours_point: float | None = None
    if step_count and duration.seconds_per_step:
        hours_point = step_count * duration.seconds_per_step / 3600
        if duration.confidence == Confidence.MEASURED:
            # The range comes from the spread of the runs themselves. It is
            # narrow, and honestly so: the five L40S runs at cap 12288 varied by
            # 2.1%.
            spread = _measured_spread(gpu_name, row_tokens or 0)
            duration.low_hours = round(hours_point / (1 + spread), 2)
            duration.high_hours = round(hours_point * (1 + spread), 2)
        else:
            duration.low_hours = round(hours_point / DRIFT_FACTOR, 2)
            duration.high_hours = round(hours_point * DRIFT_FACTOR, 2)

    if step_count and step_count > LONG_RUN_STEPS:
        warnings.append(
            f"this is {step_count:,} steps. {INCIDENTS['aiops-drift'].what_happened}"
        )

    if hours_point is not None and hours_point > FETCH_MODE_HOURS:
        warnings.append(
            f"about {hours_point:.1f} hours, which is past the point where the job "
            f"can carry its own results home. {INCIDENTS['sts-12h'].what_happened}"
        )

    # PRICE THE GPU THE RUNTIME WAS MEASURED ON, not the recommended one. Those
    # differ whenever the recommendation disagrees with what was asked for, and
    # pricing an L40S runtime at A100 rates overstated a job by 57% before this
    # was fixed on 2026-08-31.
    gpu = gpu_by_name(gpu_name)
    cost_low = cost_high = None
    if gpu and duration.low_hours is not None and duration.high_hours is not None:
        cost_low = round(duration.low_hours * gpu.usd_per_hour, 2)
        cost_high = round(duration.high_hours * gpu.usd_per_hour, 2)
        warnings.append(
            f"priced at ${gpu.usd_per_hour:.2f}/hour for a {gpu.name}, which is what "
            f"we paid on {gpu.priced_on}. Vendor prices move."
        )
        if advice.recommended and advice.recommended != gpu.name:
            warnings.append(
                f"this is timed and priced on a {gpu.name} because that is what was "
                f"asked for. We recommend a {advice.recommended} instead, and the "
                f"cost above does not reflect that."
            )

    kind, why = capacity_type(hours_point, resumable)

    return Estimate(
        steps=step_count,
        duration=duration,
        cost_low_usd=cost_low,
        cost_high_usd=cost_high,
        gpu=advice,
        capacity_type=kind,
        capacity_reason=why,
        warnings=warnings,
    )


def _measured_spread(gpu_name: str, row_tokens: int) -> float:
    """How much the matching runs disagreed with each other, as a fraction.

    Args:
        gpu_name: the GPU.
        row_tokens: the target length.

    Returns:
        Half the range divided by the midpoint. Zero when only one run matched,
        which is honest: one measurement has no spread, and the `warnings` about
        in-run drift carry the uncertainty instead.
    """
    close = [
        row for row in _rows_for(gpu_name)
        if abs(row.row_tokens - row_tokens) <= SIMILAR_LENGTH_BAND * row_tokens
    ]
    if len(close) < 2:
        return 0.0
    fastest = max(row.tokens_per_second for row in close)
    slowest = min(row.tokens_per_second for row in close)
    return (fastest - slowest) / (fastest + slowest)
