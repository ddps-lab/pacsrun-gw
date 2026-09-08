"""Generate ddpsrun_server/prices.csv — every region the catalogue prices, not just one.

WHY A DATA FILE AND NOT A TUPLE IN measurements.py. The us-west-2-only table was 30
rows and fitted in the module. All 22 AWS regions is 304, plus GCP, and a
900-line literal would bury the measurements that share that file. This is
generated, so it also cannot be hand-edited into disagreeing with the catalogue.

★ TWO PRICE BASES, AND THEY ARE NOT COMPARABLE. This is the reason for the
`basis` column rather than one number per row:

  machine       AWS. `Price` is the whole machine, GPUs included, and
                `InstanceType` names it. Dividing by the seats a pod takes gives
                a real per-pod-hour figure.
  accelerator   GCP. GPU rows carry an EMPTY InstanceType, because on GCP a GPU
                is an accelerator ATTACHED to a machine type and the catalogue
                prices the two separately. So $0.7192 for an L4 in
                asia-northeast3 is the card alone; the VM it hangs off is extra
                and this catalogue does not say which VM.

Putting those in one column would make GCP look cheapest whenever it is not, which
is the confident-wrong-number failure this service refuses. So `basis` travels
with every row, and anything that RANKS rows may only rank within one basis.

Run: python gen_prices_all.py > .../ddpsrun_server/prices.csv
"""
import collections
import csv
import re
import sys

AWS = "/Users/me/.sky/catalogs/v8/aws/vms.csv"
GCP = "/Users/me/.sky/catalogs/v8/gcp/vms.csv"
READ_ON = "2026-09-08"
MIN_GIB = 16.0

sys.path.insert(0, "/Users/me/ddps-projects/pacsrun-gw/server")
from ddpsrun_server.catalogue import CHOOSABLE            # noqa: E402

CARDS = {c.name for c in CHOOSABLE}


def card_gib(info: str) -> float | None:
    """Per-card memory out of the GpuInfo blob, in GiB. None when absent."""
    m = re.search(r"'SizeInMiB':\s*([0-9.]+)", info or "")
    return float(m.group(1)) / 1024 if m else None


def aws_rows():
    """One row per (card, gpus, region): the cheapest instance by on-demand price.

    The three filters are PACSrun's own, named in gen_prices.py's docstring and
    in measurements.py: round the fractional count, drop anything under the
    shipped 16 GiB floor, then pick the cheapest instance at each count.
    """
    buckets = collections.defaultdict(list)
    for r in csv.DictReader(open(AWS)):
        if r["AcceleratorName"] not in CARDS:
            continue
        count = int(float(r["AcceleratorCount"]) + 0.5)
        if count < 1:
            continue
        gib = card_gib(r["GpuInfo"])
        if gib is not None and gib < MIN_GIB:
            continue
        buckets[(r["AcceleratorName"], count, r["Region"])].append(r)

    for (card, count, region), rs in sorted(buckets.items()):
        by_inst = collections.defaultdict(list)
        for r in rs:
            by_inst[r["InstanceType"]].append(r)
        priced = {i: v for i, v in by_inst.items() if v[0]["Price"]}
        if priced:
            inst = min(priced, key=lambda i: float(priced[i][0]["Price"]))
            od = f'{float(by_inst[inst][0]["Price"]):.4f}'
        else:
            # No published on-demand rate: AWS sells some of the newest cards
            # through Capacity Blocks. Keep the row for its spot range.
            inst = sorted(by_inst)[0]
            od = ""
        got = by_inst[inst]
        spots = sorted(float(r["SpotPrice"]) for r in got if r["SpotPrice"])
        yield ["aws", "machine", card, count, region, inst, od,
               f"{spots[0]:.4f}" if spots else "",
               f"{spots[-1]:.4f}" if spots else "",
               len({r["AvailabilityZone"] for r in got})]


def gcp_rows():
    """One row per (card, gpus, region): that many accelerators alone.

    ★ GROUPED BY COUNT, AND THE FIRST DRAFT WAS NOT. GCP GPU rows carry an empty
    InstanceType and an `AcceleratorCount` of 1, 2, 4, 8 or 16, with `Price`
    scaling linearly: an A100 in asia-northeast3 is 1.70586 at count 1 and
    27.29373 at count 16. The first version of this function ignored the count,
    wrote `gpus` as 1, took the CHEAPEST on-demand (the 1-card row) and the
    min/max spot ACROSS ALL COUNTS -- so the A100 row claimed on-demand $1.7059
    with spot up to $25.1136, a 17x inversion. Caught by the ratio check below,
    which is why that check exists.

    GCP also offers counts AWS does not: 16 cards in one ask.
    """
    buckets = collections.defaultdict(list)
    for r in csv.DictReader(open(GCP)):
        if r.get("AcceleratorName") not in CARDS:
            continue
        if not r.get("Price"):
            continue
        count = int(float(r["AcceleratorCount"] or 0) + 0.5)
        if count < 1:
            continue
        buckets[(r["AcceleratorName"], count, r["Region"])].append(r)

    for (card, count, region), rs in sorted(buckets.items()):
        od = min(float(r["Price"]) for r in rs)
        spots = sorted(float(r["SpotPrice"]) for r in rs if r["SpotPrice"])
        zones = len({r.get("AvailabilityZone") or "" for r in rs})
        yield ["gcp", "accelerator", card, count, region, "", f"{od:.4f}",
               f"{spots[0]:.4f}" if spots else "",
               f"{spots[-1]:.4f}" if spots else "", zones]


def main():
    out = csv.writer(sys.stdout)
    out.writerow(["# generated by tools/gen_prices_all.py from ~/.sky/catalogs/v8 "
                  f"on {READ_ON}. Do not hand-edit."])
    out.writerow(["vendor", "basis", "card", "gpus", "region", "instance",
                  "usd_per_hour", "spot_low", "spot_high", "zones", "flags"])
    # ★ THE RATIO CHECK, AND WHAT IT FOUND TWICE.
    #
    # Spot is a discount, so a row whose spot exceeds its own on-demand price
    # deserves an explanation before it ships.
    #
    #   FIRST FIRING was my bug: gcp_rows ignored AcceleratorCount, so an A100
    #   row paired the 1-card on-demand price with the 16-card spot price and
    #   claimed spot was 17x on-demand. Fixed by grouping on the count.
    #
    #   SECOND FIRING is the DATA, not the grouping. 38 GCP rows still invert,
    #   consistently and by 5-17%, and both zones of a region agree with each
    #   other -- e.g. A100 x1 asia-northeast1 is Price 1.70586 / SpotPrice
    #   1.7915 in BOTH asia-northeast1-a and -c. So this catalogue really does
    #   list a GCP accelerator spot price above its on-demand price in some
    #   regions. Refusing to generate would only mean no GCP table at all, so
    #   the rows ship with a flag and nothing ranks them.
    #
    # Equality is allowed with a 0.1% tolerance: AWS spot genuinely reaches the
    # on-demand rate under pressure, and p4d.24xlarge does exactly that.
    n = collections.Counter()
    inverted = collections.Counter()
    for row in list(aws_rows()) + list(gcp_rows()):
        vendor, _basis, _card, _count, _region, _inst, od, _lo, hi = row[:9]
        flags = ""
        if od and hi and float(hi) > float(od) * 1.001:
            flags = "spot_above_ondemand"
            inverted[vendor] += 1
        out.writerow(row + [flags])
        n[vendor] += 1
    for vendor, count in sorted(n.items()):
        print(f"# {vendor} {count} rows, {inverted[vendor]} flagged "
              f"spot_above_ondemand", file=sys.stderr)


if __name__ == "__main__":
    main()
