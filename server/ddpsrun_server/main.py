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

WHAT IS NOT HERE, ON PURPOSE. `docs/08-plan.md` stage 1: "판단은 아직 없다" —
no judgement yet.
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
from fastapi.responses import PlainTextResponse

from . import naming
from . import cognito
from .auth import AuthError, Principal, TokenStore, UnknownUser, bearer_token
from .config import Settings
from .k8s import Cluster, ClusterError, NotFound
from . import estimate as estimator
from . import metrics as metrics_reader
from . import stats as stats_reader
from . import validate as validator
from .explain import EXPLAIN_TEXT
from .models import (
    CostRange,
    EstimateResponse,
    FindingView,
    GpuAdviceView,
    HoursRange,
    JobListResponse,
    JobSpecResponse,
    JobView,
    GpuSampleView,
    JudgementRequest,
    LogsResponse,
    MemberTotalsView,
    MetricsResponse,
    ProgressView,
    SubmitRequest,
    StatsResponse,
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

    # Built here rather than per request so the JWKS document is fetched once
    # per container and reused by every warm invocation. None means Cognito is
    # not configured, and `require_principal` then takes only the static branch.
    verifier = None
    if settings.cognito_pool_id and settings.cognito_client_id:
        verifier = cognito.Verifier(
            pool_id=settings.cognito_pool_id,
            region=settings.cognito_region,
            client_id=settings.cognito_client_id,
        )

    logger.info(
        "ready: %d token(s), results under %s%s, cognito %s",
        len(tokens),
        settings.result_bucket,
        settings.result_prefix,
        settings.cognito_pool_id or "not configured",
    )
    app.state.settings = settings
    app.state.tokens = tokens
    app.state.cluster = cluster
    app.state.cognito = verifier
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

    DDPSRUN-TWO-CREDENTIALS. Two kinds of credential arrive here and both end at
    the same `Principal`:

      * a Cognito id_token, from the screen and from `ddpsrun login`. Verified
        against the pool's public keys, then the verified email is looked up in
        the token file.
      * a static token, from CI, from scripts and from the agent skill. Hashed
        and looked up directly. These exist because none of those callers has a
        browser to complete a login with (`docs/16-login.md` 16.3).

    The shape test that picks a branch is NOT what admits anything. A JWT-shaped
    string still has to pass every check in `cognito.Verifier`; anything else
    still has to match a stored hash.

    Args:
        request: used to reach `app.state.tokens` and `app.state.cognito`.
        authorization: the `Authorization` header, injected by FastAPI.

    Returns:
        The authenticated `Principal`, whose `namespace` every route below uses.

    Raises:
        HTTPException: 401 when the credential does not identify anyone; 403
            when Cognito vouched for a person nobody has registered here.
    """
    try:
        credential = bearer_token(authorization)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    verifier = getattr(request.app.state, "cognito", None)
    if verifier is not None and cognito.looks_like_a_jwt(credential):
        try:
            identity = verifier.claims(credential)
        except cognito.TokenError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        try:
            return request.app.state.tokens.principal_for_email(identity.email)
        except UnknownUser as exc:
            # 403, not 401. The sign-in worked; the person simply has no
            # namespace yet, and only an operator can change that. A 401 here
            # sends them off to debug a login that is not broken.
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    try:
        return request.app.state.tokens.principal_for(credential)
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


@app.get("/v1/login-config")
def login_config(request: Request) -> dict[str, object]:
    """Where to send someone to sign in.

    NO TOKEN NEEDED, on purpose: this is what a caller reads BEFORE they have
    one. Nothing here is a secret. The client id and the login domain both
    appear in every login URL a browser shows, and the pool id is in the issuer
    of every token we hand out.

    Returns:
        `enabled` false when Cognito is not configured, in which case the screen
        keeps its paste-a-token box and the CLI keeps `--token`. Otherwise the
        three values a PKCE flow needs (`docs/16-login.md` 16.4).
    """
    settings: Settings = request.app.state.settings
    verifier = getattr(request.app.state, "cognito", None)
    if verifier is None:
        return {"enabled": False}
    return {
        "enabled": True,
        "client_id": settings.cognito_client_id,
        "issuer": verifier.issuer,
        "login_domain": settings.cognito_login_domain,
        "scopes": ["openid", "email"],
    }


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


@app.get("/v1/stats", response_model=StatsResponse)
def get_stats(request: Request, principal: PrincipalDep) -> StatsResponse:
    """What this caller's team has spent.

    Aggregate only. A caller asking for their team's figures does not thereby
    get to read another member's job names or results — those stay in each
    member's own namespace, which is where the isolation lives.

    The team's namespaces come from the server's own token file rather than from
    a label on the namespaces, which means this route needs no cluster-wide
    permission at all.

    Raises:
        HTTPException: 502 when the cluster could not be read. A token with no
            team is not an error: it returns zeroes and a note saying so.
    """
    tokens: TokenStore = request.app.state.tokens
    cluster: Cluster = request.app.state.cluster

    namespaces = tokens.namespaces_in_team(principal.team)
    jobs_by_namespace: dict[str, list[dict[str, Any]]] = {}
    try:
        for namespace in namespaces:
            jobs_by_namespace[namespace] = cluster.list_jobs(namespace)
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    totals = stats_reader.summarise(principal.team, namespaces, jobs_by_namespace)
    return StatsResponse(
        team=totals.team,
        members=[
            MemberTotalsView(
                user=m.user, jobs=m.jobs, succeeded=m.succeeded, failed=m.failed,
                running=m.running, gpu_hours=m.gpu_hours, cost_usd=m.cost_usd,
                unpriced_jobs=m.unpriced_jobs,
            )
            for m in totals.members
        ],
        jobs=totals.jobs,
        gpu_hours=totals.gpu_hours,
        cost_usd=totals.cost_usd,
        unpriced_jobs=totals.unpriced_jobs,
        note=totals.note,
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

    # THE CALLER DECIDES THIS, and a submit that does not is refused rather than
    # guessed. The server used to fill it from its own estimate, which let a
    # thirty-hour job land on reclaimable capacity without anyone being asked.
    # /v1/estimate still answers it with a reason; the answer just has to travel
    # through the person or agent doing the submitting.
    if body.capacity_type is None:
        recommendation = _estimate_for(body)
        raise HTTPException(
            status_code=400,
            detail=(
                f"capacity_type is required: 'on-demand' or 'spot'. "
                f"/v1/estimate recommends {recommendation.capacity_type!r} for this job. "
                f"{recommendation.capacity_reason}"
            ),
        )
    capacity_type = body.capacity_type
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


# DDPSRUN-PHASE-GROUPS: the two tabs the jobs screen offers, from
# `docs/15-screens.md`. Borrowed from SkyPilot's `statusGroups`
# (`sky/dashboard/src/components/jobs.jsx:97`), which splits the same way: the
# default view is what is still moving, not everything ever submitted.
#
# PACSrun defines seven phases in `api/v1alpha1/pacsjob_types.go`: Pending,
# Starting, Running, Recovering (lines 64-69) and Compared (line 85). Three of
# them end the job and never change again.
#
# Compared is the one that is easy to miss. It is not a failure: a mode=compare
# job priced every candidate offering and deliberately bought nothing
# (`pacsjob_types.go:85`). Leaving it out of this set was a real defect —
# measured 2026-09-01 against the live cluster, 12 of 24 jobs were Compared and
# every one of them showed up under the "still running" tab.
#
# A phase we do not know about (a new one added upstream) counts as active,
# because a job the screen cannot classify is one the user should still look at.
FINISHED_PHASES = frozenset({"Succeeded", "Failed", "Compared"})


@app.get("/v1/jobs", response_model=JobListResponse)
def list_jobs(
    request: Request,
    principal: PrincipalDep,
    phase: str = Query(
        default="",
        description="Filter. Empty means all. 'active' or 'finished' select a "
        "group; anything else is matched against status.phase exactly, so "
        "?phase=Failed works too.",
    ),
    limit: int = Query(
        default=200,
        ge=1,
        le=1000,
        description="How many to return, after sorting newest first.",
    ),
) -> JobListResponse:
    """This caller's own jobs, newest first.

    The screen's first view. Read from the token's namespace, so it cannot
    contain anyone else's work.

    Filtering happens here rather than in the browser because the whole list
    crosses the network otherwise: at 1 KB per job, a namespace with 500 jobs
    would send 500 KB on every 15-second poll, which is 120 MB an hour for one
    open tab. The cluster list itself is not filtered — the Kubernetes API has
    no field selector for a CRD's status.phase — so this saves bandwidth, not
    cluster work.

    Args:
        phase: 'active', 'finished', an exact phase name, or empty for all.
        limit: cap on the number returned. `total` reports how many matched
            before the cap, so the screen can say "showing 200 of 512".

    Raises:
        HTTPException: 502 when the cluster could not be read.
    """
    cluster: Cluster = request.app.state.cluster
    try:
        objects = cluster.list_jobs(principal.namespace)
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    views = [JobView.from_pacsjob(obj) for obj in objects]

    if phase == "active":
        views = [v for v in views if v.phase not in FINISHED_PHASES]
    elif phase == "finished":
        views = [v for v in views if v.phase in FINISHED_PHASES]
    elif phase:
        views = [v for v in views if v.phase == phase]

    # Newest first. creationTimestamp is RFC 3339 with fixed-width fields, so a
    # string sort is a time sort; a job with no timestamp yet sorts last rather
    # than crashing the comparison.
    views.sort(key=lambda view: view.created_at or "", reverse=True)
    total = len(views)
    return JobListResponse(jobs=views[:limit], total=total)


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


@app.get("/v1/jobs/{job_id}/spec", response_model=JobSpecResponse)
def get_job_spec(request: Request, job_id: str, principal: PrincipalDep) -> JobSpecResponse:
    """The submission this job was created from, with secrets removed.

    Two screens need it. The detail screen shows "what exactly did I run", and
    "same settings again" copies from it. Redaction is described on
    `JobSpecResponse` (DDPSRUN-SPEC-REDACT).

    Args:
        job_id: an id this server issued.
        principal: the caller. The lookup is in their namespace only, so someone
            else's job reads as 404.

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

    return JobSpecResponse.from_pacsjob(obj)


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


@app.get("/v1/jobs/{job_id}/logs", response_model=LogsResponse)
def get_logs(
    request: Request,
    job_id: str,
    principal: PrincipalDep,
    since: str | None = Query(
        default=None,
        description="The `last_timestamp` from your previous call. Lines at or "
        "before it are dropped, so you get only what you have not seen.",
    ),
    window_seconds: int = Query(
        default=30, ge=5, le=3600,
        description="How far back to read. Several times your polling interval.",
    ),
    max_lines: int = Query(
        default=2000, ge=1, le=10000,
        description="Hard cap, so a job printing thousands of lines a second "
        "cannot return an unbounded body.",
    ),
) -> LogsResponse:
    """One window of a job's output. Ask again for more.

    THIS IS NOT A STREAM AND CANNOT BE. A Lambda execution is capped at 15
    minutes; a training run is thirty hours. The caller polls, and
    `last_timestamp` is what lets it drop lines it has already printed without
    the server remembering anything about it.

    Args:
        job_id: an id this server issued.
        since: the previous call's `last_timestamp`.
        window_seconds: how far back to read.
        max_lines: cap on the window.

    Raises:
        HTTPException: 404 for an unknown id or a job with no container yet —
            the latter is the normal state for the first few minutes while a
            large image is pulled; 502 on a cluster error.
    """
    cluster: Cluster = request.app.state.cluster
    try:
        name = naming.object_name(job_id)
    except naming.NamingError as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc

    try:
        lines = cluster.job_log_window(
            principal.namespace, name, window_seconds, max_lines
        )
    except NotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="no logs yet: the job has not started a container",
        ) from exc
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # The comparison is a plain string compare, which is correct because RFC 3339
    # with a fixed number of fraction digits sorts the same way it orders in
    # time. The apiserver emits exactly that shape.
    if since:
        lines = [line for line in lines if line.split(" ", 1)[0] > since]

    return LogsResponse(
        lines=lines,
        last_timestamp=lines[-1].split(" ", 1)[0] if lines else None,
        window_seconds=window_seconds,
    )
