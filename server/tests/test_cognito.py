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
