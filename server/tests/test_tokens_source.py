"""Where the directory file comes from, and what happens when it changes.

HYPERUN-TOKENS-SOURCE. The thing under test is not authentication -- no
credential passes through here. It is the DIRECTORY: the file that says which
namespace and team each registered person gets. `auth.TokenStore` reads it;
this module decides where the bytes come from.

The two facts worth defending:
  * a Lambda and a pod fetch it the SAME way, so the two cannot disagree about
    who exists while both are answering requests;
  * a process that never restarts re-reads it, because a Lambda got that for free
    from cold starts and a pod does not.
"""

from __future__ import annotations

import json
import os
import sys
import types

import pytest

from ddpsrun_server import tokens_source

DIRECTORY = {"tokens": [{"email": "alice@example.com", "user": "alice",
                         "namespace": "ddps-alice", "team": "ddps"}]}


@pytest.fixture
def fake_boto(monkeypatch):
    """A boto3 whose secretsmanager client answers from a dict this test owns."""
    answers = {"body": json.dumps(DIRECTORY), "calls": []}

    class FakeClient:
        def get_secret_value(self, SecretId):          # noqa: N803 - boto3's name
            answers["calls"].append(SecretId)
            if answers["body"] is None:
                raise RuntimeError("secrets manager said no")
            return {"SecretString": answers["body"]}

    module = types.ModuleType("boto3")
    module.client = lambda name: FakeClient()
    monkeypatch.setitem(sys.modules, "boto3", module)
    return answers


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("HYPERUN_TOKENS_SECRET_ID", "DDPSRUN_TOKENS_SECRET_ID",
                 "HYPERUN_TOKENS_PATH", "DDPSRUN_TOKENS_PATH"):
        monkeypatch.delenv(name, raising=False)
    yield
    for name in ("HYPERUN_TOKENS_PATH", "DDPSRUN_TOKENS_PATH"):
        os.environ.pop(name, None)


def test_no_secret_id_means_the_file_is_mounted_and_nothing_is_fetched(fake_boto, tmp_path):
    # A local run and the test suite are in this state, and it is not an error:
    # `Settings.from_env` is pointed at a file that already exists.
    assert tokens_source.fetch_to_file(tmp_path / "t.json") is False
    assert fake_boto["calls"] == []


def test_the_secret_is_written_where_the_server_will_look(fake_boto, tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERUN_TOKENS_SECRET_ID", "hyperun-gw/tokens")
    target = tmp_path / "sub" / "tokens.json"

    assert tokens_source.fetch_to_file(target) is True
    assert fake_boto["calls"] == ["hyperun-gw/tokens"]
    # The directory was created: the pod's root filesystem is read-only and the
    # writable mount may be empty on the first read.
    assert json.loads(target.read_text())["tokens"][0]["user"] == "alice"
    # Both names, so a deployment part-way through the rename finds it either way.
    assert os.environ["HYPERUN_TOKENS_PATH"] == str(target)
    assert os.environ["DDPSRUN_TOKENS_PATH"] == str(target)


def test_the_old_variable_name_still_names_the_secret(fake_boto, tmp_path, monkeypatch):
    # The Lambda answering every request today is configured with this one and is
    # deliberately not being touched while the pod is proven beside it.
    monkeypatch.setenv("DDPSRUN_TOKENS_SECRET_ID", "ddpsrun-gw/tokens")
    assert tokens_source.fetch_to_file(tmp_path / "t.json") is True
    assert fake_boto["calls"] == ["ddpsrun-gw/tokens"]


def test_the_new_name_wins_when_both_are_set(fake_boto, tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERUN_TOKENS_SECRET_ID", "new")
    monkeypatch.setenv("DDPSRUN_TOKENS_SECRET_ID", "old")
    tokens_source.fetch_to_file(tmp_path / "t.json")
    assert fake_boto["calls"] == ["new"]


def test_an_unreadable_secret_fails_at_startup_and_names_the_secret(fake_boto, tmp_path, monkeypatch):
    # ★ LOUD AT STARTUP, NOT QUIET UNTIL THE FIRST REQUEST. A server with no
    # directory admits nobody, and "cannot read the token list from X" in the
    # pod's own log is an answer; a 401 on every request is not.
    monkeypatch.setenv("HYPERUN_TOKENS_SECRET_ID", "hyperun-gw/tokens")
    fake_boto["body"] = None

    with pytest.raises(RuntimeError, match="hyperun-gw/tokens"):
        tokens_source.fetch_to_file(tmp_path / "t.json")


def test_a_base64_secret_is_decoded(fake_boto, tmp_path, monkeypatch):
    import base64

    monkeypatch.setenv("HYPERUN_TOKENS_SECRET_ID", "s")

    class BinaryClient:
        def get_secret_value(self, SecretId):          # noqa: N803
            return {"SecretBinary": base64.b64encode(json.dumps(DIRECTORY).encode())}

    sys.modules["boto3"].client = lambda name: BinaryClient()
    tokens_source.fetch_to_file(tmp_path / "t.json")
    assert json.loads((tmp_path / "t.json").read_text())["tokens"][0]["user"] == "alice"


# ------------------------------------------------------------------ refreshing


def test_nothing_is_refreshed_when_the_file_is_mounted(fake_boto, tmp_path):
    # Without a secret id there is nothing to re-fetch: a mounted file changes on
    # disk by itself, and a thread polling Secrets Manager would call nothing.
    assert tokens_source.refresh_forever(tmp_path / "t.json", lambda: None) is None


def test_a_new_person_is_picked_up_without_a_restart(fake_boto, tmp_path, monkeypatch):
    # ★ WHAT A LAMBDA GOT FOR FREE AND A POD DOES NOT. Registering somebody used
    # to take effect at the next cold start; a pod stays up for weeks.
    monkeypatch.setenv("HYPERUN_TOKENS_SECRET_ID", "s")
    target = tmp_path / "t.json"
    reloads = []

    thread = tokens_source.refresh_forever(target, lambda: reloads.append(1), seconds=0.01)
    assert thread is not None

    fake_boto["body"] = json.dumps({"tokens": DIRECTORY["tokens"] + [
        {"email": "bob@example.com", "user": "bob", "namespace": "ddps-bob", "team": "ddps"}]})

    deadline = __import__("time").monotonic() + 5
    while not reloads and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)

    assert reloads, "the refresh thread never called back"
    assert len(json.loads(target.read_text())["tokens"]) == 2


def test_a_failed_refresh_does_not_kill_the_thread(fake_boto, tmp_path, monkeypatch):
    # The directory already in memory is still valid. Throwing here would take
    # down a server that is answering correctly because Secrets Manager blinked.
    monkeypatch.setenv("HYPERUN_TOKENS_SECRET_ID", "s")
    reloads = []
    thread = tokens_source.refresh_forever(tmp_path / "t.json", lambda: reloads.append(1), seconds=0.01)

    fake_boto["body"] = None            # every fetch now raises
    __import__("time").sleep(0.1)
    assert thread.is_alive()

    fake_boto["body"] = json.dumps(DIRECTORY)
    deadline = __import__("time").monotonic() + 5
    while not reloads and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    assert reloads, "the thread stopped refreshing after one failure"
