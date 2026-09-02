"""Which GPUs a user may choose, and what we can honestly say about each.

END-TO-END FLOW of this file:

  1. `CHOOSABLE` lists every card a request may name. It is the CATALOGUE's
     spelling, because that name travels into a PacsJob and PACSrun compares it
     to the CSV's `AcceleratorName` with an exact match
     (`pkg/decider/skycatalog/decider.go:607`).
  2. `/v1/schema` and the screen's dropdown are built from it, so there is one
     list rather than one per client drifting apart.
  3. `advice_for` answers what we can say about a card BEFORE anything is
     bought: whether we have ever rented it, and whether the ask can be filled
     at all on the route the caller chose.

WHY THIS IS SEPARATE FROM `measurements.py`. Those two lists answer different
questions and had been one list answering both badly:

    measurements.GPUS   cards we have RENTED. Carries a measured usable_gib and
                        a price we were actually charged. Two entries.
    catalogue.CHOOSABLE cards a user may ASK for. Carries no measurement,
                        because we have not run most of them.

Merging them forced a choice between a short dropdown and invented numbers.
`docs/04-estimate.md` settles that: a wrong number is worse than `unknown`
(market-exp2 was answered 9.14 hours and took 17.87).

THE DEFECT THIS EXISTS TO PREVENT. On 2026-09-02 a job asked for "NVIDIA L40S"
on spot and sat in Pending forever. Two separate things were wrong and neither
was visible before submitting:

  * the name. us-west-2 has 32 L40S rows in the catalogue; all were refused
    because the catalogue spells it "L40S" and nvidia-smi spells it "NVIDIA L40S".
  * the count. Some cards are only sold in whole eight-GPU machines, so asking
    for one of them cannot be filled however the name is spelled.

Grep anchor: DDPSRUN-CATALOGUE
"""

from __future__ import annotations

from dataclasses import dataclass

from .measurements import gpu_by_name


@dataclass(frozen=True)
class Choice:
    """One card a request may name.

    Attributes:
        name: the catalogue's spelling. This is what goes into the PacsJob.
        vram_gb: the number printed on the card. A spec figure, not a measured
            one; `measurements.Gpu.usable_gib` is the measured one and exists
            only for cards we have rented.
        sold_singly: whether a machine with exactly one of these card can be
            bought in us-west-2. False means the card only comes in whole
            multi-GPU machines, so `count: 1` cannot be filled on the AWS route
            however the name is spelled. Read off the catalogue on 2026-09-02
            by counting rows with AcceleratorCount == 1.0.
        note: what to tell someone who picks it, or "" when there is nothing
            they need to know.
    """

    name: str
    vram_gb: int
    sold_singly: bool
    note: str = ""


# Every NVIDIA card the AWS catalogue offers in us-west-2, read on 2026-09-02
# from catalogs/v8/aws/vms.csv (55,420 rows). Ordered by memory, because that is
# what someone choosing is actually deciding between.
#
# Inferentia, Trainium and the FPGA rows are left out: they are not GPUs and
# nothing in this service can use them.
CHOOSABLE: tuple[Choice, ...] = (
    Choice("T4", 16, True),
    Choice("T4g", 16, True, "ARM host. An x86 container image will not run on it."),
    Choice("L4", 24, True),
    Choice("A10G", 24, True),
    Choice("RTX PRO 4500", 32, True),
    Choice("V100-32GB", 32, False,
           "Only sold as a whole 8-GPU machine, so a count of 1 cannot be filled."),
    Choice("L40S", 48, True),
    Choice("RTXPRO6000", 96, True),
    Choice("A100", 40, False,
           "Only sold as a whole 8-GPU machine on AWS. RunPod sells it singly, "
           "but RunPod does not sell spot."),
    Choice("A100-80GB", 80, False,
           "Only sold as a whole 8-GPU machine on AWS. RunPod sells it singly, "
           "but RunPod does not sell spot."),
    Choice("H100", 80, True),
    Choice("H200", 141, False,
           "Only sold as a whole 8-GPU machine, so a count of 1 cannot be filled."),
    Choice("B200", 180, False,
           "Only sold as a whole 8-GPU machine, so a count of 1 cannot be filled."),
    Choice("B300", 288, False,
           "Only sold as a whole 8-GPU machine, so a count of 1 cannot be filled."),
)

BY_NAME = {choice.name.lower(): choice for choice in CHOOSABLE}


def choice_for(name: str) -> Choice | None:
    """Look a card up by the catalogue's spelling.

    Args:
        name: whatever the caller wrote.

    Returns:
        The `Choice`, or None when this is not a name the catalogue knows. None
        is the answer that matters: it means the ask can never be filled, and
        saying so before submitting is the whole point of this module.
    """
    return BY_NAME.get((name or "").strip().lower())


def nvidia_smi_spelling(name: str) -> str | None:
    """Guess which card someone meant when they used nvidia-smi's vocabulary.

    Args:
        name: a name that `choice_for` did not recognise.

    Returns:
        The catalogue's spelling, or None if this does not look like an
        nvidia-smi name for anything we know.

    WHY THIS EXISTS RATHER THAN JUST ACCEPTING BOTH. Accepting both would hide
    the difference, and the difference is real: the same job reads its own
    `PACSRUN_GPU=` lines in nvidia-smi's vocabulary and asks for capacity in the
    catalogue's. A caller who writes the wrong one should be told which one to
    write, not quietly corrected.

    Example:
        >>> nvidia_smi_spelling("NVIDIA L40S")
        'L40S'
    """
    stripped = (name or "").strip()
    for prefix in ("NVIDIA ", "nvidia "):
        if stripped.startswith(prefix):
            candidate = stripped[len(prefix):].strip()
            if choice_for(candidate):
                return choice_for(candidate).name
            # nvidia-smi prints the board, the catalogue prints the family.
            # "A100-SXM4-80GB" and "A100-PCIE-40GB" are the two we have seen.
            head = candidate.split("-")[0]
            if head == "A100" and "80" in candidate and choice_for("A100-80GB"):
                return "A100-80GB"
            if head and choice_for(head):
                return choice_for(head).name
    return None


def has_been_measured(name: str) -> bool:
    """Have we ever rented this card, so that a cost estimate rests on something.

    Args:
        name: the catalogue's spelling.

    Returns:
        True when `measurements.GPUS` carries it. False means an estimate for
        this card can only be `unknown`.
    """
    return gpu_by_name(name) is not None
