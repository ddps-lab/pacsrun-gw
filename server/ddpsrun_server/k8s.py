"""The only module that talks to kube-apiserver, plus the log relay.

END-TO-END FLOW of this file:

  1. `Cluster.connect()` loads credentials. Inside the cluster that is the
     projected ServiceAccount token the pod already has; on a laptop it falls
     back to the developer's kubeconfig so the server can be run locally.
  2. `create_job()` POSTs a PacsJob into the caller's namespace. PACSrun's
     controller picks it up from there; this server never speaks to a vendor.
  3. `get_job()` fetches one back by name.
  4. `job_log_window()` is the awkward one. A PacsJob has no logs — a *pod*
     does. So it lists pods carrying `pacsrun.io/job=<name>` (the label the
     controller writes at `internal/controller/vendorpod.go:1222`), picks slot
     0, and reads a time window of that pod's stdout. It returns a window rather
     than a stream because a Lambda execution cannot outlive 15 minutes.
  5. Every line coming back goes through `redact()` before it reaches the user,
     because the driver's own bookkeeping lines are on the same stream as the
     workload's output.
  6. `recent_log_lines()` reads the same stream WITHOUT redacting, for the
     metrics endpoint, whose whole purpose is to read one of the lines the relay
     masks. See its docstring for why the redaction lives at the point of use.

WHY THE DYNAMIC CLIENT AND NOT GENERATED TYPES. PacsJob is a CRD, so the Python
client has no model for it. `CustomObjectsApi` takes and returns plain dicts,
which is all `models.py` produces and consumes. Nothing to generate, nothing to
regenerate when the CRD gains a field.

WHAT THIS SERVER NEEDS PERMISSION TO DO. Its ServiceAccount needs `create`,
`get` and `list` on `pacsjobs`, plus `list` on `pods` and `get` on `pods/log`,
in every tenant namespace. It does NOT need `get` on `secrets`: it writes a
`secretKeyRef` and lets kubelet do the reading, so a compromise of this server
does not hand over the GitHub token.

Grep anchor: DDPSRUN-K8S
"""

from __future__ import annotations

import json
import re
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from .config import (
    PACSJOB_GROUP,
    PACSJOB_PLURAL,
    PACSJOB_VERSION,
    PACSRUN_JOB_LABEL,
    PACSRUN_SLOT_LABEL,
    PROMETHEUS_NAMESPACE,
    PROMETHEUS_SERVICE,
    USER_SECRET_NAME,
)

# The driver prints its own bookkeeping on the same stdout as the workload.
# `PACSRUN_KEEPALIVE` is emitted every 30 seconds for the whole life of the job
# purely so the log stream stays open; a user reading their training output does
# not want one of those between every progress line.
_KEEPALIVE_LINE = re.compile(r"^\s*PACSRUN_KEEPALIVE\s*$")
# The GPU telemetry line, also every 30 seconds, also dropped whole. It is not
# the user's output: it exists for /v1/jobs/{id}/metrics, which reads the same
# log unredacted. Masking it instead of dropping it left a line reading
# "<internal>=97,38380,45440,72,304.0" between every couple of training lines,
# which is exactly the noise the keepalive rule exists to prevent. Found
# 2026-08-31 running the two endpoints against the same log.
# `PACSRUN_GPU=` and `PACSRUN_GPU_HEALTH=`. The optional `_HEALTH` is not tidiness: PACSrun's
# driver started printing the second line too (driver/common/gpu-watch.sh), and the first version
# of this pattern required "=" immediately after PACSRUN_GPU, so the health line missed the DROP
# rule, fell through to the token mask below, and reached the user as "<internal>=0x0000...,0"
# between their own output lines — the exact noise this rule exists to prevent.
# `_CARD` AND `_HEALTH_CARD` WERE ADDED 2026-09-08 AND THIS PATTERN DID NOT KNOW THEM. PACSrun
# #60 started printing one reading per card, so a four-card pod emits eight of these lines every
# 30 seconds; none matched, all eight fell through to the token mask below, and a reader of their
# own training output got eight `<internal>=0,94,38200,81920,71,298` lines between every couple of
# real ones. That is precisely the noise the paragraph above says this rule exists to prevent, and
# it is the second thing that change broke by adding a line shape without telling the code that
# reads line shapes. `/v1/jobs/{id}/metrics` reads the log UNREDACTED and is unaffected either way.
_GPU_LINE = re.compile(r"^\s*PACSRUN_GPU(_CARD|_HEALTH|_HEALTH_CARD)?=")
# Any other PACSRUN_* token is an internal name (`docs/03-api.md`, first rule
# of the "응답 규칙" / response-rules section). The line around it may be the
# user's own output, so the token is masked
# and the line kept, rather than the line being dropped.
_INTERNAL_TOKEN = re.compile(r"\bPACSRUN_[A-Z0-9_]+\b")




class NotFound(Exception):
    """No such object in that namespace. Routes turn this into HTTP 404."""


class ClusterError(Exception):
    """kube-apiserver refused or could not be reached. Routes turn this into 502."""


def redact(line: str) -> str | None:
    """Clean one log line on its way to a user.

    Args:
        line: a raw line from the pod's stdout.

    Returns:
        The line to send, or None to drop it entirely.

    Example:
        >>> redact("PACSRUN_KEEPALIVE") is None
        True
        >>> redact("PACSRUN_GPU=94,38200,45440,71,298.5") is None
        True
        >>> redact("done, PACSRUN_EXIT=0")
        'done, <internal>=0'
    """
    if _KEEPALIVE_LINE.match(line) or _GPU_LINE.match(line):
        return None
    return _INTERNAL_TOKEN.sub("<internal>", line)


class Cluster:
    """A connection to kube-apiserver, and the four operations stage 1 needs."""

    def __init__(self, custom: Any, core: Any) -> None:
        self._custom = custom
        self._core = core

    @staticmethod
    def connect() -> "Cluster":
        """Load credentials and build the two API clients.

        Returns:
            A ready `Cluster`.

        Raises:
            ClusterError: neither an in-cluster ServiceAccount token nor a
                kubeconfig was usable. Failing at startup makes the reason
                visible in the pod's logs.
        """
        try:
            config.load_incluster_config()
        except config.ConfigException:
            try:
                config.load_kube_config()
            except Exception as exc:  # noqa: BLE001 - the client raises several types here
                raise ClusterError(
                    "no Kubernetes credentials: not running in a cluster and no "
                    "usable kubeconfig"
                ) from exc
        return Cluster(client.CustomObjectsApi(), client.CoreV1Api())

    def create_job(self, namespace: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create one PacsJob.

        Args:
            namespace: the caller's namespace, from their token.
            body: what `models.to_pacsjob` produced.

        Returns:
            The object as the API server stored it, with defaults filled in.

        Raises:
            ClusterError: the API server refused. Its own message is included
                because it is the one that says which field was wrong — the CRD
                carries CEL rules whose messages are written for a human.
        """
        try:
            return self._custom.create_namespaced_custom_object(
                group=PACSJOB_GROUP,
                version=PACSJOB_VERSION,
                namespace=namespace,
                plural=PACSJOB_PLURAL,
                body=body,
            )
        except ApiException as exc:
            raise ClusterError(_api_message(exc)) from exc

    def get_job(self, namespace: str, name: str) -> dict[str, Any]:
        """Fetch one PacsJob by name.

        Raises:
            NotFound: no object of that name in that namespace. This is also
                what a caller gets for a job that belongs to someone else,
                which is why guessing a job id is not worth doing.
            ClusterError: anything else.
        """
        try:
            return self._custom.get_namespaced_custom_object(
                group=PACSJOB_GROUP,
                version=PACSJOB_VERSION,
                namespace=namespace,
                plural=PACSJOB_PLURAL,
                name=name,
            )
        except ApiException as exc:
            if exc.status == 404:
                raise NotFound(name) from exc
            raise ClusterError(_api_message(exc)) from exc

    def delete_job(self, namespace: str, name: str) -> None:
        """Delete one PacsJob.

        WHAT THIS ACTUALLY STOPS. Deleting the PacsJob is what PACSrun watches;
        its controller owns the pods and the rented capacity, and removing the
        object is the signal to give them back. This server does not delete pods
        or nodes itself, and must not: it holds no knowledge of what a job
        rented, and a half-cleanup would leave capacity nobody is tracking.

        WHY THERE IS NO SEPARATE "CANCEL". A PacsJob has no field meaning "stop
        but stay". Deleting it is the only stop the CRD offers, so a cancel that
        left the row on screen would be a lie about what happened.

        Args:
            namespace: the caller's namespace, from their token. A job in any
                other namespace is not reachable from here at all.
            name: the PacsJob's Kubernetes name.

        Raises:
            NotFound: no object of that name in that namespace. Also what a
                caller gets for someone else's job, which is why guessing an id
                tells you nothing.
            ClusterError: anything else.
        """
        try:
            self._custom.delete_namespaced_custom_object(
                group=PACSJOB_GROUP,
                version=PACSJOB_VERSION,
                plural=PACSJOB_PLURAL,
                namespace=namespace,
                name=name,
            )
        except ApiException as exc:
            if exc.status == 404:
                raise NotFound(name) from exc
            raise ClusterError(f"could not delete {name}: {exc.reason}") from exc

    def list_jobs(self, namespace: str) -> list[dict[str, Any]]:
        """Every PacsJob in one namespace.

        Used by /v1/stats, which calls it once per namespace of a team rather
        than listing the cluster: the server's ClusterRole is bound per tenant
        namespace, so a cluster-wide list would need a permission it does not
        have and should not be given.

        Args:
            namespace: the namespace to list.

        Returns:
            The objects. An empty list for a namespace with no jobs, and also
            for one this server cannot see — a team whose member namespace was
            never bound is a configuration gap, not an error worth failing the
            whole request over.

        Raises:
            ClusterError: the API server refused for a reason other than not
                finding the namespace.
        """
        try:
            response = self._custom.list_namespaced_custom_object(
                group=PACSJOB_GROUP,
                version=PACSJOB_VERSION,
                namespace=namespace,
                plural=PACSJOB_PLURAL,
            )
        except ApiException as exc:
            if exc.status in (403, 404):
                return []
            raise ClusterError(_api_message(exc)) from exc
        return list(response.get("items") or [])

    def prometheus_query(self, expr: str, timeout_seconds: int = 10) -> dict:
        """Ask the in-cluster Prometheus one instant query, through the apiserver.

        DDPSRUN-PROMETHEUS-PROXY. The route is
        `/api/v1/namespaces/pacsrun-system/services/prometheus:9090/proxy/...` — the apiserver's
        own `services/proxy` subresource, which forwards to the Service and returns the reply.

        WHY THIS AND NOT AN ADDRESS OF ITS OWN. Prometheus holds one lab's GPU history, and a
        public endpoint for it means an ALB at $16.43/month plus a second place to get
        authentication wrong. This server ALREADY proves who it is to the apiserver in order to
        read a job's logs, so proxying costs one verb on a ClusterRole — a reviewable diff rather
        than an open port. The Service stays ClusterIP.

        IT NEEDED ONE SECURITY GROUP RULE, and the way it failed first is worth knowing. Both
        `services/proxy` and `pods/proxy` to 9090 HUNG — no response, no error, 20 s and zero
        bytes. The apiserver's proxy makes the CONTROL PLANE dial the pod IP, and this cluster's
        node security group admitted that source on seven ports only (443, 4443, 6443, 8443,
        9443, 10250, 10251). A security group has no reject action, so the SYN was dropped in
        silence. `pods/log` had always worked because 10250 is on that list.
        PACSRUN-APISERVER-POD-PROXY in PACSrun's terraform/cluster/main.tf is the rule that opens
        9090; without it this method times out rather than answering, and nothing says why.

        WHY AN INSTANT QUERY IS ENOUGH FOR LAMBDA, when a log stream was not. A Lambda execution
        cannot outlive 15 minutes, which is why `follow_logs` does not exist and the screen polls.
        A Prometheus query is request/response and answers in milliseconds, so a chart that
        refreshes is many short calls rather than one long one.

        Args:
            expr: a PromQL expression, passed through untouched.
            timeout_seconds: how long to wait for the apiserver's reply.

        Returns:
            Prometheus' own JSON body, parsed. Its `status` field is Prometheus' verdict and is
            handed back rather than interpreted here: a request that is valid HTTP and invalid
            PromQL is the caller's problem, not a cluster error.

        Raises:
            ClusterError: the apiserver refused or could not reach the Service. A 403 underneath
                means the ClusterRole is missing `services/proxy`; a timeout means the path above.
        """
        path = ("/api/v1/namespaces/" + PROMETHEUS_NAMESPACE + "/services/"
                + PROMETHEUS_SERVICE + ":9090/proxy/api/v1/query")
        try:
            # The raw path: the generated client has no binding for a proxy subresource, and
            # _preload_content=False stops it deserialising Prometheus' body into a Kubernetes
            # model it will never match.
            response = self._core.api_client.call_api(
                path, "GET",
                query_params=[("query", expr)],
                header_params={"Accept": "application/json"},
                auth_settings=["BearerToken"],
                response_type=None,
                _preload_content=False,
                _request_timeout=timeout_seconds,
            )
        except ApiException as exc:
            raise ClusterError(_api_message(exc)) from exc
        body = response[0].data if isinstance(response, tuple) else response.data
        try:
            return json.loads(body)
        except ValueError as exc:
            # A body that is not JSON means something other than Prometheus answered, most likely
            # the apiserver's own error page. Saying so beats a JSONDecodeError traceback.
            raise ClusterError(
                "the Prometheus proxy returned a body that is not JSON; the first 200 bytes are "
                + repr(body[:200])) from exc

    def job_pod_name(self, namespace: str, job_name: str, slot: int = 0) -> str:
        """Find the pod carrying one slot of a job.

        Args:
            namespace: the caller's namespace.
            job_name: the PacsJob's Kubernetes name.
            slot: which pod of a parallel job. Stage 1 always submits
                parallelism 1, so this is always 0 today.

        Returns:
            The pod's name.

        Raises:
            NotFound: the controller has not created the pod yet, or it has
                already been garbage-collected. Both are normal states, so the
                route reports them as "no logs yet" rather than as an error.
        """
        selector = f"{PACSRUN_JOB_LABEL}={job_name},{PACSRUN_SLOT_LABEL}={slot}"
        try:
            pods = self._core.list_namespaced_pod(namespace=namespace, label_selector=selector)
        except ApiException as exc:
            raise ClusterError(_api_message(exc)) from exc
        if not pods.items:
            raise NotFound(f"no pod yet for {job_name}")
        return pods.items[0].metadata.name

    def user_secret_names(self, namespace: str) -> list[str]:
        """Which names this namespace has registered. NAMES ONLY.

        DDPSRUN-USER-SECRET. `read_namespaced_secret` returns the values too --
        there is no "keys only" read in the Kubernetes API -- so this method
        takes `.keys()` and lets the object go. It must never be returned,
        logged, or put in an exception: the whole promise of the submit path is
        that a value goes from the Secret to kubelet without passing through
        this process, and the only reason this read exists at all is that
        `GET /v1/secrets` has to answer "what may I write" and `to_pacsjob` has
        to know whether a name is registered before it writes a secretKeyRef
        for it.

        Args:
            namespace: the caller's namespace.

        Returns:
            Sorted key names. Empty when nothing is registered yet, which is
            also what a namespace with no Secret answers -- a 404 here is the
            normal state before the first `ddpsrun secret set`, not an error.

        Raises:
            ClusterError: the API server refused for any reason other than 404.
                A 403 means the namespace has no `ddpsrun-gw-secrets`
                RoleBinding, which is an operator's onboarding step.
        """
        try:
            secret = self._core.read_namespaced_secret(
                name=USER_SECRET_NAME, namespace=namespace
            )
        except ApiException as exc:
            if exc.status == 404:
                return []
            raise ClusterError(_api_message(exc)) from exc
        return sorted((secret.data or {}).keys())

    def put_user_secret(self, namespace: str, name: str, value: str) -> bool:
        """Store one value under `name` in this namespace, replacing any it had.

        Args:
            namespace: the caller's namespace.
            name: the environment variable name, already checked by the caller.
            value: the secret. NOT logged, NOT echoed, and NOT included in any
                message raised from here -- `_api_message` reads the API
                server's own error body, which never contains the request.

        Returns:
            True when the Secret had to be created, False when it already
            existed and one key was patched into it. The caller says which
            happened so a user can tell "I added the first one" from "I
            replaced the one that was there".

        Raises:
            ClusterError: the API server refused.

        WHY `stringData` AND A PATCH. `stringData` is the write-only half of a
        Secret: the API server base64-encodes it into `data` and drops it, so
        nothing here has to encode anything. A strategic merge patch of a plain
        map MERGES its keys, so patching one name leaves the others alone --
        which matters because this server cannot read the Secret's values and
        therefore could not rewrite the whole object even if it wanted to.
        """
        body = {"stringData": {name: value}}
        try:
            self._core.patch_namespaced_secret(
                name=USER_SECRET_NAME, namespace=namespace, body=body
            )
            return False
        except ApiException as exc:
            if exc.status != 404:
                raise ClusterError(_api_message(exc)) from exc
        # First one in this namespace: the Secret does not exist yet. `create`
        # is the one secrets verb the Role cannot restrict to a name, so it is
        # granted alone and this is its only use.
        try:
            self._core.create_namespaced_secret(
                namespace=namespace,
                body=client.V1Secret(
                    metadata=client.V1ObjectMeta(
                        name=USER_SECRET_NAME,
                        namespace=namespace,
                        # So a human reading `kubectl get secret` knows who made
                        # it and that deleting it deletes people's registrations.
                        labels={"ddpsrun.io/managed-by": "ddpsrun-gw"},
                        annotations={
                            "ddpsrun.io/what": (
                                "values registered through POST /v1/secrets by "
                                "members of this namespace. One key per "
                                "environment variable name."
                            )
                        },
                    ),
                    string_data={name: value},
                ),
            )
            return True
        except ApiException as exc:
            raise ClusterError(_api_message(exc)) from exc

    def delete_user_secret(self, namespace: str, name: str) -> None:
        """Remove one registered name from this namespace.

        A registration that cannot be removed is the worse half of a
        registration: a value put in by mistake, or one that leaked, would stay
        readable by every job in the namespace forever.

        Args:
            namespace: the caller's namespace.
            name: the environment variable name to forget.

        Raises:
            NotFound: no such name here (or nothing registered at all).
            ClusterError: anything else.

        WHY `data` AND NOT `stringData`. The key lives in `data` once the API
        server has encoded it; `stringData` is gone by then, so a null there
        would delete nothing. Under a merge patch, null removes the key.

        ★ WHY THE EXISTENCE CHECK IS NOT REDUNDANT. A merge patch that nulls a
        key the object does not have is a NO-OP and the API server answers 200,
        so without this read a typo answered 204 -- "removed" -- for a name
        that was never there. Measured against the live cluster on 2026-09-08,
        where the unit test had passed because the test double raised NotFound
        and the real API does not. For a route whose reason to exist is "a
        credential leaked, take it out now", answering "done" to the wrong name
        is the one wrong answer that matters.
        """
        if name not in self.user_secret_names(namespace):
            raise NotFound(name)
        try:
            self._core.patch_namespaced_secret(
                name=USER_SECRET_NAME, namespace=namespace,
                body={"data": {name: None}},
            )
        except ApiException as exc:
            if exc.status == 404:
                raise NotFound(name) from exc
            raise ClusterError(_api_message(exc)) from exc

    def exec_in_driver(
        self,
        namespace: str,
        job_name: str,
        slot: int,
        argv: list[str],
        timeout_seconds: int,
    ) -> tuple[str, int | None]:
        """Run ONE command in a job's driver pod and return what it printed.

        THE RELAY CHAIN, spelled out. This call reaches the DRIVER pod — the
        GPU-less pod in OUR cluster — over the apiserver's exec subresource,
        an OUTBOUND WebSocket from this server. (A Lambda may OPEN WebSockets;
        it is INBOUND ones a Function URL cannot accept, which is why there is
        no terminal in the browser.) Inside that pod the argv starts shell.py,
        and shell.py tunnels the command into the workload container on the
        rented machine — verified live 2026-09-07, exit codes relay like ssh.
        The user types one line and the answer comes back from the GPU
        machine, with no kubectl and no cluster credential on their laptop.

        ONE COMMAND, NOT A TTY. A Lambda execution ends with its response, so
        nothing can hold a session open between keystrokes; the CLI's shell
        prompt sends one line per request instead.

        Args:
            namespace, job_name, slot: which driver pod, found by the same
                label lookup the logs route uses.
            argv: the exact command vector to start in the driver pod.
            timeout_seconds: how long to wait for the command to finish.

        Returns:
            (everything it printed, its exit code). The exit code is None when
            the command was still running at the timeout — reported as such,
            never as 0.

        Raises:
            NotFound: no pod — not created yet, or already collected.
            ClusterError: the apiserver refused. A 403 here means the
                ClusterRole lacks pods/exec (config/deploy/rbac.yaml).
        """
        pod = self.job_pod_name(namespace, job_name, slot)
        # A local import, the same convention lambda_handler uses for boto3:
        # the stream helper drags in websocket-client, and only this method
        # needs it.
        from kubernetes.stream import stream

        try:
            ws = stream(
                self._core.connect_get_namespaced_pod_exec,
                name=pod,
                namespace=namespace,
                command=argv,
                stdout=True,
                stderr=True,
                stdin=False,
                tty=False,
                _preload_content=False,
            )
        except ApiException as exc:
            if exc.status == 404:
                raise NotFound(pod) from exc
            raise ClusterError(_api_message(exc)) from exc

        ws.run_forever(timeout=timeout_seconds)
        output = (ws.read_stdout() or "") + (ws.read_stderr() or "")
        try:
            code: int | None = ws.returncode
        except Exception:  # noqa: BLE001 - no status frame yet means "still running"
            code = None
        ws.close()
        return output, code

    def recent_log_lines(
        self, namespace: str, job_name: str, since_seconds: int
    ) -> list[str]:
        """Read a time window of a job's log, unredacted.

        WHY UNREDACTED, when `job_logs` masks these very lines. The metrics
        endpoint exists precisely to read `PACSRUN_GPU=`, which the user-facing
        relay masks as an internal name. The two callers want opposite things
        from the same stream, so the redaction belongs at the point of use
        rather than here.

        WHY A WINDOW RATHER THAN THE WHOLE LOG. A 25-hour training run's log
        runs to hundreds of thousands of lines. `since_seconds` is what makes
        reading it every few seconds affordable, and it is enough: a chart shows
        a window anyway.

        Args:
            namespace: the caller's namespace.
            job_name: the PacsJob's Kubernetes name.
            since_seconds: how far back to read.

        Returns:
            The lines, oldest first.

        Raises:
            NotFound: the pod does not exist yet, or has been collected.
            ClusterError: the API server refused.
        """
        pod = self.job_pod_name(namespace, job_name)
        try:
            # _preload_content=False and decode by hand, exactly as
            # job_log_window below already does: the client's default path
            # wraps the body in str(), and a text/plain log comes back as the
            # repr of a bytes object — the WHOLE window as one "line" full of
            # literal \n sequences. Measured 2026-09-07 on baseline-c: the
            # default path returned 1 line for a 7200s window that kubectl put
            # at 113 lines, so the metrics scan found one stale GPU sample and
            # the screen's chart was a single old point. This reader shipped
            # without the fix the log relay got on 2026-09-01; now they match.
            response = self._core.read_namespaced_pod_log(
                name=pod,
                namespace=namespace,
                since_seconds=since_seconds,
                # The apiserver's own stamp on every line. metrics.scan() reads
                # it into GpuSample.time, which is what lets the chart put real
                # clock time on its x axis instead of "some window ago".
                timestamps=True,
                _preload_content=False,
            )
            text = response.read().decode("utf-8", "replace")
        except ApiException as exc:
            raise ClusterError(_api_message(exc)) from exc
        return text.splitlines()

    def job_log_window(
        self,
        namespace: str,
        job_name: str,
        since_seconds: int,
        tail_lines: int,
    ) -> list[str]:
        """Read one window of a job's output, timestamped and redacted.

        THIS REPLACED A STREAM, and the reason is not stylistic. The old shape
        held one connection open for the life of the job with
        `read_namespaced_pod_log(follow=True)`. A Lambda execution is capped at
        15 minutes and a training run is thirty hours, so that connection cannot
        exist. See `docs/14-serverless.md`.

        HOW THE CALLER AVOIDS DUPLICATES, and why the server needs no memory for
        it. Every line comes back prefixed with the RFC 3339 time the apiserver
        stamped it (`timestamps=True`). The caller keeps the last timestamp it
        printed and drops anything at or before it on the next window. Nothing
        is remembered here, which is what lets a fresh Lambda answer every
        request.

        Measured 2026-09-01 against a pod printing every two seconds: a
        30-second window read every 6 seconds returned 15 lines each time, of
        which 3 were new, in 137 ms.

        Args:
            namespace: the caller's namespace.
            job_name: the PacsJob's Kubernetes name.
            since_seconds: how far back to read. Make it several times the
                polling interval: too narrow and a caller that pauses misses
                lines, too wide and every request re-sends what it already sent.
                0 or less means NO time filter — just the newest `tail_lines`
                of the whole log — which is the only way to see output that is
                hours old, because this filter is measured back from now.
            tail_lines: a hard cap on the window, so a job that produces
                thousands of lines a second cannot return an unbounded body.

        Returns:
            Lines WITHOUT a trailing newline, oldest first, each still carrying
            its timestamp prefix.

        Raises:
            NotFound: the pod does not exist yet, or has been collected.
            ClusterError: the API server refused.
        """
        pod = self.job_pod_name(namespace, job_name)
        try:
            # _preload_content=False and decode by hand. The default wraps the
            # body in str(), and a text/plain log comes back as the repr of a
            # bytes object — one "line" containing literal \n sequences.
            response = self._core.read_namespaced_pod_log(
                name=pod,
                namespace=namespace,
                tail_lines=tail_lines,
                timestamps=True,
                _preload_content=False,
                # since_seconds=0 is not "everything" to the apiserver, it is
                # malformed — so a no-filter read OMITS the parameter instead.
                **({"since_seconds": since_seconds} if since_seconds > 0 else {}),
            )
            text = response.read().decode("utf-8", "replace")
        except ApiException as exc:
            raise ClusterError(_api_message(exc)) from exc

        out: list[str] = []
        for raw in text.splitlines():
            if not raw.strip():
                continue
            # redact() works on the line's content; the timestamp is ours to
            # keep, so split it off, clean the rest, and put it back.
            stamp, _, body = raw.partition(" ")
            cleaned = redact(body)
            if cleaned is not None:
                out.append(f"{stamp} {cleaned}")
        return out


def _api_message(exc: ApiException) -> str:
    """Pull the human-readable part out of a Kubernetes API error.

    The client puts the whole HTTP response in `str(exc)`, headers included.
    What a user needs is the `message` field of the Status object inside it.
    """
    body = getattr(exc, "body", None)
    if body:
        import json

        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and parsed.get("message"):
                return str(parsed["message"])
        except (ValueError, TypeError):
            pass
    return f"kube-apiserver returned {exc.status}"
