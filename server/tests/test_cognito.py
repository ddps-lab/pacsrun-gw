"""Does the Cognito branch actually verify, and actually refuse?

Every test here mints a REAL RS256 JWT with a key pair generated in the test and
serves a REAL JWKS document for it, so the code under test does the same work it
does in production: fetch a key by `kid`, verify a signature, check six claims.
Nothing is monkeypatched inside `cognito.py` itself.

The refusal tests matter more than the acceptance test. A verifier that accepts
a good token but also accepts a token from another pool is worse than no
verifier, because it looks like it is working.

Grep anchor: DDPSRUN-COGNITO-TESTS
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from ddpsrun_server import auth, cognito

POOL = "us-west-2_TestPool"
REGION = "us-west-2"
CLIENT = "1example23client45id"
ISSUER = f"https://cognito-idp.{REGION}.amazonaws.com/{POOL}"
KID = "test-key-1"


@pytest.fixture(scope="module")
def keypair():
    """One RSA key pair for the whole module. Generating it is slow."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(keypair, monkeypatch):
    """A Verifier whose JWKS document is served from the test's own key.

    Only the HTTP fetch is replaced, and it is replaced with a real JWKS
    document. The signature check, the claim checks and the `kid` lookup are the
    production ones.
    """
    numbers = keypair.public_key().public_numbers()

    def to_b64(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        import base64

        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    document = json.dumps({
        "keys": [{
            "kty": "RSA", "kid": KID, "use": "sig", "alg": "RS256",
            "n": to_b64(numbers.n), "e": to_b64(numbers.e),
        }]
    }).encode()

    class FakeResponse:
        def read(self):
            return document

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "jwt.jwks_client.urllib.request.urlopen", lambda *a, **k: FakeResponse()
    )
    return cognito.Verifier(pool_id=POOL, region=REGION, client_id=CLIENT)


def mint(keypair, **overrides) -> str:
    """Build an id_token the way Cognito would.

    Args:
        keypair: signs it.
        overrides: replace or remove any claim. Passing None removes it.

    Returns:
        The encoded JWT.
    """
    now = int(time.time())
    payload = {
        "sub": "9f1c-uuid", "iss": ISSUER, "aud": CLIENT, "token_use": "id",
        "email": "Alice@Example.com", "email_verified": True,
        "iat": now, "exp": now + 3600,
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, keypair, algorithm="RS256", headers={"kid": KID})


# --------------------------------------------------------------- 받아들인다


def test_a_good_id_token_yields_the_email_lowercased(verifier, keypair):
    """Lowercased because the token file is keyed that way and Cognito treats
    addresses case-insensitively; "Alice@Example.com" must find "alice@..."."""
    identity = verifier.claims(mint(keypair))
    assert identity.email == "alice@example.com"
    assert identity.subject == "9f1c-uuid"


# ------------------------------------------------------------------ 거절한다


def test_a_token_from_another_pool_is_refused(verifier, keypair):
    """The attack this stops: anyone can create their own Cognito pool for free,
    mint themselves a token with any email in it, and present it here."""
    other = f"https://cognito-idp.{REGION}.amazonaws.com/us-west-2_SomeoneElse"
    with pytest.raises(cognito.TokenError, match="different user pool"):
        verifier.claims(mint(keypair, iss=other))


def test_a_token_for_another_client_of_the_same_pool_is_refused(verifier, keypair):
    with pytest.raises(cognito.TokenError, match="different application"):
        verifier.claims(mint(keypair, aud="some-other-client"))


def test_an_access_token_is_refused_even_though_it_verifies(verifier, keypair):
    """Same key, same issuer, same audience — and no verified email in it. This
    is why `token_use` is checked and not assumed."""
    with pytest.raises(cognito.TokenError, match="not an id_token"):
        verifier.claims(mint(keypair, token_use="access"))


def test_an_expired_token_is_refused(verifier, keypair):
    past = int(time.time()) - 10
    with pytest.raises(cognito.TokenError, match="expired"):
        verifier.claims(mint(keypair, exp=past, iat=past - 3600))


def test_an_unverified_email_is_refused(verifier, keypair):
    """The email IS the key the namespace is looked up by, so an unverified one
    would let anyone who can make a pool user claim someone else's namespace."""
    with pytest.raises(cognito.TokenError, match="not been verified"):
        verifier.claims(mint(keypair, email_verified=False))


def test_a_token_with_no_email_is_refused(verifier, keypair):
    with pytest.raises(cognito.TokenError, match="no email"):
        verifier.claims(mint(keypair, email=None))


def test_a_token_signed_by_a_different_key_is_refused(verifier):
    """The signature check itself. A token that is correct in every claim and
    signed by the wrong key must not pass."""
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode(
        {"sub": "x", "iss": ISSUER, "aud": CLIENT, "token_use": "id",
         "email": "alice@example.com", "email_verified": True,
         "iat": now, "exp": now + 3600},
        other, algorithm="RS256", headers={"kid": KID},
    )
    with pytest.raises(cognito.TokenError):
        verifier.claims(forged)


def test_garbage_is_refused_without_raising_something_else(verifier):
    for junk in ["", "not.a.jwt", "ddpsrun-abc123", "a.b.c"]:
        with pytest.raises(cognito.TokenError):
            verifier.claims(junk)


# ----------------------------------------------------- 어느 갈래로 갈지 고르기


def test_the_shape_test_tells_the_two_credentials_apart(keypair):
    """Not a security check. It only picks a branch; both branches then verify."""
    assert cognito.looks_like_a_jwt(mint(keypair)) is True
    assert cognito.looks_like_a_jwt("ddpsrun-a1b2c3d4") is False
    assert cognito.looks_like_a_jwt("") is False
    assert cognito.looks_like_a_jwt("a.b.c") is False


# ------------------------------------------------- email -> namespace 로 잇기


def store() -> auth.TokenStore:
    return auth.TokenStore.from_document({"tokens": [
        {"sha256": auth.hash_token("alice-token"), "user": "alice",
         "namespace": "lab-alice", "team": "lab", "email": "alice@example.com"},
        {"user": "screen-only", "namespace": "lab-screen", "team": "lab",
         "email": "screen@example.com"},
    ]})


def test_a_registered_email_reaches_the_right_namespace():
    assert store().principal_for_email("alice@example.com").namespace == "lab-alice"


def test_the_lookup_is_case_insensitive():
    assert store().principal_for_email("ALICE@Example.COM").namespace == "lab-alice"


def test_a_person_may_exist_with_no_static_token_at_all():
    """Somebody who only ever uses the screen has no token to hash."""
    assert store().principal_for_email("screen@example.com").user == "screen-only"


def test_an_unregistered_email_says_what_has_to_happen():
    with pytest.raises(auth.UnknownUser, match="operator"):
        store().principal_for_email("stranger@example.com")


def test_an_entry_with_neither_key_is_refused_at_load():
    """It could never match anything, so it is a mistake, not a valid entry."""
    with pytest.raises(auth.TokenFileError, match="neither sha256 nor email"):
        auth.TokenStore.from_document(
            {"tokens": [{"user": "ghost", "namespace": "lab-ghost"}]}
        )


def test_the_same_email_twice_is_refused_at_load():
    with pytest.raises(auth.TokenFileError, match="email appears twice"):
        auth.TokenStore.from_document({"tokens": [
            {"user": "a", "namespace": "n1", "email": "same@example.com"},
            {"user": "b", "namespace": "n2", "email": "same@example.com"},
        ]})


# ------------------------------------------------ 실제 route 를 통과시켜 본다


@pytest.fixture
def app_client(tmp_path, monkeypatch, verifier):
    """A TestClient whose app has BOTH credential kinds wired up.

    The real lifespan runs, so the Verifier is built the way production builds
    it. Only two things are replaced: the cluster (a stub, since no route here
    touches it for real) and the JWKS fetch (by the `verifier` fixture, and with
    a real JWKS document).
    """
    from fastapi.testclient import TestClient

    from ddpsrun_server import main

    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"tokens": [
        {"sha256": auth.hash_token("static-token"), "user": "alice",
         "namespace": "lab-alice", "team": "lab", "email": "alice@example.com"},
    ]}))

    class StubCluster:
        def list_jobs(self, namespace):
            return []

    monkeypatch.setenv("DDPSRUN_RESULT_BUCKET", "<RESULT_BUCKET>")
    monkeypatch.setenv("DDPSRUN_TOKENS_PATH", str(tokens))
    monkeypatch.setenv("DDPSRUN_COGNITO_POOL_ID", POOL)
    monkeypatch.setenv("DDPSRUN_COGNITO_CLIENT_ID", CLIENT)
    monkeypatch.setenv("DDPSRUN_COGNITO_REGION", REGION)
    monkeypatch.setenv("DDPSRUN_COGNITO_LOGIN_DOMAIN", "https://login.example.com")
    monkeypatch.setattr(main.Cluster, "connect", staticmethod(StubCluster))

    with TestClient(main.app) as test_client:
        yield test_client


def get(client, path, credential=None):
    headers = {"Authorization": f"Bearer {credential}"} if credential else {}
    return client.request("GET", path, headers=headers)


def test_a_cognito_token_gets_through_the_real_dependency(app_client, keypair):
    assert get(app_client, "/v1/jobs", mint(keypair)).status_code == 200


def test_a_static_token_still_gets_through(app_client):
    """The point of DDPSRUN-TWO-CREDENTIALS: CI and the agent skill keep working."""
    assert get(app_client, "/v1/jobs", "static-token").status_code == 200


def test_a_bad_jwt_is_401_and_a_stranger_is_403(app_client, keypair):
    """The two must not be the same code. 401 tells someone to fix their login;
    403 tells them to ask an operator. Only one of those is their problem."""
    other = f"https://cognito-idp.{REGION}.amazonaws.com/us-west-2_Nope"
    assert get(app_client, "/v1/jobs", mint(keypair, iss=other)).status_code == 401

    stranger = get(app_client, "/v1/jobs", mint(keypair, email="nobody@example.com"))
    assert stranger.status_code == 403
    assert "operator" in stranger.json()["detail"]


def test_login_config_needs_no_token(app_client):
    """It is what a caller reads BEFORE they have one."""
    body = get(app_client, "/v1/login-config").json()
    assert body["enabled"] is True
    assert body["client_id"] == CLIENT
    assert body["login_domain"] == "https://login.example.com"


def test_login_config_carries_no_secret(app_client):
    """Everything in it appears in a login URL the browser already shows."""
    text = json.dumps(get(app_client, "/v1/login-config").json())
    for forbidden in ["secret", "password", "private"]:
        assert forbidden not in text.lower()


# ------------------------------------- DDPSRUN-REGISTER: the first-time visitor


@pytest.fixture
def register_client(tmp_path, monkeypatch, verifier):
    """Like `app_client`, plus a notification address and a fake S3/SES pair.

    NOTHING IS SENT. `notify.s3_client` and `notify.ses_client` are replaced by
    recorders, so these tests assert on the CALLS -- which is the only part this
    service owns. Whether SES then accepts the message depends on a verified
    identity, and that is checked against the real account, not here.
    """
    from fastapi.testclient import TestClient

    from ddpsrun_server import main, notify

    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"tokens": [
        {"sha256": auth.hash_token("static-token"), "user": "alice",
         "namespace": "lab-alice", "team": "lab", "email": "alice@example.com"},
    ]}))

    class FakeS3:
        def __init__(self):
            self.puts = []
            self.deletes = []
            self.existing = set()

        def put_object(self, Bucket, Key, Body, IfNoneMatch=None):
            self.puts.append((Bucket, Key, IfNoneMatch))
            if Key in self.existing:
                error = Exception("exists")
                error.response = {"Error": {"Code": "PreconditionFailed"}}
                raise error
            self.existing.add(Key)

        def delete_object(self, Bucket, Key):
            self.deletes.append((Bucket, Key))
            self.existing.discard(Key)

    class FakeSES:
        def __init__(self):
            self.sent = []
            self.refuse = ""          # set to a message to make every send fail

        def send_email(self, **kwargs):
            if self.refuse:
                raise Exception(self.refuse)
            self.sent.append(kwargs)

    s3, ses = FakeS3(), FakeSES()

    class StubCluster:
        def list_jobs(self, namespace):
            return []

    monkeypatch.setenv("DDPSRUN_RESULT_BUCKET", "<RESULT_BUCKET>")
    monkeypatch.setenv("DDPSRUN_TOKENS_PATH", str(tokens))
    monkeypatch.setenv("DDPSRUN_COGNITO_POOL_ID", POOL)
    monkeypatch.setenv("DDPSRUN_COGNITO_CLIENT_ID", CLIENT)
    monkeypatch.setenv("DDPSRUN_COGNITO_REGION", REGION)
    monkeypatch.setenv("DDPSRUN_REGISTER_NOTIFY_TO", "operator@example.ac.kr")
    monkeypatch.setattr(main.Cluster, "connect", staticmethod(StubCluster))
    monkeypatch.setattr(notify, "s3_client", lambda: s3)
    monkeypatch.setattr(notify, "ses_client", lambda region="": ses)

    with TestClient(main.app) as test_client:
        yield test_client, s3, ses


def post_register(client, credential=None):
    headers = {"Authorization": f"Bearer {credential}"} if credential else {}
    return client.request("POST", "/v1/register-request", headers=headers)


def test_an_unregistered_signed_in_person_can_ask_and_the_operator_is_emailed(
        register_client, keypair):
    """★ THE STATE THIS EXISTS FOR. Cognito verified them, so the sign-in worked;
    the token file does not name them, so every other route answers 403. Before
    2026-09-08 that was a dead end with nothing to press."""
    client, s3, ses = register_client
    token = mint(keypair, email="newcomer@example.ac.kr")

    # Every other route refuses them, and with 403 rather than 401.
    assert get(client, "/v1/jobs", token).status_code == 403

    answer = post_register(client, token)
    assert answer.status_code == 202          # queued for a human, not granted
    assert answer.json()["emailed"] is True
    assert len(ses.sent) == 1
    assert ses.sent[0]["Destination"]["ToAddresses"] == ["operator@example.ac.kr"]
    # From defaults to To: in the SES sandbox both ends must be verified, and
    # equal addresses mean one verification click instead of two.
    assert ses.sent[0]["FromEmailAddress"] == "operator@example.ac.kr"


def test_the_email_carries_the_object_the_operator_has_to_paste(register_client, keypair):
    """A token file that will not parse takes the service down at the next cold
    start (`auth.parse_token_document` raises), so the mail carries the entry
    itself rather than a description of it."""
    client, _s3, ses = register_client
    post_register(client, mint(keypair, email="a.newcomer@example.ac.kr"))
    body = ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert '"email": "a.newcomer@example.ac.kr"' in body
    # The namespace suggestion has to be a name kubectl will accept: a dot in
    # the address would otherwise produce `lab-bo.ram`, which it refuses.
    assert '"namespace": "lab-a-newcomer"' in body
    assert "kubectl create namespace lab-a-newcomer" in body
    assert "9f1c-uuid" in body                      # cognito's sub, for the record


def test_asking_twice_emails_once(register_client, keypair):
    """This endpoint is on a public URL that any Google account can reach, so a
    reload must not mail the operator again. S3 decides, with If-None-Match."""
    client, s3, ses = register_client
    token = mint(keypair, email="newcomer@example.ac.kr")

    first = post_register(client, token)
    second = post_register(client, token)

    assert first.json()["emailed"] is True
    assert second.status_code == 202 and second.json()["emailed"] is False
    assert len(ses.sent) == 1
    assert [p[2] for p in s3.puts] == ["*", "*"]
    assert s3.puts[0][1] == "ddpsrun-register/newcomer@example.ac.kr"


def test_a_person_who_is_already_registered_is_told_so_instead(register_client, keypair):
    """409, not an email. Pressing the button when you need nothing means the
    screen is stale, and telling the person that is more use than mailing an
    operator about somebody who is already in the file."""
    client, _s3, ses = register_client
    answer = post_register(client, mint(keypair, email="alice@example.com"))
    assert answer.status_code == 409
    assert "already registered" in answer.json()["detail"]
    assert ses.sent == []


def test_a_static_token_is_refused_here(register_client):
    """Anybody holding one is registered by definition and has no use for this
    route. Accepting it would only widen what the endpoint accepts."""
    client, _s3, ses = register_client
    answer = post_register(client, "static-token")
    assert answer.status_code == 401
    assert "id_token" in answer.json()["detail"]
    assert ses.sent == []


def test_an_invalid_token_gets_nowhere(register_client):
    """The endpoint skips the token FILE, not the token CHECK. A JWT signed by a
    key the pool's JWKS does not carry still fails, exactly as it does on every
    other route -- built the same way as
    `test_a_token_signed_by_a_different_key_is_refused` above."""
    client, _s3, ses = register_client
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({"sub": "x", "iss": ISSUER, "aud": CLIENT, "token_use": "id",
                         "email": "attacker@example.com", "email_verified": True,
                         "exp": int(time.time()) + 3600},
                        stranger, algorithm="RS256", headers={"kid": KID})
    assert post_register(client, forged).status_code == 401
    assert post_register(client, None).status_code == 401
    assert ses.sent == []


def test_the_screen_is_told_whether_the_button_will_work(register_client):
    """The screen draws its button from `registration_requests` and not from
    `enabled`. A deployment with Cognito and no notification address would
    otherwise offer a button that answers 503, and a first-time visitor cannot
    tell a broken service from a closed one."""
    client, _s3, _ses = register_client
    assert client.get("/v1/login-config").json()["registration_requests"] is True


def test_no_notification_address_means_the_flag_is_off():
    """The other half of the flag, asked of the settings rather than of a second
    app: two TestClients in one test share this process's environment, so the
    second one would read the first's DDPSRUN_REGISTER_NOTIFY_TO."""
    from ddpsrun_server.config import Settings
    off = Settings.from_env({"DDPSRUN_RESULT_BUCKET": "b", "DDPSRUN_TOKENS_PATH": "t"})
    assert off.register_notify_to == ""
    assert bool(off.register_notify_to) is False


def test_the_operators_address_is_not_handed_out(register_client):
    """Anyone with a Google account can read /v1/login-config. The button works
    without knowing where the mail goes, so the address is not in the answer."""
    client, _s3, _ses = register_client
    assert "operator@example.ac.kr" not in client.get("/v1/login-config").text


def test_a_failed_send_gives_the_claim_back_so_the_person_can_retry(register_client, keypair):
    """★ THE ONLY REACHABLE PATH ON 2026-09-08, and it was broken.

    The marker is claimed BEFORE the send, because that is what stops a reload
    mailing the operator twice while the first request is still in flight. But a
    marker that outlives a send which never happened is worse than no marker: the
    address is locked out AND the next press is told an operator was already
    emailed.

    This was not hypothetical when it was found. The IAM apply had just landed and
    the operator's address was not a verified SES identity, so EVERY send failed --
    the first person to press the button would have got 502 and the second a
    cheerful 202 about an email that was never sent.
    """
    client, s3, ses = register_client
    ses.refuse = "Email address not verified"
    token = mint(keypair, email="newcomer@example.ac.kr")

    first = post_register(client, token)
    assert first.status_code == 502
    assert "verified" in first.json()["detail"]
    # The claim was taken and then given back, so nothing is left behind.
    assert s3.deletes == [("<RESULT_BUCKET>", "ddpsrun-register/newcomer@example.ac.kr")]
    assert s3.existing == set()

    # And the retry is a real retry: it sends, rather than reporting a success
    # that never happened.
    ses.refuse = ""
    second = post_register(client, token)
    assert second.status_code == 202
    assert second.json()["emailed"] is True
    assert len(ses.sent) == 1


def test_a_delete_that_also_fails_does_not_replace_the_useful_error(register_client, keypair, caplog):
    """`release_marker` runs while a 502 is already on its way to the caller. If S3
    refuses the delete too, the caller must still get the message about SES -- the
    real cause -- and not a second, less useful one about S3."""
    client, s3, ses = register_client
    ses.refuse = "Email address not verified"

    def refuse_delete(Bucket, Key):
        raise Exception("AccessDenied")
    s3.delete_object = refuse_delete

    answer = post_register(client, mint(keypair, email="newcomer@example.ac.kr"))
    assert answer.status_code == 502
    assert "verified" in answer.json()["detail"]
    assert "deleted by hand" in caplog.text


def test_the_registration_email_names_every_step_a_new_person_needs(register_client, keypair):
    """★ THE HOLE THIS CLOSES. The email said TWO steps -- create the namespace,
    add the token entry -- and a person registered that way could log in, could
    submit, and their driver pod died with

        exit 10  Not authorized to perform sts:AssumeRoleWithWebIdentity

    because nothing had let a ServiceAccount in the new namespace assume the
    workload role. That is the same failure as gw #6, rebuilt into the onboarding
    flow. The role's trust policy names no namespace at all; the binding is a
    per-namespace Pod Identity association, and it has to be created too."""
    client, _s3, ses = register_client
    post_register(client, mint(keypair, email="newcomer@example.ac.kr"))
    body = ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]

    assert "kubectl create namespace lab-newcomer" in body
    assert "create serviceaccount pacsjob-writer" in body
    assert "aws eks create-pod-identity-association" in body
    assert "sts:AssumeRoleWithWebIdentity" in body
    # And why a namespace of their own at all, since it decides the result prefix.
    assert "resultPath" in body


def test_the_emailed_commands_are_not_folded_onto_one_line(register_client, keypair):
    """A single backslash before a newline inside a non-raw f-string is a LINE
    CONTINUATION, so Python ate the newline and the association command arrived as
    one long line with the flags run together. Somebody pasting that gets a shell
    error, not a working command."""
    client, _s3, ses = register_client
    post_register(client, mint(keypair, email="newcomer@example.ac.kr"))
    body = ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]

    line = next(x for x in body.splitlines()
                if "aws eks create-pod-identity-association" in x)
    assert line.rstrip().endswith("\\"), line
    assert "--namespace" not in line, "the flags were folded onto the first line"


# ------------------------------- DDPSRUN-REGISTER: one namespace naming rule


def test_the_namespace_rule_is_the_team_then_the_address():
    """★ IT WAS WRITTEN DOWN THREE DIFFERENT WAYS and all three were in use on
    2026-09-08:

        auth.py's docstring   "<team>-<user>"                 -> ddps-alice
        the registration mail "lab-" + the address local part -> lab-alice
        a migration script    "lab-" + the `user` field       -> lab-alice-gmail

    The third produced `lab-alice` for an address with no "operator" in it,
    which is how the disagreement surfaced.

    ONE RULE: the TEAM, then the address's local part. The team because it is
    decided at the same moment and a prefix that carries it says something true
    on a shared cluster; the address rather than `user` because `user` is typed
    by an operator and can be anything, while the address is what the person
    presents at every sign-in.
    """
    from ddpsrun_server.notify import namespace_suggestion

    assert namespace_suggestion("alice@example.com", "ddps") == "ddps-alice"
    assert namespace_suggestion("bo.ram@example.ac.kr", "ddps") == "ddps-bo-ram"
    # A different team gives a different namespace for the same person.
    assert namespace_suggestion("alice@example.ac.kr", "vision") == "vision-alice"
    # And nothing of the `user` field can reach the name: it is not an argument.
    assert namespace_suggestion("alice@example.ac.kr", "ddps") == "ddps-alice"


def test_a_team_with_a_dash_in_it_still_produces_one_valid_name():
    """`auth.py` uses "ddps-lab" as the example of why a namespace must never be
    SPLIT to recover a team. Building the name in the safe direction has to keep
    working for such a team."""
    from ddpsrun_server.notify import namespace_suggestion

    assert namespace_suggestion("alice@example.ac.kr", "ddps-lab") == "ddps-lab-alice"


def test_the_suggested_namespace_is_always_a_name_kubectl_accepts():
    """Kubernetes namespaces are RFC 1123 labels: lowercase letters, digits and
    hyphens, starting and ending with one of the first two. An address may hold
    dots, plus signs and capitals, and `kubectl create namespace` refuses all
    three -- so the scrub is what makes the emailed command runnable."""
    import re

    from ddpsrun_server.notify import namespace_suggestion

    label = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
    cases = [("bo.ram+x@example.ac.kr", "ddps"), ("A.B@Example.COM", "DDPS"),
             ("alice@example.com", "ddps"), ("_leading@x.com", "ddps"),
             ("trailing_@x.com", "ddps"), ("@nolocalpart.com", "ddps"),
             ("a@b.com", ""), ("a@b.com", "-weird-"),
             ("x" * 80 + "@b.com", "ddps")]
    for address, team in cases:
        got = namespace_suggestion(address, team)
        assert label.fullmatch(got), (address, team, got)
        assert len(got) <= 63, (address, team, got)


def test_an_address_or_team_with_nothing_usable_still_yields_a_name():
    """A name the operator can see is wrong and change, rather than a command
    that fails or one beginning with a hyphen."""
    from ddpsrun_server.notify import DEFAULT_TEAM, namespace_suggestion

    assert namespace_suggestion("@x.com", "ddps") == "ddps-unnamed"
    assert namespace_suggestion("", "ddps") == "ddps-unnamed"
    assert namespace_suggestion("a@b.com", "") == f"{DEFAULT_TEAM}-a"


def test_the_email_asks_for_the_team_and_lists_the_ones_that_exist(
        register_client, keypair):
    """★ THE SERVER CANNOT KNOW THE TEAM -- which one somebody belongs to is a
    fact about the lab, not about the sign-in. So the mail asks, and listing what
    already exists is the difference between a question the operator answers in a
    second and one they have to go and look up.

    The fixture's token file names team `lab`, so that is what the mail offers.
    """
    client, _s3, ses = register_client
    post_register(client, mint(keypair, email="newcomer@example.ac.kr"))
    body = ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]

    assert "DECIDE THE TEAM FIRST" in body
    assert "Teams already in the token file" in body
    assert "lab" in body
    # The commands are concrete, using that team, or they are not runnable.
    assert "kubectl create namespace lab-newcomer" in body
    assert '"team": "lab"' in body
    # And it says what to change if the team is wrong.
    assert "ALL FOUR steps" in body


def test_the_token_store_lists_its_teams_commonest_first():
    """A new member almost always joins the team that already has the most
    people, so the first entry is the one worth pre-filling into the commands."""
    store = auth.TokenStore.from_document({"tokens": [
        {"sha256": auth.hash_token("a"), "user": "a", "namespace": "ddps-a", "team": "ddps"},
        {"sha256": auth.hash_token("b"), "user": "b", "namespace": "ddps-b", "team": "ddps"},
        {"sha256": auth.hash_token("c"), "user": "c", "namespace": "vision-c", "team": "vision"},
        {"sha256": auth.hash_token("d"), "user": "d", "namespace": "solo"},
    ]})
    assert store.teams() == ["ddps", "vision"]


def test_a_token_file_with_no_team_at_all_lists_none():
    """A real state -- `team` is optional -- and the mail then asks for one
    outright instead of offering a list of none."""
    store = auth.TokenStore.from_document({"tokens": [
        {"sha256": auth.hash_token("a"), "user": "a", "namespace": "solo"},
    ]})
    assert store.teams() == []
