"""Mint an EKS bearer token without the AWS CLI.

WHY THIS FILE EXISTS. A kubeconfig authenticates to EKS through an `exec` stanza
that runs `aws eks get-token`, and the Lambda Python runtime has no `aws` binary
— the first deployment failed with `[Errno 2] No such file or directory: 'aws'`.
Shipping the CLI would add roughly 100 MB to a cold start already dominated by
reading the package (measured 4.1 s at 105.5 MB), so the token is built here
instead. `botocore` is part of the Lambda runtime, so this costs nothing.

WHY IT STAYS AN `exec` STANZA rather than a token written once into the
kubeconfig. The token is a presigned URL with a 60-second signature, and EKS
treats it as usable for about 15 minutes. A warm Lambda execution environment
lives far longer than that, so a token minted at cold start would go stale and
every later request would fail with 401. `exec` is the mechanism that re-runs
this on each call.

WHAT THE TOKEN IS. `k8s-aws-v1.` plus a base64url of a presigned STS
GetCallerIdentity URL. It is NOT a JWT. The `x-k8s-aws-id` header naming the
cluster is signed into the URL, which is what stops a token minted for one
cluster being replayed against another: the receiving apiserver puts its OWN
name in that header before calling STS, and the signature then does not match.

Run as a program, it prints the ExecCredential JSON the kubernetes client
expects on stdout:

    python3 -m ddpsrun_server.eks_token --cluster-name pacsrun --region us-west-2

Verified 2026-09-01: a token built this way answered HTTP 200 against the live
apiserver.

Grep anchor: DDPSRUN-EKS-TOKEN
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime, timedelta, timezone

# How long the presigned URL's own signature is good for. 60 seconds is what the
# AWS CLI uses; the apiserver forwards the URL to STS immediately, so the window
# only has to cover one hop.
SIGNATURE_SECONDS = 60

# What we tell the kubernetes client. Shorter than EKS's own 15-minute
# acceptance window on purpose: the client refreshes when this passes, and
# refreshing early costs one signature while refreshing late costs a 401.
CREDENTIAL_MINUTES = 10


def eks_token(cluster_name: str, region: str) -> str:
    """Build the bearer token for one cluster.

    Args:
        cluster_name: signed into the URL as `x-k8s-aws-id`. A token for one
            cluster cannot be replayed against another.
        region: which STS endpoint to sign against.

    Returns:
        `k8s-aws-v1.<base64url of the presigned URL>`.
    """
    import botocore.session
    from botocore.signers import RequestSigner

    session = botocore.session.get_session()
    client = session.create_client("sts", region_name=region)
    signer = RequestSigner(
        client.meta.service_model.service_id,
        region,
        "sts",
        "v4",
        session.get_credentials(),
        session.get_component("event_emitter"),
    )
    url = signer.generate_presigned_url(
        {
            "method": "GET",
            "body": {},
            "url": (
                f"https://sts.{region}.amazonaws.com/"
                f"?Action=GetCallerIdentity&Version=2011-06-15"
            ),
            "context": {},
            "headers": {"x-k8s-aws-id": cluster_name},
        },
        region_name=region,
        expires_in=SIGNATURE_SECONDS,
        operation_name="",
    )
    # Unpadded base64url. The apiserver strips nothing and adds nothing back, so
    # the padding characters must not be there.
    encoded = base64.urlsafe_b64encode(url.encode("utf-8")).decode("utf-8").rstrip("=")
    return f"k8s-aws-v1.{encoded}"


def exec_credential(cluster_name: str, region: str) -> dict:
    """Wrap the token in the shape a kubeconfig `exec` stanza must print.

    Args:
        cluster_name: the cluster.
        region: the region.

    Returns:
        An ExecCredential object.
    """
    expires = datetime.now(timezone.utc) + timedelta(minutes=CREDENTIAL_MINUTES)
    return {
        "apiVersion": "client.authentication.k8s.io/v1beta1",
        "kind": "ExecCredential",
        "status": {
            "expirationTimestamp": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "token": eks_token(cluster_name, region),
        },
    }


def main(argv: list[str] | None = None) -> int:
    """Print the ExecCredential. The kubernetes client reads stdout."""
    parser = argparse.ArgumentParser(description="Mint an EKS bearer token.")
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args(argv)
    json.dump(exec_credential(args.cluster_name, args.region), sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
