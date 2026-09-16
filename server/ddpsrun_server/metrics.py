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

import json
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
#
# ★ AND THE RATE COMES IN TWO UNITS, WHICH IS WHY SOME JOBS HAD NO PROGRESS BAR
# AT ALL. tqdm prints seconds per iteration only while a step takes MORE than a
# second; the moment it takes less, the same bar flips to iterations per second
# and writes "9.52it/s". This pattern ended at `s/it`, so it matched nothing on
# any run faster than one step a second, and the screen showed no Progress panel
# whatever the job was doing. `market64-exp0` at 179.09s/it had a bar and a job
# at 9.52it/s had none (reported 2026-09-11). Both units are read now and
# `parse_progress` turns the second into the first, so everything downstream --
# the projection, STEADY_STEPS, the screen -- keeps working in seconds per step.
PROGRESS_LINE = re.compile(
    r"(?P<done>\d+)/(?P<total>\d+)\s*\[(?P<elapsed>[\d:]+)<(?P<remaining>[\d:]+),\s*"
    r"(?P<pace>[\d.]+)(?P<unit>s/it|it/s)"
)

# PACSRUN-METRIC-WATCH. The TRAINING'S OWN numbers, one JSON object per line,
# printed by driver/common/metric-watch.sh on the rented machine:
#
#   PACSRUN_METRIC={"_series":"bank/adapters/AD/iter_1","step":1,"score":2.44,...}
#
# ★ WHY THIS IS A DIFFERENT THING FROM PROGRESS_LINE ABOVE, which is easy to
# miss. The progress line says HOW FAR a run has got -- step 350 of 556 -- and
# says nothing about whether it is learning. On 2026-09-15 a job finished
# `Succeeded` with a perfect progress bar while one of its nine trainings went
# backwards: its objective averaged 2.850 over the first five steps and 2.140
# over the last five. Every log that job uploaded contained the word `loss`
# exactly zero times. These lines are what closes that gap.
#
# THE SHAPE IS DELIBERATELY NOT FIXED. Unlike GPU_LINE's five frozen fields, a
# training's fields are the training's own -- `loss` for one, `score` and `kl`
# for another, `train_loss` and `val_loss` for a third -- and pinning them here
# would mean the server decides what a researcher may measure. The only two keys
# this file knows are `_series`, which says WHICH training a row belongs to, and
# the step field, which orders them.
METRIC_LINE = re.compile(r"PACSRUN_METRIC=(?P<body>\{.*\})\s*$")

# The key metric-watch.sh puts the series label under. Anything else in the row
# is the training's own.
SERIES_KEY = "_series"

# Which field orders the rows, tried in this order. Mirrors STEP_KEYS in
# metric-watch.sh; a row that reaches here has already been filtered to rows that
# HAVE one, so this only has to find which.
STEP_KEYS = ("step", "global_step", "iteration", "iter", "_step")

# How many rows of one series to keep. A run logging every step for a long time
# would otherwise put an unbounded list in a JSON response; the trend is computed
# from the whole window before thinning, so the number below only limits what is
# RETURNED, never what is measured.
MAX_METRIC_ROWS = 200

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
        peak_utilization_percent: the HIGHEST utilisation in the window, which
            is a different reading from `peak` and had to become its own field.
            `peak` is chosen by MEMORY, and the screen used to label its
            utilisation "Utilisation at peak" — so a run whose single
            highest-memory sample happened to fall between steps was reported
            as 0%. Measured on job-66b46719b854 (2026-09-09, four A100s,
            785 samples per card): its 77,631 MiB sample read utilisation 0,
            while the card's real maximum was 100 and its mean 84.6. A reader
            given "0%" concludes the GPU was idle for a run that was not.
    """

    gpu_index: int
    series: list[GpuSample] = field(default_factory=list)
    latest: GpuSample | None = None
    peak: GpuSample | None = None
    avg_utilization_percent: float | None = None
    peak_utilization_percent: float | None = None
    # ★ HOW MANY READINGS THIS CARD ACTUALLY PRINTED, which `len(series)` is
    # NOT: `downsample` thins the series to at most MAX_SAMPLES so a chart can
    # draw it. job-66b46719b854 printed 785 readings per card and the screen
    # counted the 393 that survived the thinning, under a column headed
    # "Samples" (reported 2026-09-11). The mean and the peak above are computed
    # over all 785, so this is also the count those two are based on.
    sample_count: int = 0


@dataclass
class MetricSeries:
    """One training's own numbers, and what the arithmetic says about them.

    ★ THE JUDGEMENT IS ARITHMETIC AND IT STAYS THAT WAY. An AI reads these rows
    later to say WHICH column is the objective and to write the sentence a human
    reads, and it is good at both. It is not trusted with the verdict, and that
    is a measurement rather than a preference: asked on 2026-09-15 to judge the
    very series below, `solar-pro3` answered "the run is learning and improving,
    score rises from ~2.44 to a peak of ~3.44" about a series whose slope is
    -0.0269 per step and whose last five steps average 25% below its first five.
    It had picked a mid-run peak and called it the end. The day before, it
    reported a steady `loss` in a log that contains no loss at all.

    So the fields below are computed here, from the rows, with no model
    involved. Whatever an AI later says about them is commentary printed beside
    a number that was already decided.

    Attributes:
        name: which training this is, from the `_series` key. A job that trains
            nine times produces nine of these.
        step_key: which field ordered the rows.
        rows: the rows themselves, oldest first, thinned to MAX_METRIC_ROWS.
        row_count: how many rows there were BEFORE thinning.
        first_step / last_step: the range the rows cover.
        fields: every numeric field seen, sorted. What an AI is asked to pick
            the objective from.
        trends: field name -> `MetricTrend`. One per numeric field, so nothing
            here has to guess which one matters.
    """

    name: str
    step_key: str = "step"
    rows: list[dict] = field(default_factory=list)
    row_count: int = 0
    first_step: int = 0
    last_step: int = 0
    fields: list[str] = field(default_factory=list)
    trends: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MetricTrend:
    """Which way one field moved, by two measures that disagree in useful ways.

    WHY BOTH A SLOPE AND A HEAD/TAIL COMPARISON, rather than picking one. They
    fail differently, and a reader who sees them disagree has learned something.
    The slope is a least-squares fit over every row, so it is steady but a single
    wild value drags it. The head/tail comparison averages the first and last few
    rows and ignores everything between, so it is blind to a run that improved
    and then collapsed back -- which the slope catches.

    On the real series that prompted this: slope -0.0269 per step, head 2.850,
    tail 2.140. Both say the same thing, which is why that run was a clear case.

    Attributes:
        slope: change per step, by least squares over every row.
        head / tail: the mean of the first and last `window` rows.
        change_ratio: (tail - head) / |head|, or None when head is 0.
        has_nan: whether any value was NaN or infinite. A single one is enough
            to say the training is broken, whatever the other numbers look like.
        window: how many rows went into head and tail.
    """

    slope: float
    head: float
    tail: float
    change_ratio: float | None
    has_nan: bool
    window: int


def parse_metric(line: str) -> dict | None:
    """Pull one training row out of a `PACSRUN_METRIC=` log line.

    Args:
        line: one line of the job's log, timestamp and all.

    Returns:
        The row as a dict, or None when the line is not one of ours or will not
        parse. A half-written line -- the relay can split one, though it has not
        been seen to -- is dropped rather than guessed at.
    """
    match = METRIC_LINE.search(line)
    if match is None:
        return None
    try:
        row = json.loads(match.group("body"))
    except ValueError:
        return None
    return row if isinstance(row, dict) else None


def _step_of(row: dict) -> tuple[str, int] | None:
    """Which field orders this row, and its value."""
    for name in STEP_KEYS:
        value = row.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return name, value
    return None


def trend_of(values: list[float], window: int = 5) -> MetricTrend:
    """What one field's values did, by least squares and by head against tail.

    Args:
        values: the field's values in step order. Two or more.
        window: how many from each end go into head and tail. Clamped to half
            the series, so a six-row run does not compare rows 1-5 against rows
            2-6 and call the overlap a trend.

    Returns:
        A `MetricTrend`. A series carrying a NaN gets `has_nan` and zeros:
        arithmetic on NaN propagates silently, and a slope of nan reads as "no
        answer" when the honest answer is "this training is broken".
    """
    clean = [v for v in values if isinstance(v, (int, float))]
    if any(v != v or v in (float("inf"), float("-inf")) for v in clean):
        return MetricTrend(slope=0.0, head=0.0, tail=0.0, change_ratio=None,
                           has_nan=True, window=0)
    n = len(clean)
    if n < 2:
        return MetricTrend(slope=0.0, head=0.0, tail=0.0, change_ratio=None,
                           has_nan=False, window=0)

    width = max(1, min(window, n // 2))
    head = sum(clean[:width]) / width
    tail = sum(clean[-width:]) / width

    mean_x = (n - 1) / 2
    mean_y = sum(clean) / n
    denominator = sum((i - mean_x) ** 2 for i in range(n))
    slope = 0.0 if denominator == 0 else sum(
        (i - mean_x) * (y - mean_y) for i, y in enumerate(clean)) / denominator

    ratio = None if head == 0 else (tail - head) / abs(head)
    return MetricTrend(slope=slope, head=head, tail=tail, change_ratio=ratio,
                       has_nan=False, window=width)


def series_from_rows(rows: list[dict]) -> list[MetricSeries]:
    """Group rows by training, order them, and measure every numeric field.

    Args:
        rows: every `PACSRUN_METRIC=` row in the window, in the order the log
            had them.

    Returns:
        One `MetricSeries` per training, named in the order first seen. A row
        with no step field is dropped: it cannot be placed in a series, and
        keeping it would put an unordered point in the middle of a trend.
    """
    grouped: dict[str, dict[int, dict]] = {}
    step_keys: dict[str, str] = {}
    for row in rows:
        found = _step_of(row)
        if found is None:
            continue
        key, step = found
        name = str(row.get(SERIES_KEY) or "")
        # LAST ROW WINS FOR A REPEATED STEP. metric-watch.sh re-prints what it
        # already printed when it could not write its state file, so duplicates
        # are expected and are not an error. Keying on the step number is what
        # makes that harmless.
        grouped.setdefault(name, {})[step] = row
        step_keys.setdefault(name, key)

    out: list[MetricSeries] = []
    for name, by_step in grouped.items():
        ordered = [by_step[s] for s in sorted(by_step)]
        key = step_keys[name]
        names = sorted({k for row in ordered for k, v in row.items()
                        if k not in (SERIES_KEY, key)
                        and isinstance(v, (int, float)) and not isinstance(v, bool)})
        trends = {}
        for field_name in names:
            values = [row[field_name] for row in ordered if field_name in row]
            if len(values) >= 2:
                trends[field_name] = trend_of(values)
        # Thinned by taking every k-th row, so the first and last survive: those
        # two are what a head/tail reading is computed from upstream, and a
        # reader who asks "where did it start" must not be shown row 40.
        keep = ordered
        if len(ordered) > MAX_METRIC_ROWS:
            stride = len(ordered) / MAX_METRIC_ROWS
            keep = [ordered[int(i * stride)] for i in range(MAX_METRIC_ROWS - 1)]
            keep.append(ordered[-1])
        steps = sorted(by_step)
        out.append(MetricSeries(
            name=name, step_key=key, rows=keep, row_count=len(ordered),
            first_step=steps[0], last_step=steps[-1], fields=names, trends=trends))
    return out


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
    # The highest utilisation seen, which peak_gpu's own utilisation is NOT --
    # see CardMetrics.peak_utilization_percent for the run that made the
    # difference visible.
    peak_utilization_percent: float | None = None
    # Readings the LOWEST-indexed card printed, the companion of gpu_series in
    # the same way peak_gpu and the two utilisation fields are: gpu_series has
    # been thinned for drawing and this has not. See CardMetrics.sample_count.
    sample_count: int = 0
    # ONE ENTRY PER CARD, lowest index first. A job renting four A100s used to
    # arrive here as one series — the watcher's `head -1` — and the screen drew
    # card 0 alone (baseline-c, 2026-09-08). The four fields above still
    # describe the LOWEST-indexed card so an older screen keeps working.
    cards: list[CardMetrics] = field(default_factory=list)
    # PACSRUN-METRIC-WATCH. One entry per TRAINING, which is not one per job: a
    # job that trains nine adapters produces nine, each with its own steps
    # starting at 1. Empty for a job whose image has no python3, and for one
    # that keeps its numbers in memory and writes them once at the end -- which
    # nothing outside that process can see, and the note below says so.
    metric_series: list[MetricSeries] = field(default_factory=list)


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


def _elapsed_seconds(text: str) -> int:
    """`4:02:35` or `05:54` as a number of seconds. 0 for anything unreadable.

    tqdm writes H:MM:SS once an hour has passed and MM:SS before that, so both shapes
    turn up in one log. Unreadable answers 0, which loses a comparison rather than
    winning one -- the direction that cannot promote a bar nobody can read.
    """
    parts = text.split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return 0
    seconds = 0
    for value in numbers:
        seconds = seconds * 60 + value
    return seconds


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
    # "9.52it/s" is 0.105 seconds per step. Everything below this line, and
    # every reader of Progress, works in seconds per step; converting here is
    # what keeps the unit out of the rest of the file. A rate of 0 cannot be
    # inverted and is not a real reading either, so the line is discarded.
    # A rate of zero is not a reading in either unit. tqdm prints `0.00s/it` on the
    # first line of a bar, before any item has finished; only the `it/s` half of this
    # was guarded, so that first line produced a Progress whose projected total was
    # `total * 0 / 3600` -- 0.00 h.
    if pace <= 0:
        return None
    if match.group("unit") == "it/s":
        pace = 1 / pace
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
    metric_rows: list[dict] = []

    for line in lines:
        # PACSRUN-METRIC-WATCH first, and the order matters for cost rather than
        # correctness: a long training prints far more of these than GPU lines,
        # and the three regexes below would each run over every one of them.
        row = parse_metric(line)
        if row is not None:
            metric_rows.append(row)
            continue
        card = parse_gpu_card(line)
        if card is not None:
            per_card.setdefault(card.gpu_index, []).append(card)
            continue
        sample = parse_gpu(line)
        if sample is not None:
            legacy.append(sample)
            continue
        # ★★ THE LONGEST-RUNNING BAR WINS, NOT THE LAST ONE, AND THE DIFFERENCE IS
        # WHAT THE SCREEN SHOWED. "The last one wins" is right for ONE tqdm bar and
        # wrong for a run that has several -- and a Hugging Face training has several:
        # the training loop, the dataset `map`, and one per checkpoint write.
        #
        # Measured on job-a72cfb29b593 (2026-09-16). The last progress-shaped line in
        # its log was not the training's:
        #     4000/4000 [05:54<00:00, 11.29it/s]   <- the training
        #     1/1 [00:00<00:00,  4.21it/s]         <- `Writing model shards`, and this
        #                                             is the line that won
        # so the panel read Elapsed 00:00, Remaining 00:00, Projected total 0.00 h on a
        # job that had just trained for six minutes.
        #
        # ELAPSED IS THE TEST, not the step total: a dataset `map` over 8,000 rows has a
        # bigger total than a 4,000-step training and is over in seconds. "Which of
        # these bars IS the run" is a question about time, so it is answered with time.
        found = parse_progress(line)
        if found is not None and (
                progress is None
                or _elapsed_seconds(found.elapsed) >= _elapsed_seconds(progress.elapsed)):
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
            peak_utilization_percent=max(r.utilization_percent for r in readings),
            # `readings`, not the downsampled `series` above it.
            sample_count=len(readings),
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
        peak_utilization_percent=first.peak_utilization_percent if first else None,
        sample_count=first.sample_count if first else 0,
        cards=cards,
        metric_series=series_from_rows(metric_rows),
    )
