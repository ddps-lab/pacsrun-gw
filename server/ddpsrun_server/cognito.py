"""Verify a Cognito id_token, without asking Cognito.

END-TO-END FLOW of this file:

  1. `Verifier` is built once from the pool id, the region and the app client
     id (`config.py` reads all three from the environment).
  2. A request arrives carrying a JWT. `Verifier.claims()` splits it, reads the
     `kid` out of the header, and finds the matching public key.
  3. Public keys come from the pool's JWKS document, fetched once and cached in
     the instance. A `kid` that is not in the cache triggers exactly one refetch
     — a signing key rotation is the only legitimate reason for that, and a made
     up `kid` must not be able to make us fetch on every request.
  4. The signature is checked with RS256, then six claims are checked in turn.
     Any failure raises `TokenError`, which `auth.py` turns into HTTP 401.

WHY NOT ASK COGNITO. Cognito has a `GetUser` call that would answer "is this
token good". It is a network round trip on every single request, and the screen
polls three routes every five seconds. Verifying a signature locally costs one
RSA operation, well under a millisecond, and needs no network after the first
fetch.

WHAT THIS DOES NOT DO. It does not decide what the caller may touch. It answers
"who does Cognito say this is", and nothing more. The mapping from that identity
to a namespace lives in the token file (`docs/16-login.md` 16.2), because a
namespace has to have been created by terraform first and Cognito has no way to
know whether it was.

Grep anchor: DDPSRUN-COGNITO-VERIFY
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from jwt import PyJWKClient


class TokenError(Exception):
    """The JWT is not one we accept. Routes turn this into HTTP 401.

    The message says what was wrong because the caller is a developer holding a
    token they believe is good, and "unauthorized" with no reason is the kind of
    error that costs an afternoon. Nothing in the message is secret: it says
    which claim failed, never what the correct value would be.
    """


@dataclass(frozen=True)
class CognitoIdentity:
    """What Cognito asserts about the caller. Not an authorisation decision.

    Attributes:
        email: the address the person signed in with. This is the key the token
            file is looked up by, which is exactly why `email_verified` is
            checked before this is trusted.
        subject: Cognito's own stable id for the user (`sub`). Kept because an
            email can be changed and this cannot, so it is the right thing to
            log and the right thing to key on if the mapping ever moves.
    """

    email: str
    subject: str


def looks_like_a_jwt(credential: str) -> bool:
    """Is this credential shaped like a JWT rather than a static token?

    Args:
        credential: whatever came after `Bearer `.

    Returns:
        True for three dot-separated segments whose first one decodes as JSON
        with an `alg`.

    THIS IS NOT A SECURITY CHECK. It only decides which of the two branches in
    `auth.py` runs. A string that passes this still has to survive every check
    in `Verifier.claims`; a string that fails it still has to match a stored
    hash. Nothing is admitted by looking like something.

    Example:
        >>> looks_like_a_jwt("ddpsrun-abc123")
        False
    """
    parts = credential.split(".")
    if len(parts) != 3:
        return False
    try:
        header = jwt.get_unverified_header(credential)
    except Exception:
        return False
    return "alg" in header


class Verifier:
    """Checks id_tokens from one user pool against one app client."""

    def __init__(self, pool_id: str, region: str, client_id: str) -> None:
        """Build a verifier.

        Args:
            pool_id: e.g. us-west-2_AbC123. Half of the issuer URL.
            region: the other half. A token from a pool in another region has a
                different `iss` and is refused.
            client_id: the app client the screen and the CLI use. A token minted
                for a different client of the SAME pool is refused, which is
                what keeps a second application from reusing our users.
        """
        self.pool_id = pool_id
        self.region = region
        self.client_id = client_id
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}"
        self.jwks_uri = f"{self.issuer}/.well-known/jwks.json"
        self._jwk_client: PyJWKClient | None = None

    def _keys(self) -> PyJWKClient:
        """The JWKS client, built on first use and then reused.

        Returns:
            A `PyJWKClient`. It does its own caching of individual keys, so a
            warm Lambda fetches the document once per container and then never
            again unless an unknown `kid` arrives.
        """
        if self._jwk_client is None:
            # lifespan is how long a fetched key stays usable. Cognito rotates
            # signing keys rarely; an hour means a rotation is picked up within
            # the hour without a fetch on every cold path.
            self._jwk_client = PyJWKClient(self.jwks_uri, cache_keys=True, lifespan=3600)
        return self._jwk_client

    def claims(self, token: str) -> CognitoIdentity:
        """Verify one id_token and return who it is for.

        Args:
            token: the raw JWT from the `Authorization` header.

        Returns:
            The identity Cognito asserts.

        Raises:
            TokenError: the signature is wrong, a claim is wrong, the token has
                expired, or the key could not be fetched. Six things are checked
                and `docs/16-login.md` 16.5 lists what each one prevents.
        """
        try:
            signing_key = self._keys().get_signing_key_from_jwt(token)
        except Exception as exc:
            # A bad `kid`, an unreachable JWKS, or a malformed token all land
            # here. They are one error to the caller because telling them apart
            # would tell an attacker whether a `kid` they guessed exists.
            raise TokenError(f"could not find the key this token was signed with: {exc}") from exc

        try:
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("this token has expired; sign in again") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenError("this token was issued for a different application") from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenError("this token was issued by a different user pool") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenError(f"this token did not verify: {exc}") from exc

        # `aud` and `iss` are checked above by the library. These three are
        # Cognito-specific and the library knows nothing about them.
        if payload.get("token_use") != "id":
            # An access_token from the same pool verifies against the same key
            # and carries the same iss. What it does NOT carry is a verified
            # email, so accepting one here would let a caller through with no
            # identity to map (docs/16-login.md 16.5).
            raise TokenError(
                "this is not an id_token; send the id_token, not the access_token"
            )

        email = payload.get("email")
        if not email:
            raise TokenError("this token carries no email, so there is nobody to look up")

        if payload.get("email_verified") is not True:
            # Email is the key the token file is looked up by. An unverified one
            # would let anyone who can create a Cognito user claim somebody
            # else's address and inherit their namespace.
            raise TokenError("this account's email address has not been verified")

        return CognitoIdentity(email=str(email).lower(), subject=str(payload["sub"]))
