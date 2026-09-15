"""Where the directory file comes from, for every way this server is run.

END-TO-END FLOW:

  1. Something starts the server -- a Lambda cold start, or `uvicorn` in a pod.
  2. If `HYPERUN_TOKENS_SECRET_ID` is set, `fetch_to_file()` reads that secret out
     of AWS Secrets Manager and writes it to a path on local disk, then points
     `HYPERUN_TOKENS_PATH` at it.
  3. `Settings.from_env()` reads that path like any other file, and `TokenStore`
     parses it. Nothing below this module knows where the bytes came from.

WHAT THE FILE IS, because the name misleads. It is NOT a credential anybody
presents. It is the server's own DIRECTORY: one entry per person, saying which
namespace they get, which team they are in, and whether they are an operator.
A request arrives with a Cognito id_token or a static token; the server verifies
that, and then looks the person up in here to find out what they may touch.
`auth.py` is the reader.

WHY THIS IS ITS OWN MODULE AND NOT A FUNCTION IN `lambda_handler.py`, where it
lived until 2026-09-14. There is nothing about it that needs Lambda: it calls
Secrets Manager with boto3 and writes a file, and both work anywhere with AWS
credentials. Leaving it there would have forced the pod deployment either to
import the Lambda adapter or to grow a second copy -- and a second copy of "who
is allowed in" is the one duplication this system must not have. The gateway
runs as a Lambda AND as a pod for the length of the migration, and they must
never disagree about who exists.

HOW THE POD GETS AWS CREDENTIALS, since it has none by default. EKS Pod Identity
associates a ServiceAccount with an IAM role; the agent on the node then puts
temporary credentials where boto3 finds them, exactly as the Lambda runtime does
for a function. The role needs `secretsmanager:GetSecretValue` on this one secret
and nothing else.

WHY IT IS RE-READ, which a Lambda never had to do. A Lambda re-runs this on every
cold start, so registering somebody took effect within minutes without anybody
thinking about it. A pod that stays up for weeks would read it once and never
notice a new person again. `refresh_forever()` is the answer: it re-fetches on an
interval, and the server swaps in the new directory only when it parses.

Grep anchor: HYPERUN-TOKENS-SOURCE
"""

from __future__ import annotations

import base64
import logging
import os
import pathlib
import threading

logger = logging.getLogger("ddpsrun")

# How often a long-lived process re-reads the directory. Sixty seconds is chosen
# against the thing it is for: an operator registers somebody and then tells them
# to try again. A minute is shorter than that conversation. The call is one
# GetSecretValue against a document of a few hundred bytes.
REFRESH_SECONDS = 60.0


def secret_id(env: dict[str, str] | None = None) -> str:
    """The Secrets Manager secret holding the directory, or "" for a mounted file.

    Args:
        env: environment to read; defaults to the real one. Tests pass a dict.

    Returns:
        The secret id, or "" when this deployment mounts the file instead --
        which is what a local run and the test suite do, and is not an error.
    """
    env = dict(os.environ) if env is None else env
    for prefix in ("HYPERUN_", "DDPSRUN_"):        # HYPERUN-ENV-RENAME
        value = env.get(prefix + "TOKENS_SECRET_ID", "").strip()
        if value:
            return value
    return ""


def fetch_to_file(path: pathlib.Path, sid: str = "") -> bool:
    """Read the directory out of Secrets Manager and write it to `path`.

    Args:
        path: where to write it. The caller picks somewhere writable -- /tmp on
            Lambda, an emptyDir in a pod, since the container's root filesystem
            is read-only.
        sid: the secret id. Empty asks `secret_id()`.

    Returns:
        True when a file was written, False when this deployment has no secret id
        and therefore expects the file to be mounted already.

    Raises:
        RuntimeError: the secret id is set but unreadable. Failing here rather
            than at the first request puts the reason in the process's own log
            instead of behind a 500 nobody can explain.
    """
    sid = sid or secret_id()
    if not sid:
        return False

    import boto3

    # ★ THE REGION IS PASSED AND NOT LEFT TO THE ENVIRONMENT. botocore looks for
    # `AWS_DEFAULT_REGION`; the Lambda runtime sets that itself, and a pod's
    # Deployment naturally sets `AWS_REGION` -- which is the name everything else
    # in Kubernetes uses. The pod therefore had a region in its environment and
    # still died with `You must specify a region` at startup (2026-09-15).
    # Reading both names here and handing the answer to the client means it no
    # longer matters which one the deployment happened to set.
    region = (os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "").strip()

    try:
        client = boto3.client("secretsmanager", region_name=region or None)
        response = client.get_secret_value(SecretId=sid)
    except Exception as exc:  # noqa: BLE001 - botocore raises several types here
        raise RuntimeError(f"cannot read the token list from {sid}: {exc}") from exc

    body = response.get("SecretString")
    if body is None:
        body = base64.b64decode(response["SecretBinary"]).decode("utf-8")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    # Both names, so a process whose other variables are still the old ones finds
    # the path too (HYPERUN-ENV-RENAME).
    os.environ["HYPERUN_TOKENS_PATH"] = str(path)
    os.environ["DDPSRUN_TOKENS_PATH"] = str(path)
    return True


def refresh_forever(path: pathlib.Path, on_new, seconds: float = REFRESH_SECONDS,
                    stop: threading.Event | None = None) -> threading.Thread | None:
    """Re-read the directory on an interval, for a process that does not restart.

    Args:
        path: the same path `fetch_to_file` writes.
        on_new: called with no arguments after each successful re-fetch. The
            caller reloads its `TokenStore` there.
        seconds: how long to wait between reads.
        stop: set it to end the loop. `stop.wait(seconds)` is the sleep, so
            setting it ends the thread at once rather than after the remaining
            interval.

            ★ IT EXISTS BECAUSE A THREAD NOBODY CAN STOP LEAKS INTO WHATEVER
            RUNS NEXT. The first version slept and looped forever, which is
            harmless in a server that exits by dying -- and wrong everywhere
            else. Two tests started one at a 0.01s interval and it outlived
            them, re-fetching and rewriting `os.environ` underneath the tests
            that followed, so the suite failed only in full and passed when that
            file ran alone (2026-09-15).

    Returns:
        The daemon thread, or None when there is no secret to re-read (a mounted
        file changes on disk by itself and `auth.TokenStore` can be reloaded from
        it without this).

    A FAILED FETCH IS LOGGED AND SWALLOWED. The directory already in memory is
    still valid; throwing here would take down a server that is answering
    correctly because Secrets Manager was briefly unreachable. The same reasoning
    as the driver's shell session: a convenience must not be able to fail the
    thing it decorates.
    """
    if not secret_id():
        return None

    stop = stop or threading.Event()

    def loop() -> None:
        while not stop.wait(seconds):
            try:
                if fetch_to_file(path):
                    on_new()
            except Exception as exc:  # noqa: BLE001 - see the docstring
                logger.warning("could not refresh the directory: %s", exc)

    thread = threading.Thread(target=loop, daemon=True, name="tokens-refresh")
    thread.start()
    return thread
