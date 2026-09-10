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

import json
import logging
import re
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi import Response
from fastapi.responses import PlainTextResponse

from . import artifacts
from . import naming
from . import registry      # DDPSRUN-IMAGES: the container images this lab has built
from . import cognito
from . import measurements
from . import notify
from .auth import AuthError, Principal, TokenStore, UnknownUser, bearer_token
from .config import Settings
from . import secret_expiry
from .k8s import Cluster, ClusterError, NotFound
from . import estimate as estimator
from . import metrics as metrics_reader
from . import stats as stats_reader
from . import validate as validator
from .explain import EXPLAIN_TEXT
from .models import (
    ArtifactFileView,
    ArtifactsResponse,
    CostRange,
    PriceView,
    PricesResponse,
    RateView,
    ExecRequest,
    ExecResponse,
    EstimateResponse,
    FindingView,
    GpuAdviceView,
    HoursRange,
    JobListResponse,
    ImageView,
    ImagesResponse,
    ScriptView,
    ScriptsResponse,
    JobSpecResponse,
    JobView,
    NamespacesResponse,
    SECRET_NAME_PATTERN,
    SECRET_VALUE_MAX_CHARS,
    SecretPutRequest,
    SecretPutResponse,
    SecretsResponse,
    GpuSampleView,
    JudgementRequest,
    LogsResponse,
    CardMetricsView,
    MemberTotalsView,
    MetricsResponse,
    VendorTotalsView,
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


def build_state(app: FastAPI, force: bool = False) -> None:
    """Build everything the routes need.

    DDPSRUN-BUILD-ONCE. This used to live inside `lifespan`, which uvicorn runs
    exactly once. Mangum does not: with `lifespan="auto"` it runs the ASGI
    lifespan protocol around EVERY invocation, so every request rebuilt all of
    this. Measured on the deployed function on 2026-09-02: 48 requests produced
    48 startup log lines against 3 real cold starts, and `/healthz` — a route
    that returns a two-key dict and touches nothing — took 1.78 seconds.

    The expensive part is `Cluster.connect()`. Loading the kubeconfig runs its
    `exec` credential plugin, which spawns a fresh Python interpreter to mint an
    EKS token; importing botocore in that child alone measures 309 ms. Rebuilding
    the Verifier was the other half of the waste, because it threw away the
    cached JWKS document and made every authenticated request refetch Cognito's
    public keys.

    Args:
        app: the FastAPI application to attach the state to.
        force: rebuild even if this app already has state. `lifespan` passes
            True because a process starting up genuinely wants fresh state, and
            because `app` is a module-level singleton that the tests reuse:
            without this, the second test in a run would keep the first one's
            token file and cluster stub. The Lambda path passes False, since it
            calls this once at import and never wants a second build.

    Raises:
        ConfigError, TokenFileError, ClusterError: any of these at startup is
            deliberately fatal. A server with no token file accepts nobody, and
            one with no result bucket creates jobs whose output goes nowhere.
    """
    if not force and getattr(app.state, "ready", False):
        return

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

    static_tokens, registered_emails = tokens.counts
    logger.warning(
        "ready: %d static token(s), %d registered email(s), results under %s%s, cognito %s",
        static_tokens,
        registered_emails,
        settings.result_bucket,
        settings.result_prefix,
        settings.cognito_pool_id or "not configured",
    )
    app.state.settings = settings
    app.state.tokens = tokens
    app.state.cluster = cluster
    app.state.cognito = verifier
    app.state.ready = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """What uvicorn runs once, around the life of the process."""
    build_state(app, force=True)
    yield


app = FastAPI(
    title="hyperun",
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

      * a Cognito id_token, from the screen and from `hyperun login`. Verified
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



def require_signed_in(request: Request,
                      authorization: str | None = Header(default=None)) -> cognito.CognitoIdentity:
    """Identify a caller Cognito vouched for, WITHOUT requiring registration.

    DDPSRUN-REGISTER. This is the one dependency in the file that stops at "who
    is this" and never asks "and what may they touch". Every other route uses
    `require_principal`, which answers 403 for an address the token file does
    not name -- and that 403 is precisely the state this endpoint exists to
    serve, so it cannot be behind it.

    WHAT IT STILL DEMANDS, so that the endpoint is not simply open. The
    credential has to be a JWT that passes every check in `cognito.Verifier`:
    signature against the pool's live JWKS, issuer, audience, expiry, and
    `email_verified`. A static token is refused outright -- somebody holding one
    is already registered and has no use for this route.

    Args:
        request: used to reach `app.state.cognito`.
        authorization: the `Authorization` header.

    Returns:
        The `CognitoIdentity`: the verified address and Cognito's own `sub`.

    Raises:
        HTTPException: 401 when there is no usable Cognito credential, 503 when
            this deployment has no Cognito configured at all -- which is not the
            caller's fault and must not read as one.
    """
    verifier = getattr(request.app.state, "cognito", None)
    if verifier is None:
        raise HTTPException(
            status_code=503,
            detail="this deployment has no Cognito configured, so there is no "
                   "signed-in identity to register. Ask an operator for a token.",
        )
    try:
        credential = bearer_token(authorization)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not cognito.looks_like_a_jwt(credential):
        raise HTTPException(
            status_code=401,
            detail="this endpoint needs the id_token from a Google sign-in. A "
                   "static token means you are already registered.",
        )
    try:
        return verifier.claims(credential)
    except cognito.TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


SignedInDep = Annotated[cognito.CognitoIdentity, Depends(require_signed_in)]

def namespace_for(principal: Principal, requested: str) -> str:
    """Which namespace this request reads.

    DDPSRUN-ADMIN-NAMESPACE. Every job route reads exactly one namespace: the
    caller's own, unless they asked for another with `?namespace=`. Asking is
    honoured only for a token file entry marked `admin: true`; for anyone else
    it is 403 rather than a silent fall-back to their own, because answering
    from a different namespace than the one on the request is how a screen
    shows the right rows under the wrong heading.

    Args:
        principal: the authenticated caller.
        requested: the raw `?namespace=` value, empty when absent.

    Returns:
        The namespace to read.

    Raises:
        HTTPException: 403 when a non-admin asked for a namespace that is not
            their own.
    """
    requested = (requested or "").strip()
    if not requested or requested == principal.namespace:
        return principal.namespace
    if not principal.admin:
        raise HTTPException(
            status_code=403,
            detail="Only an operator account may read another namespace.",
        )
    return requested


# The one description all seven ?namespace= parameters share, so the OpenAPI
# page says the same thing everywhere instead of drifting into variants.
# ★ `alias` IS LOAD-BEARING AND IS WHY THIS SHARED OBJECT IS SAFE TO SHARE.
# One `Query()` instance is reused by every route that takes a namespace, and
# FastAPI fills in `alias` from the PARAMETER NAME when the alias is unset --
# on the shared instance. So the first route to bind it decides the query-string
# key for ALL of them. On 2026-09-08 a new route took it as `namespace_query`
# and every other route silently started looking for `?namespace_query=`:
# `GET /v1/jobs?namespace=lab-bob` stopped seeing the parameter, fell back to
# the caller's own namespace, and answered 200 where it had answered 403.
# Namespace isolation was gone on every route at once, from a parameter NAME.
# `test_asking_for_another_namespace_without_admin_is_refused` caught it.
# Naming the alias here pins the key to `namespace` whatever a route calls its
# parameter, so the mistake cannot be made again.
NAMESPACE_QUERY = Query(
    default="",
    alias="namespace",
    description="Read this namespace instead of your own. Honoured only for an "
    "operator account (admin in the token file); anyone else gets 403.",
)

# A legal Kubernetes object name (DNS-1123 subdomain): what kubectl accepts as
# a PacsJob's metadata.name, and therefore what a by-name lookup may carry.
K8S_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?$")



def owned_by_caller(obj: dict[str, Any], principal: Principal) -> bool:
    """Does this PacsJob belong to the caller.

    ★ DDPSRUN-OWNER-GATE. WHY THIS EXISTS AT ALL, given every route already scopes
    to a namespace. Because the namespace is a TENANCY boundary and not a person,
    and seven route docstrings in this file said otherwise -- "someone else's job
    reads as 404", "it cannot contain anyone else's work". Those sentences were
    written against `auth.py`'s convention that a namespace holds one person
    ("<team>-<user>"), which nothing enforces and which this deployment does not
    follow: all three principals in the token file sit in `default`.

    MEASURED 2026-09-08 with two principals in one namespace, through the real
    routes, before this function existed:

        GET  /v1/jobs             200   the other person's job in the list
        GET  /v1/jobs/{id}        200   their detail
        GET  /v1/jobs/{id}/spec   200   their run.sh, verbatim
        POST /v1/jobs/{id}/exec   200   a shell line inside their RUNNING container
        DELETE /v1/jobs/{id}      204   their job gone, and with it the only copy
                                        of that script (nothing else stores it)

    So this is not a new policy. It is the property the docstrings already
    promised, finally implemented, and it closes a path that the /v1/scripts
    per-person grouping could otherwise be walked around: `spec.args` on the job
    detail is the same text.

    THREE DELIBERATE EXEMPTIONS, each for a reason that is not convenience:

      an operator            `admin` in the token file. They can already name any
                             namespace with ?namespace=, so gating them here would
                             remove the only way to help somebody with a stuck job
                             while changing nothing about what they can reach.
      an unowned job         no `ddpsrun.io/owner` label at all. Every one of the
                             35 jobs on this cluster is in that state -- they were
                             applied with kubectl, before the label existed -- and
                             nobody owns them, so locking everyone out of them
                             would break the screen for the jobs it mostly shows.
      the caller's own       the ordinary case.

    Args:
        obj: the PacsJob, as fetched.
        principal: the caller.

    Returns:
        True when the caller may touch it.
    """
    if principal.admin:
        return True
    owner = ((obj.get("metadata") or {}).get("labels") or {}).get(naming.OWNER_LABEL, "")
    return not owner or owner == principal.user


def require_owner(obj: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """`owned_by_caller`, or 404.

    404 AND NOT 403, which is what the docstrings promised and is the right answer
    anyway: a 403 confirms that a job with that id exists and belongs to somebody,
    which is one bit more than the caller is entitled to.

    Args:
        obj: the PacsJob, as fetched.
        principal: the caller.

    Returns:
        The same object, so this can wrap a fetch in one expression.

    Raises:
        HTTPException: 404 when it is somebody else's.
    """
    if not owned_by_caller(obj, principal):
        raise HTTPException(status_code=404, detail="no such job")
    return obj

def resolve_object_name(job_id: str) -> str:
    """The Kubernetes object behind a path's {job_id} — two spellings.

    DDPSRUN-JOB-BY-NAME. An id this server issued ("job-<12 hex>") maps through
    naming.object_name, exactly as before. Anything else that is a legal
    Kubernetes object name is used AS the object name — which is what lets a
    PacsJob applied with kubectl (no id, no label; on 2026-09-01 that was every
    job on the cluster) be opened, read and cancelled from the screen. This
    widens nothing: every route that calls this still looks only inside the
    caller's own namespace (or the one an operator asked for), the same
    boundary the job list already shows.

    Raises:
        HTTPException: 404 when the value is neither spelling — not 400,
            because the routes deliberately do not distinguish "malformed"
            from "absent" for things the caller cannot read anyway.
    """
    try:
        return naming.object_name(job_id)
    except naming.NamingError:
        pass
    if K8S_NAME.fullmatch(job_id):
        return job_id
    raise HTTPException(status_code=404, detail="no such job")


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
        return {"enabled": False, "registration_requests": False}
    return {
        "enabled": True,
        "client_id": settings.cognito_client_id,
        "issuer": verifier.issuer,
        "login_domain": settings.cognito_login_domain,
        "scopes": ["openid", "email"],
        # DDPSRUN-REGISTER. Whether POST /v1/register-request can actually reach
        # an operator. The screen draws its button from this and NOT from
        # `enabled`: a deployment with Cognito but no notification address would
        # otherwise offer a button that answers 503, and a first-time visitor
        # cannot tell a broken service from a closed one.
        "registration_requests": bool(settings.register_notify_to),
        # The operator's address is deliberately NOT here. Anyone with a Google
        # account can read this route's answer, and handing them an inbox to
        # aim at is not something this endpoint needs to do for the button to
        # work.
    }



@app.post("/v1/register-request", status_code=202)
def register_request(request: Request, identity: SignedInDep) -> dict[str, object]:
    """Ask an operator to give this signed-in address a namespace.

    DDPSRUN-REGISTER. The state this serves: Cognito verified somebody, so their
    sign-in worked, and `auth.principal_for_email` still refuses them because
    nobody has registered the address. Until 2026-09-08 that was a dead end --
    the screen showed the 403 text and there was nothing to press.

    WHY 202 AND NOT 200. Nothing has been granted. An email has been queued to a
    human who may ignore it, and a 200 on a request whose whole content is "please
    decide" reads as a decision.

    WHY REPEATING IT SENDS NOTHING. `notify.already_asked` writes a marker with
    `If-None-Match: *`, so the second request from the same address answers 202
    with `emailed: false`. Reloading the screen must not mail the operator again,
    and this endpoint is on a public URL that any Google account can reach.

    Returns:
        `emailed` true when this call sent the mail, false when an earlier one
        already did. Both are successes from the caller's side: the operator has
        been told either way, which is what they asked for.

    Raises:
        HTTPException: 409 when the caller is ALREADY registered -- pressing this
        then means the screen is out of date, and telling them so is more useful
        than emailing an operator about somebody who needs nothing. 503 when this
        deployment has no notification address. 502 when S3 or SES refused.
    """
    settings: Settings = request.app.state.settings
    if not settings.register_notify_to:
        raise HTTPException(
            status_code=503,
            detail="this deployment cannot email an operator: no registration "
                   "notification address is configured. Ask an operator directly.",
        )
    try:
        request.app.state.tokens.principal_for_email(identity.email)
    except UnknownUser:
        pass                      # the expected case: this is why they are here
    else:
        raise HTTPException(
            status_code=409,
            detail=f"{identity.email} is already registered. Reload the page -- "
                   f"your sign-in works and you need nothing from an operator.",
        )

    try:
        seen_before = notify.already_asked(settings.result_bucket, identity.email)
    except notify.NotifyError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not seen_before:
        try:
            notify.send_registration_request(
                email=identity.email,
                subject_id=identity.subject,
                notify_to=settings.register_notify_to,
                notify_from=settings.register_notify_from,
                # DDPSRUN-REGISTER. The mail ASKS for the team, because the
                # server cannot know which one somebody belongs to. Listing the
                # ones that already exist turns that into a question the
                # operator answers in a second.
                known_teams=request.app.state.tokens.teams(),
            )
        except notify.NotifyError as exc:
            # ★ GIVE THE CLAIM BACK. The marker was written a moment ago to stop a
            # reload mailing twice; leaving it after a send that never happened
            # locks this address out permanently AND tells the next press that an
            # operator was already emailed. Found 2026-09-08 while verifying the
            # apply, where it is the only reachable path: the operator's address
            # is not a verified SES identity yet, so every send fails.
            notify.release_marker(settings.result_bucket, identity.email)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.info("registration request for %s (emailed=%s)",
                identity.email, not seen_before)
    return {
        "email": identity.email,
        "emailed": not seen_before,
        "message": (
            "An operator has been emailed and will create a namespace for you."
            if not seen_before else
            "An operator was already emailed about this address. Nothing further "
            "was sent; the decision is theirs."
        ),
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
        # The caller's own hours, used ONLY for the cost line and only when our
        # time model has already said `unknown`. It never becomes the duration:
        # a figure we did not measure must not be reported as ours.
        expected_hours=body.expected_hours,
        # DDPSRUN-AWS-PRICES. These three reach the PRICE, not the runtime. The
        # throughput table was measured on one-card pods on RunPod, so neither
        # the count nor the vendor can change what we claim about step time --
        # but both change the machine that gets rented and what it costs.
        gpu_count=body.gpu.count if body.gpu else 1,
        parallelism=body.parallelism,
        vendors=body.vendors,
        asked_capacity=body.capacity_type,
        regions=body.regions,
    )



@app.get("/v1/prices", response_model=PricesResponse)
def prices_route(
    card: str | None = Query(default=None),
    vendor: str | None = Query(default=None),
    region: str | None = Query(default=None),
) -> PricesResponse:
    """What every GPU the catalogue knows costs, in every region it prices.

    DDPSRUN-PRICES. NO TOKEN NEEDED, for the same reason `/v1/schema` needs none:
    a published list price is not this lab's information. It is also what somebody
    reads BEFORE deciding whether to ask for an account.

    WHAT THIS FIXES. The service could only speak about one region. The estimate
    priced us-west-2, the screen showed us-west-2, and "what does an H100 cost in
    Seoul" had no answer -- while `placement.regions` sat in the CRD unused, so
    there was no way to ask for another region either. Both halves are fixed
    together, because a price you can look at and not request is not much use.

    THE TWO BASES ARE NOT COMPARABLE and the answer says so per row. AWS and
    RunPod rows price a whole unit that runs a pod; GCP rows price the
    accelerator alone, because a GPU there attaches to a machine type the
    catalogue prices separately. Sorting the two together would put GCP on top
    whenever it is not actually cheaper.

    THREE VENDORS SINCE 2026-09-09, AND TWO SOURCES. aws and gcp come out of the
    SkyPilot catalogue; runpod's 105 rows come from RunPod's own catalog endpoint,
    the one PACSrun's decider calls, so they were read on a different day and
    `note` names both dates. RunPod rows carry no region (that vendor publishes
    one price per GPU type with no location dimension) and no spot price (it
    sells none, which the `no_spot` flag states rather than leaving to guesswork).

    Args:
        card: filter to one card, as the catalogue spells it.
        vendor: 'aws', 'gcp' or 'runpod'.
        region: one region. Matches no RunPod row, by the reason above.

    Returns:
        Every matching row, the AWS region list, and which region an ask that
        names none really gets. Unfiltered this is about 610 rows -- some 70 KB
        of JSON -- which is deliberate: a price table is read by sorting and
        filtering it, and one request beats a round trip per card.
    """
    rows = measurements.PRICE_ROWS
    if vendor:
        rows = tuple(r for r in rows if r.vendor == vendor.strip().lower())
    if card:
        wanted = card.strip().lower()
        rows = tuple(r for r in rows if r.card.lower() == wanted)
    if region:
        rows = tuple(r for r in rows if r.region == region.strip())

    return PricesResponse(
        rows=[PriceView(
            vendor=r.vendor, basis=r.basis, card=r.card, gpus=r.gpus,
            region=r.region, instance=r.instance, usd_per_hour=r.usd_per_hour,
            spot_low=r.spot_low, spot_high=r.spot_high, zones=r.zones,
            flags=r.flags,
        ) for r in rows],
        regions=list(measurements.AWS_REGIONS),
        default_region=measurements.DEFAULT_AWS_REGION,
        priced_on=measurements.AWS_PRICED_ON,
        note=(
            f"aws and gcp rows were read from the SkyPilot catalogue on "
            f"{measurements.AWS_PRICED_ON}; runpod rows from RunPod's own catalog "
            f"API on {measurements.RUNPOD_PRICED_ON}. On-demand is published per "
            f"region and moves rarely; spot is per zone and moves continuously, so "
            f"it is given as the range across the zones in that one snapshot. An "
            f"ask that names no region gets {measurements.DEFAULT_AWS_REGION} and "
            f"nothing else, so name a region in placement.regions to reach any "
            f"other AWS row here. AWS and RunPod rows price the whole unit that "
            f"runs a pod; GCP rows price the cards alone and the VM they attach to "
            f"is extra. A RunPod row has no region because that vendor publishes "
            f"one price per GPU type with no location dimension, and no spot price "
            f"because it sells no spot -- its `zones` is instead how many data "
            f"centers had stock at the moment of the snapshot, which goes stale."
        ),
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
        cost_usd=CostRange(low=result.cost_low_usd, high=result.cost_high_usd,
                           basis=result.cost_basis),
        rate=RateView(
            usd_per_hour_low=result.rate.usd_per_hour_low,
            usd_per_hour_high=result.rate.usd_per_hour_high,
            vendor=result.rate.vendor,
            machines=result.rate.machines,
            basis=result.rate.basis,
        ),
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
def validate_route(body: JudgementRequest, request: Request,
                   principal: PrincipalDep) -> ValidateResponse:
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
        # DDPSRUN-CATALOGUE. What the caller asked for, so the checks can say
        # whether it can be bought at all before anything is submitted.
        gpu_name=gpu_name_for(body),
        gpu_count=(body.gpu.count if body.gpu else 1),
        capacity_type=body.capacity_type,
        # THE POD COUNT IS PART OF "can this be bought". PACSrun refuses a
        # machine carrying more cards than the WHOLE job needs, so one pod
        # asking for one A100-80GB is unfillable and eight pods asking for one
        # each fill a p4de.24xlarge exactly (aws.go:333).
        parallelism=body.parallelism,
        # DDPSRUN-REGIONS. The sizes AWS offers vary by region, so "can this be
        # bought" cannot be answered without knowing where.
        regions=body.regions,
        # DDPSRUN-VENDOR-CHOICE. Four of the six vendor names can be priced and
        # not rented, so whether the list the caller sent is sensible depends on
        # the mode. Both go in together.
        vendors=body.vendors,
        placement_mode=body.placement_mode,
        # DDPSRUN-SECRET-NAMES. `to_pacsjob` refuses a word the deployment does
        # not hold, and until 2026-09-08 validate did not look at these at all
        # -- so the only way to learn a wrong name was a submit. Both go in
        # together: names without the bindings would make every name look wrong.
        secrets=body.secrets,
        # Both sources, because `secret-name-unknown` must not fire on a name
        # this namespace registered for itself. Only asked for when the request
        # names a secret, so validate stays a no-cluster-call route otherwise.
        known_secrets={
            **request.app.state.settings.secret_bindings,
            **(_own_secret_names(request, principal.namespace)
               if body.secrets else {}),
        },
        # DDPSRUN-SECRET-EXPIRY. Only the namespace's own registrations carry a
        # date; an operator binding points at a Secret whose lifetime is the
        # operator's business and this server is not told about it.
        secret_expiries=(_own_secret_names(request, principal.namespace)
                         if body.secrets else {}),
        # DDPSRUN-GROUP. Whether the pods need a rendezvous, and how big one is.
        group_size=(body.group.size if body.group else 1),
        group_mode=(body.group.mode if body.group else "independent"),
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

    Aggregate only. A caller asking for their team's figures does not thereby get
    to read another member's job names or results: this route returns totals, and
    the routes that return job detail check the job's `ddpsrun.io/owner` label
    (DDPSRUN-OWNER-GATE).

    THE OLD WORDING SAID THE ISOLATION LIVED IN "each member's own namespace",
    which is a convention `auth.py` documents and nothing enforces -- and this
    deployment does not follow it. The isolation is the owner check; the namespace
    is a tenancy boundary that may hold a whole team.

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
    # ★ THE CALLER'S OWN FIGURE IS NOW ONLY THEIR OWN. It used to fold the
    # ownerless bucket in whenever the caller was an operator, on the reasoning
    # that only an operator can apply a PacsJob with kubectl so those jobs are
    # theirs. The reasoning holds for who APPLIED them and not for whose spend
    # they are: this cluster's ownerless bucket is 35 jobs from the kubectl era
    # in the `default` namespace, and folding them in made an operator's "My
    # spend" read $105.18 when the jobs they had actually submitted came to
    # $42.61. Asked about on 2026-09-10. The bucket is still its own row in the
    # members table, under the name `kubectl`, so nothing is hidden -- it is
    # just no longer attributed to whoever happens to be an operator today.
    caller_cost = sum(
        m.cost_usd for m in totals.members if m.user == principal.user
    )
    return StatsResponse(
        team=totals.team,
        caller=principal.user,
        caller_cost_usd=round(caller_cost, 2),
        members=[
            MemberTotalsView(
                user=m.user, jobs=m.jobs, succeeded=m.succeeded, failed=m.failed,
                running=m.running, gpu_hours=m.gpu_hours, cost_usd=m.cost_usd,
                unpriced_jobs=m.unpriced_jobs,
            )
            for m in totals.members
        ],
        vendors=[
            VendorTotalsView(
                vendor=v.vendor, jobs=v.jobs, gpu_hours=v.gpu_hours,
                cost_usd=v.cost_usd, unpriced_jobs=v.unpriced_jobs,
            )
            for v in totals.vendors
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
            refuses it; 404 when `continue_from` names a job that is not this
            caller's; 502 when kube-apiserver could not be reached.
    """
    settings: Settings = request.app.state.settings
    cluster: Cluster = request.app.state.cluster

    job_id = naming.new_job_id()

    # DDPSRUN-CONTINUE-FROM. Chain this job onto a previous one's result path.
    #
    # WHY THE LOOKUP IS HERE AND NOT IN `to_pacsjob`. That function is pure --
    # no cluster calls -- and this needs one, because the only place the
    # previous job's `spec.resultPath` exists is the previous job. It also
    # needs the ownership check, and `require_owner` answers 404 rather than
    # 403 on purpose: a 403 would confirm that a job with that id exists and
    # belongs to somebody.
    #
    # A JOB WITH NO resultPath IS REFUSED RATHER THAN INHERITED FROM. That is
    # what a job submitted before this server wrote the field looks like, and
    # copying an empty string would give the new job no destination at all --
    # a 21-hour run whose results have nowhere to go, discovered at the end.
    inherited_result_path = None
    if body.continue_from:
        try:
            previous = require_owner(
                cluster.get_job(principal.namespace,
                                resolve_object_name(body.continue_from)),
                principal)
        except NotFound as exc:
            raise HTTPException(
                status_code=404,
                detail=(f"continue_from names {body.continue_from!r}, which is not a job of "
                        f"yours in {principal.namespace}."),
            ) from exc
        except ClusterError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        inherited_result_path = ((previous.get("spec") or {}).get("resultPath") or "").strip()
        if not inherited_result_path:
            raise HTTPException(
                status_code=400,
                detail=(f"{body.continue_from} has no spec.resultPath, so there is nothing to "
                        f"continue from. A job applied with kubectl before this server wrote "
                        f"that field looks like this; submit without continue_from and this "
                        f"job gets a path of its own."),
            )

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
        # DDPSRUN-USER-SECRET. Looked up ONLY when the request names a secret,
        # so an ordinary submit costs no extra cluster call. `to_pacsjob` is
        # pure and cannot read the cluster itself, so the names come in as an
        # argument; without them a name this namespace registered would be
        # refused as unknown.
        own = (
            frozenset(_own_secret_names(request, principal.namespace))
            if body.secrets else frozenset()
        )
        obj = to_pacsjob(body, principal, settings, job_id, capacity_type,
                         own_secrets=own,
                         inherited_result_path=inherited_result_path)
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


@app.get("/v1/secrets", response_model=SecretsResponse)
def secrets_route(
    request: Request,
    principal: PrincipalDep,
    namespace: str = NAMESPACE_QUERY,
) -> SecretsResponse:
    """Which names a job may put in `secrets` — the names only.

    DDPSRUN-SECRET-NAMES. `secrets: ["GITHUB_PAT"]` is not a field a submitter
    fills with a value; it is a word that opens the server's vault, and the
    server refuses a word it does not hold. Until this route existed the ONLY
    way to learn the accepted words was to guess one and read them off the
    refusal — so an agent either guessed or failed to learn (reported
    2026-09-08 by a session that went looking for the list and found no route
    for it among the eighteen).

    NO VALUES, AND NO INTERNAL NAMES EITHER. What comes back is the WORD and
    nothing more: which Kubernetes Secret holds it is an internal name, and
    `docs/03-api.md` keeps those inside (the same rule strips them from
    `GET /v1/jobs/{id}/spec`, DDPSRUN-SPEC-REDACT). The value stays in the Kubernetes
    Secret: the server writes a `secretKeyRef` into the PacsJob and kubelet
    resolves it, so this process never holds the string at all
    (`config/deploy/rbac.yaml` grants no `secrets` verb — deliberately).

    WHY IT NEEDS A TOKEN when /v1/schema does not. The names say what this lab
    integrates with — a GitHub PAT, a judge's credentials — which is closer to
    "how our runs are wired" than to a published price list.

    Returns:
        A `SecretsResponse`. An empty list with a note is the honest answer for
        a deployment where nobody has stored one yet, and it says who can.
    """
    settings: Settings = request.app.state.settings
    bindings = settings.secret_bindings
    namespace = namespace_for(principal, namespace)
    # DDPSRUN-USER-SECRET. Two sources answer this question and a submitter does
    # not care which: the deployment's own bindings, and whatever this namespace
    # registered for itself. `own` says which is which, because only the second
    # kind can be changed from here.
    own = _own_secret_names(request, namespace)
    names = sorted(set(bindings) | set(own))
    expired = [f"{n} (expired {when})" for n, when in sorted(own.items())
               if when and secret_expiry.is_past(when)]
    if names:
        note = ""
    else:
        note = (
            "nothing is registered for this namespace yet, so a job asking for "
            "a secret is refused. Register your own with `hyperun secret-set "
            "<NAME>` — it is stored in your namespace and only jobs there can "
            "read it. A value shared by the whole deployment is an operator's "
            "job instead (DDPSRUN_SECRET_BINDINGS). Values are never returned "
            "by this API."
        )
    if expired:
        # DDPSRUN-SECRET-EXPIRY. Said in the note rather than by hiding the name:
        # the name still works as far as this API is concerned (the value is
        # still there and still injectable), and what has stopped working is the
        # credential inside it. Hiding it would make `validate`'s refusal look
        # like a typo.
        note = ((note + " ") if note else "") + (
            "PAST ITS DATE: " + ", ".join(expired) + ". A job asking for one of "
            "these is refused by `validate`. Re-store it with `hyperun secret-set`.")
    return SecretsResponse(names=names, own=sorted(own), note=note)


def _own_secret_names(request: Request, namespace: str) -> dict[str, str | None]:
    """What this namespace registered, name -> expiry, or empty when it cannot say.

    WHY A 403 IS SWALLOWED HERE AND NOWHERE ELSE. Registering values is opt-in
    per namespace: the `ddpsrun-gw-secrets` RoleBinding is a separate onboarding
    step (`config/deploy/rbac.yaml`), and a namespace without it must still get
    an answer from this route — the operator's bindings are perfectly usable
    there. Turning that into a 502 would make the LIST route fail for a
    deployment that simply has not enabled the WRITE route, which reads as "the
    server is broken" instead of "this is not switched on for you". The write
    route does not swallow it: there, the 403 IS the answer.
    """
    try:
        return request.app.state.cluster.user_secrets(namespace)
    except ClusterError:
        return {}


@app.put("/v1/secrets/{name}", response_model=SecretPutResponse)
def put_secret(
    name: str,
    body: SecretPutRequest,
    request: Request,
    principal: PrincipalDep,
    namespace: str = NAMESPACE_QUERY,
) -> SecretPutResponse:
    """Register one value under one name, for jobs in your own namespace.

    DDPSRUN-USER-SECRET. ★ THIS IS THE ONE ROUTE WHERE A SECRET VALUE CROSSES
    THIS API, and every other part of the design says values do not. The reason
    the exception is worth it: before this existed, using a credential in a job
    meant asking an operator to edit `terraform.tfvars` and run `terraform
    apply`, so a researcher who needed their own HuggingFace token could not
    submit at all until somebody else's working day. What the exception costs is
    written down rather than glossed:

      the value passes through this process   in memory, for the length of one
                                              request. It is not logged, not
                                              echoed, and not stored here.
      TLS ends at the Function URL            AWS terminates it; the hop to
                                              kube-apiserver is TLS again.
      a Kubernetes Secret is base64, not      anyone with `get secrets` in that
      encryption                              namespace can read it. That is the
                                              same exposure the operator's own
                                              bindings have always had.
      this server cannot read it back         the Role grants `get` on this one
                                              Secret, so `GET /v1/secrets`
                                              lists names -- and no route
                                              returns a value.

    SCOPED TO ONE NAMESPACE, which is the whole point. `namespace_for` gives a
    non-admin their own and 403s any other, and the RBAC underneath is a Role
    bound per namespace rather than the cluster-wide ClusterRoleBinding the
    other verbs use -- so even a bug here cannot write a Secret into
    kube-system. Jobs in another namespace cannot name what you registered.

    Args:
        name: the environment variable name your script reads, e.g. `HF_TOKEN`.
            Upper case, digits and underscore. It is also the key inside the
            namespace's Secret, so nothing has to map one to the other.
        body: `{"value": "..."}`. Max 64 KiB.

    Returns:
        A `SecretPutResponse`. Never the value.

    Raises:
        HTTPException: 400 for a name that is not a legal environment variable
            name or a value that is empty or too long; 409 for a name the
            deployment already binds (the operator's would win at submit time,
            so storing yours would be storing something that never gets used);
            502 when the cluster refused -- a 403 underneath means this
            namespace has no `ddpsrun-gw-secrets` RoleBinding yet.
    """
    settings: Settings = request.app.state.settings
    namespace = namespace_for(principal, namespace)

    if not re.match(SECRET_NAME_PATTERN, name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{name!r} is not usable as an environment variable name. Use "
                f"upper-case letters, digits and underscore, starting with a "
                f"letter — the name your script reads, e.g. HF_TOKEN."
            ),
        )
    # The length check is HERE and not a pydantic constraint on the field: a
    # pydantic failure answers 422 with the offending input echoed in
    # `detail[].input`, and for this field that input is the secret.
    if not body.value:
        raise HTTPException(
            status_code=400,
            detail="the value is empty. To remove a name, use DELETE instead.",
        )
    if len(body.value) > SECRET_VALUE_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"the value is {len(body.value):,} characters and the limit is "
                f"{SECRET_VALUE_MAX_CHARS:,}. A whole Kubernetes Secret is "
                f"capped at 1 MiB and this is one key among several."
            ),
        )
    if name in settings.secret_bindings:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{name} is already bound by this deployment, and a binding "
                f"wins over a namespace's own value when a job asks for the "
                f"name. Storing yours here would store something no job would "
                f"ever read. Pick a different name, or ask an operator to "
                f"change the binding."
            ),
        )

    # DDPSRUN-SECRET-EXPIRY. Checked here so a typo in the date is a 400 now
    # rather than a name that quietly never expires.
    if body.expires_at is not None and not secret_expiry.parse(body.expires_at):
        raise HTTPException(
            status_code=400,
            detail=(f"expires_at must be an ISO-8601 timestamp, e.g. "
                    f"2026-09-10T02:27:00Z. Got {body.expires_at!r}."),
        )
    try:
        created = request.app.state.cluster.put_user_secret(
            namespace, name, body.value, expires_at=body.expires_at)
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return SecretPutResponse(name=name, namespace=namespace, created=created,
                             expires_at=body.expires_at)


@app.delete("/v1/secrets/{name}", status_code=204)
def delete_secret(
    name: str,
    request: Request,
    principal: PrincipalDep,
    namespace: str = NAMESPACE_QUERY,
) -> None:
    """Forget one registered name in your namespace.

    The other half of registering. A value put in by mistake, or one that has
    leaked, would otherwise stay readable by every job in the namespace for as
    long as the namespace exists.

    Raises:
        HTTPException: 404 when that name is not registered here (an operator
            binding is not registered HERE either, and cannot be removed from
            this route); 502 when the cluster refused.
    """
    settings: Settings = request.app.state.settings
    namespace = namespace_for(principal, namespace)
    if name in settings.secret_bindings:
        raise HTTPException(
            status_code=404,
            detail=(
                f"{name} is a binding this deployment holds, not something your "
                f"namespace registered, so there is nothing here to remove. "
                f"Only an operator changes a binding."
            ),
        )
    try:
        request.app.state.cluster.delete_user_secret(namespace, name)
    except NotFound as exc:
        raise HTTPException(
            status_code=404,
            detail=f"{name} is not registered in {namespace}.",
        ) from exc
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/namespaces", response_model=NamespacesResponse)
def list_namespaces(request: Request, principal: PrincipalDep) -> NamespacesResponse:
    """Which namespaces this caller may read — the screen's namespace picker.

    Everything here comes from the server's own token file, the same source
    `/v1/stats` reads teams from, so this route costs no Kubernetes call and no
    new cluster permission. What it does NOT promise is that the Lambda's role
    can actually read every namespace listed: reading one needs a RoleBinding
    in it, created alongside the namespace itself (docs/16-login.md 16.2). A
    namespace listed here without its binding answers 502 when picked, which
    names the real problem instead of hiding the namespace.
    """
    tokens: TokenStore = request.app.state.tokens
    return NamespacesResponse(
        namespaces=tokens.all_namespaces() if principal.admin else [principal.namespace],
        own=principal.namespace,
        selectable=principal.admin,
    )


@app.get("/v1/jobs", response_model=JobListResponse)
def list_jobs(
    request: Request,
    principal: PrincipalDep,
    namespace: str = NAMESPACE_QUERY,
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

    The screen's first view. Read from the token's namespace and then filtered to
    the caller's own jobs by `owned_by_caller`, so it cannot contain anyone else's
    work.

    DDPSRUN-OWNER-GATE. THE SECOND SENTENCE WAS FALSE UNTIL 2026-09-08. It rested
    on one namespace holding one person, which nothing enforces and this
    deployment does not do -- all three principals sit in `default`. So this route
    handed every namespace-mate's job, with their owner name and their S3 result
    prefix on each row, to anyone in the namespace. That is where the exposure
    started: no id had to be guessed.

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
        objects = cluster.list_jobs(namespace_for(principal, namespace))
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # DDPSRUN-OWNER-GATE. The list is where the exposure STARTED: nobody had to
    # guess an id, because this route handed every namespace-mate's job -- with
    # their owner name and their S3 result prefix on each row -- to anyone in the
    # namespace, under a docstring saying "it cannot contain anyone else's work".
    # Filtering here is what makes that sentence true. `owned_by_caller` keeps an
    # operator seeing everything and keeps the unowned kubectl-era jobs visible.
    objects = [obj for obj in objects if owned_by_caller(obj, principal)]
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
def get_job(
    request: Request,
    job_id: str,
    principal: PrincipalDep,
    namespace: str = NAMESPACE_QUERY,
) -> JobView:
    """Report one job's state.

    Args:
        job_id: an id this server issued.
        principal: the caller. The lookup is scoped to their namespace AND
            checked against the job's own `ddpsrun.io/owner` label (an operator
            may name another namespace with ?namespace=), so a job belonging
            to someone else reads as 404, not 403 -- we do not confirm that
            another user's job exists. DDPSRUN-OWNER-GATE: true since 2026-09-08 and not before,
            when it
            rested on one namespace holding one person -- which nothing
            enforces and this deployment does not do.

    Raises:
        HTTPException: 404 for an unknown or malformed id; 502 on a cluster error.
    """
    cluster: Cluster = request.app.state.cluster
    name = resolve_object_name(job_id)

    try:
        obj: dict[str, Any] = require_owner(
            cluster.get_job(namespace_for(principal, namespace), name), principal)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return JobView.from_pacsjob(obj)


@app.delete("/v1/jobs/{job_id}", status_code=204)
def cancel_job(
    request: Request,
    job_id: str,
    principal: PrincipalDep,
    namespace: str = NAMESPACE_QUERY,
) -> Response:
    """Stop a job and take it off the list.

    DDPSRUN-CANCEL. Deleting the PacsJob is the only stop the CRD offers, and it
    is what PACSrun's controller watches to give back whatever the job rented.
    This server never deletes a pod or a node itself: it does not know what a
    job took, and a partial cleanup would strand capacity nobody is tracking.

    WHY THIS EXISTS. A job can sit in Pending forever with no way out. On
    2026-09-02 one asked for an L40S on spot: RunPod does not sell spot so it was
    refused before the catalogue was read, and no AWS row matched the ask, so the
    controller retried the same failure every eleven minutes. There was no way to
    stop it from the screen or the CLI, and `kubectl` is exactly what this
    service exists so that nobody needs.

    A finished job can be cancelled too. Nothing is stopped in that case; the row
    goes away, which is the other thing people want this button for.

    Args:
        job_id: an id this server issued.
        principal: the caller. The job is FETCHED and its owner checked before
            anything is deleted, so someone else's job reads as 404 and cannot
            be cancelled by guessing. DDPSRUN-OWNER-GATE: true since 2026-09-08 and not before, when
            it
            rested on one namespace holding one person -- which nothing
            enforces and this deployment does not do. Before it, any
            caller sharing the namespace could cancel a running job of
            somebody else's -- measured 204 -- and deleting a job destroys the
            only copy of its script.

    Returns:
        204 with no body. There is nothing useful to say about a thing that is
        now gone.

    Raises:
        HTTPException: 404 for an unknown or malformed id; 502 on a cluster error.
    """
    cluster: Cluster = request.app.state.cluster
    name = resolve_object_name(job_id)

    try:
        # FETCH BEFORE DESTROYING, added 2026-09-08. This route used to delete
        # straight away, so any caller sharing the namespace could cancel
        # somebody else's running job by its id -- measured 204 -- and deleting a
        # job destroys the only copy of its script. One extra read on a
        # destructive route is the cheapest possible price for that.
        where = namespace_for(principal, namespace)
        require_owner(cluster.get_job(where, name), principal)
        cluster.delete_job(where, name)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    logger.warning("cancelled %s for %s", job_id, principal.user)
    return Response(status_code=204)


@app.get("/v1/jobs/{job_id}/spec", response_model=JobSpecResponse)
def get_job_spec(
    request: Request,
    job_id: str,
    principal: PrincipalDep,
    namespace: str = NAMESPACE_QUERY,
) -> JobSpecResponse:
    """The submission this job was created from, with secrets removed.

    Two screens need it. The detail screen shows "what exactly did I run", and
    "same settings again" copies from it. Redaction is described on
    `JobSpecResponse` (DDPSRUN-SPEC-REDACT).

    Args:
        job_id: an id this server issued.
        principal: the caller. The lookup is scoped to their namespace and to
            the job's owner, so someone else's job reads as 404. DDPSRUN-OWNER-GATE: true since
            2026-09-08 and not before, when it
            rested on one namespace holding one person -- which nothing
            enforces and this deployment does not do.
            This route is the SECOND path to a script -- `spec.args` is the
            same text the Scripts screen groups by person -- so gating one
            and not the other gated nothing.

    Raises:
        HTTPException: 404 for an unknown or malformed id; 502 on a cluster error.
    """
    cluster: Cluster = request.app.state.cluster
    name = resolve_object_name(job_id)

    try:
        obj: dict[str, Any] = require_owner(
            cluster.get_job(namespace_for(principal, namespace), name), principal)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return JobSpecResponse.from_pacsjob(obj)


@app.get("/v1/scripts", response_model=ScriptsResponse)
def scripts_route(
    request: Request,
    principal: PrincipalDep,
    namespace: str | None = Query(default=None),
) -> ScriptsResponse:
    """The scripts this caller has submitted before, newest first.

    DDPSRUN-SCRIPTS-ROUTE. The Script box on the New job screen is where a run.sh goes, and until
    this route existed there was no way to get one back: the screen sent it, the job ran it, and
    finding it again meant opening jobs one at a time and reading the Submitted spec panel.

    IT COSTS ONE CLUSTER CALL AND STORES NOTHING. `list_jobs` already returns whole objects --
    /v1/jobs throws the specs away and keeps the status -- so the scripts are in hand before this
    function starts. Nothing is written anywhere; deleting a job deletes its script with it.

    THE SHAPE IT RECOGNISES is the one the screen sends: args == ["bash", "-lc", <text>]. A
    kubectl job, an argv list or an image running its own entrypoint carries no script by that
    definition, and those are left out rather than guessed at -- which is why an empty answer
    comes with a note saying which of the two emptinesses it is.

    Returns:
        A `ScriptsResponse`, newest first, one entry per DISTINCT text.
    """
    cluster: Cluster = request.app.state.cluster
    # DDPSRUN-SCRIPTS-NAMESPACE. Hoisted so the ANSWER can name whose scripts
    # these are. Every listing is one namespace's and never a mixture -- the
    # caller's own, or another one when an operator asked for it -- and a list of
    # somebody's training scripts with no owner printed on it reads as
    # "everybody's", which on a shared cluster is the wrong thing to assume.
    where = namespace_for(principal, namespace)
    try:
        objects = cluster.list_jobs(where)
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ★ KEYED ON (OWNER, TEXT) AND NOT ON THE TEXT ALONE, changed 2026-09-08.
    #
    # THE QUESTION THIS ROUTE IS ASKED is "which training scripts has each person
    # run", and keying on the text alone cannot answer it: two people running the
    # same run.sh collapsed into ONE entry that kept the first job's metadata and
    # merely counted the second. So the listing named one of the two and silently
    # dropped the other.
    #
    # AND THE NAMESPACE IS NOT THE PERSON. Scoping the read to a namespace looked
    # like per-person separation and is not: a namespace is a tenancy boundary
    # that may hold a whole team, and in this deployment it does -- all three
    # principals in the token file sit in `default`. The per-person fact was
    # already on every job as `ddpsrun.io/owner` (models.to_pacsjob stamps it
    # from principal.user) and this route was not reading it.
    #
    # dict keeps insertion order and the objects are walked newest first, so the
    # first sighting of a (person, text) pair is also their most recent run of it
    # -- which is the one worth naming.
    seen: dict[tuple[str, str], ScriptView] = {}
    ordered = sorted(
        objects,
        key=lambda o: (o.get("metadata") or {}).get("creationTimestamp") or "",
        reverse=True,
    )
    for obj in ordered:
        args = ((obj.get("spec") or {}).get("args")) or []
        if len(args) != 3 or args[0] != "bash" or args[1] != "-lc":
            continue
        text = args[2]
        if not isinstance(text, str) or not text.strip():
            continue
        meta = obj.get("metadata") or {}
        labels = meta.get("labels") or {}
        # Empty when the job did not come through this gateway. Left empty rather
        # than filled with "unknown": that string would sit in the owner column
        # looking like somebody's username.
        owner = labels.get(naming.OWNER_LABEL, "")
        key = (owner, text)
        if key in seen:
            seen[key].used += 1
            continue
        display = labels.get(naming.DISPLAY_NAME_LABEL, "") or meta.get("name", "")
        seen[key] = ScriptView(
            script=text,
            job_id=labels.get(naming.JOB_ID_LABEL, ""),
            name=display,
            owner=owner,
            # A filename for saving it. Kubernetes names are already restricted
            # to lowercase letters, digits, dots and hyphens, but a display name
            # from an annotation is free text -- so anything else becomes a
            # hyphen rather than reaching a Content-Disposition or a file system.
            filename=(re.sub(r"[^A-Za-z0-9._-]+", "-", display).strip("-")
                      or "run") + ".sh",
            created_at=meta.get("creationTimestamp"),
            used=1,
            lines=len(text.splitlines()) or 1,
        )

    owners = sorted({view.owner for view in seen.values()})
    note = ""
    if not seen:
        note = (
            "No job in this namespace carries a script this route recognises. It reads the "
            "text back out of the job itself, so the job has to have `args == [\"bash\", "
            "\"-lc\", <text>]` -- which is what `hyperun submit --script run.sh` and the New "
            "job screen's Script box both produce. A job whose args are something else (a "
            "`kubectl apply` with its own args, or an image that runs its own ENTRYPOINT) has "
            "no text here to give back."
        )
    elif owners == [""]:
        # Worth saying, because the screen then has one unnamed group and that
        # looks like the grouping is broken rather than like the jobs predating it.
        note = (
            "None of these jobs records who submitted it. A job carries "
            f"`{naming.OWNER_LABEL}` only when it was created through this service; one made "
            "with `kubectl apply` does not, and the submitter cannot be recovered afterwards."
        )
    return ScriptsResponse(namespace=where, owners=owners,
                           scripts=list(seen.values()), note=note)


@app.get("/v1/images", response_model=ImagesResponse)
def images_route(request: Request, principal: PrincipalDep) -> ImagesResponse:
    """Every container image this lab has already built.

    DDPSRUN-IMAGES-ROUTE. The Image field on the New job screen was free text with an ECR URL in
    its placeholder, so the one thing it could not do was offer the addresses that exist. The
    mechanics, and why it filters nothing by owner and lists no AMIs, are in registry.py.

    IT NEEDS A TOKEN LIKE EVERY OTHER ROUTE, and that is the only access rule it has: this is a
    shared lab account whose repositories belong to several projects, and every caller here holds
    a token this deployment issued.

    Returns:
        An `ImagesResponse`. A registry that refuses answers 200 with an empty list and a note
        rather than 502: the Image box still accepts anything typed into it, so a caller who
        cannot see the list is inconvenienced and not blocked. The note names the refusal so an
        operator missing the IAM policy (DDPSRUN-IMAGES-READ in terraform/lambda) is sent to the
        right place instead of concluding the lab has built nothing.
    """
    settings: Settings = request.app.state.settings
    try:
        catalogue = registry.list_images()
    except Exception as exc:  # noqa: BLE001 - botocore raises several types here
        logger.info("the registry refused the image list for %s: %s", principal.user, exc)
        return ImagesResponse(
            images=[],
            note=f"The container registry refused the list ({exc}). Type the image address "
                 f"instead; this box accepts anything.",
        )

    return ImagesResponse(
        images=[
            ImageView(
                repository=row.repository,
                registry=row.registry,
                tags=row.tags,
                pushed_at=row.pushed_at,
                addresses=row.addresses(),
            )
            for row in catalogue.images
        ],
        truncated=catalogue.truncated,
        note="" if catalogue.images else "This account holds no container repositories.",
    )


@app.get("/v1/jobs/{job_id}/artifacts", response_model=ArtifactsResponse)
def get_artifacts(
    request: Request,
    job_id: str,
    principal: PrincipalDep,
    namespace: str = NAMESPACE_QUERY,
) -> ArtifactsResponse:
    """The job's result files, each with a link that downloads it.

    DDPSRUN-ARTIFACTS-ROUTE. The prefix listed is the one on the JOB OBJECT
    (spec.resultPath, which this server wrote at submit time), never one the
    caller names — that is the scoping the screen relies on. The mechanics —
    ListObjectsV2, what a presigned URL is, why downloads bypass Lambda, and
    the fence around foreign buckets — are narrated in artifacts.py.

    Raises:
        HTTPException: 404 for an unknown job; 502 when the cluster or S3
            refused. An S3 refusal here usually means the IAM policy
            (DDPSRUN-ARTIFACTS-READ in terraform/lambda) is missing, and hiding
            that behind an empty list would send the operator hunting in the
            wrong place.
    """
    cluster: Cluster = request.app.state.cluster
    name = resolve_object_name(job_id)
    try:
        obj: dict[str, Any] = require_owner(
            cluster.get_job(namespace_for(principal, namespace), name), principal)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    result_path = (obj.get("spec") or {}).get("resultPath") or ""
    address = artifacts.split_result_path(result_path)
    if address is None:
        return ArtifactsResponse(
            files=[], total=0,
            note="This job has no result path, so there is nothing to list.",
        )

    settings: Settings = request.app.state.settings
    try:
        listing = artifacts.list_artifacts(
            *address,
            result_bucket=settings.result_bucket,
            result_prefix=settings.result_prefix,
        )
    except artifacts.ForeignResultPath as exc:
        return ArtifactsResponse(files=[], prefix=result_path, total=0, note=str(exc))
    except Exception as exc:  # noqa: BLE001 - botocore raises several types here
        raise HTTPException(status_code=502, detail=f"S3 refused the list: {exc}") from exc

    files = [
        ArtifactFileView(
            name=f.name,
            size_bytes=f.size_bytes,
            last_modified=f.last_modified,
            url=f.url,
        )
        for f in listing.files
    ]
    return ArtifactsResponse(
        files=files,
        prefix=result_path,
        total=len(files),
        truncated=listing.truncated,
        note="" if files else "Nothing is uploaded here yet.",
    )


@app.post("/v1/jobs/{job_id}/exec", response_model=ExecResponse)
def exec_in_job(
    request: Request,
    job_id: str,
    body: ExecRequest,
    principal: PrincipalDep,
    namespace: str = NAMESPACE_QUERY,
) -> ExecResponse:
    """Run one command inside a running job's workload container.

    DDPSRUN-EXEC. This is `hyperun shell`'s server half, and it exists so a
    researcher NEVER needs kubectl: the same relay an operator reached with
    `kubectl exec` (driver pod -> shell.py -> the workload container on the
    rented machine, verified live 2026-09-07) is reached here through the
    apiserver by this server's own identity. One command per request — the
    mechanics and the no-TTY reasoning are on `Cluster.exec_in_driver`.

    Raises:
        HTTPException: 404 for an unknown job or a job with no pod; 409 for a
            finished job, because its containers are gone and "not found"
            would send the caller hunting for a typo; 502 when the apiserver
            refused (a 403 underneath means the ClusterRole lacks pods/exec).
    """
    cluster: Cluster = request.app.state.cluster
    name = resolve_object_name(job_id)
    ns = namespace_for(principal, namespace)
    try:
        obj: dict[str, Any] = require_owner(cluster.get_job(ns, name), principal)
    except NotFound as exc:
        raise HTTPException(status_code=404, detail="no such job") from exc
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    phase = ((obj.get("status") or {}).get("phase")) or ""
    if phase in FINISHED_PHASES:
        raise HTTPException(
            status_code=409,
            detail=f"this job has finished ({phase}) — its containers are gone, "
            "so there is nothing to run a command in",
        )

    # PACSRUN-SHELL-SESSION vs the one-shot form, and the difference is what the
    # person sees between two commands.
    #
    #   without `session`   `sh -lc <line>` -- a FRESH shell every request, so a
    #                       `cd` is gone by the next one. Byte for byte the
    #                       request this route made before the flag existed, which
    #                       is what a script wants and what an older CLI sends.
    #   with `session`      the line is typed into a shell ALREADY RUNNING in the
    #                       driver pod, so `cd`, exported variables and an
    #                       activated venv survive. The holder is
    #                       driver/common/shellsession.py; it is opened on first
    #                       use and closed with the workload.
    #
    # THE LINE GOES ON STDIN IN THE SESSION FORM, never in the argv: an argv is
    # visible in `ps` on the driver pod and lands in the apiserver's audit log,
    # and a line typed into a debugging shell can carry anything the person
    # pasted. `exec_in_driver` opens that channel only when stdin is not None.
    stdin: str | None = None
    if body.session:
        argv = ["python3", "/app/driver/common/shellsession.py", "send",
                "--slot", str(body.slot), "--seq", str(body.seq)]
        stdin = body.command
    else:
        argv = ["python3", "/app/driver/aws/shell.py", "--", "sh", "-lc", body.command]
    try:
        output, code = cluster.exec_in_driver(ns, name, body.slot, argv,
                                              body.timeout_seconds, stdin=stdin)
    except NotFound as exc:
        raise HTTPException(
            status_code=404,
            detail="no pod for this job yet (or it is already gone)",
        ) from exc
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # THE SESSION FORM ANSWERS IN JSON AND THE ONE-SHOT FORM DOES NOT, so the
    # reply is unpacked here rather than handed through. `shellsession.py send`
    # prints one object: the output since `seq`, the new sequence, and whether
    # the driver pod's bounded buffer had already dropped anything this caller
    # had not read. `exit_code` stays the CLIENT's, which is 0 when the verb
    # worked -- a session has no per-line exit code, because the shell it types
    # into is still running.
    seq, lost, note = 0, False, ""
    if body.session:
        try:
            answer = json.loads(output or "{}")
        except json.JSONDecodeError:
            # The client prints JSON on every path, so this means something else
            # wrote to that stdout -- a python traceback, or the exec itself
            # failing. Hand it back verbatim; inventing a shape would hide it.
            answer = {"output": output, "error": "the driver pod did not answer in JSON"}
        if answer.get("error"):
            # `gone` means the session timed out or never existed. The CLI turns
            # that into "reopening", which is the only case where starting a new
            # shell is right -- the old one is provably not there any more.
            raise HTTPException(
                status_code=409 if answer.get("gone") else 502,
                detail=str(answer["error"]),
            )
        output = answer.get("output", "")
        seq = int(answer.get("seq", 0))
        lost = bool(answer.get("lost", False))
        if lost:
            note = ("output older than what is shown was dropped from the driver "
                    "pod's buffer before this request read it")

    return ExecResponse(
        output=output,
        exit_code=code,
        seq=seq,
        lost=lost,
        note=(
            note
            if note or code is not None
            else f"still running when the {body.timeout_seconds}s window closed; "
            "the output shown is what had arrived by then"
        ),
    )


@app.get("/v1/metrics/query")
def query_metrics(
    request: Request,
    principal: PrincipalDep,
    expr: str = Query(
        ..., min_length=1, max_length=512,
        description="A PromQL expression. Passed to Prometheus untouched.",
    ),
) -> dict:
    """Ask the in-cluster Prometheus one instant query.

    DDPSRUN-PROMETHEUS-PROXY. THIS IS THE HALF OF MONITORING /v1/jobs/{id}/metrics CANNOT DO.
    That route reads a job's own log, so it answers only while the pod exists — when the pod is
    garbage-collected a finished job's chart is gone. Prometheus keeps the series after the pod,
    which is the whole reason it was deployed.

    THE PATH IS Lambda -> apiserver -> Service, with no public endpoint anywhere. Prometheus is
    ClusterIP; this server already authenticates to the apiserver to read logs, so it borrows that
    to reach the Service through `services/proxy`. The alternative was an ALB at $16.43/month and
    a second place to get authentication wrong.

    IT IS AN INSTANT QUERY AND NOT A RANGE ONE, deliberately, and not because range queries are
    hard. A range query is where a caller can ask for a million points by accident; an instant
    query answers about now, which is what a screen refreshing every ten seconds actually needs.
    A range endpoint should exist when something needs history in one call, with its own bounds.

    WHAT THIS DELIBERATELY DOES NOT DO IS SCOPE THE QUERY TO THE CALLER. Every principal that can
    reach this route sees every series. Today that is honest — nothing has been pushed to
    Prometheus yet, so there is nothing to leak — and it must be fixed before it holds more than
    one team's readings. The fix is a label matcher forced onto `expr` from the principal's
    namespace, and it belongs in the same change that starts pushing.

    Args:
        expr: a PromQL expression, e.g. `up` or `pacsrun_gpu_utilization`.

    Raises:
        HTTPException: 502 when the apiserver refuses or cannot reach Prometheus. A 403 underneath
            means the ClusterRole is missing `services/proxy`.
    """
    cluster: Cluster = request.app.state.cluster
    try:
        return cluster.prometheus_query(expr)
    except ClusterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/v1/jobs/{job_id}/metrics", response_model=MetricsResponse)
def get_metrics(
    request: Request,
    job_id: str,
    principal: PrincipalDep,
    namespace: str = NAMESPACE_QUERY,
    window_seconds: int = Query(
        default=3600, ge=60, le=604800,
        description="How far back to read the log. An hour by default. The "
        "window is measured back from NOW, so for a finished job ask for one "
        "that reaches past its startedAt — the readings sit at the end of its "
        "life, not near the present. The cap is seven days because a pod that "
        "old has usually been collected anyway.",
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
    name = resolve_object_name(job_id)

    # DDPSRUN-OWNER-GATE. Neither of these routes fetched the job, so both
    # read straight from the driver pod behind an id -- another person's
    # training output in one case and their GPU samples in the other. The
    # owner is written on the JOB and nowhere else, so the object has to be
    # read to know whose pod this is. One small GET against a log window or
    # a Prometheus range query is not a cost worth trading for it.
    where = namespace_for(principal, namespace)
    try:
        require_owner(cluster.get_job(where, name), principal)
        lines = cluster.recent_log_lines(
            where, name, window_seconds
        )
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
        peak_gpu=_gpu_view(reading.peak_gpu),
        avg_utilization_percent=reading.avg_utilization_percent,
        peak_utilization_percent=reading.peak_utilization_percent,
        cards=[
            CardMetricsView(
                gpu_index=c.gpu_index,
                series=[v for v in (_gpu_view(r) for r in c.series) if v],
                latest=_gpu_view(c.latest),
                peak=_gpu_view(c.peak),
                avg_utilization_percent=c.avg_utilization_percent,
                peak_utilization_percent=c.peak_utilization_percent,
            )
            for c in reading.cards
        ],
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
        gpu_index=sample.gpu_index,
        time=sample.time,
    )


@app.get("/v1/jobs/{job_id}/logs", response_model=LogsResponse)
def get_logs(
    request: Request,
    job_id: str,
    principal: PrincipalDep,
    namespace: str = NAMESPACE_QUERY,
    since: str | None = Query(
        default=None,
        description="The `last_timestamp` from your previous call. Lines at or "
        "before it are dropped, so you get only what you have not seen.",
    ),
    window_seconds: int = Query(
        default=30, ge=0, le=3600,
        description="How far back to read. Several times your polling interval. "
        "0 means no time filter at all — just the last max_lines of the log — "
        "which is what a screen opening on a long-running or finished job "
        "needs: its history is hours old, and any window measured back from "
        "now would miss it.",
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
    name = resolve_object_name(job_id)

    # DDPSRUN-OWNER-GATE. Neither of these routes fetched the job, so both
    # read straight from the driver pod behind an id -- another person's
    # training output in one case and their GPU samples in the other. The
    # owner is written on the JOB and nowhere else, so the object has to be
    # read to know whose pod this is. One small GET against a log window or
    # a Prometheus range query is not a cost worth trading for it.
    where = namespace_for(principal, namespace)
    try:
        require_owner(cluster.get_job(where, name), principal)
        lines = cluster.job_log_window(
            where, name, window_seconds, max_lines
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
