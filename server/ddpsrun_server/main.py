"""The HTTP surface: four routes, and the wiring that holds the other modules together.

END-TO-END FLOW of one submission, which is what this whole stage exists to do:

  1. Startup. `lifespan` reads `Settings.from_env()`, loads the token file, and
     connects to kube-apiserver. Any of the three failing stops the pod, so a
     misconfiguration shows up as a CrashLoopBackOff with a readable reason
     rather than as 500s nobody can explain.
  2. `POST /v1/jobs` arrives with `Authorization: Bearer <token>`.
     `require_principal` hashes the token and gets back a user and a namespace.
  3. `new_job_id()` mints `job-<12 hex>`.
  4. `to_pacsjob()` fills in namespace, ServiceAccount, resultPath and
     parallelism from identity and settings, never from the body.
  5. `Cluster.create_job()` POSTs it. PACSrun's controller takes over from
     there: it solves for an offering, creates a driver pod, and the driver
     rents the GPU.
  6. The caller gets `{job_id, name, result_path}`.
  7. `GET /v1/jobs/{job_id}` maps the id back to the object name, fetches it in
     the caller's namespace, and returns the filtered view.
  8. `GET /v1/jobs/{job_id}/logs` finds the driver pod behind that job and
     streams its stdout, redacting the runner's own bookkeeping lines.

`/v1/explain` and `/v1/schema` were added with the CLI in stage 2. They make
no judgement either — one is static prose, the other is generated from the
request model — but they are what lets an agent use this service without having
read a document.

WHAT IS NOT HERE, ON PURPOSE. `docs/08-plan.md` stage 1: "판단은 아직 없다."
No `/validate`, no `/estimate`, no upload, no cancel. Those are stage 3, and
putting a half-formed version of them here would mean two sources of truth for
the same judgement. `/v1/gpus` is missing for a different reason: answering it
needs a vendor API key in this pod and a catalogue cache, which is
`docs/04-estimate.md`'s subject, not a route we can bolt on.

Grep anchor: DDPSRUN-ROUTES
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from . import naming
from .auth import AuthError, Principal, TokenStore, bearer_token
from .config import Settings
from .k8s import Cluster, ClusterError, NotFound
from . import estimate as estimator
from . import metrics as metrics_reader
from . import validate as validator
from .explain import EXPLAIN_TEXT
from .models import (
    CostRange,
    EstimateResponse,
    FindingView,
    GpuAdviceView,
    HoursRange,
    JobView,
    GpuSampleView,
    JudgementRequest,
    MetricsResponse,
    ProgressView,
    SubmitRequest,
    SubmitResponse,
    ValidateResponse,
    cap_from,
    gpu_name_for,
    to_pacsjob,
    vram_gb_for,
)

logger = logging.getLogger("ddpsrun")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build everything the routes need, once, before the first request.

    Anything that fails here kills the process. That is the intent: a server
    with no token file accepts nobody, and a server with no result bucket
    creates jobs whose output goes nowhere. Both are better as a startup crash.
    """
    settings = Settings.from_env()
    tokens = TokenStore.load(settings.tokens_path)
    cluster = Cluster.connect()
    logger.info("ready: %d token(s), results under %s%s",
                len(tokens), settings.result_bucket, settings.result_prefix)
    app.state.settings = settings
    app.state.tokens = tokens
    app.state.cluster = cluster
    yield


app = FastAPI(
    title="ddpsrun",
    version="0.1.0",
    description="Submit a GPU job and get results back. No kubectl, no AWS account.",
    lifespan=lifespan,
)


def require_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """FastAPI dependency: identify the caller or refuse the request.

    Args:
        request: used only to reach `app.state.tokens`.
        authorization: the `Authorization` header, injected by FastAPI.

    Returns:
        The authenticated `Principal`, whose `namespace` every route below uses.

    Raises:
        HTTPException: 401, with a message that does not say which half was wrong.
    """
    try:
        return request.app.state.tokens.principal_for(bearer_token(authorization))
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


PrincipalDep = Annotated[Principal, Depends(require_principal)]


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    """Liveness probe. Deliberately does not touch kube-apiserver: a probe that
    fails when the cluster is briefly busy would restart a server that is fine."""
    return {"status": "ok"}


@app.get("/v1/explain", response_class=PlainTextResponse)
def explain() -> str:
    """Say what this service is and how to use it, in prose.

    WHO THIS IS FOR: a coding agent that has a shell and this URL and has read
    nothing else. `docs/07-agent-skill.md` makes the case — a document in a
    repository goes stale the moment the API changes, whereas an answer the
    running server gives is true by construction.

    Deliberately NOT behind a token. It reveals no user data and no internal
    name, and needing a credential to find out what a thing is would be the
    wrong way round.
    """
    return EXPLAIN_TEXT


@app.get("/v1/schema")
def schema() -> dict[str, Any]:
    """Return the JSON Schema of a request.

    Generated from `JudgementRequest`, which is what /v1/estimate, /v1/validate
    and /v1/jobs all accept. It has to be that model and not `SubmitRequest`:
    when stage 3 widened the routes and this still described the narrower shape,
    an agent reading it could not learn that `training` and `script` existed —
    and a generated schema that does not match the routes has lost the only
    property that made generating it worthwhile.

    Every field's `description` is the one written on the model, which is why
    those descriptions are written for a stranger.

    Like `/v1/explain`, no token required.
    """
    return JudgementRequest.model_json_schema()


def _estimate_for(body: JudgementRequest) -> estimator.Estimate:
    """Run the estimator over a request. Shared by /v1/estimate, /v1/validate
    and /v1/jobs, so all three reach the same conclusion about the same job."""
    return estimator.estimate(
        gpu_name=gpu_name_for(body),
        cap=cap_from(body),
        pairs=body.training.pairs,
        epochs=body.training.epochs,
        row_tokens=body.training.row_tokens,
        batch_size=body.training.batch_size,
        grad_accum=body.training.grad_accum,
        mitigations_on=all(validator.mitigations_from(body.env, body.script)),
        resumable=body.training.resumable,
        vocab=body.training.vocab,
    )


@app.post("/v1/estimate", response_model=EstimateResponse)
def estimate_route(body: JudgementRequest, principal: PrincipalDep) -> EstimateResponse:
    """How long, how much, and on which GPU. Submits nothing.

    Args:
        body: the same body you would submit, plus `training` facts the server
            cannot read out of a container image.

    Returns:
        An `EstimateResponse`. `hours.confidence` says how much to believe it,
        and `unknown` is a real answer: the last time we estimated a combination
        we had never measured, we were 96% out.
    """
    result = _estimate_for(body)
    return EstimateResponse(
        steps=result.steps,
        hours=HoursRange(
            low=result.duration.low_hours,
            high=result.duration.high_hours,
            confidence=result.duration.confidence,
        ),
        cost_usd=CostRange(low=result.cost_low_usd, high=result.cost_high_usd),
        basis=result.duration.basis,
        gpu=GpuAdviceView(
            recommended=result.gpu.recommended,
            recommended_vram_gb=result.gpu.recommended_vram_gb,
            peak_logits_gib=result.gpu.peak_logits_gib,
            reason=result.gpu.reason,
        ),
        capacity_type=result.capacity_type,
        capacity_reason=result.capacity_reason,
        warnings=result.warnings,
    )


@app.post("/v1/validate", response_model=ValidateResponse)
def validate_route(body: JudgementRequest, principal: PrincipalDep) -> ValidateResponse:
    """Check a job without running it.

    Args:
        body: the same body you would submit. Attach the text of your run.sh as
            `script` and four more checks become available.

    Returns:
        A `ValidateResponse`. `not_checked` lists what no check could look at,
        so a clean result is not mistaken for a complete one.
    """
    result = validator.validate(
        env=body.env,
        script=body.script,
        cap=cap_from(body),
        vram_gb=vram_gb_for(body),
        job_estimate=_estimate_for(body),
    )
    return ValidateResponse(
        ok=result.ok,
        findings=[
            FindingView(level=f.level, code=f.code, message=f.message, fix=f.fix)
            for f in result.findings
        ],
        not_checked=result.not_checked,
    )


@app.post("/v1/jobs", response_model=SubmitResponse, status_code=201)
def submit(request: Request, body: JudgementRequest, principal: PrincipalDep) -> SubmitResponse:
    """Submit a job.

    Args:
        body: see `models.SubmitRequest`. It cannot name a namespace, a
            ServiceAccount or a result path — those come from the token.
        principal: the caller.

    Returns:
        The new job's id, its name, and where its output will land.

    Raises:
        HTTPException: 400 when the body names an unknown secret or the CRD
            refuses it; 502 when kube-apiserver could not be reached.
    """
    settings: Settings = request.app.state.settings
    cluster: Cluster = request.app.state.cluster

    job_id = naming.new_job_id()
    # The one judgement stage 1 could not make and stage 3 can. See to_pacsjob:
    # leaving capacityType empty means spot, and spot means no RunPod.
    capacity_type = _estimate_for(body).capacity_type
    try:
        obj = to_pacsjob(body, principal, settings, job_id, capacity_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        created = cluster.create_job(principal.namespace, obj)
    except ClusterError as exc:
        # The CRD's CEL rules produce messages written for a human ("specify
        # exactly one of gpus.name or gpus.vramGB"), so they are worth passing
        # through rather than flattening into "invalid request".
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    logger.info("submitted %s for %s in %s", job_id, principal.user, principal.namespace)
    return SubmitResponse(
        job_id=job_id,
        name=body.name,
        result_path=(created.get("spec") or {}).get("resultPath", ""),
    )


@app.get("/v1/jobs/{job_id}", response_model=JobView)
def get_job(request: Request, job_id: str, principal: PrincipalDep) -> JobView:
    """Report one job's state.

    Args:
        job_id: an id this server issued.
        principal: the caller. The lookup happens in their namespace only, so a
            job belonging to someone else reads as 404, not 403 — we do not
            confirm that another user's job exists.

    Raises:
        HTTPException: 404 for an unknown or malformed id; 502 on a cluster error.
    """
    cluster: Cluster = request.app.state.cluster
    try:
        name = naming.object_name(job_id)
    except naming.NamingError as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc

    try:
        obj: dict[str, Any] = cluster.get_job(principal.namespace, name)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return JobView.from_pacsjob(obj)


@app.get("/v1/jobs/{job_id}/metrics", response_model=MetricsResponse)
def get_metrics(
    request: Request,
    job_id: str,
    principal: PrincipalDep,
    window_seconds: int = Query(
        default=3600, ge=60, le=86400,
        description="How far back to read the log. An hour by default.",
    ),
) -> MetricsResponse:
    """GPU usage and training progress, read out of the job's own log.

    NOTHING IS STORED. The job's script prints one `PACSRUN_GPU=` line every 30
    seconds and the training library prints its own progress line, so both are
    already in the log next to the output they describe. Reading a window of it
    is cheaper than keeping a second copy, and it cannot disagree with the log.

    Args:
        job_id: an id this server issued.
        window_seconds: how far back to read. A wider window costs more to read
            and shows more history.

    Raises:
        HTTPException: 404 for an unknown id or a job with no container yet;
            502 on a cluster error.
    """
    cluster: Cluster = request.app.state.cluster
    try:
        name = naming.object_name(job_id)
    except naming.NamingError as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc

    try:
        lines = cluster.recent_log_lines(principal.namespace, name, window_seconds)
    except NotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="no metrics yet: the job has not started a container",
        ) from exc
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    reading = metrics_reader.scan(lines, window_seconds)
    return MetricsResponse(
        latest_gpu=_gpu_view(reading.latest_gpu),
        gpu_series=[view for view in (_gpu_view(s) for s in reading.gpu_series) if view],
        progress=(
            ProgressView(
                step=reading.progress.step,
                total_steps=reading.progress.total_steps,
                percent=reading.progress.percent,
                seconds_per_step=reading.progress.seconds_per_step,
                elapsed=reading.progress.elapsed,
                remaining=reading.progress.remaining,
                projected_total_hours=round(reading.progress.projected_total_hours, 2),
                steady=reading.progress.steady,
            )
            if reading.progress
            else None
        ),
        window_seconds=reading.window_seconds,
        note=reading.note,
    )


def _gpu_view(sample: metrics_reader.GpuSample | None) -> GpuSampleView | None:
    """Turn one parsed reading into its response shape."""
    if sample is None:
        return None
    return GpuSampleView(
        utilization_percent=sample.utilization_percent,
        memory_used_mib=sample.memory_used_mib,
        memory_total_mib=sample.memory_total_mib,
        memory_percent=sample.memory_percent,
        temperature_c=sample.temperature_c,
        power_w=sample.power_w,
    )


@app.get("/v1/jobs/{job_id}/logs")
def get_logs(
    request: Request,
    job_id: str,
    principal: PrincipalDep,
    follow: bool = Query(default=False, description="Keep streaming as new lines arrive."),
) -> StreamingResponse:
    """Stream a job's output.

    Args:
        job_id: an id this server issued.
        follow: when true the response stays open for the life of the job.

    Returns:
        `text/plain` lines. The runner's own `PACSRUN_*` bookkeeping is masked
        and its 30-second keepalive lines are dropped (`k8s.redact`).

    Raises:
        HTTPException: 404 for an unknown id or a job whose pod does not exist
            yet — the latter is the normal state for the first few seconds, so
            the detail says so; 502 on a cluster error.
    """
    cluster: Cluster = request.app.state.cluster
    settings: Settings = request.app.state.settings
    try:
        name = naming.object_name(job_id)
    except naming.NamingError as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc

    try:
        # Consuming the first line here rather than inside the response body is
        # what makes a missing pod a 404. Once StreamingResponse has begun the
        # status line is already on the wire and an error can only appear as
        # text in the middle of the log.
        lines = cluster.job_logs(principal.namespace, name, follow, settings.log_tail_lines)
        first = next(lines, None)
    except NotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="no logs yet: the job has not started a container",
        ) from exc
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    def body():
        if first is not None:
            yield first
            yield from lines

    return StreamingResponse(body(), media_type="text/plain; charset=utf-8")
