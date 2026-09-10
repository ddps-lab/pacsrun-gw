"""What a caller may send, what they get back, and the translation between the two.

END-TO-END FLOW of this file:

  1. `SubmitRequest` is what `POST /v1/jobs` accepts. It holds only things a
     user can actually know: an image, a command, environment values, which GPU,
     roughly how long. It has no namespace, no ServiceAccount, no result path.
  2. `to_pacsjob()` takes that request plus the caller's `Principal` and the
     server `Settings` and returns the PacsJob object to POST to Kubernetes. The
     fields the user did not send are filled in here from identity, never from
     the request — that is the whole point (`docs/03-api.md`, the table titled
     the "서버가 채우는 것" / what-the-server-fills table).
  3. `JobView.from_pacsjob()` goes the other way: it takes the object Kubernetes
     returns and produces the response, dropping every internal name on the way
     out (`docs/03-api.md`, first rule of the response-rules section).

WHAT THIS STAGE DELIBERATELY DOES NOT DO. `docs/08-plan.md` stage 1 says
"판단은 아직 없다" — no judgement yet. So nothing here chooses a region, decides
on-demand versus
spot, turns fetch mode on, or estimates a duration. `placement` is left off the
object entirely, which makes PACSrun apply its own defaults exactly as it does
for a hand-written PacsJob today. Those decisions arrive in stage 3 with
`/validate` and `/estimate`.

Grep anchors: DDPSRUN-SERVER-FILLS, DDPSRUN-NO-INTERNAL-NAMES
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from . import naming
from .auth import Principal
from .config import PACSJOB_GROUP, PACSJOB_VERSION, USER_SECRET_NAME, Settings
from .stats import job_cost, job_hours

# DDPSRUN-SCRIPT-SIZE. How long a `script` may be, and why there is a number at
# all rather than "as long as you like".
#
# THE SCRIPT TRAVELS INSIDE THE JOB OBJECT: to_pacsjob puts it in
# spec.args as ["bash", "-lc", <text>], so it is stored in etcd with the
# PacsJob and is subject to etcd's request limit — 1.5 MiB by default, for the
# WHOLE object. Without a limit here the failure arrives from the apiserver as
# "etcdserver: request is too large", which names nothing the submitter can
# act on.
#
# WHY 256 KiB. Measured 2026-09-08 on the live cluster: baseline-c's whole
# PacsJob is 4,682 bytes and its args are 302 — the real training script is
# 19,655 bytes and lives in S3, fetched by those 302 bytes (see the section on
# big scripts in agent/references/script-contract.md). So this cap is thirteen
# times the largest script anybody here has written inline and a sixth of
# etcd's own limit, which leaves room for the rest of the object. A script
# bigger than this is not an inline script: it is a repository, and the two
# ways to run one are in that same document.
SCRIPT_MAX_CHARS = 256 * 1024

# Environment variable names a user may not set. PACSrun's controller already
# refuses them (`internal/controller/pacsjob_controller.go`, PACSRUN-ENV-GUARD),
# but rejecting here produces a message that names the offending variable
# instead of a controller error the user never sees.
RESERVED_ENV_PREFIX = "PACSRUN_"

# Recorded on the object so that stage 3 can compare what `/estimate` predicted
# with what the job actually took. Nothing reads it yet.
EXPECTED_HOURS_ANNOTATION = "ddpsrun.io/expected-hours"


class GroupRequest(BaseModel):
    """DDPSRUN-GROUP. Do this job's pods talk to each other.

    ★ WHY THIS FIELD HAD TO EXIST BEFORE ANYTHING COULD JUDGE A DISTRIBUTED JOB.
    The CRD has had `spec.group` since 2026-09-05 and this API did not, so the
    only way to submit distributed training was `kubectl apply` -- which means
    no estimate, no validate, no cost, and none of the checks this server exists
    to run. `parallelism` alone cannot say it: its own description promises
    pods that NEVER talk, and the two shapes have opposite scheduling rules
    (independent pods start as machines arrive; a group starts only when every
    member has one).

    HOW size RELATES TO parallelism, in PACSrun's own words: the number of
    groups is DERIVED, `parallelism / size`, so `parallelism` keeps meaning
    "how many pods" and nobody multiplies two fields to know what they asked
    for. parallelism 6 with size 2 is three groups of two.

    NOTHING HERE IS CALLED num_nodes, and that is PACSrun's decision, not a
    naming preference: its unit of allocation is a POD and a pod is not a node
    -- on 2026-09-05 one g4dn.12xlarge held four pods with four distinct GPU
    uuids. How many pods sit on one machine is the solver's answer, derived from
    price.
    """

    size: int = Field(
        default=1,
        ge=1,
        le=256,
        description="How many pods form one group. `parallelism` divided by this "
        "is the number of groups, so parallelism 6 with size 2 is three groups "
        "of two.",
    )
    mode: str = Field(
        default="independent",
        pattern="^(independent|distributed)$",
        description="`independent` -- the pods never talk, identical to sending "
        "no group at all. `distributed` -- they form one process group: every "
        "pod is told its peers' addresses through PACSRUN_MASTER_ADDR / "
        "PACSRUN_MASTER_PORT and PACSRUN_GROUP_RANK, and NONE starts its "
        "workload until the whole group has a machine. Your script has to read "
        "those and hand them to its launcher; nothing translates them for you.",
    )


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
        description="Exact GPU model as the CATALOGUE spells it: L40S, A100-80GB, "
        "T4, L4, H100. NOT the name nvidia-smi prints — that one carries an "
        "NVIDIA prefix and a board suffix, and nothing will match it. "
        "GET /v1/schema lists every name on offer.",
    )
    count: int = Field(default=1, ge=1, le=8, description="How many GPUs per pod.")

    @model_validator(mode="after")
    def exactly_one_style(self) -> "GpuRequest":
        if (self.vram_gb is None) == (self.name is None):
            raise ValueError("give exactly one of gpu.vram_gb or gpu.name")
        return self


# DDPSRUN-VENDOR-CHOICE. The vendor names PACSrun recognises, and the four of
# them that can only be PRICED.
#
# WHY THEY ARE REPEATED HERE. They are PACSrun's list (`knownVendors` in
# internal/controller/vendorpod.go) and the CRD carries them as an enum, so a
# typo would already be refused -- by the Kubernetes API, at submit time, with a
# message about an enum. Repeating them turns that into a 400 from /v1/validate
# that names the bad word and lists the ones that would have worked, before
# anything is submitted. Same reason GpuRequest repeats the CRD's CEL rule.
#
# THE SPLIT IS THE PART THAT MATTERS. aws and runpod have an execution path: the
# built-in KubePACS solver over EC2, and a rented container behind a driver pod.
# The other four are answered from the SkyPilot catalog CSVs, which needs no
# credential of any kind -- enough to state a price, nothing like enough to rent
# a machine, because no actuator here understands their machine names. So they
# belong with `mode: compare`, which stops after the ranking.
RUNNABLE_VENDORS: tuple[str, ...] = ("aws", "runpod")
PRICE_ONLY_VENDORS: tuple[str, ...] = ("gcp", "azure", "lambda", "nebius")
KNOWN_VENDORS: tuple[str, ...] = RUNNABLE_VENDORS + PRICE_ONLY_VENDORS

# What the walk does with its candidates. The words and their meanings are the
# CRD's (spec.placement.mode); this copy exists so the request can be checked
# before it is sent.
PLACEMENT_MODES: tuple[str, ...] = ("ordered", "cheapest", "compare")


class SubmitRequest(BaseModel):
    """The body of `POST /v1/jobs`.

    Note what is missing: namespace, serviceAccountName and resultPath. A caller
    cannot set them, so a caller cannot write into another user's folder or run
    as another user's identity.

    PLACEMENT IS PARTLY THE CALLER'S SINCE 2026-09-08 (DDPSRUN-VENDOR-CHOICE).
    Three of its fields are theirs -- `capacity_type`, `vendors` and
    `placement_mode` -- and the rest is still not offered. Before that date the
    server wrote `placement: {capacityType}` and nothing else, so a caller could
    not say "run this on RunPod" or "price every vendor and buy nothing", both
    of which PACSrun's CRD has had all along.
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
        # DDPSRUN-SECRET-NAMES. Two different names exist and a wrapper header
        # showed both on 2026-09-04: `GITHUB_PAT (from Secret slm-rca-clone via
        # spec.env)`. Which one goes here was written down nowhere, so a
        # submitter had to guess between the variable their script reads and
        # the Kubernetes object an operator created.
        description="ENVIRONMENT VARIABLE names to inject — the names your "
        "script reads, e.g. [\"GITHUB_PAT\"]. NOT the name of a Kubernetes "
        "Secret: the server maps each one to a Secret and key itself. The "
        "value never travels through this API, and neither does that internal "
        "name. Ask `ddpsrun secrets` for the list this deployment accepts; a "
        "name that is not on it is refused.",
    )
    gpu: GpuRequest | None = Field(
        default=None, description="Omit for a CPU-only job."
    )
    cpus: str | None = Field(default=None, description='CPU request, e.g. "4".')
    memory: str | None = Field(default=None, description='Memory request, e.g. "16Gi".')
    capacity_type: str | None = Field(
        default=None,
        pattern="^(spot|on-demand)$",
        description="How the machine is bought. YOU decide this, not the server. "
        "`on-demand` costs more and is not taken away; `spot` is cheaper and can be "
        "reclaimed mid-run. Call /v1/estimate first — it answers with a recommendation "
        "and the reason. Required: leaving it out is refused rather than guessed, "
        "because a wrong value here is invisible until the job has already run "
        "somewhere you did not intend.",
    )
    parallelism: int = Field(
        default=1,
        ge=1,
        le=256,
        description="How many pods run at once. They are INDEPENDENT workers that never talk "
        "to each other, so this is for a batch you can split, not for distributed training. "
        "The placement decides the machines: several pods may land on one multi-GPU box or on "
        "one box each. Combine with gpu.count, which is GPUs PER POD.",
    )
    group: GroupRequest | None = Field(
        default=None,
        description="Omit for independent pods, which is what parallelism alone "
        "means. Send it with mode 'distributed' for one process group per "
        "`size` pods -- data-parallel training, tensor parallel across pods, "
        "anything that needs a rendezvous. DDPSRUN-GROUP.",
    )
    expected_hours: float | None = Field(
        default=None,
        gt=0,
        description="Your own guess at the runtime. Recorded, and used for the "
        "cost line when our own time model cannot answer -- then the estimate "
        "labels the figure `user-supplied`, because it is your number and not "
        "ours.",
    )
    continue_from: str | None = Field(
        default=None,
        # DDPSRUN-CONTINUE-FROM. A multi-iteration job cannot chain its own
        # rounds today: each submit gets a fresh `resultPath` from its own job
        # id, the wrapper's resume step reads only its own
        # `PACSRUN_RESULT_PATH`, and the container's credential is scoped to
        # that one prefix -- so iteration 2 cannot see iteration 1's
        # checkpoint even though the same person submitted both. Naming the
        # previous job is the whole fix: the server copies ITS resultPath, so
        # both rounds read and write one place.
        description="The job id of a previous run of yours whose result path "
        "this job should reuse, e.g. \"job-3e1e34cb042c\". Use it to continue "
        "a multi-iteration run: without it every submit writes to a fresh "
        "prefix and the resume step finds nothing. Must be a job of YOURS in "
        "the same namespace; anything else is refused.",
    )
    vendors: list[str] = Field(
        default_factory=list,
        description="WHO the machines may be bought from. Empty means no "
        "restriction, which is how every job behaved before this field existed. "
        f"Runnable: {', '.join(RUNNABLE_VENDORS)}. Price-only: "
        f"{', '.join(PRICE_ONLY_VENDORS)} -- these are answered from catalogue "
        "CSVs and no actuator here can rent from them, so list one only together "
        "with placement_mode 'compare', which stops after the ranking.",
    )
    regions: list[str] = Field(
        default_factory=list,
        description="Which regions may answer, as PACSrun's placement.regions "
        "spells them: a bare vendor ('gcp'), or a vendor and region "
        "('aws/us-east-1'). EMPTY IS NOT 'anywhere' FOR AWS -- it is the "
        "operator's one default region, us-west-2 in this deployment "
        "(PACSrun's placement.go:376, grep PACSRUN-AWS-ONE-REGION). So a job "
        "that wants a cheaper region has to name it. GET /v1/prices lists "
        "every region the catalogue prices.",
    )
    placement_mode: str | None = Field(
        default=None,
        pattern="^(ordered|cheapest|compare)$",
        description="What the walk does with its candidates. 'ordered' (the "
        "default when omitted) asks them in order and stops at the first that "
        "answers, comparing nothing. 'cheapest' asks every candidate and buys "
        "the cheapest answer. 'compare' asks every candidate, ranks them, and "
        "then STOPS -- nothing is bought, and the job ends in the terminal phase "
        "Compared with the winner, the runner-up and the margin in its message. "
        "'compare' is the only mode that costs nothing to run.",
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

        # DDPSRUN-VENDOR-CHOICE. An unrecognised vendor is an ERROR and not a
        # skip, for the reason PACSrun's own validateVendors gives: the word
        # matches no placement candidate, so ignoring it would leave the walk
        # with nothing that vendor covers and the job would run somewhere the
        # user did not name, silently.
        unknown = [v for v in self.vendors if v not in KNOWN_VENDORS]
        if unknown:
            raise ValueError(
                f"vendors: {unknown[0]!r} is not a vendor this service knows. "
                f"Use one of: {', '.join(KNOWN_VENDORS)}."
            )
        duplicates = sorted({v for v in self.vendors if self.vendors.count(v) > 1})
        if duplicates:
            raise ValueError(
                f"vendors lists {', '.join(duplicates)} more than once. "
                f"A vendor appears at most once."
            )
        return self


class SubmitResponse(BaseModel):
    """What `POST /v1/jobs` returns."""

    job_id: str
    name: str
    result_path: str = Field(
        description="Where this job's output will be written. Yours to read."
    )


class ScriptView(BaseModel):
    """One script this caller has submitted before.

    DDPSRUN-SCRIPTS. WHY THIS IS READ BACK OUT OF THE JOBS AND NOT STORED ANYWHERE. The screen's
    Script box sends the same text twice -- as `args` (what runs) and as `script` (what validate
    reads) -- and the server throws `script` away, exactly as its own field description promises.
    But `args` is on the PacsJob for as long as the job exists, so the script a job ran is
    already durable, already scoped to the caller's namespace, and already deletable by deleting
    the job. Adding a bucket for scripts would create a second copy that can disagree with the
    first, need its own lifecycle, and need write permission this service does not have.

    SO THE ONLY SHAPE THIS ROUTE RECOGNISES is the one the screen sends:
    `args == ["bash", "-lc", <text>]`. A job whose args are anything else -- a kubectl job, an
    argv list, an image with its own entrypoint -- carries no script by this definition and is
    left out rather than guessed at.
    """

    script: str = Field(description="The text, exactly as it was submitted.")
    job_id: str = Field(default="", description="The most recent job that ran it.")
    name: str = Field(default="", description="That job's display name.")
    created_at: str | None = Field(
        default=None, description="When that job was created, newest first in the listing."
    )
    owner: str = Field(
        default="",
        description="WHO submitted it, from the job's ddpsrun.io/owner label. "
        "Empty when the job was not created through this gateway -- a job made "
        "with `kubectl apply` carries no owner, and saying 'unknown' would be a "
        "guess about a person.",
    )
    filename: str = Field(
        default="",
        description="A name to save this script under, built from the job's own "
        "display name. Downloading needs a filename and 'download' is not one; "
        "the job name is what the person who wrote the script will recognise.",
    )
    used: int = Field(
        default=1,
        description="How many of this caller's jobs ran this exact text. The same run.sh "
        "submitted five times is one entry with used=5, not five entries -- a list where "
        "every retry is its own row is a list nobody scrolls.",
    )
    lines: int = Field(default=1, description="How many lines it has, so the screen can say so.")


class ScriptsResponse(BaseModel):
    """What `GET /v1/scripts` returns: this caller's own scripts, newest first."""

    namespace: str = Field(
        description="WHICH NAMESPACE was read. Not the same thing as whose "
        "scripts these are -- see `owners`. One namespace can hold several "
        "people and in this deployment it does: all three principals in the "
        "token file sit in `default`, so scoping by namespace separates nobody."
    )
    owners: list[str] = Field(
        default_factory=list,
        description="★ WHO ran something in this listing, sorted. THIS is the "
        "per-person axis, and the namespace is not: a namespace is a tenancy "
        "boundary that may hold a whole team. An empty string in this list means "
        "jobs whose submitter was never recorded, which is every job created "
        "with `kubectl apply` rather than through this gateway.",
    )
    scripts: list[ScriptView] = Field(default_factory=list)
    note: str = Field(
        default="",
        description="Why the list looks as it does, in the two cases that need saying -- no recognised script at all, AND scripts that record no submitter. The second branch fires on a NON-EMPTY list, so this is not only an empty-list explanation. An empty list with no note reads as "
        "'you have never submitted a script', which is a different fact from 'none of your "
        "jobs was submitted in a shape this route recognises'.",
    )


class ImageView(BaseModel):
    """One container repository this lab has built, as the Image box offers it.

    DDPSRUN-IMAGES. The Image field was a free-text box with a 70-character ECR URL in its
    placeholder, and a typo in any part of it is not caught here: the request is valid, the job
    is created, and the answer arrives as an ImagePullBackOff on a machine already rented. The
    mechanics and the four deliberate omissions -- no owner filtering, no AMIs, no pagination,
    no per-request cost -- are narrated in registry.py.
    """

    repository: str = Field(description='The repository name, e.g. "pacsrun/operator".')
    registry: str = Field(
        description="The host part, so the screen can build the pullable address without "
        "knowing this deployment's account id."
    )
    tags: list[str] = Field(
        default_factory=list,
        description="The newest tags, newest first, capped at a handful per repository. Empty "
        "for a repository holding only untagged images -- a real state, shown rather than "
        "hidden, because an empty row is the answer to \"why can I not find my image\".",
    )
    pushed_at: str = Field(
        default="",
        description="When the newest image was pushed, or empty for a repository with none.",
    )
    addresses: list[str] = Field(
        default_factory=list,
        description="`registry/repository:tag` for every tag above, ready to paste into the "
        "Image field. Built here rather than in the browser so one place decides the shape.",
    )


class ImagesResponse(BaseModel):
    """What `GET /v1/images` returns."""

    images: list[ImageView] = Field(default_factory=list)
    truncated: bool = Field(
        default=False,
        description="True when this account holds more repositories than one page. Said out "
        "loud rather than showing a prefix of the truth as if it were all of it.",
    )
    note: str = Field(
        default="",
        description="Why the list is empty, when it is. An empty list with no note would read "
        "as 'this lab has built nothing', which is a different fact from 'the registry "
        "refused the question'.",
    )


class ArtifactFileView(BaseModel):
    """One result file on the artifacts screen."""

    name: str = Field(description="Path relative to the job's own result folder.")
    size_bytes: int
    last_modified: str = Field(description="When S3 last wrote it, RFC 3339.")
    url: str = Field(
        description="A presigned GET for this one file. It downloads straight "
        "from S3 — the bytes never pass through this server — and it stops "
        "working after artifacts.EXPIRES_SECONDS; ask this route again for a "
        "fresh one."
    )


class ArtifactsResponse(BaseModel):
    """What `GET /v1/jobs/{id}/artifacts` returns."""

    files: list[ArtifactFileView]
    prefix: str = Field(
        default="",
        description="The S3 address that was listed, for `aws s3 sync` by hand.",
    )
    total: int = Field(description="How many files are listed.")
    truncated: bool = Field(
        default=False,
        description="True when the folder holds more than one page (1000 keys) "
        "and only the first page is shown.",
    )
    note: str = Field(
        default="",
        description="Why the list is empty when that needs saying: the job has "
        "no resultPath, nothing is uploaded yet, or the path points outside "
        "this server's bucket and was refused.",
    )


class ExecRequest(BaseModel):
    """What `POST /v1/jobs/{id}/exec` accepts: one command for the workload."""

    command: str = Field(
        min_length=1,
        max_length=4000,
        description="Run as `sh -lc <command>` inside the workload container "
        "on the rented machine, via the driver pod's shell relay.",
    )
    slot: int = Field(default=0, ge=0, le=255, description="Which pod of a parallel job.")
    timeout_seconds: int = Field(
        default=20,
        ge=1,
        le=25,
        description="How long to wait for the command. The ceiling is 25 "
        "because the Lambda serving this route dies at 30 no matter what.",
    )


class ExecResponse(BaseModel):
    """What `POST /v1/jobs/{id}/exec` returns."""

    output: str = Field(description="stdout and stderr, in arrival order.")
    exit_code: int | None = Field(
        default=None,
        description="The command's exit code, relayed from the workload "
        "container like ssh would. None means it was still running when the "
        "timeout closed — never 0.",
    )
    note: str = Field(default="", description="Anything the caller should know, in words.")


class SecretsResponse(BaseModel):
    """What `GET /v1/secrets` returns: the accepted words, and nothing else.

    NOT THE VALUES, and NOT WHERE THEY LIVE EITHER. A first draft of this also
    returned the Kubernetes Secret's name and key so an operator would know
    where to go — and `config.SecretBinding`'s own docstring refuses that:
    those are internal names, and `docs/03-api.md` says internal names do not
    cross the API boundary. The same rule already strips them from
    `GET /v1/jobs/{id}/spec` (DDPSRUN-SPEC-REDACT). What a submitter needs is
    the word that works; what an operator needs is in their own cluster.
    """

    names: list[str] = Field(
        default_factory=list,
        description="Every word a job may put in `secrets`, e.g. [\"GITHUB_PAT\"] "
        "— the operator's bindings and your namespace's own registrations "
        "together, because what a submitter needs is the list that works.",
    )
    own: list[str] = Field(
        default_factory=list,
        description="Which of `names` your namespace registered itself, through "
        "`ddpsrun secret-set`. You can replace or remove these; the rest belong "
        "to the deployment and only an operator changes them.",
    )
    note: str = Field(
        default="",
        description="Why the list is empty when it is, and what to do about it.",
    )


# DDPSRUN-USER-SECRET. An environment variable name, which is also the key this
# value gets inside the namespace's Secret. Deliberately narrower than what
# either side accepts: POSIX says a name is letters, digits and underscore and
# must not start with a digit, and shells only export the upper-case form
# reliably. Kubernetes would accept `my.token` as a key and `export my.token`
# is not a thing, so the narrow rule is the honest one.
SECRET_NAME_PATTERN = r"^[A-Z][A-Z0-9_]*$"

# One value's ceiling. A whole Secret is capped at 1 MiB by the API server and
# this is one key among several, so the per-value limit has to be well under
# that. 64 KiB holds any token, any private key, and any kubeconfig, and stops
# somebody using the vault as a file store.
SECRET_VALUE_MAX_CHARS = 64 * 1024


class SecretPutRequest(BaseModel):
    """The body of `PUT /v1/secrets/{name}`: one value, and nothing else.

    ★ `value` CARRIES NO FIELD CONSTRAINTS AND THAT IS DELIBERATE. A pydantic
    failure answers 422 with the offending input echoed back in
    `detail[].input` — which for this one field would be the secret itself. The
    length check therefore lives in the route, which raises our own message and
    never repeats what it was given. The name is in the URL for the same
    reason: a body holding only the value cannot have a name error echo it.

    WHY THE NAME IS NOT IN HERE. `PUT /v1/secrets/{name}` is idempotent by
    shape -- the same call twice leaves the same state -- which is what
    "register this value under this name" actually is.
    """

    value: str = Field(
        description="The secret. Sent once, stored in a Kubernetes Secret in "
        "your namespace, and never returned by any route: `GET /v1/secrets` "
        "answers names only. Max 64 KiB.",
    )
    expires_at: str | None = Field(
        default=None,
        # DDPSRUN-SECRET-EXPIRY. Decided 2026-09-09, after a judge credential
        # expired at 14:27Z on 2026-09-08 and the only way to find that out was
        # to submit a job and watch it fail at the Bedrock call -- hours in, on
        # a rented GPU. A temporary credential is the normal case here
        # (GetFederationToken gives 36 h), so the tool has to be able to say
        # "this one is past its date" before the money is spent.
        description="When this value stops working, as an ISO-8601 timestamp "
        "(e.g. \"2026-09-10T02:27:00Z\"). Optional, and only worth sending for "
        "a TEMPORARY credential -- a federation token, an assumed-role session. "
        "It is stored beside the value and `validate` refuses a job that asks "
        "for a name whose date has passed, so an expiry is found before a GPU "
        "is rented instead of an hour into the run.",
    )


class SecretPutResponse(BaseModel):
    """What `PUT /v1/secrets/{name}` answers. No value, by construction."""

    name: str = Field(description="The name you can now put in `secrets`.")
    namespace: str = Field(
        description="Where it was stored. Only jobs in this namespace can use it."
    )
    created: bool = Field(
        description="True when this was the first value registered in this "
        "namespace, false when a name was added to or replaced in the existing "
        "set. Says which of 'I added one' and 'I overwrote one' happened."
    )
    expires_at: str | None = Field(
        default=None,
        description="The expiry you sent, echoed so you can see it was stored.",
    )


class NamespacesResponse(BaseModel):
    """What `GET /v1/namespaces` returns: the caller's namespace picker."""

    namespaces: list[str] = Field(
        description="For an operator (admin in the token file), every namespace "
        "the token file names; for anyone else, exactly their own. The screen "
        "draws a picker only when there is more than one to pick."
    )
    own: str = Field(
        description="The caller's home namespace — what every request without "
        "an explicit ?namespace= reads, and the picker's initial value."
    )
    selectable: bool = Field(
        description="Whether this caller may ask for a namespace other than "
        "their own. The server enforces this with 403 regardless; the field "
        "only tells the screen whether to draw the picker at all."
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
    user: str = Field(
        default="",
        description="Who submitted it. Read from the ddpsrun.io/owner label the "
        "server itself wrote at submit time, so it cannot be forged by editing "
        "the object: a caller can only ever see their own namespace anyway.",
    )
    created_at: str | None = None
    started_at: str | None = Field(
        default=None,
        description="When the job's pod first ran, from status.startedAt "
        "(PACSRUN-JOB-CLOCK). Absent while the job is still waiting for a "
        "machine, which is exactly what makes queue time visible: "
        "started_at - created_at is the wait, finished_at - started_at is the run.",
    )
    finished_at: str | None = Field(
        default=None,
        description="When the job reached Succeeded or Failed, from "
        "status.finishedAt. Absent while it is still running.",
    )
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
    cost_usd: float | None = Field(
        default=None,
        description="What this job has cost so far, in dollars: the hours "
        "between status.startedAt and status.finishedAt (or now, while it "
        "still runs) times the measured hourly price of its machine, times "
        "parallelism — the same arithmetic /v1/stats uses for the team total "
        "(stats.job_cost), so the two screens cannot disagree. None when the "
        "job never reached a machine, or ran on one we have no measured price "
        "for; the screen shows '-' because a zero here would be a lie.",
    )
    result_path: str | None = Field(
        default=None,
        description="Where the output is. This is the one place a namespace name "
        "crosses the API boundary, because it is part of the S3 key. The screen "
        "now downloads through /v1/jobs/{id}/artifacts instead, but this field "
        "stays: the CLI and scripts read results with `aws s3 sync <this>`.",
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

        # Price the job with the same two functions the team total uses
        # (DDPSRUN-STATS), so a job's own row and its share of /v1/stats can
        # never disagree. hours is None for a job that never reached Running —
        # it spent nothing — and job_cost is None for a machine with no
        # measured price. Both surface as null, never as a false $0.00.
        hours = job_hours(obj, datetime.now(timezone.utc))
        cost = job_cost(obj, hours) if hours is not None else None

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
            user=labels.get(naming.OWNER_LABEL, ""),
            created_at=metadata.get("creationTimestamp"),
            # PACSRUN-JOB-CLOCK stamps these two once each and never rewrites
            # them, so a job that lost its machine and restarted keeps its
            # original startedAt. That is deliberate: the screen wants elapsed
            # wall time, and recovery_count already reports the interruptions.
            started_at=status.get("startedAt"),
            finished_at=status.get("finishedAt"),
            gpu=gpu,
            vendor=vendor,
            recovery_count=int(status.get("recoveryCount", 0) or 0),
            cost_usd=cost,
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
    own_secrets: set[str] | frozenset[str] | None = None,
    inherited_result_path: str | None = None,
) -> dict[str, Any]:
    """Turn a submit request into the PacsJob object to create.

    DDPSRUN-SERVER-FILLS. Four things the user did not send are added here:

      namespace           from the token
      serviceAccountName  from cluster settings
      resultPath          from the token's namespace plus the job id

    `placement.capacityType` is written too, but it is NOT one of the fields the
    server decides. THE CALLER DECIDES IT and the server copies it down.

    WHY IT CANNOT BE LEFT EMPTY, whoever chooses it. An empty capacityType means
    spot (`pkg/decider/decider.go:331`) and PACSrun defaults a job to spot
    (`internal/controller/placement.go:202`), while RunPod's decider declines any
    request that is not on-demand before it even reads the catalogue
    (`pkg/decider/runpod/decider.go:242`). A job that says nothing quietly loses
    RunPod as a candidate — it still runs, just never there.

    WHY THE SERVER STOPPED CHOOSING IT (2026-09-01). It used to fill this from
    `estimate.capacity_type`, which meant a user could submit a thirty-hour job
    onto reclaimable capacity without ever being asked. The judgement still
    exists and /v1/estimate still answers it with a reason; what changed is that
    the answer is now shown to whoever is submitting and they put it in the
    request. A submit without it is refused, not guessed.

    Args:
        request: the validated body.
        principal: the authenticated caller.
        settings: cluster-wide configuration.
        job_id: the id already minted for this submission.
        capacity_type: "on-demand" or "spot". None writes no placement at all,
            which is stage 1's behaviour and is what the tests for the identity
            fields still exercise.
        inherited_result_path: DDPSRUN-CONTINUE-FROM. The `spec.resultPath` of
            the job named by `continue_from`, already fetched AND already
            checked to belong to this principal by the route. None means the
            request named no previous job, and then this job gets its own path
            as every job did before. Passed in rather than looked up here for
            the same reason `own_secrets` is: this function makes no cluster
            calls, which is what lets every test of it run without a server.
        own_secrets: the names this namespace registered itself, from
            `Cluster.user_secrets`. DDPSRUN-USER-SECRET. Passed in rather
            than read here because this function makes no cluster calls -- it
            is pure, which is what lets every test of it run without a server.
            None means "not looked up", and then only the operator's bindings
            can satisfy a name.

    Returns:
        A dict ready to POST to the Kubernetes API.

    Raises:
        ValueError: the request names a secret the operator has not bound.
    """
    env_entries: list[dict[str, Any]] = [
        {"name": key, "value": value} for key, value in sorted(request.env.items())
    ]

    registered = set(own_secrets or ())
    for secret_name in sorted(set(request.secrets)):
        binding = settings.secret_bindings.get(secret_name)
        if binding is not None:
            # The operator's binding, which points at a Secret and key somebody
            # else created for somebody else's reasons.
            #
            # THE OPERATOR WINS when both exist, and `PUT /v1/secrets/{name}`
            # refuses such a name for exactly this reason: a user whose value
            # was silently ignored in favour of a deployment-wide one would
            # debug the wrong thing for as long as it took to give up.
            source: dict[str, str] = {
                "name": binding.secret_name, "key": binding.secret_key
            }
        elif secret_name in registered:
            # DDPSRUN-USER-SECRET. Registered in this namespace through
            # `PUT /v1/secrets/{name}`, so the Secret's name is fixed and the
            # key is the environment variable name itself.
            source = {"name": USER_SECRET_NAME, "key": secret_name}
        else:
            allowed = ", ".join(sorted(set(settings.secret_bindings) | registered))
            raise ValueError(
                f"there is no secret called {secret_name!r}. Available: "
                f"{allowed or '(none)'}. Register one of your own with "
                f"`ddpsrun secret-set {secret_name}`, which stores it in your "
                f"namespace and never puts the value in this job's spec."
            )
        # secretKeyRef and never a literal: the CRD's own description explains
        # that a literal here leaks through `kubectl get -o yaml`, events,
        # controller logs, backups and audit logs.
        env_entries.append({"name": secret_name, "valueFrom": {"secretKeyRef": source}})

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
        # DDPSRUN-CONTINUE-FROM. The inherited path when one was asked for, and
        # this job's own otherwise. NOT a merge and not a fallback: if the
        # route could not prove the previous job is this caller's, it raises
        # rather than passing None, so a silent "you got a fresh prefix
        # instead" -- which is how a resume step finds nothing and trains from
        # scratch for 21 hours -- cannot happen here.
        "resultPath": (inherited_result_path
                       or result_path_for(settings, principal, job_id, request.name)),
    }
    if request.group is not None and (request.group.size > 1
                                      or request.group.mode != "independent"):
        # DDPSRUN-GROUP. Omitted when it says nothing -- size 1 and
        # `independent` is exactly what no group at all means, and writing it
        # anyway would put a field on every PacsJob for no reason and make a
        # `kubectl get -o yaml` read as though the job were distributed.
        spec["group"] = {"size": request.group.size, "mode": request.group.mode}
    if request.command:
        spec["command"] = request.command
    if request.args:
        spec["args"] = request.args
    elif getattr(request, "script", None) and not request.command:
        # ★ A SUBMIT THAT CARRIES A SCRIPT AND NOTHING TO RUN RUNS THE SCRIPT.
        #
        # `script` began life as a VALIDATE-ONLY field -- four checks read the text
        # and the submit path threw it away. That made `ddpsrun submit --script
        # run.sh` a trap: the CLI read the file, the server checked it, and the
        # job carried no command at all. Measured 2026-09-08 on the real models:
        # spec.args and spec.command both None, so the operator's shellCommand
        # refuses the driver pod ("nothing to run") AFTER the job was accepted.
        #
        # An agent following agent/skills/ddpsrun/SKILL.md hit this exactly: step 1
        # writes a run.sh, step 3 validates it with --script, step 4 submits -- and
        # nothing anywhere told it to ALSO pass `--arg bash --arg -lc --arg
        # "$(cat run.sh)"`. It wrote a script, checked it, submitted it, and the
        # script never ran.
        #
        # FIXED HERE AND NOT IN THE CLI, because every client had the same hole:
        # the CLI, the agent skill, and anything written against the API later. The
        # screen was already sending both fields with the same text, so it is
        # unaffected.
        #
        # `["bash","-lc",text]` is the shape the screen sends and the shape
        # /v1/scripts reads back, so a job submitted this way also appears on the
        # Scripts screen. An explicit `command` or `args` still wins: somebody who
        # named one meant it, and their script may be fetched inside the container.
        # getattr, because `script` lives on JudgementRequest and this function is
        # typed for its parent SubmitRequest. /v1/jobs receives the subclass, so
        # the field is there on the real path; a caller building a bare
        # SubmitRequest has no script and must not crash on the lookup.
        spec["args"] = ["bash", "-lc", getattr(request, "script")]
    if env_entries:
        spec["env"] = env_entries
    if resources:
        spec["resources"] = resources
    # DDPSRUN-VENDOR-CHOICE. All three placement fields the caller may set, in
    # one dict, so that a job naming vendors but no capacity type still gets a
    # placement block -- before 2026-09-08 the block existed only when a
    # capacity type did, which is why this is not three separate ifs.
    placement: dict[str, Any] = {}
    if capacity_type:
        placement["capacityType"] = capacity_type
    if request.vendors:
        placement["vendors"] = list(request.vendors)
    if request.placement_mode:
        placement["mode"] = request.placement_mode
    # DDPSRUN-REGIONS. Dropped until 2026-09-08, exactly as `vendors` was: the CRD
    # has had placement.regions all along and the gateway sent nothing, so every
    # job through this screen got the operator's one default AWS region and there
    # was no way to ask for another (PACSRUN-AWS-ONE-REGION).
    if request.regions:
        placement["regions"] = list(request.regions)
    if placement:
        spec["placement"] = placement

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
    market-exp2 estimate 96% wrong.
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
        # This is a CLAIM the caller makes about their own script, and nothing
        # in PACSrun acts on it: it only changes whether spot is defensible in
        # the advice. Said plainly here because a reader took it for a feature
        # on 2026-09-08 and planned a 21-hour run around it.
        description="A CLAIM ABOUT YOUR SCRIPT, not a feature the tool "
        "provides. Nothing here saves or restores anything: after a Recovering "
        "the container starts EMPTY and your script has to find its own "
        "checkpoint and continue. What does survive is the result path — the "
        "server writes `spec.resultPath` once from the job id and recovery "
        "reuses the same PacsJob, so a script may rely on that path being the "
        "same after a restart. Setting this true only tells the advice that "
        "losing the machine does not cost the whole run.",
    )


class JudgementRequest(SubmitRequest):
    """A submit request plus the facts needed to judge it.

    Inherits every field of `SubmitRequest`, so the same body works for
    /v1/estimate, /v1/validate and /v1/jobs.
    """

    training: TrainingFacts = Field(default_factory=TrainingFacts)
    script: str | None = Field(
        default=None,
        max_length=SCRIPT_MAX_CHARS,
        description="The text of your run.sh. Four validate checks are skipped "
        "without it. IT IS ALSO WHAT RUNS when you send no `command` and no "
        "`args`: the job then gets args ['bash','-lc',<this text>], which is the "
        "same shape the screen sends and the shape GET /v1/scripts reads back. "
        "An explicit `command` or `args` wins, for the case where the script is "
        "fetched inside the container instead. Never stored anywhere -- it lives "
        "on the job, so deleting the job deletes it.",
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
    """What that runtime costs: the hours above times the rate below.

    Both ends are None whenever either factor is missing, which is most often
    the hours. The RATE is the half that is now almost always known -- see
    `RateView` -- so a null cost with a non-null rate means "we know what an
    hour costs, not how many hours".
    """

    low: float | None = None
    high: float | None = None
    basis: str = Field(
        default="",
        # A reader deciding whether to spend 21 hours has to know whose number
        # this is. Before 2026-09-09 there was no third state: either we had
        # measured hours or the cost line was blank.
        description="Whose figure the total is. `measured` -- our own "
        "throughput table answered the hours. `user-supplied` -- it could not, "
        "and your `expected_hours` was multiplied by a published rate, so the "
        "arithmetic is ours and the uncertainty is yours. Empty when there is "
        "no total at all.",
    )


class RateView(BaseModel):
    """What one hour of this job's machines costs.

    Separate from `cost_usd` because the two fail independently. Twelve of the
    fourteen choosable cards have no throughput measurement, so their hours are
    `unknown` -- and before this field existed the cost line went blank with
    them, leaving twelve cards saying nothing about money at all. A rate needs
    no measurement of ours: it is a published price.
    """

    usd_per_hour_low: float | None = Field(
        default=None,
        description="The whole job's hourly rate at the cheapest end. Equal to "
        "the high end for on-demand, which is published per region; lower for "
        "spot, which is per availability zone and moves.",
    )
    usd_per_hour_high: float | None = None
    vendor: str = Field(
        default="",
        description="Which vendor this price belongs to: 'aws', 'runpod', or "
        "empty when neither could be priced. It matters, and since 2026-09-09 it "
        "matters for eight cards rather than one: an L40S is $1.09/pod-hour on "
        "RunPod against $1.8610/machine-hour on AWS, an H100 is $2.89 against "
        "$6.88, and for the A100, H200, B200 and B300 AWS will not sell a single "
        "card at all while RunPod builds a one-card pod.",
    )
    machines: int = Field(
        default=1,
        description="How many machines the job rents. The rate is for all of "
        "them, so 4 pods of one L40S is 4 x $1.8610 = $7.4440/hour.",
    )
    basis: str = Field(
        default="",
        description="The machine type, the vendor, the capacity type and the "
        "date the price was read. Written even when the numbers are null, "
        "because why we cannot price something is the useful half of that "
        "answer.",
    )


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
    rate: RateView
    basis: str
    gpu: GpuAdviceView
    capacity_type: str
    capacity_reason: str
    warnings: list[str] = Field(default_factory=list)


class PriceView(BaseModel):
    """One row of the catalogue's price table.

    DDPSRUN-PRICES. Until 2026-09-08 this service could only speak about
    us-west-2, so "what does an H100 cost in Seoul" had no answer anywhere in
    it -- not in the estimate, not on the screen, not in the CLI. And until
    2026-09-09 it could not speak about RunPod at all beyond the two cards we
    had rented, so an `A100` ask -- the card baseline-c actually ran on that
    vendor -- was answered `unknown` for cost.
    """

    vendor: str = Field(
        description="'aws', 'gcp' or 'runpod'. aws and runpod rows can both "
        "price a job, because those are the two vendors PACSrun can price AND "
        "rent; gcp rows are here to be looked at and nothing ranks them against "
        "the other two -- see `basis`."
    )
    basis: str = Field(
        description="WHAT THE PRICE COVERS, and the two values must not be "
        "compared. 'machine' (AWS, RunPod) is the whole unit that runs a pod, "
        "GPUs included, and `instance` names it -- an EC2 instance type on AWS, "
        "a RunPod GPU type id on RunPod, where a machine IS a pod. RunPod "
        "publishes a per-GPU price and this column is that price times `gpus`. "
        "'accelerator' (GCP) is the cards ALONE -- a GPU on "
        "GCP attaches to a machine type and the catalogue prices the two "
        "separately, so the VM is extra and the catalogue does not say which VM."
    )
    card: str
    gpus: int = Field(description="How many of that card this row covers.")
    region: str = Field(
        description="The region. EMPTY ON EVERY RUNPOD ROW, and not for want of "
        "looking: that vendor publishes one price per GPU type with no location "
        "dimension at all, so there is no per-region price to state."
    )
    instance: str = Field(
        description="Machine type. Empty on every GCP row. On a RunPod row it is "
        "that vendor's GPU type id, e.g. 'NVIDIA A100 80GB PCIe'."
    )
    usd_per_hour: float | None = Field(
        default=None,
        description="On-demand. Null when the catalogue publishes none: AWS "
        "sells some of the newest cards through Capacity Blocks instead.",
    )
    spot_low: float | None = None
    spot_high: float | None = Field(
        default=None,
        description="Spot is per zone and moves, so it is a range across the "
        "zones in one snapshot, not a number. Both spot fields are null on every "
        "RunPod row because that vendor sells no spot -- see the no_spot flag.",
    )
    zones: int = Field(
        description="How many places carried this row, and it means two things. "
        "AWS and GCP: how many availability zones offer it, which moves rarely. "
        "RunPod: how many data centers reported SELLABLE STOCK at the moment of "
        "the snapshot, which is volatile and can be 0 for a card whose price is "
        "published. Do not read a RunPod zones as availability now."
    )
    flags: str = Field(
        default="",
        description="'spot_above_ondemand' when this row's spot price exceeds "
        "its own on-demand price. 38 GCP rows do, consistently and with both "
        "zones of a region agreeing, which is the catalogue's own content. No "
        "AWS row does. 'no_spot' on every RunPod row, which is a statement that "
        "the vendor sells none rather than a missing value. Nothing ranks a "
        "flagged row.",
    )


class PricesResponse(BaseModel):
    """What GET /v1/prices returns."""

    rows: list[PriceView]
    regions: list[str] = Field(
        description="Every AWS region here, which is also the list "
        "`placement.regions` accepts as 'aws/<region>'. RunPod contributes none: "
        "a RunPod row has no region, and `placement.regions: ['runpod']` names "
        "the VENDOR rather than a place."
    )
    default_region: str = Field(
        description="Where an ask that names NO region actually buys: the "
        "operator's one AWS default. Not a preference -- PACSrun gives an "
        "unqualified AWS ask exactly one region (PACSRUN-AWS-ONE-REGION)."
    )
    priced_on: str = Field(
        description="When the SkyPilot catalogue was read, which dates the aws "
        "and gcp rows. RunPod rows come from that vendor's own API on a different "
        "day and `note` gives both dates."
    )
    note: str
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

    ★ IT LOOKED IN THE WRONG TABLE, fixed 2026-09-08. A named GPU was resolved
    through `measurements.gpu_by_name`, which knows only the cards we have
    RENTED -- two of them. `catalogue.CHOOSABLE` knows the printed memory of all
    fourteen a request may name.

    WHAT THAT COST, and it was silent. `validate.check_memory` opens with
    `if cap is None or vram_gb is None: return []`, so asking for one of the
    twelve unrented cards BY NAME skipped both memory checks -- the
    `PYTORCH_CUDA_ALLOC_CONF` one and the TRL-patch one -- while asking for the
    same card by `vram_gb` ran them. Two of validate's checks turned themselves
    off depending on which of two equivalent ways you named a GPU, and nothing
    said so. Measured with `gpu: {name: "L4"}` and `cap: 12288`: two findings
    instead of four.

    Both tables spell `vram_gb` the same way -- "the number printed on the
    card" -- so this changes no number for the two cards that are in both. It
    only stops the other twelve answering None.

    Returns:
        The floor in GB, or None for a CPU-only job. A request by model name is
        resolved to that model's printed memory.
    """
    from . import catalogue
    from .measurements import gpu_by_name

    if request.gpu is None:
        return None
    if request.gpu.vram_gb:
        return request.gpu.vram_gb
    name = request.gpu.name or ""
    choice = catalogue.choice_for(name)
    if choice is not None:
        return choice.vram_gb
    # A name the catalogue does not know still reaches here. `check_gpu_is_buyable`
    # is what refuses it; this only declines to invent a memory figure for it.
    gpu = gpu_by_name(name)
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
    gpu_index: int = Field(
        default=0,
        description="Which card, as nvidia-smi numbers it. 0 for a reading off "
        "the old five-field line, which describes card 0 and has no index.",
    )
    time: str = Field(
        default="",
        description="When the apiserver stamped this reading, RFC 3339. The "
        "chart's x axis; empty on logs that were read without timestamps.",
    )


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


class CardMetricsView(BaseModel):
    """One GPU card's readings, for a job that rents several."""

    gpu_index: int
    series: list[GpuSampleView] = Field(default_factory=list)
    latest: GpuSampleView | None = None
    peak: GpuSampleView | None = Field(
        default=None,
        description="The reading with the most memory in use on THIS card.",
    )
    avg_utilization_percent: float | None = None
    peak_utilization_percent: float | None = Field(
        default=None,
        description="The HIGHEST utilisation on this card, which is not the "
        "utilisation inside `peak` -- that one is chosen by memory and can be a "
        "sample taken between steps.",
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
    peak_gpu: GpuSampleView | None = Field(
        default=None,
        description="The reading with the most memory in use. The headline for "
        "a finished job, whose latest_gpu is the idle card just before teardown "
        "(0%, 0 MiB) and says nothing about the run itself.",
    )
    avg_utilization_percent: float | None = Field(
        default=None,
        description="Mean utilisation over the window's samples.",
    )
    peak_utilization_percent: float | None = Field(
        default=None,
        description="The HIGHEST utilisation in the window. It is NOT "
        "`peak_gpu.utilization_percent`: that sample is chosen by memory, and on "
        "job-66b46719b854 (four A100s, 785 samples) the highest-memory sample "
        "happened to fall between steps and read 0 -- against a real maximum of "
        "100 and a mean of 84.6. Reporting the first as the peak told a reader "
        "the GPU had been idle for a run that was not.",
    )
    cards: list[CardMetricsView] = Field(
        default_factory=list,
        description="One entry per GPU card, lowest index first. A job renting "
        "four A100s reported one card until 2026-09-08 (the watcher kept "
        "`head -1`), so three quarters of it was invisible. The single-card "
        "fields above describe the lowest-indexed card, unchanged.",
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


# ---------------------------------------------------------------------------
# Stage 4b: /v1/stats.
#
# Team figures, added up from the jobs. Aggregate only: a caller asking for
# their team's numbers is not thereby entitled to read another member's job
# names or results, which stay namespace-scoped.
#
# Grep anchor: DDPSRUN-STATS-MODELS
# ---------------------------------------------------------------------------


class MemberTotalsView(BaseModel):
    """One person's figures inside a team."""

    user: str
    jobs: int
    succeeded: int
    failed: int
    running: int
    gpu_hours: float
    cost_usd: float
    unpriced_jobs: int = Field(
        description="Jobs whose hours are counted but whose cost is not, because they "
        "ran on a machine we have no measured price for."
    )


class VendorTotalsView(BaseModel):
    """One vendor's share of the team's figures."""

    vendor: str = Field(
        description="Who sold the machines: status.currentOffering.vendor. "
        "Jobs recorded before that field existed group under 'unknown'."
    )
    jobs: int
    gpu_hours: float
    cost_usd: float
    unpriced_jobs: int


class StatsResponse(BaseModel):
    """What /v1/stats returns."""

    team: str
    vendors: list[VendorTotalsView] = Field(
        default_factory=list,
        description="The same jobs added up by who sold the machines — computed "
        "in the same pass as members, so the two tables cannot disagree.",
    )
    caller: str = Field(
        default="",
        description="The requesting token's own user name, so the screen can "
        "pick 'your' row out of members without guessing. Jobs applied with "
        "kubectl belong to the member 'admin', not to any caller.",
    )
    caller_cost_usd: float = Field(
        default=0.0,
        description="What the CALLER has spent — the number Home's 'My spend' "
        "card shows, computed here because the screen makes no decisions. It is "
        "their own member row and nothing else. It used to fold in the ownerless "
        "bucket for an operator account, on the reasoning that only an operator "
        "can apply a job with kubectl; that says who applied them, not whose "
        "spend they are, and on this cluster it made an operator's 'My spend' "
        "read the whole team total ($105.18) against the $42.61 they had "
        "actually submitted. The bucket is still its own row, named `kubectl`.",
    )
    members: list[MemberTotalsView] = Field(default_factory=list)
    jobs: int = 0
    gpu_hours: float = 0.0
    cost_usd: float = 0.0
    unpriced_jobs: int = 0
    note: str = Field(
        default="",
        description="Why the figures are incomplete, in words. Empty when they are not.",
    )


class LogsResponse(BaseModel):
    """One window of a job's output.

    NOT A STREAM. A Lambda execution is capped at 15 minutes and a training run
    is thirty hours, so the caller asks repeatedly instead of holding a
    connection. Every line keeps the timestamp the apiserver stamped it with, so
    the caller can drop what it has already printed by remembering one value.
    """

    lines: list[str] = Field(
        default_factory=list,
        description="Oldest first, each prefixed with an RFC 3339 timestamp. The "
        "runner's own bookkeeping lines are removed.",
    )
    last_timestamp: str | None = Field(
        default=None,
        description="The timestamp of the last line here, or null when the window "
        "was empty. Send it back as `since` next time and only newer lines return.",
    )
    window_seconds: int = Field(
        description="How far back this window reached. Make it several times your "
        "polling interval: too narrow and a pause loses lines, too wide and every "
        "request re-sends what it already sent."
    )



class JobSpecResponse(BaseModel):
    """What `GET /v1/jobs/{id}/spec` returns: the submission, as stored.

    The detail screen shows this so a user can answer "what exactly did I run?"
    months later, and so "same settings again" has something to copy from. It is
    SkyPilot's `Show SkyPilot YAML` panel (`sky/dashboard/src/pages/jobs/[job].js`)
    with our object in place of theirs.

    DDPSRUN-SPEC-REDACT: two things are removed on the way out.

    1. Every `env` entry carrying a `valueFrom`. The value itself was never in
       the object (that is the point of `secretKeyRef`), but the Kubernetes
       Secret's name and key ARE, and they are cluster internals a caller has no
       use for. The entry survives with its `name` only, so the screen can still
       say "GITHUB_PAT was set", and the name goes into `redacted`.
    2. `serviceAccountName`, for the same reason `JobView.from_pacsjob` drops it:
       it names an identity inside our cluster.
    """

    job_id: str
    name: str
    spec: dict[str, Any] = Field(
        description="The PacsJob spec with the two removals above applied."
    )
    redacted: list[str] = Field(
        default_factory=list,
        description="Names of the env entries whose source was removed. Shown to "
        "the user as 'set from a secret' rather than silently vanishing, because "
        "an env var that disappears from the screen reads as a bug.",
    )

    @staticmethod
    def from_pacsjob(obj: dict[str, Any]) -> "JobSpecResponse":
        """Build the response from the raw object.

        Args:
            obj: the PacsJob as the Kubernetes API returned it.

        Returns:
            A `JobSpecResponse`. A job with no env at all yields an empty
            `redacted` list, not an error.
        """
        metadata = obj.get("metadata") or {}
        labels = metadata.get("labels") or {}
        annotations = metadata.get("annotations") or {}
        spec = dict(obj.get("spec") or {})

        spec.pop("serviceAccountName", None)

        redacted: list[str] = []
        env = spec.get("env")
        if isinstance(env, list):
            kept: list[dict[str, Any]] = []
            for entry in env:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("name", ""))
                if "valueFrom" in entry:
                    redacted.append(name)
                    kept.append({"name": name, "fromSecret": True})
                else:
                    kept.append(dict(entry))
            spec["env"] = kept

        job_id = labels.get(naming.JOB_ID_LABEL) or naming.job_id_from_object_name(
            metadata.get("name", "")
        )
        return JobSpecResponse(
            job_id=job_id or "",
            name=(
                annotations.get(naming.DISPLAY_NAME_ANNOTATION)
                or labels.get(naming.DISPLAY_NAME_LABEL)
                or metadata.get("name", "")
            ),
            spec=spec,
            redacted=redacted,
        )


class JobListResponse(BaseModel):
    """What GET /v1/jobs returns: this caller's own jobs, newest first.

    ONLY THIS CALLER'S. The list is read from the token's namespace and nowhere
    else, so it cannot show a teammate's work — the same boundary every other
    route uses. /v1/stats adds a team's numbers up, but it carries no job names
    for exactly this reason.
    """

    jobs: list[JobView] = Field(default_factory=list)
    total: int = Field(
        default=0,
        description="How many jobs matched the filter before `limit` cut the "
        "list. Equal to len(jobs) when nothing was cut. The screen needs both "
        "numbers to say 'showing 200 of 512' instead of quietly hiding 312.",
    )
