"""Tell the operator that somebody signed in and has no namespace yet.

END-TO-END FLOW of one registration request:

  1. A person signs in with Google. Cognito verifies them and hands the screen an
     id_token. That token is GOOD -- the signature checks out and the address is
     real -- and every route still answers 403, because being known to Google is
     not the same as being registered here (`auth.principal_for_email`).
  2. The screen shows the "not registered" screen and offers one button. Pressing
     it posts to `POST /v1/register-request` with that same id_token.
  3. `main.require_signed_in` verifies the token the same way every other route
     does and returns the address WITHOUT looking it up in the token file, which
     is the one thing that makes this route reachable at all.
  4. `already_asked()` writes a marker object to S3 with `If-None-Match: *`. If the
     object is already there the write fails with 412 and no email is sent -- see
     WHY EXACTLY ONCE below.
  5. `send_registration_request()` calls SES `SendEmail` once, to the operator's
     address, with the exact JSON the operator has to paste into the token file
     and the command that puts it back in Secrets Manager.
  6. If step 5 FAILS, `release_marker()` deletes what step 4 wrote. Without that
     the marker outlives a send that never happened, and the next press answers
     202 "an operator was already emailed" when nobody was -- see WHY THE CLAIM
     IS GIVEN BACK below.

WHY EXACTLY ONCE PER ADDRESS, AND WHAT IT DOES NOT PROTECT. This endpoint is on a
public Lambda URL and the only thing it demands is a valid Cognito id_token, which
ANY Google account can obtain: `allow_admin_create_user_only` blocks the built-in
username/password sign-up, not a federated first sign-in. So without a marker, one
person reloading the screen sends one email per reload. The marker bounds it to
one email per distinct address, forever.

★ WHY THE CLAIM IS GIVEN BACK WHEN THE SEND FAILS, and why the marker is written
FIRST anyway. The marker has to be claimed before the send, or a reload mails the
operator again while the first request is still in flight -- the claim is what
makes "once" true. But a claim that survives a failed send is worse than no claim
at all: the person is locked out AND told they succeeded.

MEASURED 2026-09-08, right after the IAM apply: this path is not hypothetical, it
is the ONLY path, because the operator's address is not yet a verified SES
identity and every send therefore fails. The first person to press the button
would have got 502, and the second press 202 with "An operator was already
emailed about this address" -- which would have been false.

So the send is wrapped and the marker deleted on failure. `s3:DeleteObject` is
granted on `ddpsrun-register/*` and nowhere else, verified with
`simulate-principal-policy` after the apply: the same action on the results
prefix and on the bucket root is implicitDeny.

It does NOT bound an attacker holding many Google accounts. Two things do: SES's
own quota, which is 200 messages a day and 1 a second while the account is in the
sandbox (measured 2026-09-08, `ProductionAccessEnabled: False`), and the fact that
in the sandbox SES REFUSES to send to any address that has not been verified -- so
the only inbox this can ever reach is the operator's own.

WHAT IT COSTS. SES bills $0.10 per 1,000 messages. Twenty lab members registering
once each is 20 messages, $0.002. The sandbox ceiling of 200 a day, hit every day
for a month, is 6,000 messages and $0.60. The S3 marker is one PutObject
($0.005 per 1,000 requests, so $0.0001 for those twenty) holding zero bytes; S3
charges for storage by the byte and these have no body.

Grep anchor: DDPSRUN-REGISTER
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("ddpsrun.notify")

# Where the marker objects go. A prefix of its own, NOT under the results prefix,
# because the IAM policy that lets this Lambda write is scoped to this path and
# must not reach a job's output: `terraform/lambda/main.tf`'s register_write
# statement names `<bucket>/ddpsrun-register/*` and nothing else.
MARKER_PREFIX = "ddpsrun-register/"

# The subject line. Fixed so the operator can filter on it.
SUBJECT = "[ddpsrun] registration request"


class NotifyError(RuntimeError):
    """Sending failed. Carries a sentence written for the person who pressed the
    button, because that person is looking at the screen and cannot see a log."""


def s3_client():
    """A boto3 S3 client, imported late.

    WHY THE IMPORT IS INSIDE THE FUNCTION. `boto3` is on the Lambda runtime but is
    not a dependency of the test suite, and a module-level import would make every
    test in this repo need it. The same reason `registry.ecr_client` does it.

    Returns:
        The client. Raises `NotifyError` when boto3 is absent, which is what a
        local run without it looks like.
    """
    try:
        import boto3
    except ImportError as exc:                                    # pragma: no cover
        raise NotifyError(
            "this deployment cannot record registration requests: the boto3 "
            "library is missing from the server."
        ) from exc
    return boto3.client("s3")


def ses_client(region: str = ""):
    """A boto3 SES v2 client, imported late for the same reason as above.

    Args:
        region: which region's SES to use. Empty lets boto3 take it from the
            environment, which in Lambda is the function's own region -- and the
            verified identity lives there, so guessing another one would fail on
            an unverified sender rather than on a missing setting.

    Returns:
        The client.
    """
    try:
        import boto3
    except ImportError as exc:                                    # pragma: no cover
        raise NotifyError(
            "this deployment cannot send email: the boto3 library is missing "
            "from the server."
        ) from exc
    return boto3.client("sesv2", region_name=region) if region else boto3.client("sesv2")


def marker_key(email: str) -> str:
    """Where the marker for one address lives.

    Args:
        email: the verified address, any case.

    Returns:
        The S3 key. The address is lowercased so `A@x` and `a@x` share one marker,
        matching `auth.principal_for_email`, which lowercases before looking up.
        No hashing: this bucket is already the operator's own and a readable key
        is what makes `aws s3 ls` a useful way to see who has asked.
    """
    return f"{MARKER_PREFIX}{email.strip().lower()}"


def already_asked(bucket: str, email: str, client=None) -> bool:
    """Record that this address has asked, and say whether it already had.

    THE WHOLE POINT IS THAT THIS IS ONE ROUND TRIP AND NOT TWO. A read-then-write
    would let two requests from the same person a second apart both see "no
    marker" and both send. `If-None-Match: *` makes S3 itself decide: the write
    succeeds for exactly one of them and the other gets 412.

    Args:
        bucket: the results bucket, which is the only bucket this service knows.
        email: the verified address.
        client: an S3 client, for tests. Built when omitted.

    Returns:
        False when this is the first ask, True when a marker was already there.

    Raises:
        NotifyError: S3 refused for any reason other than the precondition, which
            is a real failure and must not be reported to the caller as "already
            sent" -- that would silently drop their request.
    """
    client = client or s3_client()
    try:
        client.put_object(Bucket=bucket, Key=marker_key(email), Body=b"",
                          IfNoneMatch="*")
        return False
    except Exception as exc:
        # botocore raises ClientError with the wire error in `.response`. Reading
        # it by attribute rather than importing botocore keeps this module
        # importable without boto3, which is how the tests run.
        response = getattr(exc, "response", None)
        name = ""
        if isinstance(response, dict):
            error = response.get("Error")
            if isinstance(error, dict):
                name = str(error.get("Code", ""))
        # TWO CODES, NOT ONE. `PreconditionFailed` is the plain answer; S3 returns
        # `ConditionalRequestConflict` (409) instead when two conditional writes
        # to the same key race, and both mean "somebody got there first", which
        # for this purpose is the same answer.
        if name in ("PreconditionFailed", "ConditionalRequestConflict"):
            return True
        raise NotifyError(
            f"could not record the request: {exc}. Nothing was emailed, so try "
            f"again or ask an operator directly."
        ) from exc


def release_marker(bucket: str, email: str, client=None) -> None:
    """Undo `already_asked`, so a failed send can be retried.

    WHY THIS IS BEST-EFFORT AND SWALLOWS ITS OWN ERRORS. It runs while an
    exception is already on its way to the caller -- the send failed and they are
    about to get a 502 naming the real cause. Raising a second error here would
    replace that message with a less useful one about S3, and the state it leaves
    behind (a marker for a send that did not happen) is exactly what the 502
    already tells them to escalate. So it logs and returns.

    Args:
        bucket: the results bucket.
        email: the verified address whose marker to remove.
        client: an S3 client, for tests. Built when omitted.
    """
    try:
        client = client or s3_client()
        client.delete_object(Bucket=bucket, Key=marker_key(email))
    except Exception as exc:                                      # noqa: BLE001
        logger.warning("could not release the registration marker for %s: %s. "
                       "A retry will report 'already emailed' until it is "
                       "deleted by hand.", email, exc)
def registration_body(email: str, subject_id: str, namespace_hint: str) -> str:
    """The email's text: what happened, and the two steps that answer it.

    WHY THE SNIPPET IS IN THE MAIL. The operator's job is to add one object to the
    token file in Secrets Manager. Describing that in prose means they have to
    remember the field names, and a token file that will not parse takes the whole
    service down at the next cold start (`auth.parse_token_document` raises). So
    the mail carries the object itself, ready to paste.

    Args:
        email: the address that asked.
        subject_id: Cognito's `sub` for them. Included because an address can be
            changed and this cannot, so it is the durable identifier if the
            mapping ever has to be reconstructed.
        namespace_hint: a namespace name derived from the address, offered as a
            suggestion only -- the namespace has to EXIST in the cluster before
            it works, and only the operator can see whether it does
            (`docs/16-login.md` 16.2).

    Returns:
        The plain-text body.
    """
    entry = json.dumps({"email": email, "user": email, "namespace": namespace_hint,
                        "team": "lab"}, indent=2)
    return f"""{email} signed in with Google and is not registered with ddpsrun.

Cognito verified them, so the sign-in itself worked. Every route still answers 403
because nobody has given this address a namespace, and only an operator can.

  address    {email}
  cognito id {subject_id}

TO REGISTER THEM, two steps in this order. The namespace has to exist first: an
entry naming a namespace that is not in the cluster produces a token that
authenticates and then fails on every read.

  1. create the namespace, if it is not there already

     kubectl create namespace {namespace_hint}

  2. add this object to the `tokens` array in the token secret

{entry}

     The secret is the one named in terraform/lambda/outputs.tf (tokens_secret).
     Read it, add the object, put it back:

     aws secretsmanager get-secret-value --secret-id <tokens_secret> \\
       --query SecretString --output text > /tmp/tokens.json
     # edit /tmp/tokens.json, then
     aws secretsmanager put-secret-value --secret-id <tokens_secret> \\
       --secret-string file:///tmp/tokens.json

     The server reads the secret at cold start, so the change lands on the next
     one. `team` decides whose spend figures they can see; `namespace` decides
     which jobs are theirs. There is no `sha256` field because this person signs
     in through Google and needs no static token.

TO REFUSE THEM, do nothing. They stay at 403 and see the same screen.

This address can only ever generate ONE of these emails -- a marker object under
s3://<results bucket>/{MARKER_PREFIX} makes the second request send nothing. Delete
that object if you want the reminder again.
"""


def namespace_suggestion(email: str) -> str:
    """A namespace name derived from an address, for the operator to accept or ignore.

    Args:
        email: the verified address.

    Returns:
        `lab-<local part>`, lowercased, with everything a Kubernetes name cannot
        hold replaced by a hyphen. Kubernetes namespaces are RFC 1123 labels:
        lowercase letters, digits and hyphens, starting and ending with one of the
        first two. A dotted address like `bo.ram@x.com` would otherwise produce
        `lab-bo.ram`, which `kubectl create namespace` refuses.
    """
    local = (email or "").strip().lower().split("@")[0]
    cleaned = "".join(c if c.isalnum() else "-" for c in local).strip("-")
    return f"lab-{cleaned}" if cleaned else "lab-unnamed"


def send_registration_request(*, email: str, subject_id: str, notify_to: str,
                              notify_from: str, region: str = "", client=None) -> None:
    """Send the operator one email about one person.

    Args:
        email: the verified address that asked.
        subject_id: Cognito's `sub` for them.
        notify_to: the operator's address. In the SES sandbox this MUST be a
            verified identity or SES refuses the call, which is the reason the
            blast radius of this endpoint is exactly one inbox.
        notify_from: the From address. Also has to be verified. Setting it equal
            to `notify_to` is the cheapest working configuration: one verification
            click covers both.
        region: SES region, or empty for the function's own.
        client: an SES client, for tests.

    Raises:
        NotifyError: SES refused. The message names the address, because the
            overwhelmingly likely cause is that it was never verified.
    """
    client = client or ses_client(region)
    body = registration_body(email, subject_id, namespace_suggestion(email))
    try:
        client.send_email(
            FromEmailAddress=notify_from,
            Destination={"ToAddresses": [notify_to]},
            Content={"Simple": {
                "Subject": {"Data": f"{SUBJECT}: {email}"},
                "Body": {"Text": {"Data": body}},
            }},
        )
    except Exception as exc:
        raise NotifyError(
            f"the request could not be emailed to the operator: {exc}. While this "
            f"account is in the SES sandbox both {notify_from} and {notify_to} "
            f"have to be verified identities."
        ) from exc
    logger.info("registration request emailed for %s", email)
