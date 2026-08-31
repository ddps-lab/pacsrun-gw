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
from .explain import EXPLAIN_TEXT
from .models import JobView, SubmitRequest, SubmitResponse, to_pacsjob

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
    """Return the JSON Schema of a submit request.

    Generated from `SubmitRequest` itself, so it cannot drift from what the
    server actually accepts. Every field's `description` is the one written on
    the model, which is why those descriptions are written for a stranger.

    Like `/v1/explain`, no token required.
    """
    return SubmitRequest.model_json_schema()


@app.post("/v1/jobs", response_model=SubmitResponse, status_code=201)
def submit(request: Request, body: SubmitRequest, principal: PrincipalDep) -> SubmitResponse:
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
    try:
        obj = to_pacsjob(body, principal, settings, job_id)
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
