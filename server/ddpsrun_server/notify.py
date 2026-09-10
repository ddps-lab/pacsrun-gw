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
SUBJECT = "[hyperun] registration request"

# The team used in the example commands when the token file names none. Not a
# policy -- just a value that makes the emailed command runnable instead of one
# beginning with a hyphen. The operator changes it.
DEFAULT_TEAM = "ddps"


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
def registration_body(email: str, subject_id: str, namespace_hint: str,
                      known_teams: list[str] | None = None) -> str:
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
        namespace_hint: the name `namespace_suggestion` answered for this
            address and the team below. A suggestion only: the namespace has to
            EXIST in the cluster before it works, and only the operator can see
            whether it does (`docs/16-login.md` 16.2).
        known_teams: the teams already in the token file, commonest first
            (`auth.TokenStore.teams`). ★ THE MAIL ASKS FOR THE TEAM because the
            server cannot know it -- which team somebody belongs to is a fact
            about the lab, not about the sign-in. Listing what exists is the
            difference between a question the operator can answer in a second
            and one they have to go and look up.

    Returns:
        The plain-text body.
    """
    teams = [t for t in (known_teams or []) if t]
    team = teams[0] if teams else DEFAULT_TEAM
    entry = json.dumps({"email": email, "user": user_suggestion(email),
                        "namespace": namespace_hint, "team": team}, indent=2)
    if teams:
        roster = ("Teams already in the token file, commonest first: "
                  + ", ".join(teams) + ".")
    else:
        roster = ("No team is named in the token file yet, so `" + DEFAULT_TEAM
                  + "` is used below. Pick the name you want; it only has to be "
                  + "consistent with itself.")
    return f"""{email} signed in with Google and is not registered with ddpsrun.

Cognito verified them, so the sign-in itself worked. Every route still answers 403
because nobody has given this address a namespace, and only an operator can.

  address    {email}
  cognito id {subject_id}

WHY A NAMESPACE OF THEIR OWN, rather than adding them to an existing one. The
namespace is what separates people here, and it separates more than the job list:
`spec.resultPath` is s3://<bucket>/<prefix><NAMESPACE>/<job>/, so two people in
one namespace write their results into one prefix and each can read the other's.
Sharing a namespace is a deliberate choice for a team that wants it, not the
default for a new person.

★ DECIDE THE TEAM FIRST. Everything below is written for team `{team}`, because
the namespace is `<team>-<the local part of their address>` and the commands need
a concrete name to be runnable. {roster}

If this person is on a different team, change it in ALL FOUR steps and in the
`team` field of the entry -- the namespace name and the `team` field have to
agree, and nothing checks that they do. The two are decided together at this
moment and at no other, so this is the moment to get them right.

TO REGISTER THEM, FOUR STEPS IN THIS ORDER. Steps 1-3 are all needed before the
first job can rent a machine; step 4 is what lets them in.

  1. create the namespace

     kubectl create namespace {namespace_hint}

  2. create the workload ServiceAccount IN that namespace

     kubectl -n {namespace_hint} create serviceaccount pacsjob-writer

  3. ★ let that ServiceAccount assume the workload role. THIS STEP IS THE ONE
     THAT IS EASY TO MISS AND THE FAILURE IS LATE: without it the job is
     accepted, solves cleanly, and the driver pod dies in its own configuration
     check with

       exit 10  configuration error: ... Not authorized to perform
                sts:AssumeRoleWithWebIdentity

     The role's trust policy names no namespace at all -- what binds the pair is
     this association, one per namespace/ServiceAccount:

     aws eks create-pod-identity-association --cluster-name <cluster> \\
       --namespace {namespace_hint} --service-account pacsjob-writer \\
       --role-arn <the pacsrun-workload role arn>

     `aws eks list-pod-identity-associations --cluster-name <cluster>` shows what
     already exists. The gateway itself needs nothing: its own permissions come
     from a ClusterRoleBinding that already covers every namespace.

  4. add this object to the `tokens` array in the token secret

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


def _label(raw: str) -> str:
    """Squash a string into an RFC 1123 label, or "" when nothing survives.

    Kubernetes namespaces are RFC 1123 labels: lowercase letters, digits and
    hyphens, starting and ending with one of the first two. An address may hold
    dots, plus signs and capitals and `kubectl create namespace` refuses all
    three, so `bo.ram+x@x.com` has to become `bo-ram-x` before it can reach a
    command somebody pastes.
    """
    cleaned = "".join(c if c.isalnum() else "-" for c in (raw or "").strip().lower())
    return cleaned.strip("-")


def namespace_suggestion(email: str, team: str = DEFAULT_TEAM) -> str:
    """The namespace a new person gets: their team, then their address.

    ★ THE RULE, and it took the team on 2026-09-08. It was `lab-<local part>`,
    with `lab-` a fixed prefix that meant nothing. A prefix that carries the TEAM
    says something true about the person on a shared cluster, and `team` is being
    decided at that same moment anyway -- both fields are written by the operator
    when somebody is added, and neither is decided at any other time.

    THE TEAM IS ASKED FOR, NOT GUESSED. This function cannot know it; the caller
    supplies it, and `registration_body` lists the teams already in the token
    file so the operator confirms one rather than inventing it.

    AND IT IS NEVER READ BACK OUT. Nothing splits a namespace on a dash to
    recover the team -- that breaks the moment a team is called "ddps-lab", and
    guessing wrong puts a person's figures in another team's total. `team` stays
    its own field. See `auth.TokenStore`'s docstring.

    Args:
        email: the verified address the person signs in with.
        team: the team they belong to. Defaults to `DEFAULT_TEAM` so a caller
            with nothing to say still gets a runnable name rather than one
            starting with a hyphen.

    Returns:
        `<team>-<local part>`, both squashed to RFC 1123 labels. Truncated to 63
        characters, which is the label limit -- a long address would otherwise
        produce a name `kubectl create namespace` refuses.

    Example:
        >>> namespace_suggestion("alice@example.ac.kr", "ddps")
        'ddps-alice'
    """
    return f"{_label(team) or DEFAULT_TEAM}-{user_suggestion(email)}"[:63].strip("-")


def user_suggestion(email: str) -> str:
    """The `user` field a new person gets: the local part of their address.

    ★ IT USED TO SUGGEST THE WHOLE ADDRESS, and that is what this fixes. The
    registration mail printed `{"user": "<the full email>"}` for the operator to
    paste, and `user` is written into the `ddpsrun.io/owner` LABEL, where an `@`
    is not a legal character. `naming.label_value` scrubs it rather than failing,
    so nothing crashed -- the job screen simply said "Submitted by
    hyundo-gmail.com". A mangled address is not a person's name.
    
    AND IT IS THE SAME RULE THE NAMESPACE USES, deliberately. Two names were
    being decided at the same moment by different rules: the namespace came from
    the local part and `user` came from the whole address, so they drifted the
    moment either was typed by hand. This deployment has one account whose
    `user` is a short handle and whose namespace is derived from the address, and
    nothing relates the two strings. One rule means `newcomer@example.com` gets
    namespace `ddps-newcomer` and user `newcomer`, and a reader can see they are
    the same person.

    A SUGGESTION, NOT A CONSTRAINT. The operator still writes the entry, and
    `user` may be anything -- it is a display name and not a security boundary
    (`auth.Principal`). What changed is what the mail proposes.

    Args:
        email: the verified address the person signs in with.

    Returns:
        The local part squashed to an RFC 1123 label, or "unnamed" when nothing
        survives that (an address of only punctuation). Never empty, because an
        empty `user` would drop the owner label and make the job look like one
        applied with kubectl.

    Example:
        >>> user_suggestion("newcomer@example.com")
        'newcomer'
    """
    return _label((email or "").split("@")[0]) or "unnamed"


def send_registration_request(*, email: str, subject_id: str, notify_to: str,
                              notify_from: str, region: str = "", client=None,
                              known_teams: list[str] | None = None) -> None:
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
        known_teams: the teams already in the token file, for the mail to offer.

    Raises:
        NotifyError: SES refused. The message names the address, because the
            overwhelmingly likely cause is that it was never verified.
    """
    client = client or ses_client(region)
    teams = [t for t in (known_teams or []) if t]
    team = teams[0] if teams else DEFAULT_TEAM
    body = registration_body(email, subject_id,
                             namespace_suggestion(email, team), teams)
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
