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

from .measurements import aws_counts, gpu_by_name


@dataclass(frozen=True)
class Choice:
    """One card a request may name.

    Attributes:
        name: the catalogue's spelling. This is what goes into the PacsJob.
        vram_gb: the number printed on the card. A spec figure, not a measured
            one; `measurements.Gpu.usable_gib` is the measured one and exists
            only for cards we have rented.
        note: what to tell someone who picks it, or "" when there is nothing
            they need to know.

    WHAT USED TO BE HERE AND WHY IT LEFT. This carried `sold_singly: bool`, read
    off the CSV by hand on 2026-09-02. It is now derived, because it was BOTH
    duplicated and too coarse:

      duplicated  `measurements.aws_counts(name)` reads the same fact from the
                  same CSV, so two copies could disagree and only one was
                  generated.
      too coarse  "sold singly" is one cell of an 8-wide row. AWS offers the L40S
                  in machines of 1, 4 and 8, so a pod asking for 2 of them is
                  just as unfillable as a pod asking for 1 A100-80GB -- and the
                  boolean said nothing about it. Measured over all fourteen
                  cards at counts 1 through 8 with one pod: 82 of the 112 asks
                  cannot be filled, and the boolean caught 6 of them.
    """

    name: str
    vram_gb: int
    note: str = ""

    @property
    def sold_singly(self) -> bool:
        """Can a machine with exactly one of this card be bought in us-west-2.

        Returns:
            True when `aws_counts` contains 1. Kept as a name because it reads
            well at the one place a single-card ask is what matters; anything
            deciding about a REAL ask should call `measurements.aws_fillable`,
            which also knows the pod count.
        """
        return 1 in aws_counts(self.name)


# Every NVIDIA card the AWS catalogue offers in us-west-2, read on 2026-09-02
# from catalogs/v8/aws/vms.csv (55,420 rows). Ordered by memory, because that is
# what someone choosing is actually deciding between.
#
# Inferentia, Trainium and the FPGA rows are left out: they are not GPUs and
# nothing in this service can use them.
CHOOSABLE: tuple[Choice, ...] = (
    Choice("T4", 16),
    Choice("T4g", 16, "ARM host. An x86 container image will not run on it."),
    Choice("L4", 24),
    Choice("A10G", 24),
    Choice("RTX PRO 4500", 32),
    Choice("V100-32GB", 32),
    Choice("L40S", 48),
    Choice("RTXPRO6000", 96),
    Choice("A100", 40, "AWS sells it only as a whole 8-GPU machine. RunPod sells "
                       "it singly, but RunPod does not sell spot."),
    Choice("A100-80GB", 80, "AWS sells it only as a whole 8-GPU machine. RunPod "
                            "sells it singly, but RunPod does not sell spot."),
    Choice("H100", 80),
    Choice("H200", 141),
    Choice("B200", 180),
    Choice("B300", 288, "One availability zone in us-west-2 offers it, so a "
                        "zone-level outage leaves nowhere to retry."),
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
