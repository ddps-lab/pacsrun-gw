"""The only module that talks to kube-apiserver, plus the log relay.

END-TO-END FLOW of this file:

  1. `Cluster.connect()` loads credentials. Inside the cluster that is the
     projected ServiceAccount token the pod already has; on a laptop it falls
     back to the developer's kubeconfig so the server can be run locally.
  2. `create_job()` POSTs a PacsJob into the caller's namespace. PACSrun's
     controller picks it up from there; this server never speaks to a vendor.
  3. `get_job()` fetches one back by name.
  4. `job_logs()` is the awkward one. A PacsJob has no logs — a *pod* does. So it
     lists pods carrying `pacsrun.io/job=<name>` (the label the controller
     writes at `internal/controller/vendorpod.go:1222`), picks slot 0, and
     streams that pod's stdout.
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

import re
from typing import Any, Iterator

from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException

from .config import (
    PACSJOB_GROUP,
    PACSJOB_PLURAL,
    PACSJOB_VERSION,
    PACSRUN_JOB_LABEL,
    PACSRUN_SLOT_LABEL,
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
_GPU_LINE = re.compile(r"^\s*PACSRUN_GPU=")
# Any other PACSRUN_* token is an internal name (`docs/03-api.md`, 응답 규칙
# 첫째). The line around it may be the user's own output, so the token is masked
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
            text = self._core.read_namespaced_pod_log(
                name=pod, namespace=namespace, since_seconds=since_seconds
            )
        except ApiException as exc:
            raise ClusterError(_api_message(exc)) from exc
        return text.splitlines()

    def job_logs(
        self,
        namespace: str,
        job_name: str,
        follow: bool,
        tail_lines: int,
    ) -> Iterator[str]:
        """Yield a job's output, line by line, already redacted.

        Args:
            namespace: the caller's namespace.
            job_name: the PacsJob's Kubernetes name.
            follow: keep the connection open and yield new lines as they arrive.
            tail_lines: how much backlog to send first.

        Yields:
            Lines with a trailing newline, ready to write to the HTTP response.

        Raises:
            NotFound: propagated from `job_pod_name`.
            ClusterError: the API server refused the log request.
        """
        pod = self.job_pod_name(namespace, job_name)
        if not follow:
            try:
                text = self._core.read_namespaced_pod_log(
                    name=pod, namespace=namespace, tail_lines=tail_lines
                )
            except ApiException as exc:
                raise ClusterError(_api_message(exc)) from exc
            for raw in text.splitlines():
                cleaned = redact(raw)
                if cleaned is not None:
                    yield cleaned + "\n"
            return

        # `watch.Watch().stream` on read_namespaced_pod_log gives us one line per
        # iteration and closes when the container exits. It is the same call the
        # driver's own log relay uses against RunPod, one level up.
        stream = watch.Watch().stream(
            self._core.read_namespaced_pod_log,
            name=pod,
            namespace=namespace,
            follow=True,
            tail_lines=tail_lines,
            _preload_content=False,
        )
        try:
            for raw in stream:
                cleaned = redact(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
                if cleaned is not None:
                    yield cleaned + "\n"
        except ApiException as exc:
            raise ClusterError(_api_message(exc)) from exc


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
