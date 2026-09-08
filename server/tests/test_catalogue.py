"""Can the GPU a caller asked for actually be bought, and do we say so first.

DDPSRUN-CATALOGUE. Written after a job asked for "NVIDIA L40S" on spot on
2026-09-02 and sat in Pending forever, retrying the same failure every eleven
minutes. Two separate things were wrong and neither was visible before
submitting. Every test here is one of those two, or the vocabulary rule that
caused them.

The numbers come from the real catalogue: catalogs/v8/aws/vms.csv, 55,420 rows,
read 2026-09-02. us-west-2 has 32 L40S rows and 20 of them are single-GPU
machines, so the capacity was there the whole time.
"""

from __future__ import annotations

import pytest

from ddpsrun_server import catalogue, measurements, validate as v
from ddpsrun_server.estimate import Estimate


def findings(gpu_name=None, count=1, capacity="on-demand"):
    return v.check_gpu_is_buyable(gpu_name, count, capacity)


def codes(result):
    return {f.code for f in result}


# ------------------------------------------------------------------ 어휘


def test_the_nvidia_smi_spelling_is_refused_and_the_right_one_is_named():
    """The exact failure of 2026-09-02. PACSrun compares the name to the CSV's
    AcceleratorName with an exact match, so the prefix loses all 32 rows."""
    result = findings("NVIDIA L40S")
    assert "gpu-name-vocabulary" in codes(result)
    fix = next(f.fix for f in result if f.code == "gpu-name-vocabulary")
    assert "'L40S'" in fix


def test_the_board_suffix_is_understood_too():
    """nvidia-smi prints the board, the catalogue prints the family."""
    assert catalogue.nvidia_smi_spelling("NVIDIA A100-SXM4-80GB") == "A100-80GB"


def test_the_catalogue_spelling_passes():
    assert "gpu-name-vocabulary" not in codes(findings("L40S"))
    assert "gpu-name-unknown" not in codes(findings("L40S"))


def test_a_name_nobody_offers_lists_what_is_offered():
    result = findings("RTX 4090")
    assert "gpu-name-unknown" in codes(result)
    fix = next(f.fix for f in result if f.code == "gpu-name-unknown")
    for expected in ("T4", "L40S", "H100"):
        assert expected in fix


def test_no_gpu_asked_for_produces_nothing():
    assert findings(None) == []


# ------------------------------------------- 한 개짜리로 살 수 있는가


def test_a_card_sold_only_in_eights_is_refused_for_a_count_of_one():
    """The second half of the 2026-09-02 failure, and the one that survives even
    after the name is fixed. us-west-2 has zero A100-80GB rows with
    AcceleratorCount == 1.0."""
    assert "gpu-count-unfillable" in codes(findings("A100-80GB", count=1))


def test_the_same_ask_becomes_fillable_when_eight_pods_fill_the_machine():
    """WHY THE OLD UNCONDITIONAL ERROR WAS WRONG. PACSrun keeps a row only when
    `AcceleratorCount <= gpusPerPod * podCount`
    (PACSrun/pkg/decider/skycatalog/aws.go:333), so one pod asking for one card
    cannot have a p4de.24xlarge -- but EIGHT pods asking for one card each fill
    it exactly, and that ask is fine."""
    assert "gpu-count-unfillable" in codes(findings("A100-80GB", count=1))
    assert "gpu-count-unfillable" not in codes(
        v.check_gpu_is_buyable("A100-80GB", 1, None, 8))


def test_a_count_no_machine_carries_is_caught_even_though_it_is_not_one():
    """THE HAZARD THE COUNT BOX INTRODUCED on 2026-09-08. AWS sells the L40S in
    machines of 1, 4 and 8. A pod asking for 2 is refused on both sides -- a
    1-card machine is too small and a 4-card machine exceeds one pod's whole
    job -- and the old check passed it because the count was not 1. Measured
    over all fourteen cards at counts 1-8 with one pod: 82 of the 112 asks
    cannot be filled, and the old check caught 6."""
    unfillable = [
        (choice.name, count)
        for choice in catalogue.CHOOSABLE
        for count in range(1, 9)
        if not measurements.aws_cheapest(choice.name, count, 1)
    ]
    assert len(unfillable) == 82
    assert ("L40S", 2) in unfillable
    assert "gpu-count-unfillable" in codes(findings("L40S", count=2))


def test_the_remedy_names_a_count_that_would_actually_work():
    """A remedy that does not name a working value is a restatement of the
    problem. Every size it offers has to be one AWS really sells."""
    fix = [f for f in findings("L40S", count=2)
           if f.code == "gpu-count-unfillable"][0].fix
    assert "1, 4, 8" in fix
    assert "raise parallelism to 2" in fix


def test_the_same_card_is_fine_when_eight_are_asked_for():
    assert "gpu-not-sold-singly" not in codes(findings("A100-80GB", count=8))


def test_a_card_sold_singly_is_not_flagged():
    for name in ("T4", "L4", "A10G", "L40S", "H100"):
        assert "gpu-not-sold-singly" not in codes(findings(name)), name


# ---------------------------------------------------------- spot 과 vendor


def test_spot_says_it_leaves_aws_as_the_only_vendor():
    """RunPod does not sell spot and its decider refuses before reading the
    catalogue, so a spot ask silently loses a whole vendor."""
    assert "spot-excludes-runpod" in codes(findings("L40S", capacity="spot"))


def test_on_demand_does_not_say_it():
    assert "spot-excludes-runpod" not in codes(findings("L40S", capacity="on-demand"))


def test_the_spot_note_is_information_not_a_block():
    """It is a real choice, not a mistake. Blocking it would be wrong."""
    note = next(f for f in findings("L40S", capacity="spot")
                if f.code == "spot-excludes-runpod")
    assert note.level == v.INFO


# ------------------------------------------------- 재본 적 없는 카드


def test_a_card_we_have_never_rented_is_flagged_as_a_guess():
    """`measurements.GPUS` holds only cards we have actually paid for. Everything
    else can be chosen, and its cost figure is not a measurement."""
    assert "gpu-never-rented" in codes(findings("T4"))
    assert "gpu-never-rented" not in codes(findings("L40S"))


def test_measured_and_choosable_are_deliberately_different_lists():
    """Merging them forces a choice between a two-item dropdown and invented
    numbers. docs/04-estimate.md settles that: unknown beats a wrong number."""
    from ddpsrun_server.measurements import GPUS

    measured = {g.name for g in GPUS}
    choosable = {c.name for c in catalogue.CHOOSABLE}
    assert measured < choosable, "everything measured must also be choosable"
    assert len(choosable) > len(measured)


def test_every_measured_card_uses_the_catalogue_spelling():
    """The join between the two lists is the name, so a drift here silently
    turns every estimate into `unknown`."""
    from ddpsrun_server.measurements import GPUS, THROUGHPUT

    choosable = {c.name for c in catalogue.CHOOSABLE}
    for gpu in GPUS:
        assert gpu.name in choosable, gpu.name
    for row in THROUGHPUT:
        assert row.gpu in choosable, row.gpu


def test_the_screen_offers_exactly_what_the_catalogue_lists():
    """One list, not one per client. The dropdown is generated from CHOOSABLE."""
    import pathlib
    import re

    html = (pathlib.Path(__file__).resolve().parents[2] / "ui" / "index.html").read_text()
    block = html[html.index('<select id="f-gpu">'):]
    block = block[:block.index("</select>")]
    offered = set(re.findall(r'<option value="([^"]+)"', block))
    assert offered == {c.name for c in catalogue.CHOOSABLE}
