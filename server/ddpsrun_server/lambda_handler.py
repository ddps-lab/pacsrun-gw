"""The entry point AWS Lambda calls, and the two things that differ from a pod.

END-TO-END FLOW of one invocation:

  1. Lambda calls `handler(event, context)`. `event` is an HTTP request in the
     Function URL's own JSON shape, not an ASGI scope.
  2. `Mangum` translates it and calls the same FastAPI app `main.py` builds, so
     every route, request model and generated schema is unchanged.
  3. The app's `lifespan` runs once per execution environment, which is where
     `Settings`, the token store and the cluster connection are built. On a warm
     invocation none of that happens again.

WHAT IS DIFFERENT FROM RUNNING AS A POD, and it is only two things.

  THE TOKEN LIST. A pod mounts it from a Secret; Lambda has no mounts. It is
  read from Secrets Manager here and written to a file under /tmp, because
  `TokenStore.load` takes a path and there is no reason for it to learn a second
  way to be fed. /tmp survives for the life of the execution environment, so a
  warm invocation does not call Secrets Manager again.

  THE CLUSTER CREDENTIALS. A pod has /var/run/secrets/kubernetes.io with a CA
  and a ServiceAccount token. Lambda has neither, so `k8s.Cluster.connect` falls
  back to a kubeconfig — and one is written here from `eks:DescribeCluster` plus
  an `exec` stanza that mints an EKS token from the function's own execution
  role. That role is registered as an access entry; see terraform/lambda.

WHY BOTH ARE FILES RATHER THAN OBJECTS PASSED IN. The alternative is teaching
`config.py` and `k8s.py` a Lambda-shaped second path, and then every test either
covers one shape or both. Writing two small files at cold start leaves the rest
of the server unable to tell where it is running.

Grep anchor: DDPSRUN-LAMBDA-HANDLER
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import sys

TMP = pathlib.Path("/tmp")
TOKENS_PATH = TMP / "ddpsrun-tokens.json"
KUBECONFIG_PATH = TMP / "ddpsrun-kubeconfig.yaml"
CA_PATH = TMP / "ddpsrun-cluster-ca.crt"


def _write_token_file() -> None:
    """Fetch the token list from Secrets Manager and put it where the server looks.

    Raises:
        RuntimeError: the secret is unreadable. Failing here rather than at the
            first request means the reason appears in the function's own logs
            instead of as a 500 nobody can explain.
    """
    secret_id = os.environ.get("DDPSRUN_TOKENS_SECRET_ID", "").strip()
    if not secret_id:
        raise RuntimeError(
            "DDPSRUN_TOKENS_SECRET_ID is not set. On Lambda the token list comes "
            "from Secrets Manager; there is no file to mount."
        )
    import boto3  # provided by the Lambda runtime, so it is not in our package

    try:
        response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)
    except Exception as exc:  # noqa: BLE001 - botocore raises several types here
        raise RuntimeError(f"cannot read the token list from {secret_id}: {exc}") from exc

    body = response.get("SecretString")
    if body is None:
        body = base64.b64decode(response["SecretBinary"]).decode("utf-8")
    TOKENS_PATH.write_text(body, encoding="utf-8")
    os.environ["DDPSRUN_TOKENS_PATH"] = str(TOKENS_PATH)


def _write_kubeconfig() -> None:
    """Build a kubeconfig from eks:DescribeCluster and point KUBECONFIG at it.

    THE `exec` STANZA IS THE WHOLE POINT. It tells the kubernetes client to mint a
    fresh bearer token for every call, signed with whatever credentials the
    environment has — here, the function's execution role. That role is an EKS
    access entry, so the apiserver recognises it. Measured 2026-09-01: the
    entry's `kubernetesGroups` value arrives in the request's Groups, which is
    why RBAC is bound to a group rather than to a username.

    IT RUNS OUR OWN PYTHON, NOT `aws eks get-token`. The first deployment did the
    latter and every cluster call failed with
    `[Errno 2] No such file or directory: 'aws'` — the Lambda Python runtime has
    no AWS CLI, and shipping one would add roughly 100 MB to a cold start already
    dominated by reading the package. `ddpsrun_server.eks_token` builds the same
    token with botocore, which the runtime does provide.

    AND IT STAYS AN `exec` RATHER THAN A TOKEN WRITTEN ONCE. EKS accepts one of
    these for about 15 minutes; a warm execution environment lives longer than
    that, so a token minted at cold start would go stale and later requests would
    401.

    Raises:
        RuntimeError: the cluster could not be described.
    """
    cluster_name = os.environ.get("DDPSRUN_CLUSTER_NAME", "").strip()
    if not cluster_name:
        raise RuntimeError("DDPSRUN_CLUSTER_NAME is not set")
    import boto3

    try:
        described = boto3.client("eks").describe_cluster(name=cluster_name)["cluster"]
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"cannot describe cluster {cluster_name}: {exc}") from exc

    CA_PATH.write_bytes(base64.b64decode(described["certificateAuthority"]["data"]))
    region = os.environ.get("AWS_REGION", "us-west-2")

    KUBECONFIG_PATH.write_text(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "clusters": [{
                    "name": cluster_name,
                    "cluster": {
                        "server": described["endpoint"],
                        "certificate-authority": str(CA_PATH),
                    },
                }],
                "users": [{
                    "name": cluster_name,
                    "user": {"exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        # sys.executable, not "python3": the runtime's interpreter
                        # lives at an absolute path and PATH inside a subprocess
                        # is not something to rely on.
                        "command": sys.executable,
                        "args": ["-m", "ddpsrun_server.eks_token",
                                 "--cluster-name", cluster_name,
                                 "--region", region],
                        # The subprocess needs to find both our package and the
                        # runtime's botocore, and it does not inherit sys.path.
                        "env": [
                            {"name": "PYTHONPATH", "value": os.pathsep.join(sys.path)},
                        ],
                    }},
                }],
                "contexts": [{
                    "name": cluster_name,
                    "context": {"cluster": cluster_name, "user": cluster_name},
                }],
                "current-context": cluster_name,
            }
        ),
        encoding="utf-8",
    )
    os.environ["KUBECONFIG"] = str(KUBECONFIG_PATH)


# COLD START WORK, deliberately at import time. Lambda charges for it either way,
# and doing it here means a warm invocation skips it entirely. Measured
# 2026-09-01: the import of our dependencies alone is about 4 seconds at 512 MB,
# and these two calls are milliseconds beside it.
_write_token_file()
_write_kubeconfig()

from mangum import Mangum  # noqa: E402 - must follow the two writes above

from .main import app, build_state  # noqa: E402

# DDPSRUN-BUILD-ONCE. Build the state here, at import, which Lambda runs once
# per execution environment. Then tell Mangum NOT to run the ASGI lifespan:
# "auto" runs it around every invocation, which rebuilt the kubernetes client
# and the JWKS cache on every single request and cost 1.4 seconds each time
# (measured on the deployed function, 2026-09-02).
build_state(app)

handler = Mangum(app, lifespan="off")
