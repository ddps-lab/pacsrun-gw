"""Token parsing and lookup.

The point being defended: a token maps to exactly one namespace, and nothing a
caller sends can change which one.
"""

import pytest

from ddpsrun_server import auth


def store_with(*entries):
    return auth.TokenStore(auth.parse_token_document({"tokens": list(entries)}))


def entry(token, user, namespace):
    return {"sha256": auth.hash_token(token), "user": user, "namespace": namespace}


def test_a_known_token_names_its_user_and_namespace():
    store = store_with(entry("s3cret", "alice", "lab-alice"))
    principal = store.principal_for("s3cret")
    assert principal.user == "alice"
    assert principal.namespace == "lab-alice"


def test_an_unknown_token_is_refused():
    store = store_with(entry("s3cret", "alice", "lab-alice"))
    with pytest.raises(auth.AuthError):
        store.principal_for("not-the-token")


def test_two_users_never_share_a_namespace_by_accident():
    store = store_with(
        entry("token-a", "alice", "lab-alice"),
        entry("token-b", "bob", "lab-bob"),
    )
    assert store.principal_for("token-a").namespace == "lab-alice"
    assert store.principal_for("token-b").namespace == "lab-bob"


def test_the_file_may_not_hold_a_raw_token():
    # The most likely operator mistake: pasting the token where the hash goes.
    # It would still work, which is exactly why it has to be caught.
    with pytest.raises(auth.TokenFileError, match="not a 64-character hex"):
        auth.parse_token_document(
            {"tokens": [{"sha256": "s3cret", "user": "alice", "namespace": "lab-alice"}]}
        )


def test_a_missing_field_is_named():
    with pytest.raises(auth.TokenFileError, match="namespace"):
        auth.parse_token_document(
            {"tokens": [{"sha256": auth.hash_token("x"), "user": "alice"}]}
        )


def test_an_empty_file_is_refused():
    with pytest.raises(auth.TokenFileError):
        auth.parse_token_document({"tokens": []})
    with pytest.raises(auth.TokenFileError):
        auth.parse_token_document([])


def test_the_same_hash_twice_is_refused():
    same = auth.hash_token("s3cret")
    with pytest.raises(auth.TokenFileError, match="twice"):
        auth.parse_token_document(
            {
                "tokens": [
                    {"sha256": same, "user": "alice", "namespace": "lab-alice"},
                    {"sha256": same, "user": "bob", "namespace": "lab-bob"},
                ]
            }
        )


@pytest.mark.parametrize(
    "header",
    [None, "", "s3cret", "Basic s3cret", "Bearer", "Bearer   "],
)
def test_a_header_that_is_not_bearer_is_refused(header):
    with pytest.raises(auth.AuthError):
        auth.bearer_token(header)


def test_the_scheme_is_case_insensitive():
    assert auth.bearer_token("bearer s3cret") == "s3cret"
    assert auth.bearer_token("Bearer  s3cret ") == "s3cret"
