"""Generate ddpsrun_server/prices.csv — every region the catalogue prices, not just one.

WHY A DATA FILE AND NOT A TUPLE IN measurements.py. The us-west-2-only table was 30
rows and fitted in the module. All 22 AWS regions is 304, plus GCP, and a
900-line literal would bury the measurements that share that file. This is
generated, so it also cannot be hand-edited into disagreeing with the catalogue.

★ TWO PRICE BASES, AND THEY ARE NOT COMPARABLE. This is the reason for the
`basis` column rather than one number per row:

  machine       AWS and RunPod. `usd_per_hour` is the whole unit that runs a
                pod, GPUs included, and `instance` names it. On AWS that is the
                EC2 machine and `Price` states it directly; dividing by the
                seats a pod takes gives a real per-pod-hour figure. On RunPod a
                machine IS a pod (decider.go:686), and the vendor publishes a
                per-GPU price, so runpod_rows() multiplies it by the card count
                to reach the same kind of number -- see that function for the
                invoice that measures the multiplication.
  accelerator   GCP. GPU rows carry an EMPTY InstanceType, because on GCP a GPU
                is an accelerator ATTACHED to a machine type and the catalogue
                prices the two separately. So $0.7192 for an L4 in
                asia-northeast3 is the card alone; the VM it hangs off is extra
                and this catalogue does not say which VM.

Putting those in one column would make GCP look cheapest whenever it is not, which
is the confident-wrong-number failure this service refuses. So `basis` travels
with every row, and anything that RANKS rows may only rank within one basis.

THREE VENDORS, TWO SOURCES, TWO READ DATES. aws and gcp are read out of the
SkyPilot catalogue in ~/.sky/catalogs/v8; runpod is not in that tree at all and
is read from RunPod's own catalog endpoint, the same one PACSrun's decider calls.
The header line the generator writes carries both dates, and every consumer that
prints "priced on" has to name the one that belongs to the rows it showed.

Run: python gen_prices_all.py > .../ddpsrun_server/prices.csv
"""
import os
import pathlib
import collections
import csv
import io
import json
import re
import sys
import urllib.request

AWS = os.path.expanduser("~/.sky/catalogs/v8/aws/vms.csv")
GCP = os.path.expanduser("~/.sky/catalogs/v8/gcp/vms.csv")
READ_ON = "2026-09-08"
MIN_GIB = 16.0

# ★ THE THIRD SOURCE IS NOT THE SkyPilot CATALOGUE. AWS and GCP come out of
# ~/.sky/catalogs/v8, which SkyPilot fetches; RunPod is not in that tree at all
# (checked 2026-09-09: v8 holds aws, gcp and common). Its prices come from the
# ONE endpoint PACSrun's own decider reads, so the table cannot disagree with
# what the cluster will be charged (pkg/decider/runpod/catalog.go:6). Refresh
# the snapshot with the RunPod API key that is already in the cluster:
#
#   KEY=$(kubectl get secret pacsrun-runpod -n pacsrun-system \
#           -o jsonpath='{.data.RUNPOD_API_KEY}' | base64 -d)
#   mkdir -p ~/.sky/catalogs/v8/runpod
#   curl -sS -H "Authorization: Bearer $KEY" -H "Accept: application/json" \
#     "https://api.runpod.io/v2/catalog/gpus?include=AVAILABILITY&cloud=SECURE&product=POD" \
#     -o ~/.sky/catalogs/v8/runpod/catalog-gpus-secure.json
#   unset KEY
#
# The response carries no credential, but it is a 20 KB vendor dump and this
# repository is PUBLIC, so it stays out of git exactly as the two vms.csv do.
RUNPOD = os.path.expanduser("~/.sky/catalogs/v8/runpod/catalog-gpus-secure.json")
RUNPOD_READ_ON = "2026-09-09"

# ★ THE CLUSTER'S OWN REFUSALS, READ OFF THE RUNNING ConfigMap RATHER THAN
# INVENTED, and a named-model ask does not escape them: decider.go:415-421 says
# so in as many words -- "THE BOUND APPLIES TO A NAMED MODEL TOO ... naming
# MI300X outright is a decline, not an override". A row for a card the cluster
# refuses would therefore be a price for something no ask can buy.
#
# Read on 2026-09-09 from `kubectl get configmap pacsrun-catalog-policy -n
# pacsrun-system`: vendorGpuDenySubstrings is the single fragment MIG (the rest
# of that value is a commented-out Blackwell block, and parseSet drops "#"
# lines -- internal/pacs/policy.go:492) and vendorMinGpuMemoryGB is 16. Both
# equal PACSrun's shipped defaults (builtinDenySubstrings,
# builtinMinGPUMemoryGB -- pkg/decider/runpod/decider.go:487-488), so this
# table matches a cluster that sets neither key as well as this one.
RUNPOD_DENY = ("MIG",)
RUNPOD_MIN_MEMORY_GB = 16

# ★ SHADEFORM IS FETCHED, NOT READ OFF DISK, and that is not a shortcut. `~/.sky/catalogs/v8`
# has aws, gcp, runpod and common on this machine and no shadeform directory -- SkyPilot writes
# one only for a cloud it is configured for. The DECIDER already reads this same URL with no
# credential (pkg/decider/skycatalog, PACSRUN-CSV-VENDOR), so fetching it here keeps the price
# table and the solve reading one source instead of two that can disagree.
SHADEFORM = ("https://raw.githubusercontent.com/skypilot-org/skypilot-catalog/master/"
             "catalogs/v8/shadeform/vms.csv")
SHADEFORM_READ_ON = "2026-09-10"

# ★★ THE UNIT BUG IN THAT CSV, and it must be handled here or every Shadeform row is dropped.
# Its GpuInfo writes GiB into the field named `SizeInMiB`: L4 = 24, H100 = 80, where aws writes
# 22888 and 81920. Read literally a Shadeform H100 is 0.078 GiB and falls under every floor.
# PACSrun's decider fixes it by plausibility rather than by vendor name
# (pkg/decider/skycatalog/decider.go, perGPUVRAMGiB): under 1024 the number is GiB, because no
# GPU has less than 1 GiB and the oldest card in these catalogues is a 16 GiB V100. Same rule
# here, same reason -- it stays right if upstream fixes it.
SHADEFORM_MIB_IS_GIB_BELOW = 1024

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
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


def matches_model(runpod_name: str, asked: str) -> bool:
    """Would PACSrun's RunPod decider treat this catalog entry as `asked`?

    A PORT OF matchesModel (pkg/decider/runpod/decider.go:661-669) and it has to
    stay one. Every RunPod row below claims "an ask spelled X reaches this card",
    and that function is the only thing in the system that decides whether it
    does -- the name compared is the USER's own word out of
    spec.resources.gpus.name, against RunPod's own short label, with no third
    vocabulary in between.

    THE RULE: upper-cased equal, or the RunPod label is the asked name followed
    by a space and a variant. The trailing space is the entire reason "L4" does
    not swallow "L40S".

        asked        RunPod label     verdict
        L40S         L40S             match, equal
        A100         A100 SXM         match, a variant of the same family
        L4           L40S             NO match, and the space is why
        A100-80GB    A100 SXM         NO match. That spelling is AWS's, not a
                                      family name, so the RunPod path declines it
        RTXPRO6000   RTX PRO 6000     NO match. AWS writes this one without
                                      spaces and our catalogue follows AWS

    Args:
        runpod_name: the `name` field of a catalog entry.
        asked: the card as our catalogue spells it (catalogue.CHOOSABLE).

    Returns:
        True when the decider would call them the same GPU model.
    """
    a = (runpod_name or "").strip().upper()
    b = (asked or "").strip().upper()
    if not a or not b:
        return False
    return a == b or a.startswith(b + " ")


def runpod_rows():
    """Every (card, RunPod GPU type, GPU count) Secure Cloud will sell us.

    ★ THE PUBLISHED PRICE IS PER GPU AND THIS FUNCTION MULTIPLIES IT. RunPod
    states one `price.secure` per GPU TYPE with no location dimension at all
    (catalog.go:8-9), and PACSrun prices a pod as `price.secure * gpusPerPod`
    (decider.go:774). So `usd_per_hour` here is the WHOLE POD, which is why
    these rows carry basis "machine" beside AWS's: for this vendor a machine IS
    a pod (decider.go:686), and the number means the same thing in both.

    THE MULTIPLICATION IS MEASURED, WHICH IS WHY IT IS ALLOWED HERE. baseline-c
    rented 4 x A100 SXM on 2026-09-04 at $1.59 per GPU-hour, so the arithmetic
    predicts $6.36/hour; RunPod's own `myself.currentSpendPerHr` read 6.388 on
    2026-09-07, which is 0.44% above the prediction, the remainder being the
    pod's disk. The invoice side is in .claude/memory/facts/cost-ledger.md.

    WHY THE FILTERS ARE PACSrun's, COPIED NOT CHOSEN. A price for a card the
    cluster refuses to buy is worse than no row, so the six tests below are the
    ones the decider itself applies: Secure and a real price and maxCount
    (sellableFor, decider.go:618-631), NVIDIA (isCUDA, decider.go:462), and the
    cluster bound's VRAM floor and deny fragments (vendorBound.allows,
    decider.go:573-589). The MIG partitions are the visible casualty -- "PRO
    6000 MIG 48GB" is on Secure Cloud at $1.09 with maxCount 16 as of
    2026-09-09, and no ask can reach it.

    WHY SPOT AND REGION ARE EMPTY. RunPod sells no spot at all, so `main` flags
    every row `no_spot`: an empty spot column that means "this vendor has none"
    must not be read as "we did not look". And the price has no region, so
    `region` is empty for the same reason GCP's `instance` is -- the vendor does
    not price that dimension.

    WHY BOTH VARIANTS OF A FAMILY SURVIVE while aws_rows() collapses to the
    cheapest instance per key. AWS's key is (card, count, region) and the region
    separates two rows; RunPod has no region, so the GPU type IS the row. They
    are also not interchangeable: read 2026-09-09, an ask for "H100" reaches
    H100 PCIe at $2.89/GPU, H100 NVL at $3.19 and H100 SXM at $3.49 -- a 21%
    spread over interconnect, and the caller may want to see which one it got.

    Returns:
        (rows, unreachable). `rows` is a list of the 10 columns `main` writes,
        `flags` excluded. `unreachable` is one (card, near-misses) pair per
        CHOOSABLE card RunPod will not sell under our spelling, where the second
        element holds RunPod's own labels for the same silicon when there are
        any -- the RTXPRO6000 case. Returned rather than printed so this stays a
        pure function, and returned rather than yielded because a generator
        cannot hand back a second value.
    """
    with open(RUNPOD) as handle:
        catalog = json.load(handle)["gpus"]

    deny = tuple(f.strip().upper() for f in RUNPOD_DENY if f.strip())
    sellable = []
    for entry in catalog:
        price = float((entry.get("price") or {}).get("secure") or 0)
        cap = int((entry.get("maxCount") or {}).get("secure") or 0)
        name = entry.get("name") or ""
        ident = entry.get("id") or ""
        if not entry.get("secure") or price <= 0 or cap < 1:
            continue
        if (entry.get("manufacturer") or "").strip().upper() != "NVIDIA":
            continue
        if int(entry.get("memory") or 0) < RUNPOD_MIN_MEMORY_GB:
            continue
        # The deny fragments are matched against BOTH the id and the short name,
        # case-insensitively, exactly as vendorBound.allows does (decider.go:577).
        if any(f in f"{ident} {name}".upper() for f in deny):
            continue
        sellable.append(entry)

    rows = []
    unreachable = []
    for card in sorted(CARDS):
        hits = [e for e in sellable if matches_model(e["name"], card)]
        if not hits:
            # A NEAR MISS IS THE CASE WORTH NAMING: RunPod has the silicon and
            # our spelling cannot reach it. Two tests, because the two real
            # instances of this fail in opposite directions and one rule catches
            # only one of them:
            #
            #   RTXPRO6000   our name is RunPod's with the spaces taken out, so
            #                comparing space-stripped finds 'RTX PRO 6000'.
            #   A100-80GB    our name is RunPod's family name plus AWS's memory
            #                suffix, so space-stripping finds nothing and the
            #                leading model run (A100) is what matches 'A100 SXM'.
            #
            # Both print RunPod's own label and its stated VRAM, so the reader
            # decides whether it is the same card rather than trusting the test.
            flat = card.replace(" ", "").upper()
            lead = re.match(r"[A-Za-z0-9]+", card)
            lead = lead.group(0).upper() if lead else ""
            near = sorted(
                f"{e['name']!r} ({e['memory']} GB)" for e in sellable
                if flat in e["name"].replace(" ", "").upper()
                or (lead and e["name"].replace(" ", "").upper().startswith(lead)))
            unreachable.append((card, near))
            continue
        for entry in sorted(hits, key=lambda e: e["name"]):
            per_gpu = float(entry["price"]["secure"])
            cap = int(entry["maxCount"]["secure"])
            # dataCenters is the ONLY place location appears in this response,
            # and since 2026-09-09 it lists only the data centers that can sell
            # the type right now -- the 2026-08-10 snapshot listed all 31 with
            # NONE for most, this one lists 5 for A100 SXM and all 5 have stock.
            # So this count is a STOCK reading and it goes stale in minutes,
            # which the `zones` docstring in measurements.py says out loud.
            seen_in = len(entry.get("dataCenters") or [])
            for count in range(1, cap + 1):
                rows.append(["runpod", "machine", card, count, "", entry["id"],
                             f"{per_gpu * count:.4f}", "", "", seen_in])
    return rows, unreachable


def shadeform_rows():
    """One row per (card, count, region) Shadeform sells. A MACHINE price, like aws and runpod.

    RETURNS (rows, notes). `notes` are the things a reader needs told rather than left to infer
    from an absence -- the same shape runpod_rows() uses.

    ★ THE BASIS IS `machine` AND NOT `accelerator`. Shadeform sells whole instances: the CSV's
    Price is for the machine including its host, exactly as an AWS instance type is. gcp is the
    only `accelerator` vendor in this table because GCP really does price cards separately from
    hosts.

    ★ THE INSTANCE NAME IS THE COMPOSITE `<cloud>_<type>` VERBATIM. `massedcompute_A100_sxm4_80Gx8`
    names the sub-provider and its type, and both halves are needed to buy it -- the create body
    takes `cloud` and `shade_instance_type` as separate required fields. Splitting it here would
    throw away the half a reader needs to know WHO the machine comes from, which on this
    marketplace decides the boot time, whether the address is NATed, and who to ask when it
    breaks (all three measured differing between hyperstack and massedcompute on 2026-09-10).

    ★ NO SPOT. The CSV has a SpotPrice column and it is empty for every row; the live API's 750
    availability entries were all `on_demand` on 2026-09-10. Flagged `no_spot`, the same
    statement runpod rows carry, because two empty columns otherwise cannot be told apart from
    "we never looked".
    """
    text = urllib.request.urlopen(SHADEFORM, timeout=60).read().decode("utf-8")
    # ★ MATCHED CASE-INSENSITIVELY, AND THE NEAR-MISS DETECTOR IS WHAT FOUND WHY. Shadeform
    # spells it `RTXPro6000` and CHOOSABLE spells it `RTXPRO6000`, which is AWS's spelling -- so
    # an exact-membership test dropped every row of a 96 GiB card that the compares had already
    # shown in stock. It is the same class of miss that cost RunPod two cards on 2026-09-09.
    #
    # THE CARD NAME WE WRITE IS OURS, not the vendor's: the table is read by `hourly_rate(card,
    # ...)` with a name a user typed, and the vendor's capitalisation is not part of that
    # contract. The driver already compares case-insensitively (shadeform.sellable_rows), so the
    # two halves agree.
    ours_by_lower = {c.lower(): c for c in CARDS}
    buckets = collections.defaultdict(list)
    seen_cards = set()
    for r in csv.DictReader(io.StringIO(text)):
        vendor_card = r.get("AcceleratorName")
        seen_cards.add(vendor_card)
        card = ours_by_lower.get(str(vendor_card or "").lower())
        if card is None:
            continue
        if not r.get("Price"):
            continue
        count = int(float(r["AcceleratorCount"] or 0) + 0.5)
        if count < 1:
            continue
        buckets[(card, count, r.get("Region") or "")].append(r)

    rows = []
    for (card, count, region), rs in sorted(buckets.items()):
        cheapest = min(rs, key=lambda r: float(r["Price"]))
        rows.append(["shadeform", "machine", card, count, region,
                     cheapest.get("InstanceType") or "",
                     f"{float(cheapest['Price']):.4f}", "", "", len(rs)])

    notes = []
    # WHAT WE ASK FOR AND SHADEFORM DOES NOT SELL. Printed rather than inferred: a card absent
    # from the table is indistinguishable from one nobody stocks.
    missing = sorted(c for c in CARDS if not any(r[2] == c for r in rows))
    for card in missing:
        # A NEAR MISS IS THE INTERESTING LINE: it means Shadeform HAS the card and our spelling
        # cannot reach it, which is a defect. A card with no near miss is simply not stocked.
        near = sorted(n for n in seen_cards
                      if n and card.lower().replace(" ", "") in str(n).lower().replace(" ", ""))
        notes.append((card, near))
    return rows, notes


def main():
    out = csv.writer(sys.stdout)
    out.writerow([f"# generated by tools/gen_prices_all.py. aws and gcp rows from "
                  f"~/.sky/catalogs/v8 on {READ_ON}; runpod rows from RunPod's own "
                  f"catalog API on {RUNPOD_READ_ON}; shadeform rows from the SkyPilot "
                  f"catalog on GitHub on {SHADEFORM_READ_ON}. Do not hand-edit."])
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
    runpod, unreachable = runpod_rows()
    shadeform, sf_missing = shadeform_rows()
    for row in list(aws_rows()) + list(gcp_rows()) + runpod + shadeform:
        vendor, _basis, _card, _count, _region, _inst, od, _lo, hi = row[:9]
        # `no_spot` is a STATEMENT, not a missing value. RunPod sells no spot at
        # all, and without the flag a reader has to guess whether the two empty
        # spot columns mean "none exists" or "we never looked" -- the same
        # distinction `unknown` carries everywhere else in this service.
        # shadeform joins runpod here for the same measured reason: its CSV's
        # SpotPrice column is empty on every row and the live API offered no
        # spot capacity at all on 2026-09-10.
        flags = "no_spot" if vendor in ("runpod", "shadeform") else ""
        if od and hi and float(hi) > float(od) * 1.001:
            flags = "spot_above_ondemand"
            inverted[vendor] += 1
        out.writerow(row + [flags])
        n[vendor] += 1
    for vendor, count in sorted(n.items()):
        print(f"# {vendor} {count} rows, {inverted[vendor]} flagged "
              f"spot_above_ondemand", file=sys.stderr)
    # ★ WHAT RUNPOD WILL NOT SELL UNDER OUR SPELLING, printed because a card
    # silently absent from 105 rows is indistinguishable from one RunPod does
    # not stock. A near miss on the right is the interesting line: it means
    # RunPod HAS the card and the name in catalogue.CHOOSABLE cannot reach it.
    for card, near in sf_missing:
        print(f"# shadeform sells no card matching {card!r}"
              + (f" -- but its catalogue lists {', '.join(near)}"
                 if near else ""),
              file=sys.stderr)
    for card, near in unreachable:
        print(f"# runpod sells no card matching {card!r}"
              + (f" -- but it sells {', '.join(near)}, which matchesModel does "
                 f"not accept for that spelling" if near else ""),
              file=sys.stderr)


if __name__ == "__main__":
    main()
