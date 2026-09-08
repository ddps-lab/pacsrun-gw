"""Reading a running job's GPU usage and training progress out of its own log.

END-TO-END FLOW of one `/v1/jobs/{id}/metrics`:

  1. The job's `run.sh` prints one line every 30 seconds:
     `PACSRUN_GPU=94,38200,45440,71,298`
     and the training library already prints its own progress line whenever it
     finishes a step.
  2. Both land on the same stdout the driver pod relays, so both are already in
     the pod's log.
  3. `scan()` reads a window of that log and pulls the two line shapes out.
  4. `Metrics` carries the latest GPU reading, a downsampled series for a chart,
     the current step, and a runtime projection refreshed from the job's own
     observed pace.

WHY THERE IS NO DATABASE, AND THIS IS THE DESIGN DECISION IN THIS FILE. The
obvious shape for a metrics endpoint is a time series store, and it would mean
the server holds state for the first time: something to back up, something to
size, something that disagrees with the log when one of them is truncated. But
the data is ALREADY durable in the log, next to the output it describes, and
`read_namespaced_pod_log` takes a `since_seconds` window so reading a slice is
cheap. So nothing is stored, and the server stays a pure function of the
cluster.

WHAT THIS COSTS. When the pod is garbage-collected the metrics go with it,
exactly as the logs do. A finished job's chart is gone. That is a real
limitation and `docs/12-monitoring.md` states it rather than hiding it.

WHY THE PROGRESS LINE IS WORTH PARSING AT ALL, when `/v1/estimate` exists. The
estimate is made from other people's runs. This is made from THIS run, and after
about 50 steps it is better: bank-exp2v2's projection was 32% out at step 1 and
within 4% by step 50.

Grep anchor: DDPSRUN-METRICS
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# What PACSrun's drivers print every 30 seconds. The five fields are nvidia-smi's,
# in this order:
#   utilization.gpu, memory.used, memory.total, temperature.gpu, power.draw
#
# WHO PRINTS IT CHANGED, AND THE REGEX DELIBERATELY DID NOT. It used to be the
# researcher's run.sh, per section 9 of script-contract.md. PACSrun's drivers now
# prepend driver/common/gpu-watch.sh to every GPU workload on every vendor, so the
# line arrives whether or not the script cooperates (grep PACSRUN-GPU-WATCH). The
# format is frozen at five fields precisely so that both producers parse here and an
# old script that still has its own loop is not broken -- two watchers print two
# lines per interval and `scan` takes the latest.
GPU_LINE = re.compile(
    r"PACSRUN_GPU=(\d+),(\d+),(\d+),(\d+),([\d.]+)"
)

# THE PER-CARD LINE, sent since 2026-09-08 by driver/common/gpu-watch.sh — one
# per card, with the card's own nvidia-smi index first:
#   PACSRUN_GPU_CARD=<index>,<util>,<used>,<total>,<temp>,<power>
#
# WHY IT EXISTS. The watcher used to keep `head -1` and report card 0 alone, so
# baseline-c — four A100s in one pod — drew one card and three quarters of a $44
# run was invisible on this screen.
#
# A DIFFERENT PREFIX, NOT A SIXTH FIELD, and that is what keeps the two apart:
# `PACSRUN_GPU=(\d+),` cannot match "PACSRUN_GPU_CARD=", so a per-card line is
# never read as the old shape with the index mistaken for a percentage. Card 0
# still arrives on BOTH lines (the watcher sends the old one for compatibility);
# `scan` keys by index, so the two land on the same card and cannot double it.
GPU_CARD_LINE = re.compile(
    r"PACSRUN_GPU_CARD=(\d+),(\d+),(\d+),(\d+),(\d+),([\d.]+)"
)

# What the training library prints on its own. Two shapes have to be read,
# because tqdm writes the second one and the first is what appears once the
# elapsed and remaining times are known:
#   350/556 [4:02:35<2:22:44, 41.57s/it]
#   35%|###   | 350/556 [4:02:35<2:22:44, 41.57s/it]
PROGRESS_LINE = re.compile(
    r"(?P<done>\d+)/(?P<total>\d+)\s*\[(?P<elapsed>[\d:]+)<(?P<remaining>[\d:]+),\s*"
    r"(?P<pace>[\d.]+)s/it"
)

# Below this many steps the job's own pace is not yet worth quoting.
# bank-exp2v2 was 32% out at step 1, 8% out at step 5, and 4% out at step 50.
STEADY_STEPS = 50

# How many GPU samples to return at most. A 25-hour job produces about 3,000,
# and a chart 800 pixels wide cannot show more than this many anyway.
MAX_SAMPLES = 400


@dataclass(frozen=True)
class GpuSample:
    """One `nvidia-smi` reading.

    Attributes:
        utilization_percent: how busy the GPU was. A low number for a long
            stretch means the run is waiting on data rather than computing.
        memory_used_mib: what matters most. aiops-exp1 died of memory, and this
            curve approaching the total is the warning that never existed.
        memory_total_mib: what the card reports, which is less than the number
            printed on the box.
        temperature_c: degrees.
        power_w: watts drawn.
    """

    utilization_percent: int
    memory_used_mib: int
    memory_total_mib: int
    temperature_c: int
    power_w: float
    # Which card this reading is from, as nvidia-smi numbers them. 0 for a
    # reading off the old five-field line, which describes card 0 and carries
    # no index of its own.
    gpu_index: int = 0
    # When the apiserver stamped the log line this reading came from (RFC
    # 3339), "" when the line carried no stamp. This is the chart's x axis:
    # without it the screen could only say "somewhere in the last hour".
    time: str = ""

    @property
    def memory_percent(self) -> float:
        """How full the card is, 0 to 100."""
        if not self.memory_total_mib:
            return 0.0
        return round(100 * self.memory_used_mib / self.memory_total_mib, 1)


@dataclass(frozen=True)
class Progress:
    """Where the training run has got to, and what its own pace implies.

    Attributes:
        step: steps finished.
        total_steps: steps in the run.
        seconds_per_step: the library's own figure, which is a running average.
        elapsed: the library's own elapsed string, e.g. "4:02:35".
        remaining: the library's own remaining string.
        projected_total_hours: `total_steps * seconds_per_step`, in hours.
        steady: whether enough steps have run for the projection to be worth
            quoting. Below `STEADY_STEPS` it is not.
    """

    step: int
    total_steps: int
    seconds_per_step: float
    elapsed: str
    remaining: str
    projected_total_hours: float
    steady: bool

    @property
    def percent(self) -> float:
        """How far through, 0 to 100."""
        if not self.total_steps:
            return 0.0
        return round(100 * self.step / self.total_steps, 1)


@dataclass
class CardMetrics:
    """One GPU card's own readings, for a job that rents several.

    Attributes:
        gpu_index: the card, as nvidia-smi numbers it.
        series: that card's readings over the window, oldest first, downsampled.
        latest: its most recent reading.
        peak: the reading with the most memory in use — what a post-mortem
            asks for, since a finished job's LAST reading is the idle card.
        avg_utilization_percent: mean utilisation over the window.
    """

    gpu_index: int
    series: list[GpuSample] = field(default_factory=list)
    latest: GpuSample | None = None
    peak: GpuSample | None = None
    avg_utilization_percent: float | None = None


@dataclass
class Metrics:
    """Everything a monitoring screen needs about one job.

    Attributes:
        latest_gpu: the most recent reading, or None when the job has not
            printed one. A CPU job, or one whose image has no nvidia-smi, never
            will, which is not an error.
        gpu_series: readings over the window, oldest first, downsampled.
        progress: the training run's own position, or None before the first
            progress line.
        window_seconds: how far back the log was read.
        note: what the caller should know about what is missing, in plain words.
    """

    latest_gpu: GpuSample | None = None
    gpu_series: list[GpuSample] = field(default_factory=list)
    progress: Progress | None = None
    window_seconds: int = 0
    note: str = ""
    # The reading with the most memory in use — the number a post-mortem asks
    # for first, because memory is what kills runs (aiops-exp1), and because a
    # FINISHED job's latest_gpu is the idle card just before teardown (0%,
    # 0 MiB), which answers nothing about the run itself.
    peak_gpu: GpuSample | None = None
    avg_utilization_percent: float | None = None
    # ONE ENTRY PER CARD, lowest index first. A job renting four A100s used to
    # arrive here as one series — the watcher's `head -1` — and the screen drew
    # card 0 alone (baseline-c, 2026-09-08). The three fields above still
    # describe the LOWEST-indexed card so an older screen keeps working.
    cards: list[CardMetrics] = field(default_factory=list)


def parse_gpu_card(line: str) -> GpuSample | None:
    """Pull one CARD's reading out of a per-card log line.

    Args:
        line: a raw log line, possibly carrying an apiserver timestamp.

    Returns:
        A `GpuSample` with `gpu_index` set, or None when the line is not a
        per-card reading.

    Example:
        >>> parse_gpu_card("PACSRUN_GPU_CARD=3,99,44950,81920,63,366.04").gpu_index
        3
    """
    match = GPU_CARD_LINE.search(line)
    if not match:
        return None
    stamp = line.split(" ", 1)[0]
    timed = stamp if len(stamp) >= 20 and stamp[:2] == "20" and "T" in stamp else ""
    return GpuSample(
        gpu_index=int(match.group(1)),
        utilization_percent=int(match.group(2)),
        memory_used_mib=int(match.group(3)),
        memory_total_mib=int(match.group(4)),
        temperature_c=int(match.group(5)),
        power_w=float(match.group(6)),
        time=timed,
    )


def parse_gpu(line: str) -> GpuSample | None:
    """Pull one GPU reading out of a log line.

    Args:
        line: a raw log line.

    Returns:
        A `GpuSample`, or None when the line is not one.

    Example:
        >>> sample = parse_gpu("PACSRUN_GPU=94,38200,45440,71,298.5")
        >>> sample.memory_percent
        84.1
    """
    match = GPU_LINE.search(line)
    if not match:
        return None
    # The apiserver's timestamp prefix, when the log was read with
    # timestamps=True: "2026-09-07T11:49:33.19Z PACSRUN_GPU=...". The search
    # above never cared about the prefix; this only harvests it for the
    # chart's x axis.
    stamp = line.split(" ", 1)[0]
    timed = stamp if len(stamp) >= 20 and stamp[:2] == "20" and "T" in stamp else ""
    return GpuSample(
        utilization_percent=int(match.group(1)),
        memory_used_mib=int(match.group(2)),
        memory_total_mib=int(match.group(3)),
        temperature_c=int(match.group(4)),
        power_w=float(match.group(5)),
        time=timed,
    )


def parse_progress(line: str) -> Progress | None:
    """Pull the training run's position out of a log line.

    Args:
        line: a raw log line.

    Returns:
        A `Progress`, or None when the line is not one.

    Example:
        A real line from bank-exp2v2's log:

        >>> p = parse_progress("350/556 [4:02:35<2:22:44, 41.57s/it]")
        >>> p.step, p.total_steps, round(p.projected_total_hours, 2)
        (350, 556, 6.42)
    """
    match = PROGRESS_LINE.search(line)
    if not match:
        return None
    step = int(match.group("done"))
    total = int(match.group("total"))
    pace = float(match.group("pace"))
    return Progress(
        step=step,
        total_steps=total,
        seconds_per_step=pace,
        elapsed=match.group("elapsed"),
        remaining=match.group("remaining"),
        projected_total_hours=total * pace / 3600,
        steady=step >= STEADY_STEPS,
    )


def downsample(samples: list[GpuSample], limit: int = MAX_SAMPLES) -> list[GpuSample]:
    """Thin a series to at most `limit` points, keeping the shape.

    Args:
        samples: readings, oldest first.
        limit: how many to keep.

    Returns:
        Every nth sample, with the last one always kept so the chart ends where
        the job actually is.
    """
    if len(samples) <= limit:
        return samples
    stride = len(samples) // limit + 1
    thinned = samples[::stride]
    if thinned[-1] is not samples[-1]:
        thinned.append(samples[-1])
    return thinned


def scan(lines: object, window_seconds: int) -> Metrics:
    """Read a job's metrics out of its log.

    Args:
        lines: an iterable of log lines. The caller decides where they come
            from, which keeps this function testable without a cluster.
        window_seconds: how far back the caller asked the log to go, recorded
            so the answer can say what it covers.

    Returns:
        A `Metrics`. Every field may be empty: a job that has not started, or
        whose run.sh prints neither line shape, produces an empty answer with a
        `note` explaining which.
    """
    progress: Progress | None = None

    # Per-card readings, keyed by index, and the old-shape readings kept apart.
    # WHY APART: the watcher sends card 0 on BOTH lines (its per-card line and
    # the old five-field one, for readers that know only the old shape), so
    # counting both would give card 0 two samples per interval. If any per-card
    # line was seen the old ones are dropped; if none was, the old ones ARE the
    # answer and become card 0 — which is what a log written before 2026-09-08,
    # or by a researcher's own watch loop, contains.
    per_card: dict[int, list[GpuSample]] = {}
    legacy: list[GpuSample] = []

    for line in lines:
        card = parse_gpu_card(line)
        if card is not None:
            per_card.setdefault(card.gpu_index, []).append(card)
            continue
        sample = parse_gpu(line)
        if sample is not None:
            legacy.append(sample)
            continue
        # A progress line is overwritten many times a run; the last one wins.
        found = parse_progress(line)
        if found is not None:
            progress = found

    # WHY THESE TWO NOTES NO LONGER BLAME THE USER'S SCRIPT. They used to send the
    # reader to the script contract, because printing the GPU line was the script's
    # job. It is the platform's job now (see GPU_LINE above), so "no readings" no
    # longer means "you forgot". It means the pod has no card, the image has no
    # nvidia-smi, or nothing has run yet -- and the watcher says WHICH, in a
    # PACSRUN_GPU_WATCH line sitting in this same log. Pointing at that line is more
    # useful than pointing at a document, because it is evidence about THIS run.
    # Fold the old-shape readings in only when no per-card line was seen (the
    # reasoning is above, where they were collected), so that from here on
    # "samples" means "every reading this window has, whatever shape it came
    # in" — which is what the notes below and the fields at the end are about.
    if not per_card and legacy:
        per_card[0] = legacy
    samples = [r for readings in per_card.values() for r in readings]

    note = ""
    if not samples and progress is None:
        note = (
            "no GPU readings and no progress lines in this window. The job may not "
            "have started computing yet. If it has, look for a PACSRUN_GPU_WATCH "
            "line in the log: it says whether the GPU watcher started, or found no "
            "nvidia-smi in the image."
        )
    elif not samples:
        note = (
            "training progress is here but no GPU readings are. Look for a "
            "PACSRUN_GPU_WATCH line in the log: the usual causes are an image with "
            "no nvidia-smi, or a job that asked for no GPU."
        )
    elif progress is None:
        note = (
            "GPU readings are here but no training progress line is. The run may "
            "still be installing or downloading a model."
        )
    elif not progress.steady:
        note = (
            f"only {progress.step} steps have run, so this job's own projection is "
            f"not settled yet. bank-exp2v2's was 32% out at step 1 and within 4% "
            f"by step {STEADY_STEPS}."
        )

    cards = [
        CardMetrics(
            gpu_index=index,
            series=downsample(readings),
            latest=readings[-1],
            peak=max(readings, key=lambda r: r.memory_used_mib),
            avg_utilization_percent=round(
                sum(r.utilization_percent for r in readings) / len(readings), 1
            ),
        )
        for index, readings in sorted(per_card.items())
        if readings
    ]

    # The three single-card fields describe the LOWEST-INDEXED card, which is
    # the one they described before cards[] existed. An older screen therefore
    # sees exactly what it saw yesterday rather than a mixture of four cards.
    first = cards[0] if cards else None
    return Metrics(
        latest_gpu=first.latest if first else None,
        gpu_series=first.series if first else [],
        progress=progress,
        window_seconds=window_seconds,
        note=note,
        peak_gpu=first.peak if first else None,
        avg_utilization_percent=first.avg_utilization_percent if first else None,
        cards=cards,
    )
