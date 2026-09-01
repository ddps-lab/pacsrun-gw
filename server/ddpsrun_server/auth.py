"""Who is calling, and which namespace are they allowed to touch.

END-TO-END FLOW of this file:

  1. An operator writes a token file and puts it in a Kubernetes Secret, which
     the server pod mounts. The file lists SHA-256 hashes of tokens, never the
     tokens themselves (step 4 says why).
  2. `TokenStore.load()` reads that file once at startup into a dict keyed by hash.
  3. A request arrives with `Authorization: Bearer <token>`.
     `TokenStore.principal_for()` hashes the presented token and looks it up.
  4. On a hit it returns a `Principal` carrying the user name and the namespace
     that user's jobs live in. Every route then uses `principal.namespace` and
     NEVER a namespace from the request body — that is what stops one user
     submitting into another user's namespace (`docs/03-api.md`).

WHY HASHES AND NOT THE TOKENS. The file is readable by anyone who can read the
Secret or exec into the pod, and it also ends up in `kubectl get secret -o yaml`
output and in backups. A hash cannot be replayed against the API. It costs one
`hashlib.sha256` call per request.

WHY THIS IS A STATIC FILE AND NOT COGNITO. Stage 1 of `docs/08-plan.md` says
"인증은 token 한 종류" — one kind of token, and only one. Cognito is the decided
end state (open item 3 in that
file, resolved) but it needs a browser round trip that no CLI exists to perform
yet (open item 4, unresolved). This module is the seam: when Cognito lands,
`principal_for` gains a second branch that validates a JWT, and nothing above it
changes.

Grep anchor: DDPSRUN-AUTH
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass


class AuthError(Exception):
    """The caller could not be identified. Routes turn this into HTTP 401."""


class UnknownUser(Exception):
    """Cognito vouched for this person, but nobody registered them here.

    Separate from `AuthError` because the two mean different things to whoever
    is reading the response. `AuthError` is 401, "I do not know who you are".
    This is 403, "I know exactly who you are and you are not on the list" —
    which is an operator's job to fix, not the caller's.
    """


class TokenFileError(RuntimeError):
    """The token file is missing or malformed. Raised at startup, never later."""


@dataclass(frozen=True)
class Principal:
    """An authenticated caller.

    Attributes:
        user: display name, used in labels and log lines. Not a security
            boundary on its own.
        namespace: the Kubernetes namespace this caller's jobs live in. This is
            the security boundary: a caller can only create, read, and stream
            logs from objects in this one namespace.
        team: which group this caller belongs to. NOT a security boundary — it
            only decides which namespaces `/v1/stats` adds together. Isolation
            is the namespace's job, and it stays that way precisely so that a
            mistake in team bookkeeping can never show one person another's job.
    """

    user: str
    namespace: str
    team: str = ""


def hash_token(token: str) -> str:
    """Hash a bearer token the same way the token file stores it.

    Args:
        token: the raw token as the user typed it.

    Returns:
        Lowercase hex SHA-256. An operator generating a new token runs the same
        function (see `docs/09-server.md`) so the two always agree.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenStore:
    """The set of tokens this server accepts, held in memory.

    The file it comes from looks like this:

        {
          "tokens": [
            {"sha256": "<64 hex chars>", "user": "alice",
             "namespace": "ddps-alice", "team": "ddps"}
          ]
        }

    The namespace is by convention "<team>-<user>", but nothing here derives one
    from the other: splitting a namespace on a dash to find the team breaks the
    moment a team is called "ddps-lab", and guessing wrong would put a person's
    numbers in the wrong team's total.
    """

    def __init__(
        self,
        by_hash: dict[str, Principal],
        by_email: dict[str, Principal] | None = None,
    ) -> None:
        self._by_hash = by_hash
        self._by_email = by_email or {}

    @staticmethod
    def from_document(document: object) -> "TokenStore":
        """Build a store from an already-parsed token document.

        Exists so callers do not have to know that `parse_token_document`
        returns two maps. `load` reads a file; this takes the same thing already
        in memory, which is what tests want.

        Args:
            document: whatever `json.load` produced.

        Returns:
            A `TokenStore`.

        Raises:
            TokenFileError: on any shape the server cannot use.
        """
        by_hash, by_email = parse_token_document(document)
        return TokenStore(by_hash, by_email)

    @staticmethod
    def load(path: str) -> "TokenStore":
        """Read and validate the token file.

        Args:
            path: filesystem path to the JSON file, normally a mounted Secret key.

        Returns:
            A `TokenStore` ready to answer `principal_for`.

        Raises:
            TokenFileError: the file is absent, is not JSON, or an entry is
                missing one of the three required fields. Failing here rather
                than at first request means a bad file is visible in the pod's
                startup logs instead of as a 401 nobody can explain.
        """
        try:
            with open(path, "r", encoding="utf-8") as handle:
                document = json.load(handle)
        except OSError as exc:
            raise TokenFileError(f"cannot read the token file at {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise TokenFileError(f"the token file at {path} is not valid JSON: {exc}") from exc

        by_hash, by_email = parse_token_document(document)
        return TokenStore(by_hash, by_email)

    def principal_for(self, token: str) -> Principal:
        """Identify the caller behind a bearer token.

        Args:
            token: the raw token from the `Authorization` header.

        Returns:
            The `Principal` that token belongs to.

        Raises:
            AuthError: no such token. The message deliberately says nothing
                about which part was wrong.
        """
        presented = hash_token(token)
        # A plain `in` on the dict would be a timing side channel: Python's
        # short-circuiting string compare returns sooner for a hash that
        # differs in its first characters, which leaks a prefix one byte at a
        # time. Comparing every entry with compare_digest costs one pass over a
        # handful of 64-character strings and leaks nothing.
        for stored_hash, principal in self._by_hash.items():
            if hmac.compare_digest(stored_hash, presented):
                return principal
        raise AuthError("unknown token")

    def principal_for_email(self, email: str) -> Principal:
        """Identify a caller Cognito has already vouched for.

        DDPSRUN-COGNITO-DIRECTORY. Cognito answered "who is this"; this answers
        "and what may they touch". The two are deliberately separate, because a
        namespace has to exist in the cluster before anyone can be given it and
        Cognito has no way to know whether it does (`docs/16-login.md` 16.2).

        Args:
            email: the verified address out of the id_token, already lowercased.

        Returns:
            The `Principal` that address is registered to.

        Raises:
            UnknownUser: the signature was good and the address is real, but
                nobody has registered it here. Routes turn this into HTTP 403,
                not 401: refusing with "who are you" after a successful sign-in
                sends people to debug their login, which is not the problem.
        """
        principal = self._by_email.get(email.strip().lower())
        if principal is None:
            raise UnknownUser(
                f"{email} signed in successfully but is not registered with this "
                f"service. An operator has to create a namespace for you and add "
                f"you to the token file."
            )
        return principal

    def namespaces_in_team(self, team: str) -> list[str]:
        """Every namespace belonging to one team, sorted.

        WHY THIS LIVES IN THE TOKEN STORE and not in the cluster. The server
        already holds the mapping, so answering from here costs no Kubernetes
        permission at all — no `list` on namespaces, no ClusterRoleBinding for
        it, nothing new to grant. The alternative was labelling namespaces and
        listing them, which works but widens what a compromise of this pod
        reaches for no benefit.

        Args:
            team: the team name. Empty returns nothing rather than everything,
                because a caller with no team must not be handed the whole
                cluster's figures.

        Returns:
            The namespaces, sorted and deduplicated.
        """
        if not team:
            return []
        return sorted({p.namespace for p in self._by_hash.values() if p.team == team})

    def __len__(self) -> int:
        return len(self._by_hash)


def parse_token_document(document: object) -> tuple[dict[str, Principal], dict[str, Principal]]:
    """Turn the parsed token file into the two lookup maps.

    Kept separate from `TokenStore.load` so tests can exercise the validation
    without touching the filesystem.

    Args:
        document: whatever `json.load` produced.

    Returns:
        `(by_hash, by_email)`. The first is keyed by lowercase hex SHA-256 and
        answers a static token; the second is keyed by lowercase email and
        answers a Cognito sign-in. An entry may appear in one, the other, or
        both: a person who uses the CLI from a script AND signs in to the screen
        has both keys pointing at the same Principal.

    Raises:
        TokenFileError: on any shape the server cannot use.
    """
    if not isinstance(document, dict):
        raise TokenFileError("the token file must be a JSON object")

    entries = document.get("tokens")
    if not isinstance(entries, list) or not entries:
        raise TokenFileError('the token file must have a non-empty "tokens" array')

    by_hash: dict[str, Principal] = {}
    by_email: dict[str, Principal] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise TokenFileError(f"tokens[{index}] is not an object")
        # `team` is deliberately not required. A deployment with one group has no
        # use for it, and an absent team simply means /v1/stats has nobody to add
        # this caller to.
        # `team` is not required, and since Cognito landed neither is `sha256`:
        # a person who only ever uses the screen has no static token at all.
        # What IS required is at least one way to recognise them.
        missing = [field for field in ("user", "namespace") if not entry.get(field)]
        if missing:
            raise TokenFileError(f"tokens[{index}] is missing {', '.join(missing)}")
        if not entry.get("sha256") and not entry.get("email"):
            raise TokenFileError(
                f"tokens[{index}] has neither sha256 nor email, so nothing can ever "
                f"match it. Give it a token hash, a Cognito email, or both."
            )

        digest = str(entry.get("sha256", "")).strip().lower()
        if digest:
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise TokenFileError(
                    f"tokens[{index}].sha256 is not a 64-character hex SHA-256. "
                    f"Store the HASH of the token here, not the token."
                )
            if digest in by_hash:
                raise TokenFileError(f"tokens[{index}].sha256 appears twice")

        principal = Principal(
            user=str(entry["user"]).strip(),
            namespace=str(entry["namespace"]).strip(),
            team=str(entry.get("team", "")).strip(),
        )
        if digest:
            by_hash[digest] = principal

        email = str(entry.get("email", "")).strip().lower()
        if email:
            # Lowercased on both sides. Cognito treats addresses case-insensitively
            # and `cognito.Verifier` lowercases what it returns, so a file written
            # with "Alice@Example.com" still matches.
            if email in by_email:
                raise TokenFileError(f"tokens[{index}].email appears twice")
            by_email[email] = principal

    return by_hash, by_email


def bearer_token(header_value: str | None) -> str:
    """Pull the token out of an `Authorization` header.

    Args:
        header_value: the raw header, or None when the client sent none.

    Returns:
        The token with no scheme prefix.

    Raises:
        AuthError: the header is absent or is not a Bearer header.
    """
    if not header_value:
        raise AuthError("no Authorization header")
    parts = header_value.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AuthError("Authorization must be 'Bearer <token>'")
    return parts[1].strip()
