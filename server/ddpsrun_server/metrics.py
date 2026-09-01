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

# What `script-contract.md` section 8 tells a run.sh to print. The five fields
# are nvidia-smi's, in this order:
#   utilization.gpu, memory.used, memory.total, temperature.gpu, power.draw
GPU_LINE = re.compile(
    r"PACSRUN_GPU=(\d+),(\d+),(\d+),(\d+),([\d.]+)"
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
class Metrics:
    """Everything a monitoring screen needs about one job.

    Attributes:
        latest_gpu: the most recent reading, or None when the job has not
            printed one. A job whose run.sh does not follow section 8 of the
            script contract never will, which is not an error.
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
    return GpuSample(
        utilization_percent=int(match.group(1)),
        memory_used_mib=int(match.group(2)),
        memory_total_mib=int(match.group(3)),
        temperature_c=int(match.group(4)),
        power_w=float(match.group(5)),
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
    samples: list[GpuSample] = []
    progress: Progress | None = None

    for line in lines:
        sample = parse_gpu(line)
        if sample is not None:
            samples.append(sample)
            continue
        # A progress line is overwritten many times a run; the last one wins.
        found = parse_progress(line)
        if found is not None:
            progress = found

    note = ""
    if not samples and progress is None:
        note = (
            "no GPU readings and no progress lines in this window. Either the job "
            "has not started computing yet, or its run.sh does not print them. "
            "Section 8 of the script contract has the four lines that add them."
        )
    elif not samples:
        note = (
            "training progress is here but no GPU readings are. Add the "
            "PACSRUN_GPU= watcher from section 8 of the script contract."
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

    return Metrics(
        latest_gpu=samples[-1] if samples else None,
        gpu_series=downsample(samples),
        progress=progress,
        window_seconds=window_seconds,
        note=note,
    )
