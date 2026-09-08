"""Operator-supplied settings, read once from the process environment.

END-TO-END FLOW of this file:

  1. The server pod's Deployment sets a handful of environment variables
     (see `config/deploy/` once that exists).
  2. `Settings.from_env()` reads them at import time in `main.py` and returns one
     frozen object.
  3. Every other module takes that object as an argument. Nothing else calls
     `os.getenv`, so there is exactly one place to look when a value is wrong.

WHY the values live in the environment rather than in a file in this repo: they
are account identifiers (a bucket name, a ServiceAccount name). Committing them
would put them in a repository we intend to open later. `docs/00-overview.md`
records that rule and CI enforces it.

Grep anchor: DDPSRUN-CONFIG
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


# The Kubernetes API group/version/plural of the object this server creates.
# Taken from PACSrun's own CRD: `config/crd/pacsrun.io_pacsjobs.yaml` says
# `group: pacsrun.io`, `plural: pacsjobs`, and `api/v1alpha1/groupversion_info.go:21`
# says `version: v1alpha1`. If PACSrun ever bumps the version this is the one
# place to change.
PACSJOB_GROUP = "pacsrun.io"
PACSJOB_VERSION = "v1alpha1"
PACSJOB_PLURAL = "pacsjobs"

# The label PACSrun's controller puts on every pod it creates for a job, so we
# can find the pod whose logs a user asked for.
# Source: `internal/controller/pacsjob_controller.go:59` (`jobLabelKey`) and
# `:61` (`jobSlotLabelKey`).
PACSRUN_JOB_LABEL = "pacsrun.io/job"
PACSRUN_SLOT_LABEL = "pacsrun.io/slot"


class ConfigError(RuntimeError):
    """A required setting is missing or malformed. Raised at startup, never later."""


@dataclass(frozen=True)
class SecretBinding:
    """Where one named secret actually lives in the cluster.

    A user writes `"secrets": ["GITHUB_PAT"]` in a submit request. They must not
    have to know that the value sits in the Kubernetes Secret `some-secret`
    under key `token` — that is an internal name, and `docs/03-api.md` says
    internal names do not cross the API boundary. This maps the one to the other.
    """

    secret_name: str
    secret_key: str


@dataclass(frozen=True)
class Settings:
    """Everything the server needs to know that is not in the request.

    Attributes:
        result_bucket: S3 bucket every job's output goes to. The server builds
            `resultPath` from it so a user cannot write into someone else's
            folder (`docs/03-api.md`, the "서버가 채우는 것" / what-the-server-fills table).
        result_prefix: key prefix inside that bucket, e.g. `pacsrun/`. Always
            ends with a slash; `from_env` appends one if the operator forgot.
        service_account: the ServiceAccount name every job's pods run as. Fixed
            per cluster today; becomes per-namespace when multi-tenancy is on.
        tokens_path: file holding the API tokens. Mounted from a Kubernetes
            Secret. See `auth.py`.
        secret_bindings: name a user may write -> where it really is.
        log_tail_lines: how many lines of backlog `/v1/jobs/{id}/logs` returns
            before it starts following. 2000 is enough to see a training run's
            most recent progress lines without downloading hours of output.
        cognito_pool_id: the Cognito user pool that signs id_tokens. Empty
            disables the Cognito branch entirely and leaves static tokens as the
            only credential.
        cognito_client_id: the app client id an id_token must be addressed to.
        cognito_region: which region the pool is in. Half of the issuer URL, so
            a wrong value refuses every token rather than accepting a foreign one.
        cognito_login_domain: the Hosted UI, e.g.
            https://ddpsrun-x.auth.us-west-2.amazoncognito.com. The server never
            calls it; it hands the address to the screen and the CLI, which is
            why it is configuration and not something derived here.
        register_notify_to: DDPSRUN-REGISTER. Where a "somebody signed in and has
            no namespace" email goes. Empty turns the endpoint off and the screen
            then tells the person to contact an operator by other means -- which
            is the honest answer for a deployment that has not set this up, and
            better than a button that fails.
        register_notify_from: the From address on that email. Defaults to
            `register_notify_to`, because while the SES account is in the sandbox
            BOTH ends have to be verified identities and setting them equal means
            one verification click instead of two.
    """

    result_bucket: str
    result_prefix: str
    service_account: str
    tokens_path: str
    secret_bindings: dict[str, SecretBinding]
    log_tail_lines: int
    # All four default to empty because "no Cognito" is a supported state, not a
    # half-configured one: the server then accepts static tokens only, which is
    # what a local run, a test, and every deployment before 2026-09-01 does.
    cognito_pool_id: str = ""
    cognito_client_id: str = ""
    cognito_region: str = ""
    cognito_login_domain: str = ""
    # Empty means "this deployment cannot email an operator", which is a
    # supported state for the same reason no-Cognito is: every deployment before
    # 2026-09-08 was in it.
    register_notify_to: str = ""
    register_notify_from: str = ""

    @staticmethod
    def from_env(env: dict[str, str] | None = None) -> "Settings":
        """Build the settings, failing loudly on anything missing.

        Args:
            env: the environment to read. Defaults to the real process
                environment; tests pass a dict instead.

        Returns:
            A frozen `Settings`.

        Raises:
            ConfigError: a required variable is absent or a JSON one will not parse.
        """
        env = dict(os.environ) if env is None else env

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ConfigError(
                    f"{name} is not set. The server cannot fill in a job's "
                    f"resultPath without it, and a job with no resultPath "
                    f"silently produces nothing to collect."
                )
            return value

        prefix = env.get("DDPSRUN_RESULT_PREFIX", "pacsrun/").strip()
        # The trailing slash is load-bearing, exactly as it is in PACSrun's
        # tenancy guard: without it the prefix `pacsrun/lab-a` also matches
        # `pacsrun/lab-arthur/...`.
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        raw_secrets = env.get("DDPSRUN_SECRET_BINDINGS", "{}").strip() or "{}"
        try:
            parsed = json.loads(raw_secrets)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"DDPSRUN_SECRET_BINDINGS is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ConfigError("DDPSRUN_SECRET_BINDINGS must be a JSON object")

        bindings: dict[str, SecretBinding] = {}
        for public_name, where in parsed.items():
            if not isinstance(where, dict) or "name" not in where or "key" not in where:
                raise ConfigError(
                    f'DDPSRUN_SECRET_BINDINGS["{public_name}"] must be '
                    f'{{"name": "<secret>", "key": "<key>"}}'
                )
            bindings[public_name] = SecretBinding(str(where["name"]), str(where["key"]))

        try:
            tail = int(env.get("DDPSRUN_LOG_TAIL_LINES", "2000"))
        except ValueError as exc:
            raise ConfigError("DDPSRUN_LOG_TAIL_LINES must be an integer") from exc

        return Settings(
            result_bucket=required("DDPSRUN_RESULT_BUCKET"),
            result_prefix=prefix,
            # DDPSRUN-WORKLOAD-SA. The default is the ServiceAccount PACSrun's own terraform
            # wired to the EC2/STS role, because that role's trust policy names exactly one
            # namespace/ServiceAccount pair. It read "pacsrun-workload" until 2026-09-08 --
            # which is the ROLE's name, not the ServiceAccount's -- and every AWS job then
            # died with "Not authorized to perform sts:AssumeRoleWithWebIdentity", exit 10,
            # before renting anything. See terraform/lambda/variables.tf for the measurement.
            service_account=env.get("DDPSRUN_SERVICE_ACCOUNT", "pacsjob-writer").strip(),
            tokens_path=required("DDPSRUN_TOKENS_PATH"),
            secret_bindings=bindings,
            log_tail_lines=tail,
            # DDPSRUN-REGISTER. From defaults to To rather than to a made-up
            # no-reply address: an unverified sender is refused by SES in the
            # sandbox, so a default nobody verified would make the button fail
            # on every deployment that set only one of the two.
            register_notify_to=env.get("DDPSRUN_REGISTER_NOTIFY_TO", "").strip(),
            register_notify_from=(
                env.get("DDPSRUN_REGISTER_NOTIFY_FROM", "").strip()
                or env.get("DDPSRUN_REGISTER_NOTIFY_TO", "").strip()),
            # All three empty means no Cognito. That is a supported state, not a
            # broken one: the server then accepts static tokens only, which is
            # exactly what it did before Cognito existed and is what a local run
            # or a test wants (`docs/16-login.md` 16.3).
            cognito_pool_id=env.get("DDPSRUN_COGNITO_POOL_ID", "").strip(),
            cognito_client_id=env.get("DDPSRUN_COGNITO_CLIENT_ID", "").strip(),
            cognito_region=env.get(
                "DDPSRUN_COGNITO_REGION", env.get("AWS_REGION", "")
            ).strip(),
            cognito_login_domain=env.get("DDPSRUN_COGNITO_LOGIN_DOMAIN", "").rstrip("/"),
        )

# DDPSRUN-PROMETHEUS-PROXY. Where the Prometheus this server queries lives.
#
# Named here rather than passed in because there is exactly ONE: it is deployed by
# config/deploy/prometheus.yaml in the PACSrun repo, into the operator's own namespace, and a
# second one would be a different design rather than a different value. The Service is ClusterIP
# and stays that way — this server reaches it through the apiserver's `services/proxy`, so it
# needs no address of its own and no ALB.
PROMETHEUS_NAMESPACE = "pacsrun-system"
PROMETHEUS_SERVICE = "prometheus"
