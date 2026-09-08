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
        admin: True only for an operator account. It changes exactly one thing:
            routes that accept an explicit `?namespace=` honour it for an admin
            and answer 403 for everyone else (`main.namespace_for`). Everything
            a request does still happens in exactly one namespace — this flag
            only lets an operator say which one.
    """

    user: str
    namespace: str
    team: str = ""
    admin: bool = False


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
             "namespace": "lab-alice", "team": "ddps"}
          ]
        }

    ★ THE NAMESPACE NAMING RULE, AND IT WAS WRITTEN DOWN THREE DIFFERENT WAYS.
    Corrected 2026-09-08 after all three were found in use at once:

        this docstring   "<team>-<user>"                 -> ddps-alice
        the registration
        email            "lab-" + the email's local part -> lab-alice
        a migration      "lab-" + the `user` field       -> lab-alice-gmail

    ONE RULE NOW: the TEAM, then the local part of the address the person signs
    in with, both scrubbed to RFC 1123 labels (lowercase, digits and hyphens, so
    `ddps` + `bo.ram+x@...` becomes `ddps-bo-ram-x`).
    `notify.namespace_suggestion` is the only implementation of it, and the
    registration email prints what it answers, so an operator following that
    mail cannot drift from it.

    ★ THE TEAM IS ASKED FOR, NOT GUESSED. The server cannot know which team a
    new person belongs to -- that is a fact about the lab, not about the
    sign-in -- so the mail lists the teams already in this file (`teams()`) and
    puts the commonest into its example commands for the operator to confirm or
    change. `team` and `namespace` are decided together at the moment somebody
    is added, which is the only moment either is decided at all.

    WHY THE ADDRESS AND NOT THE `user` FIELD, which is what a migration reached
    for once. `user` is typed by an operator and can be anything -- it was
    "second-account" for an address with no "operator" in it -- while the address is
    what the person actually presents at every sign-in. A name derived from the
    thing that identifies them cannot go stale against it.

    ★★ THE TEAM IS IN THE NAME AND MUST STILL NEVER BE READ BACK OUT OF IT.
    These are two different directions and only one is safe:

        team -> namespace   fine. The team is known at the moment a person is
                            added, and building a name from it is what makes the
                            prefix mean something on a shared cluster.
        namespace -> team   NEVER. Splitting on a dash breaks the moment a team
                            is called "ddps-lab", and guessing wrong puts a
                            person's numbers in another team's total.

    So `team` stays its own field and every reader uses THAT. No code in this
    file, or anywhere else, splits a namespace to find a team.

    ★★ AND IT IS A SUGGESTION, NOT A CONSTRAINT. Nothing in this file derives a
    namespace from anything: the value written here is the value used, full stop.
    An operator who wants `lab-bo-ram` for `bo.ram@example.ac.kr` types that
    and it works. The rule exists so a new person gets a name without anybody
    inventing one, not to stop anybody choosing.
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

    def teams(self) -> list[str]:
        """Every team name this store knows, commonest first.

        ★ FOR THE REGISTRATION EMAIL, which has to ASK. The namespace of a new
        person is `<team>-<address local part>`, and the server cannot work out
        which team somebody belongs to -- that is a fact about the lab, not about
        the sign-in. So the mail lists the teams that already exist and puts the
        commonest one into its example commands, and the operator changes it when
        the new person is on another team.

        Commonest first rather than alphabetical: a new member almost always
        joins the team that already has the most people, so the first entry is
        the one worth pre-filling.

        Returns:
            The names, deduplicated. Empty when nobody has a team, which is a
            real state -- the `team` field is optional -- and the mail then asks
            for one outright instead of offering a list of none.
        """
        counted: dict[str, int] = {}
        for principal in set(self._by_hash.values()) | set(self._by_email.values()):
            if principal.team:
                counted[principal.team] = counted.get(principal.team, 0) + 1
        return [name for name, _ in
                sorted(counted.items(), key=lambda pair: (-pair[1], pair[0]))]

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

    def all_namespaces(self) -> list[str]:
        """Every namespace the token file names, sorted and deduplicated.

        This is what an operator's namespace picker shows. It comes from the
        token file for the same reason `namespaces_in_team` does: the server
        already holds the mapping, so answering costs no Kubernetes permission.
        A namespace nobody is registered in is deliberately absent — there is
        nobody whose jobs could be in it, and listing it would only invite a
        502 from a namespace the Lambda's role has no RoleBinding in.
        """
        principals = list(self._by_hash.values()) + list(self._by_email.values())
        return sorted({p.namespace for p in principals})

    def __len__(self) -> int:
        """How many people this store can recognise.

        Counts distinct principals, not keys. Someone with both a static token
        and a Cognito email is one person and appears in both maps; counting
        keys would report them twice, and counting only `_by_hash` would miss
        anyone who signs in through the browser and has no static token at all.
        """
        return len(set(self._by_hash.values()) | set(self._by_email.values()))

    @property
    def counts(self) -> tuple[int, int]:
        """(static tokens, registered emails). For the startup log line, where
        one number cannot say which kind of credential is missing."""
        return len(self._by_hash), len(self._by_email)


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

        # `admin` is opt-in and strictly boolean. JSON has real booleans, and
        # accepting "true"-the-string would let a quoting mistake hand out
        # cross-namespace read. Absent means False, which is right for everyone
        # but the operator accounts (docs/16-login.md 16.2).
        admin = entry.get("admin", False)
        if not isinstance(admin, bool):
            raise TokenFileError(
                f"tokens[{index}].admin must be true or false, not {admin!r}"
            )

        principal = Principal(
            user=str(entry["user"]).strip(),
            namespace=str(entry["namespace"]).strip(),
            team=str(entry.get("team", "")).strip(),
            admin=admin,
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
