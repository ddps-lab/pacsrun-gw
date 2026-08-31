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
) -> dict[str, Any]:
    """Turn a submit request into the PacsJob object to create.

    DDPSRUN-SERVER-FILLS. Four things the user did not send are added here:

      namespace           from the token
      serviceAccountName  from cluster settings
      resultPath          from the token's namespace plus the job id
      parallelism         1, fixed for now

    Args:
        request: the validated body.
        principal: the authenticated caller.
        settings: cluster-wide configuration.
        job_id: the id already minted for this submission.

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
        "parallelism": 1,
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
