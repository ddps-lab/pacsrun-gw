"""Everything we have actually measured, and nothing we have not.

END-TO-END FLOW of this file:

  1. It holds three tables of numbers: what each finished job's throughput was,
     what each GPU costs per hour, and how much memory each GPU really gives you.
  2. `estimate.py` reads them and does arithmetic. It contains no numbers of its
     own, so a reader who distrusts an estimate has exactly one place to look.
  3. When a job finishes, one row is added to `THROUGHPUT`. That is the whole
     maintenance story: the estimates get better because the table gets longer.

WHY THE NUMBERS LIVE APART FROM THE CODE THAT USES THEM. Every value here came
from a real run and can be traced to a log. Mixing them into the arithmetic
would make it impossible to tell a measurement from an assumption, and the
difference is the entire point of `confidence` in the estimate response.

WHAT IS NOT HERE. No number we did not observe. There is no A100 entry at cap
18432 because we never ran one, and `estimate.py` answers `unknown` for that
combination rather than interpolating off a single point.

Source for every row: `docs/04-estimate.md`, which in turn cites the training
logs of the eight jobs run between 2026-08-20 and 2026-08-31.

Grep anchor: DDPSRUN-MEASUREMENTS
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Throughput:
    """One finished job's training throughput.

    Attributes:
        job: which run this came from, so the log can be found again.
        gpu: the GPU model as the vendor names it.
        cap: `--max-len`, the longest sequence the trainer will accept. This is
            what decides peak memory, because the longest sample in the dataset
            grows until it hits the cap.
        row_tokens: the average length of one response, in tokens.
        approximate_row: True when `row_tokens` was read off a summary rather
            than computed exactly. Six of the eight are approximate; the two
            exact ones are the only rows the fit below should be trusted on.
        tokens_per_second: `num_tokens / train_runtime` from the last line of
            the training log. Measured in tokens, not steps, because seconds
            per step swings with sequence length and this mostly does not.
    """

    job: str
    gpu: str
    cap: int
    row_tokens: int
    approximate_row: bool
    tokens_per_second: int


# The eight runs. `docs/04-estimate.md` section 2 is the same table.
THROUGHPUT: tuple[Throughput, ...] = (
    Throughput("telecom-exp1", "L40S", 12288, 4144, False, 1505),
    Throughput("telecom-exp2", "L40S", 12288, 4000, True, 1495),
    Throughput("bank-exp1", "L40S", 12288, 4100, True, 1480),
    Throughput("bank-exp1v2", "L40S", 12288, 4144, False, 1474),
    Throughput("bank-exp2v2", "L40S", 12288, 4100, True, 1555),
    Throughput("market-exp2", "L40S", 18432, 10619, False, 1000),
    Throughput("aiops-exp1", "A100-SXM4-80GB", 12288, 5600, True, 1682),
    Throughput("aiops-exp2", "L40S", 12288, 5600, True, 1357),
)


@dataclass(frozen=True)
class Gpu:
    """One GPU model we have rented, with what it costs and what it really gives.

    Attributes:
        name: the model as the vendor names it.
        vram_gb: the number printed on the card, which is what a user asks for.
        usable_gib: what PyTorch could actually allocate, measured. Always less
            than the printed number: the driver and the CUDA context take a
            share before any tensor exists.
        usd_per_hour: RunPod's on-demand price when we rented it.
        priced_on: when that price was observed. Vendor prices move.
    """

    name: str
    vram_gb: int
    usable_gib: float
    usd_per_hour: float
    priced_on: str


# Only what we have rented. A GPU absent here cannot be priced, and
# `estimate.py` says so rather than guessing.
GPUS: tuple[Gpu, ...] = (
    # 44.39 GiB usable measured during the aiops-exp1 OOM, which printed how
    # much was free at the moment it failed (docs/04-estimate.md section 5).
    Gpu("L40S", 48, 44.39, 0.99, "2026-08-30"),
    Gpu("A100-SXM4-80GB", 80, 79.15, 1.59, "2026-08-29"),
)

# Qwen3-4B's vocabulary. This is the single biggest term in the memory
# calculation and it is a property of the MODEL, not of the GPU or the data —
# so a different model needs a different number and `estimate.py` takes it as an
# argument rather than reading it from here.
QWEN3_4B_VOCAB = 151_936

# DPO builds logits for a chosen answer and a rejected answer, so the buffer is
# twice what a plain fine-tune would need. Named because the 2 in the formula is
# otherwise unexplainable.
DPO_RESPONSES_PER_PAIR = 2

# bfloat16. Two bytes per logit.
LOGIT_BYTES = 2

# The step formula's two script-side constants, from `train_dpo_m3.py:91`. A
# user running a different script has different ones, so these are only the
# defaults that `estimate.py` falls back to.
DEFAULT_BATCH_SIZE = 1
DEFAULT_GRAD_ACCUM = 8


@dataclass(frozen=True)
class Incident:
    """One thing that went wrong, kept so a check can cite it.

    A warning that says "this may run out of memory" is ignored. One that says
    "aiops-exp1 died after 4 steps asking for 6.75 GiB with 3.43 GiB free" is
    not. Every check in `validate.py` points at one of these.
    """

    key: str
    what_happened: str


INCIDENTS: dict[str, Incident] = {
    incident.key: incident
    for incident in (
        Incident(
            "aiops-oom",
            "aiops-exp1 died after 4 steps on an L40S: it asked CUDA for a "
            "6.75 GiB logits buffer with 3.43 GiB free. The same job finished "
            "on the same GPU once PYTORCH_CUDA_ALLOC_CONF and the TRL patch "
            "were both on, and got 22.7% faster per step as well.",
        ),
        Incident(
            "runpod-no-spot",
            "RunPod does not sell spot. PACSrun's decider refuses it before it "
            "reads the catalogue, and PACSrun's own default IS spot, so a job "
            "that does not say on-demand loses RunPod as a candidate entirely.",
        ),
        Incident(
            "market-mis-estimate",
            "market-exp2 was estimated at 9.14 hours and took 17.87. The cap "
            "was 18432 rather than 12288 and nothing at that length had been "
            "measured. 96% wrong.",
        ),
        Incident(
            "aiops-drift",
            "aiops-exp2 was 1,776 steps and its projected total swung between "
            "28.80 and 38.21 hours while it ran, a 33% spread. The cause was "
            "never established.",
        ),
        Incident(
            "sts-12h",
            "STS temporary credentials expire after at most 12 hours "
            "(DurationSeconds caps at 43200). A job longer than that cannot "
            "upload its own results at the end and needs fetch mode, where the "
            "driver pod collects them instead.",
        ),
    )
}


def gpu_by_name(name: str) -> Gpu | None:
    """Look up a GPU we have rented.

    Args:
        name: the model name, matched case-insensitively.

    Returns:
        The `Gpu`, or None when we have never rented it and therefore cannot
        price it or say how much memory it really gives.
    """
    lowered = (name or "").strip().lower()
    for gpu in GPUS:
        if gpu.name.lower() == lowered:
            return gpu
    return None


def gpu_by_vram(vram_gb: int) -> Gpu | None:
    """Find the cheapest GPU we have rented that has at least this much memory.

    Args:
        vram_gb: the number printed on the card.

    Returns:
        The cheapest sufficient `Gpu`, or None when nothing we have rented is
        large enough.
    """
    candidates = [gpu for gpu in GPUS if gpu.vram_gb >= vram_gb]
    return min(candidates, key=lambda gpu: gpu.usd_per_hour) if candidates else None
