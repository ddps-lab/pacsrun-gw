"""What a caller may send, what they get back, and the translation between the two.

END-TO-END FLOW of this file:

  1. `SubmitRequest` is what `POST /v1/jobs` accepts. It holds only things a
     user can actually know: an image, a command, environment values, which GPU,
     roughly how long. It has no namespace, no ServiceAccount, no result path.
  2. `to_pacsjob()` takes that request plus the caller's `Principal` and the
     server `Settings` and returns the PacsJob object to POST to Kubernetes. The
     fields the user did not send are filled in here from identity, never from
     the request — that is the whole point (`docs/03-api.md`, the table titled
     "서버가 채우는 것").
  3. `JobView.from_pacsjob()` goes the other way: it takes the object Kubernetes
     returns and produces the response, dropping every internal name on the way
     out (`docs/03-api.md`, 응답 규칙 첫째).

WHAT THIS STAGE DELIBERATELY DOES NOT DO. `docs/08-plan.md` stage 1 says
"판단은 아직 없다". So nothing here chooses a region, decides on-demand versus
spot, turns fetch mode on, or estimates a duration. `placement` is left off the
object entirely, which makes PACSrun apply its own defaults exactly as it does
for a hand-written PacsJob today. Those decisions arrive in stage 3 with
`/validate` and `/estimate`.

Grep anchors: DDPSRUN-SERVER-FILLS, DDPSRUN-NO-INTERNAL-NAMES
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from . import naming
from .auth import Principal
from .config import PACSJOB_GROUP, PACSJOB_VERSION, Settings

# Environment variable names a user may not set. PACSrun's controller already
# refuses them (`internal/controller/pacsjob_controller.go`, PACSRUN-ENV-GUARD),
# but rejecting here produces a message that names the offending variable
# instead of a controller error the user never sees.
RESERVED_ENV_PREFIX = "PACSRUN_"

# Recorded on the object so that stage 3 can compare what `/estimate` predicted
# with what the job actually took. Nothing reads it yet.
EXPECTED_HOURS_ANNOTATION = "ddpsrun.io/expected-hours"


class GpuRequest(BaseModel):
    """Which GPU the job wants, in the two styles PACSrun's CRD accepts.

    The CRD enforces "exactly one of gpus.name or gpus.vramGB" with a CEL rule
    (`config/crd/pacsrun.io_pacsjobs.yaml:197`). Repeating the rule here turns a
    Kubernetes admission error into a 400 that says which field to fix.
    """

    vram_gb: int | None = Field(
        default=None,
        ge=1,
        description="Minimum memory per GPU, as the vendor prints it on the card: "
        "24 for an L4, 48 for an L40S, 80 for an H100.",
    )
    name: str | None = Field(
        default=None,
        description="Exact GPU model as the catalog spells it, e.g. L40S, A100-SXM4-80GB.",
    )
    count: int = Field(default=1, ge=1, le=8, description="How many GPUs per pod.")

    @model_validator(mode="after")
    def exactly_one_style(self) -> "GpuRequest":
        if (self.vram_gb is None) == (self.name is None):
            raise ValueError("give exactly one of gpu.vram_gb or gpu.name")
        return self


class SubmitRequest(BaseModel):
    """The body of `POST /v1/jobs`.

    Note what is missing: namespace, serviceAccountName, resultPath, placement.
    A caller cannot set them, so a caller cannot write into another user's
    folder or run as another user's identity.
    """

    name: str = Field(
        min_length=1,
        max_length=63,
        description="A name for your own benefit. It appears in the result path "
        "and in the job listing. It does not have to be unique.",
    )
    image: str = Field(min_length=1, description="Container image to run.")
    command: list[str] | None = Field(
        default=None,
        description="Entry point override. Leave unset to keep the image's own.",
    )
    args: list[str] | None = Field(
        default=None,
        description="Arguments. With RunPod these REPLACE the image's CMD, "
        "which is how a one-line workload is expressed today.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Non-secret configuration, passed to the container verbatim.",
    )
    secrets: list[str] = Field(
        default_factory=list,
        description="Names of secrets to inject. The value never travels through "
        "this API; the server resolves the name to a Kubernetes Secret.",
    )
    gpu: GpuRequest | None = Field(
        default=None, description="Omit for a CPU-only job."
    )
    cpus: str | None = Field(default=None, description='CPU request, e.g. "4".')
    memory: str | None = Field(default=None, description='Memory request, e.g. "16Gi".')
    parallelism: int = Field(
        default=1,
        ge=1,
        le=256,
        description="How many pods run at once. They are INDEPENDENT workers that never talk "
        "to each other, so this is for a batch you can split, not for distributed training. "
        "The placement decides the machines: several pods may land on one multi-GPU box or on "
        "one box each. Combine with gpu.count, which is GPUs PER POD.",
    )
    expected_hours: float | None = Field(
        default=None,
        gt=0,
        description="Your own guess at the runtime. Recorded, not yet acted on.",
    )

    @model_validator(mode="after")
    def something_to_run(self) -> "SubmitRequest":
        # An image with neither command nor args runs whatever the image's own
        # ENTRYPOINT/CMD is. That is legitimate, so this is not an error — but a
        # reserved environment name never is.
        for key in self.env:
            if key.startswith(RESERVED_ENV_PREFIX):
                raise ValueError(
                    f"env[{key!r}] uses the reserved prefix {RESERVED_ENV_PREFIX!r}. "
                    f"Those names belong to the job runner."
                )
        overlap = sorted(set(self.env) & set(self.secrets))
        if overlap:
            raise ValueError(
                f"{', '.join(overlap)} appears in both env and secrets. "
                f"Pick one: env for a literal, secrets for a stored value."
            )
        return self


class SubmitResponse(BaseModel):
    """What `POST /v1/jobs` returns."""

    job_id: str
    name: str
    result_path: str = Field(
        description="Where this job's output will be written. Yours to read."
    )


class JobView(BaseModel):
    """What `GET /v1/jobs/{id}` returns.

    Every field here is safe to show a user. Everything the object also carries
    that is not — namespace, ServiceAccount name, the excluded-offering list,
    the blamed node names — is dropped in `from_pacsjob`.
    """

    job_id: str
    name: str
    phase: str = Field(
        description="Pending, Starting, Running, Recovering, Succeeded, or Failed. "
        "Empty until the controller has looked at the job once."
    )
    message: str = Field(default="", description="Detail, mostly on failure.")
    created_at: str | None = None
    gpu: str | None = Field(
        default=None, description="What it is actually running on, once it is running."
    )
    vendor: str | None = Field(
        default=None, description="Who it was rented from, e.g. runpod, aws."
    )
    recovery_count: int = Field(
        default=0,
        description="How many times the job lost its machine and was restarted.",
    )
    result_path: str | None = Field(
        default=None,
        description="Where the output is. This is the one place a namespace name "
        "crosses the API boundary, because it is part of the S3 key and a "
        "stage-1 caller has no other way to collect their results. It goes away "
        "when /v1/jobs/{id}/artifacts starts handing out download URLs.",
    )

    @staticmethod
    def from_pacsjob(obj: dict[str, Any]) -> "JobView":
        """Build the response from the raw object the Kubernetes API returned.

        Args:
            obj: the PacsJob as a plain dict (the dynamic client gives us JSON,
                not a typed object).

        Returns:
            A `JobView`. Unknown or absent fields become defaults rather than
            raising: a job the controller has not touched yet has no `status` at
            all, and that is the normal first second of every job's life.
        """
        metadata = obj.get("metadata") or {}
        labels = metadata.get("labels") or {}
        annotations = metadata.get("annotations") or {}
        spec = obj.get("spec") or {}
        status = obj.get("status") or {}

        job_id = labels.get(naming.JOB_ID_LABEL) or naming.job_id_from_object_name(
            metadata.get("name", "")
        )

        # DDPSRUN-NO-INTERNAL-NAMES: status.currentOffering has exactly four
        # fields — vendor, instanceType, zone, region (`api/v1alpha1/
        # shared_types.go:131`). The first two answer "what am I running on";
        # zone and region answer "where in our account", which is our business
        # and not the caller's, so they stay behind.
        offering = status.get("currentOffering") or {}
        gpu = offering.get("instanceType") or None
        vendor = offering.get("vendor") or None

        return JobView(
            job_id=job_id or "",
            # The annotation first: it holds the name the user typed, Korean and
            # all. The label is the ASCII remains of it, and the object name is
            # the last resort for a PacsJob somebody applied by hand.
            name=(
                annotations.get(naming.DISPLAY_NAME_ANNOTATION)
                or labels.get(naming.DISPLAY_NAME_LABEL)
                or metadata.get("name", "")
            ),
            phase=status.get("phase", ""),
            message=status.get("message", ""),
            created_at=metadata.get("creationTimestamp"),
            gpu=gpu,
            vendor=vendor,
            recovery_count=int(status.get("recoveryCount", 0) or 0),
            result_path=spec.get("resultPath"),
        )


def result_path_for(settings: Settings, principal: Principal, job_id: str, name: str) -> str:
    """Build the one S3 location this job is allowed to write to.

    The namespace comes from the token, so a caller cannot aim this anywhere
    else. PACSrun's own guard checks the same prefix a second time on the
    cluster side (PACSRUN-RESULT-TENANCY), which is what makes a hand-applied
    PacsJob obey the rule too.

    Args:
        settings: holds the bucket and the prefix.
        principal: supplies the namespace.
        job_id: makes the folder unique even when two jobs share a name.
        name: the user's display name, for a folder they can recognise.

    Returns:
        An `s3://` URI ending in a slash.

    Example:
        s3://<RESULT_BUCKET>/pacsrun/lab-alice/bank-exp2-3f9a1c4e7b02/
    """
    folder = naming.label_value(name) or "job"
    suffix = job_id[len(naming.JOB_ID_PREFIX):]
    return (
        f"s3://{settings.result_bucket}/{settings.result_prefix}"
        f"{principal.namespace}/{folder}-{suffix}/"
    )


def to_pacsjob(
    request: SubmitRequest,
    principal: Principal,
    settings: Settings,
    job_id: str,
    capacity_type: str | None = None,
) -> dict[str, Any]:
    """Turn a submit request into the PacsJob object to create.

    DDPSRUN-SERVER-FILLS. Four things the user did not send are added here:

      namespace           from the token
      serviceAccountName  from cluster settings
      resultPath          from the token's namespace plus the job id

    Since stage 3 there is a fifth: `placement.capacityType`.

    THAT ONE IS NOT A PREFERENCE, IT IS A CORRECTNESS FIX. An empty
    capacityType means spot (`pkg/decider/decider.go:331`) and PACSrun defaults
    a job to spot (`internal/controller/placement.go:202`), while RunPod's
    decider declines any request that is not on-demand before it even reads the
    catalogue (`pkg/decider/runpod/decider.go:242`). So a job that says nothing
    quietly loses RunPod as a candidate. `estimate.capacity_type` decides what
    to write and why.

    Args:
        request: the validated body.
        principal: the authenticated caller.
        settings: cluster-wide configuration.
        job_id: the id already minted for this submission.
        capacity_type: "on-demand" or "spot". None writes no placement at all,
            which is stage 1's behaviour and is what the tests for the identity
            fields still exercise.

    Returns:
        A dict ready to POST to the Kubernetes API.

    Raises:
        ValueError: the request names a secret the operator has not bound.
    """
    env_entries: list[dict[str, Any]] = [
        {"name": key, "value": value} for key, value in sorted(request.env.items())
    ]

    for secret_name in sorted(set(request.secrets)):
        binding = settings.secret_bindings.get(secret_name)
        if binding is None:
            allowed = ", ".join(sorted(settings.secret_bindings)) or "(none configured)"
            raise ValueError(
                f"there is no secret called {secret_name!r}. Available: {allowed}"
            )
        # secretKeyRef and never a literal: the CRD's own description explains
        # that a literal here leaks through `kubectl get -o yaml`, events,
        # controller logs, backups and audit logs.
        env_entries.append(
            {
                "name": secret_name,
                "valueFrom": {
                    "secretKeyRef": {"name": binding.secret_name, "key": binding.secret_key}
                },
            }
        )

    resources: dict[str, Any] = {}
    if request.cpus:
        resources["cpus"] = request.cpus
    if request.memory:
        resources["memory"] = request.memory
    if request.gpu is not None:
        gpus: dict[str, Any] = {"count": request.gpu.count}
        if request.gpu.vram_gb is not None:
            gpus["vramGB"] = request.gpu.vram_gb
        else:
            gpus["name"] = request.gpu.name
        resources["gpus"] = gpus

    spec: dict[str, Any] = {
        "image": request.image,
        # THIS WAS HARDCODED TO 1 AND THAT WAS WRONG. PacsJob's parallelism is the number of
        # independent worker PODS, and gpus.count is the number of GPUs each pod gets; the two
        # together are how a job fills a multi-GPU machine. Pinning it at 1 quietly removed
        # that, so a user asking for eight workers on two 4-GPU boxes got one worker. The
        # ceiling of 256 is PacsJob's own: status.completedSlots is capped at 256 entries
        # (config/crd/pacsrun.io_pacsjobs.yaml), and a job with more slots than that cannot
        # record which of them finished.
        "parallelism": request.parallelism,
        "serviceAccountName": settings.service_account,
        "resultPath": result_path_for(settings, principal, job_id, request.name),
    }
    if request.command:
        spec["command"] = request.command
    if request.args:
        spec["args"] = request.args
    if env_entries:
        spec["env"] = env_entries
    if resources:
        spec["resources"] = resources
    if capacity_type:
        spec["placement"] = {"capacityType": capacity_type}

    annotations: dict[str, str] = {naming.DISPLAY_NAME_ANNOTATION: request.name}
    if request.expected_hours is not None:
        annotations[EXPECTED_HOURS_ANNOTATION] = str(request.expected_hours)

    metadata: dict[str, Any] = {
        "name": naming.object_name(job_id),
        "namespace": principal.namespace,
        "labels": naming.labels(job_id, principal.user, request.name),
        "annotations": annotations,
    }

    return {
        "apiVersion": f"{PACSJOB_GROUP}/{PACSJOB_VERSION}",
        "kind": "PacsJob",
        "metadata": metadata,
        "spec": spec,
    }


# ---------------------------------------------------------------------------
# Stage 3: /v1/estimate and /v1/validate.
#
# These take the SAME body as a submit, plus what the server cannot work out on
# its own. The reason they share a shape is that a caller should be able to
# check a request and then send that exact request, with nothing rewritten in
# between — a validate that accepts a different shape from a submit eventually
# passes something the submit refuses.
#
# Grep anchor: DDPSRUN-JUDGEMENT-MODELS
# ---------------------------------------------------------------------------


class TrainingFacts(BaseModel):
    """What the server cannot read out of a container image.

    Every field here is something only the user knows: how big their dataset is,
    how long their sequences are, what their trainer's batch settings are. The
    server asks for them rather than guessing, because guessing is what made the
    market 실험2 estimate 96% wrong.
    """

    pairs: int | None = Field(
        default=None, gt=0,
        description="How many training pairs the dataset holds. Without it there "
        "is no step count and therefore no runtime.",
    )
    epochs: int | None = Field(
        default=None, gt=0, description="How many passes over the dataset."
    )
    row_tokens: int | None = Field(
        default=None, gt=0,
        description="Average length of ONE response, in tokens. Without it there "
        "is no runtime. Read it off a previous run's log if you have one.",
    )
    cap: int | None = Field(
        default=None, gt=0,
        description="--max-len, the longest sequence the trainer accepts. This is "
        "what decides peak memory, because the longest sample grows to meet it.",
    )
    batch_size: int = Field(
        default=1, gt=0, description="per_device_train_batch_size. Our script uses 1."
    )
    grad_accum: int = Field(
        default=8, gt=0, description="gradient_accumulation_steps. Our script uses 8."
    )
    vocab: int = Field(
        default=151_936, gt=0,
        description="The model's vocabulary size. 151,936 is Qwen3-4B. This term "
        "dominates the memory calculation, so a different model needs its own.",
    )
    resumable: bool = Field(
        default=False,
        description="Whether the job can restart from a checkpoint. It changes "
        "whether losing the machine costs the whole run.",
    )


class JudgementRequest(SubmitRequest):
    """A submit request plus the facts needed to judge it.

    Inherits every field of `SubmitRequest`, so the same body works for
    /v1/estimate, /v1/validate and /v1/jobs.
    """

    training: TrainingFacts = Field(default_factory=TrainingFacts)
    script: str | None = Field(
        default=None,
        description="The text of your run.sh. Optional, and four checks are "
        "skipped without it. It is read and thrown away, never stored.",
    )


class HoursRange(BaseModel):
    """A runtime answer. Both ends are None when we will not guess."""

    low: float | None = None
    high: float | None = None
    confidence: str = Field(
        description="measured = we have run something close. interpolated = we "
        "can fit between two runs. unknown = we would be guessing, and the last "
        "time we guessed we were 96% out."
    )


class CostRange(BaseModel):
    """What that runtime costs, at the price we last paid."""

    low: float | None = None
    high: float | None = None


class GpuAdviceView(BaseModel):
    """Which GPU, and the working that led there."""

    recommended: str | None = None
    recommended_vram_gb: int | None = None
    peak_logits_gib: float
    reason: str


class EstimateResponse(BaseModel):
    """What /v1/estimate returns."""

    steps: int | None = None
    hours: HoursRange
    cost_usd: CostRange
    basis: str
    gpu: GpuAdviceView
    capacity_type: str
    capacity_reason: str
    warnings: list[str] = Field(default_factory=list)


class FindingView(BaseModel):
    """One thing worth saying about a job before it runs."""

    level: str = Field(description="error, warning or info")
    code: str = Field(description="a short stable identifier a script can act on")
    message: str
    fix: str | None = None


class ValidateResponse(BaseModel):
    """What /v1/validate returns. Nothing was submitted."""

    ok: bool = Field(description="False when any finding is an error")
    findings: list[FindingView] = Field(default_factory=list)
    not_checked: list[str] = Field(
        default_factory=list,
        description="What no check could look at. Listed rather than passed over "
        "in silence, so a clean result is not mistaken for a complete one.",
    )


def cap_from(request: JudgementRequest) -> int | None:
    """Find `--max-len` wherever the caller happened to put it.

    We have written it three ways across eight jobs: as `training.cap`, as an
    `ML` environment variable the script passes through, and inline in the
    script's own command line. All three are legitimate, so all three are read.

    Args:
        request: the judgement request.

    Returns:
        The cap, or None when it appears nowhere.
    """
    if request.training.cap:
        return request.training.cap
    for key in ("ML", "MAX_LEN", "max_len"):
        raw = request.env.get(key)
        if raw and raw.strip().isdigit():
            return int(raw.strip())
    if request.script:
        import re

        match = re.search(r"--max-len[= ]+(\d+)", request.script)
        if match:
            return int(match.group(1))
    return None


def gpu_name_for(request: JudgementRequest) -> str:
    """Which GPU to estimate against.

    A request that names a model is estimated on that model. One that gives only
    a memory floor is estimated on the cheapest card we have rented that clears
    it, because that is what the placement would buy.

    Args:
        request: the judgement request.

    Returns:
        A GPU model name, or "" for a job that asked for no GPU.
    """
    from .measurements import gpu_by_vram

    if request.gpu is None:
        return ""
    if request.gpu.name:
        return request.gpu.name
    gpu = gpu_by_vram(request.gpu.vram_gb or 0)
    return gpu.name if gpu else ""


def vram_gb_for(request: JudgementRequest) -> int | None:
    """How much GPU memory the request effectively asks for.

    Args:
        request: the judgement request.

    Returns:
        The floor in GB, or None for a CPU-only job. A request by model name is
        resolved to that model's memory.
    """
    from .measurements import gpu_by_name

    if request.gpu is None:
        return None
    if request.gpu.vram_gb:
        return request.gpu.vram_gb
    gpu = gpu_by_name(request.gpu.name or "")
    return gpu.vram_gb if gpu else None


# ---------------------------------------------------------------------------
# Stage 4: /v1/jobs/{id}/metrics.
#
# Nothing here is stored. The values come from parsing the job's own log, which
# is already durable next to the output it describes. See `metrics.py` for why
# that beats a time series store.
#
# Grep anchor: DDPSRUN-METRICS-MODELS
# ---------------------------------------------------------------------------


class GpuSampleView(BaseModel):
    """One nvidia-smi reading."""

    utilization_percent: int
    memory_used_mib: int
    memory_total_mib: int
    memory_percent: float = Field(
        description="How full the card is. This is the one to watch: running out "
        "of memory is what killed a run, and this curve approaching 100 is the "
        "warning that did not exist at the time."
    )
    temperature_c: int
    power_w: float


class ProgressView(BaseModel):
    """Where the training run has got to, by its own reckoning."""

    step: int
    total_steps: int
    percent: float
    seconds_per_step: float
    elapsed: str = Field(description="As the training library prints it, e.g. 4:02:35")
    remaining: str
    projected_total_hours: float
    steady: bool = Field(
        description="False while too few steps have run for the projection to be "
        "worth quoting. One run was 32% out at step 1 and within 4% by step 50."
    )


class MetricsResponse(BaseModel):
    """What /v1/jobs/{id}/metrics returns.

    Every field can be empty. A job that has not started computing, or whose
    script does not print the two line shapes, gets an empty answer and a `note`
    saying which of those it is.
    """

    latest_gpu: GpuSampleView | None = None
    gpu_series: list[GpuSampleView] = Field(
        default_factory=list,
        description="Readings over the window, oldest first, thinned to at most 400 points.",
    )
    progress: ProgressView | None = None
    window_seconds: int = Field(
        description="How far back the log was read. Older readings are still in "
        "the log; ask for a bigger window to see them."
    )
    note: str = Field(
        default="",
        description="What is missing and why, in plain words. Empty when nothing is.",
    )
