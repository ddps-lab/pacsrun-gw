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
    Throughput("aiops-exp1", "A100-80GB", 12288, 5600, True, 1682),
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
        aliases: OTHER SPELLINGS OF THE SAME RENTED CARD, because the vocabulary
            table below is real: status.currentOffering carries nvidia-smi's
            spelling ("A100-SXM4-80GB" after the maker prefix is stripped) while
            this table keys on the catalogue's ("A100-80GB"). Until 2026-09-07
            that mismatch left every job unpriced — Cost "-" on each row and
            Team spend $0.00 for jobs running on the very cards priced here.
    """

    name: str
    vram_gb: int
    usable_gib: float
    usd_per_hour: float
    priced_on: str
    aliases: tuple[str, ...] = ()


# Only what we have rented. A GPU absent here cannot be priced, and
# `estimate.py` says so rather than guessing.
# DDPSRUN-GPU-NAMES. These are the CATALOGUE's spelling, not nvidia-smi's.
#
# There are at least three vocabularies for the same card and they are not
# interchangeable:
#
#   nvidia-smi   "NVIDIA L40S", "NVIDIA A100-SXM4-80GB"   what PACSRUN_GPU= carries
#   catalogue    "L40S", "A100-80GB"                      what a placement ask must say
#   RunPod       "A100 PCIe", "A100 SXM"                   RunPod's own names
#
# The catalogue spelling is the one that has to be right here, because this name
# travels into a PacsJob and PACSrun compares it to the CSV's AcceleratorName with
# an exact match (`pkg/decider/skycatalog/decider.go:607`). On 2026-09-02 a job
# asked for "NVIDIA L40S" and sat in Pending forever: us-west-2 has 32 L40S rows,
# and every one of them was refused on the name.
#
# WHAT BELONGS IN THIS TUPLE, AND WHAT DOES NOT. Only cards we have actually
# rented. `usable_gib` is not a spec number — L40S's 44.39 came out of the CUDA
# message when aiops-exp1 died — and `usd_per_hour` is what we were actually
# charged. A card nobody has run has neither, and inventing them would produce a
# confident wrong estimate, which `docs/04-estimate.md` says is worse than
# `unknown`. The list of cards a user may CHOOSE is a different, longer list and
# lives in `catalogue.py`.
GPUS: tuple[Gpu, ...] = (
    # 44.39 GiB usable measured during the aiops-exp1 OOM, which printed how
    # much was free at the moment it failed (docs/04-estimate.md section 5).
    Gpu("L40S", 48, 44.39, 0.99, "2026-08-30"),
    # nvidia-smi reports the SXM4 spelling; it is the same card this price was
    # observed on (RunPod's A100 80GB), so it is an alias, not a guess.
    Gpu("A100-80GB", 80, 79.15, 1.59, "2026-08-29", aliases=("A100-SXM4-80GB",)),
)

# ---------------------------------------------------------------------------
# DDPSRUN-AWS-PRICES. What AWS charges for the cards a user may CHOOSE.
#
# WHY THIS TABLE EXISTS, AND WHY IT IS NOT THE ONE ABOVE. `GPUS` above is what we
# have RENTED, and its price is RunPod's on-demand rate at the moment we were
# charged it. Two entries. `catalogue.CHOOSABLE` offers fourteen cards, so twelve
# of them had no price at all and `/v1/estimate` answered `unknown` for both hours
# AND cost. Hours genuinely cannot be answered -- see WHY WE DO NOT EXTRAPOLATE
# below -- but the hourly RATE is a published number sitting in the same CSV that
# `catalogue.Choice.sold_singly` was already read from.
#
# AND IT FIXES A WRONG NUMBER, not just a missing one. Before this table,
# `estimate()` priced EVERY job at `GPUS.usd_per_hour`, which is RunPod's rate. An
# AWS L40S job was quoted $0.99/hour against a published AWS on-demand rate of
# $1.8610 -- 47% low. Underestimating by half is the same defect class as
# market-exp2, which is the incident this whole module is shaped around.
#
# WHAT ONE ROW IS. The cheapest us-west-2 machine, by on-demand price, that carries
# exactly `gpus` of `card`. Read from ~/.sky/catalogs/v8/aws/vms.csv (54,043 rows)
# on 2026-09-08 by a generator, not by hand, because 30 rows of prices copied by
# eye is 30 chances to shift a digit.
#
# ON-DEMAND IS ONE NUMBER, SPOT IS A RANGE, and that difference is not cosmetic.
# On-demand is published per region and moves rarely. Spot is per availability
# zone and moves continuously, so `spot_low`/`spot_high` are the cheapest and
# dearest zone in the SAME snapshot. TWO INDEPENDENT MEASUREMENTS LAND INSIDE THAT
# RANGE, which is why the range is carried at all rather than dropped as
# untrustworthy -- both taken with `aws ec2 describe-spot-price-history` on
# 2026-09-08 during the live runs in
# experiments/runpod/raw-logs/s57-vendor-choice-and-compare-2026-09-08.md:
#
#   g6.xlarge     us-west-2d  measured $0.4825   this table 0.4444-0.5454   inside
#   gr6.4xlarge   us-west-2d  measured $0.5521   (not a row: 1 L4, dearer than
#                                                 g6.xlarge, so never the cheapest)
#
# The spot DISCOUNT is not a factor and must not be applied as one: it was 55-68%
# of on-demand on g6.xlarge and 35-41% on gr6.4xlarge. That is why every row
# carries its own spot numbers instead of one multiplier.
#
# ★ WHY WE DO NOT EXTRAPOLATE RUNTIME TO AN UNRENTED CARD, however tempting a
# spec ratio looks. We have exactly ONE controlled comparison -- two runs at the
# same cap and the same response length on two different cards:
#
#   aiops-exp1   A100-80GB   cap 12288   5,600 tokens   1,682 tok/s
#   aiops-exp2   L40S        cap 12288   5,600 tokens   1,357 tok/s   ratio 1.24
#
# Two cards with one point each fix a line through those two cards and leave NO
# third card to test it on. A model whose error is unmeasurable by construction is
# not a measurement, and the last time an estimate was made anyway it was 96%
# wrong. So `estimate.py` still answers `unknown` for hours on the other twelve
# cards, and answers a real number for the RATE.
#
# WHAT THIS TABLE DOES NOT COVER. RunPod, whose prices come from `GPUS` above and
# exist for two cards only; and every region other than us-west-2, which is the
# only region PACSrun's AWS route has ever bought in.
AWS_PRICE_REGION = "us-west-2"
AWS_PRICED_ON = "2026-09-08"


@dataclass(frozen=True)
class AwsMachine:
    """One AWS machine type, as the catalogue prices it.

    Attributes:
        card: the catalogue's spelling of the GPU, matching `catalogue.Choice.name`.
        gpus: how many of that card the machine carries. This is the number
            PACSrun compares an ask against, and the comparison is a RANGE and not
            an equality -- see `aws_fillable`.
        instance: the machine type. Named so a price can be checked against AWS's
            own page.
        usd_per_hour: on-demand, for the WHOLE machine. None when the catalogue
            publishes no on-demand rate for it: AWS sells some of the newest cards
            through Capacity Blocks instead, and `p5e.48xlarge` (H200) is such a
            row in this snapshot -- it has a spot price and an empty Price column.
        spot_low: the cheapest availability zone's spot price in this snapshot.
        spot_high: the dearest. Equal to spot_low when only one zone offers it.
        zones: how many availability zones the row was seen in, so a single-zone
            card (B300, one zone) is visibly less available than a four-zone one.
    """

    card: str
    gpus: int
    instance: str
    usd_per_hour: float | None
    spot_low: float | None
    spot_high: float | None
    zones: int


# DROPPED BY THE FILTERS (printed so the exclusions are visible, not implied):
#   g6f.2xlarge      AcceleratorCount 0.25   rounds to 0
#   g6f.4xlarge      AcceleratorCount 0.5    11.18 GiB < 16
#   g6f.large        AcceleratorCount 0.125  rounds to 0
#   g6f.xlarge       AcceleratorCount 0.125  rounds to 0
#   gr6f.4xlarge     AcceleratorCount 0.5    11.18 GiB < 16

AWS_MACHINES: tuple[AwsMachine, ...] = (
    AwsMachine("T4", 1, "g4dn.xlarge", 0.5260, 0.0631, 0.2086, 5),
    AwsMachine("T4", 4, "g4dn.12xlarge", 3.9120, 1.3937, 1.5391, 5),
    AwsMachine("T4", 8, "g4dn.metal", 7.8240, 3.8532, 4.1120, 5),
    AwsMachine("T4g", 1, "g5g.xlarge", 0.4200, 0.1234, 0.1571, 3),
    AwsMachine("T4g", 2, "g5g.16xlarge", 2.7440, 0.8326, 1.1337, 3),
    AwsMachine("L4", 1, "g6.xlarge", 0.8048, 0.4444, 0.5454, 4),
    AwsMachine("L4", 4, "g6.12xlarge", 4.6016, 1.5794, 2.1059, 4),
    AwsMachine("L4", 8, "g6.48xlarge", 13.3504, 5.7420, 6.8526, 4),
    AwsMachine("A10G", 1, "g5.xlarge", 1.0060, 0.5869, 0.6680, 3),
    AwsMachine("A10G", 4, "g5.12xlarge", 5.6720, 2.9406, 3.5339, 3),
    AwsMachine("A10G", 8, "g5.48xlarge", 16.2880, 3.2736, 7.2926, 3),
    AwsMachine("RTX PRO 4500", 1, "g7.2xlarge", 2.5200, 0.7692, 0.8622, 4),
    AwsMachine("RTX PRO 4500", 2, "g7.12xlarge", 7.1283, 2.0537, 2.6974, 4),
    AwsMachine("RTX PRO 4500", 4, "g7.24xlarge", 14.2566, 1.5131, 4.0954, 4),
    AwsMachine("RTX PRO 4500", 8, "g7.48xlarge", 28.5133, 4.2787, 5.0269, 4),
    AwsMachine("V100-32GB", 8, "p3dn.24xlarge", 31.2120, 5.5250, 7.7890, 2),
    AwsMachine("L40S", 1, "g6e.xlarge", 1.8610, 1.0555, 1.2863, 4),
    AwsMachine("L40S", 4, "g6e.12xlarge", 10.4926, 3.1110, 7.4346, 4),
    AwsMachine("L40S", 8, "g6e.48xlarge", 30.1312, 7.3472, 12.6860, 4),
    AwsMachine("RTXPRO6000", 1, "g7e.2xlarge", 3.3631, 1.5964, 3.3631, 4),
    AwsMachine("RTXPRO6000", 2, "g7e.12xlarge", 8.2861, 2.5417, 8.2861, 4),
    AwsMachine("RTXPRO6000", 4, "g7e.24xlarge", 16.5722, 6.4355, 16.5722, 4),
    AwsMachine("RTXPRO6000", 8, "g7e.48xlarge", 33.1443, 13.9304, 33.1443, 4),
    AwsMachine("A100", 8, "p4d.24xlarge", 21.9576, 16.2246, 17.8254, 4),
    AwsMachine("A100-80GB", 8, "p4de.24xlarge", 27.4471, 18.9276, 21.6732, 3),
    AwsMachine("H100", 1, "p5.4xlarge", 6.8800, 2.6295, 2.6295, 4),
    AwsMachine("H100", 8, "p5.48xlarge", 55.0400, 19.9398, 21.0363, 4),
    AwsMachine("H200", 8, "p5en.48xlarge", 63.2960, 27.1035, 27.1792, 3),
    AwsMachine("B200", 8, "p6-b200.48xlarge", 113.9328, 39.9348, 40.5845, 3),
    AwsMachine("B300", 8, "p6-b300.48xlarge", 142.4160, 43.4369, 43.4369, 1),
)


def aws_machines_for(card: str) -> tuple[AwsMachine, ...]:
    """Every priced AWS machine carrying this card.

    Args:
        card: the catalogue's spelling.

    Returns:
        The matching rows, cheapest count first. Empty when us-west-2 does not
        offer the card at all.
    """
    key = (card or "").strip().lower()
    return tuple(m for m in AWS_MACHINES if m.card.lower() == key)


def aws_counts(card: str) -> tuple[int, ...]:
    """How many of this card AWS sells at once, in us-west-2.

    Args:
        card: the catalogue's spelling.

    Returns:
        The distinct machine sizes, ascending. `(8,)` means the card only comes
        as a whole eight-GPU machine. This REPLACES a hand-maintained boolean:
        `catalogue.Choice` used to carry `sold_singly`, which is just `1 in` this
        answer, and carrying the derived form let it drift from the CSV.
    """
    return tuple(sorted({m.gpus for m in aws_machines_for(card)}))


def aws_fillable(card: str, gpus_per_pod: int, pod_count: int) -> tuple[AwsMachine, ...]:
    """Which machines PACSrun's AWS reader would actually accept for this ask.

    THE RULE IS PACSrun's, COPIED NOT INVENTED. `pkg/decider/skycatalog/aws.go:333`
    keeps a row only when

        AcceleratorCount >= gpusPerPod  AND  AcceleratorCount <= gpusPerPod * podCount

    A machine must carry at least one pod's worth, and no more than the whole
    job's worth. Both halves matter and the second one is the surprising one:

        count 1, parallelism 1  -> maxUsefulCards 1 -> p4de.24xlarge (8 cards) is
                                   REFUSED. An A100-80GB cannot be had.
        count 1, parallelism 8  -> maxUsefulCards 8 -> p4de.24xlarge is ACCEPTED,
                                   and the eight pods fill it.

    So "this card is not sold one at a time" is true or false depending on the pod
    count, and `validate.py` used to report it as an unconditional error.

    Args:
        card: the catalogue's spelling.
        gpus_per_pod: `gpu.count` -- how many cards one pod asks for.
        pod_count: `parallelism`. Values below 1 are treated as 1, matching the
            `podCount <= 0` fallback at aws.go:312 which degenerates the ceiling
            to `gpusPerPod` alone.

    Returns:
        The acceptable machines, or empty when nothing fits.
    """
    per_pod = max(1, gpus_per_pod)
    pods = max(1, pod_count)
    ceiling = per_pod * pods
    return tuple(
        m for m in aws_machines_for(card) if per_pod <= m.gpus <= ceiling
    )


def aws_cheapest(card: str, gpus_per_pod: int,
                 pod_count: int) -> tuple[AwsMachine, int] | None:
    """The machine this ask would be bought on, and how many pods it seats.

    WHY PER POD-HOUR AND NOT PER MACHINE-HOUR. This is the axis PACSrun's own
    solver ranks on: `pkg/decider/skycatalog/decider.go:1211` builds every
    alternate with `PricePerPodHour: o.PricePerHour / fits`. Picking the cheapest
    MACHINE would answer g6e.xlarge ($1.8610, one L40S) for a four-card pod, which
    cannot host it at all.

    Worked example, L40S, one card per pod, four pods:

        g6e.xlarge     $1.8610/hr   1 card   seats 1 pod    $1.8610 per pod-hour
        g6e.12xlarge  $10.4926/hr   4 cards  seats 4 pods   $2.6232 per pod-hour
        g6e.48xlarge  $30.1312/hr   8 cards  refused: 8 > 1*4

    so four g6e.xlarge at $7.4440/hr total beats one g6e.12xlarge at $10.4926/hr,
    and this returns g6e.xlarge with 1 seat.

    Args:
        card: the catalogue's spelling.
        gpus_per_pod: `gpu.count`.
        pod_count: `parallelism`.

    Returns:
        `(machine, seats)`, or None when nothing fits or nothing that fits has a
        published on-demand price. Seats is how many pods that one machine holds,
        which is `gpus // gpus_per_pod` -- the same integer division as
        `PACSrun/pkg/decider/decider.go:607`.
    """
    per_pod = max(1, gpus_per_pod)
    priced = [m for m in aws_fillable(card, gpus_per_pod, pod_count)
              if m.usd_per_hour is not None]
    if not priced:
        return None
    best = min(priced, key=lambda m: m.usd_per_hour / (m.gpus // per_pod))
    return best, best.gpus // per_pod


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
    # status.currentOffering carries nvidia-smi's spelling, which prefixes the
    # maker: "NVIDIA L40S" for the card this table calls "L40S". The prefix
    # carries no information (every card here is NVIDIA's), so it is stripped
    # before matching rather than repeated in every alias list.
    if lowered.startswith("nvidia "):
        lowered = lowered[len("nvidia "):]
    for gpu in GPUS:
        if gpu.name.lower() == lowered:
            return gpu
        if any(alias.lower() == lowered for alias in gpu.aliases):
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
