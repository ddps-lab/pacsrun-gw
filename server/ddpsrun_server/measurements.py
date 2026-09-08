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
# ★ THE ROWS LIVE IN prices.csv, NOT IN THIS FILE, and 2026-09-08 is when that
# changed. The first version of this table was us-west-2 only: 30 rows, small
# enough to read here. All 22 AWS regions plus GCP is 610, and a 610-line literal
# would bury the eight measured throughput rows that share this module. Being
# generated also means it cannot be hand-edited into disagreeing with the
# catalogue -- `tools/gen_prices_all.py` writes it and prints what it dropped.
#
# WHY ALL REGIONS AT ALL, when a solve only ever uses one. Two reasons, and the
# second is the one that makes the first useful:
#   * `GET /v1/prices` answers "what does this card cost anywhere", which had no
#     answer before. us-west-2 was the only region this service could speak about.
#   * `placement.regions` lets a caller ASK for another region, and the gateway
#     now sends it. Being able to look at a price you cannot request is not much
#     of a feature.
#
# ★★ WHAT AN ASK GETS WHEN IT NAMES NO REGION: exactly ONE region, the
# operator's own default. PACSrun's placement.go:376 says so --
# "For AWS it means the operator's ONE --region default" (grep:
# PACSRUN-AWS-ONE-REGION) -- and this deployment's operator sets
# PACSRUN_AWS_HOME_REGION=us-west-2. So DEFAULT_AWS_REGION below is not a
# preference, it is where an unqualified ask really buys, and pricing an
# unqualified ask at the globally cheapest region would be a wrong number
# dressed as a helpful one.
AWS_PRICES_FILE = "prices.csv"
AWS_PRICED_ON = "2026-09-08"
DEFAULT_AWS_REGION = "us-west-2"

# Kept under the old name because `validate` and the tests read it, and it still
# means what it said: the region this service speaks about unless told otherwise.
AWS_PRICE_REGION = DEFAULT_AWS_REGION


@dataclass(frozen=True)
class PriceRow:
    """One (vendor, card, count, region) the catalogue prices.

    Attributes:
        vendor: "aws" or "gcp". Only AWS rows are used to price a job, because
            AWS is the vendor this service can both price and rent; GCP rows are
            here so `/v1/prices` can show them, and nothing ranks the two
            together -- see `basis`.
        basis: WHAT THE PRICE COVERS, and the two values are not comparable.
            "machine" (AWS) is the whole instance with its GPUs, and `instance`
            names it. "accelerator" (GCP) is the cards ALONE: GCP GPU rows carry
            an empty InstanceType because a GPU there is attached to a machine
            type and the catalogue prices the two separately. So a GCP row is
            not "what a job costs" -- the VM it hangs off is extra, and the
            catalogue does not say which VM.
        card: the catalogue's spelling, matching `catalogue.Choice.name`.
        gpus: how many of that card the row covers. AWS goes to 8; GCP to 16.
        region: the region, e.g. "us-west-2".
        instance: the machine type. Empty for every GCP row, by the reason above.
        usd_per_hour: on-demand. None when the catalogue publishes none -- AWS
            sells some of the newest cards through Capacity Blocks instead.
        spot_low: cheapest zone's spot price in the same snapshot.
        spot_high: dearest zone's.
        zones: how many zones the row was seen in, so a single-zone card (B300)
            is visibly less available than a four-zone one.
        flags: "spot_above_ondemand" when this row's spot price exceeds its own
            on-demand price. 38 GCP rows do, consistently and by 5-17%, with both
            zones of a region agreeing -- A100 x1 asia-northeast1 is Price
            1.70586 and SpotPrice 1.7915 in both -a and -c. That is the
            catalogue's own content, not a grouping mistake (an earlier draft of
            the generator DID have one, pairing a 1-card price with a 16-card
            spot). Zero AWS rows are flagged. Nothing ranks a flagged row.
    """

    vendor: str
    basis: str
    card: str
    gpus: int
    region: str
    instance: str
    usd_per_hour: float | None
    spot_low: float | None
    spot_high: float | None
    zones: int
    flags: str = ""


def _load_prices() -> tuple[PriceRow, ...]:
    """Read prices.csv, which sits beside this module and ships inside the zip.

    The release workflow does `cp -r ddpsrun_server build/`, so any file in the
    package directory is in the deployment package. Read once at import: the
    file is about 40 KB and a per-request read would pay for it on every call.

    Returns:
        Every row. An unreadable or missing file is NOT swallowed -- a service
        that silently prices nothing looks identical to one whose catalogue says
        nothing, and telling those apart is the whole point of `unknown` here.
    """
    import csv
    import pathlib

    path = pathlib.Path(__file__).with_name(AWS_PRICES_FILE)
    rows: list[PriceRow] = []
    with path.open(newline="") as handle:
        for record in csv.DictReader(
                line for line in handle if not line.startswith("#")):
            def number(key: str) -> float | None:
                raw = (record.get(key) or "").strip()
                return float(raw) if raw else None

            rows.append(PriceRow(
                vendor=record["vendor"], basis=record["basis"],
                card=record["card"], gpus=int(record["gpus"]),
                region=record["region"], instance=record["instance"],
                usd_per_hour=number("usd_per_hour"),
                spot_low=number("spot_low"), spot_high=number("spot_high"),
                zones=int(record["zones"] or 0),
                flags=(record.get("flags") or "").strip(),
            ))
    return tuple(rows)


PRICE_ROWS: tuple[PriceRow, ...] = _load_prices()

# The AWS half, which is the only half anything prices a job from.
AWS_MACHINES: tuple[PriceRow, ...] = tuple(
    row for row in PRICE_ROWS if row.vendor == "aws")

# Every AWS region the catalogue prices, for `/v1/prices` and for telling a
# caller which names `placement.regions` will accept.
AWS_REGIONS: tuple[str, ...] = tuple(sorted({row.region for row in AWS_MACHINES}))


# `AwsMachine` was the old name for a us-west-2-only row. Kept as an alias so a
# reader who greps the older commits or the raw logs lands somewhere.
AwsMachine = PriceRow


def aws_machines_for(card: str,
                     regions: tuple[str, ...] | list[str] | None = None
                     ) -> tuple[PriceRow, ...]:
    """Every priced AWS machine carrying this card, in the regions that apply.

    Args:
        card: the catalogue's spelling.
        regions: which AWS regions the ask allows. None or empty means the ask
            named none, which gets the operator's ONE default region -- not
            every region (PACSRUN-AWS-ONE-REGION).

    Returns:
        The matching rows. Empty when no allowed region offers the card.
    """
    key = (card or "").strip().lower()
    allowed = {r.strip() for r in (regions or []) if r.strip()} or {DEFAULT_AWS_REGION}
    return tuple(row for row in AWS_MACHINES
                 if row.card.lower() == key and row.region in allowed)


def aws_counts(card: str,
               regions: tuple[str, ...] | list[str] | None = None) -> tuple[int, ...]:
    """How many of this card AWS sells at once, in the regions that apply.

    Args:
        card: the catalogue's spelling.
        regions: as `aws_machines_for`.

    Returns:
        The distinct machine sizes, ascending. `(8,)` means whole eight-GPU
        machines only. THIS VARIES BY REGION -- the H100 comes as 1 or 8 in
        us-west-2 and as 8 only in some others -- which is why the region has to
        travel with the question.
    """
    return tuple(sorted({m.gpus for m in aws_machines_for(card, regions)}))


def aws_fillable(card: str, gpus_per_pod: int, pod_count: int,
                 regions: tuple[str, ...] | list[str] | None = None
                 ) -> tuple[PriceRow, ...]:
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
        regions: as `aws_machines_for`.

    Returns:
        The acceptable machines, or empty when nothing fits.
    """
    per_pod = max(1, gpus_per_pod)
    pods = max(1, pod_count)
    ceiling = per_pod * pods
    return tuple(m for m in aws_machines_for(card, regions)
                 if per_pod <= m.gpus <= ceiling)


def aws_cheapest(card: str, gpus_per_pod: int, pod_count: int,
                 regions: tuple[str, ...] | list[str] | None = None
                 ) -> tuple[PriceRow, int] | None:
    """The machine this ask would be bought on, and how many pods it seats.

    WHY PER POD-HOUR AND NOT PER MACHINE-HOUR. This is the axis PACSrun's own
    solver ranks on: `pkg/decider/skycatalog/decider.go:1211` builds every
    alternate with `PricePerPodHour: o.PricePerHour / fits`. Picking the cheapest
    MACHINE would answer g6e.xlarge ($1.8610, one L40S) for a four-card pod, which
    cannot host it at all.

    Worked example, L40S, one card per pod, four pods, us-west-2:

        g6e.xlarge     $1.8610/hr   1 card   seats 1 pod    $1.8610 per pod-hour
        g6e.12xlarge  $10.4926/hr   4 cards  seats 4 pods   $2.6232 per pod-hour
        g6e.48xlarge  $30.1312/hr   8 cards  refused: 8 > 1*4

    so four g6e.xlarge at $7.4440/hr total beats one g6e.12xlarge at $10.4926/hr,
    and this returns g6e.xlarge with 1 seat.

    Args:
        card: the catalogue's spelling.
        gpus_per_pod: `gpu.count`.
        pod_count: `parallelism`.
        regions: as `aws_machines_for`. With several allowed regions the cheapest
            across them wins and the answer names its own region, because a price
            without its region is not checkable.

    Returns:
        `(row, seats)`, or None when nothing fits or nothing that fits has a
        published on-demand price. Seats is how many pods that one machine holds,
        which is `gpus // gpus_per_pod` -- the same integer division as
        `PACSrun/pkg/decider/decider.go:607`.
    """
    per_pod = max(1, gpus_per_pod)
    priced = [m for m in aws_fillable(card, gpus_per_pod, pod_count, regions)
              if m.usd_per_hour is not None]
    if not priced:
        return None
    best = min(priced, key=lambda m: (m.usd_per_hour / (m.gpus // per_pod),
                                      m.region))
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
