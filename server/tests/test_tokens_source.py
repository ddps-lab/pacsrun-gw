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

from pathlib import Path as pathlib_Path

from ddpsrun_server import tokens_source

DIRECTORY = {"tokens": [{"email": "alice@example.com", "user": "alice",
                         "namespace": "ddps-alice", "team": "ddps"}]}


@pytest.fixture
def fake_boto(monkeypatch):
    """A boto3 whose secretsmanager client answers from a dict this test owns."""
    answers = {"body": json.dumps(DIRECTORY), "calls": [], "regions": []}

    class FakeClient:
        def get_secret_value(self, SecretId):          # noqa: N803 - boto3's name
            answers["calls"].append(SecretId)
            if answers["body"] is None:
                raise RuntimeError("secrets manager said no")
            return {"SecretString": answers["body"]}

    module = types.ModuleType("boto3")
    # `**kw` because the real call passes `region_name=`. A stub that takes only
    # the service name hides a signature change instead of catching it -- and
    # this one did: the region argument was added after the pod died with
    # `You must specify a region`, and these tests went red for the wrong reason.
    def client(name, **kw):
        answers["regions"].append(kw.get("region_name"))
        return FakeClient()

    module.client = client
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


@pytest.fixture
def stopper():
    """A stop Event that is always set, whatever the test does.

    ★ WITHOUT THIS THE SUITE FAILS ONLY WHEN RUN IN FULL. A refresh thread left
    running keeps re-fetching every 0.01s and rewriting HYPERUN_TOKENS_PATH in
    the real `os.environ`, so the tests that ran next authenticated against a
    tmp_path belonging to a test that had already finished. It passed on its own
    and failed in the suite, which is the least useful shape a failure can take.
    """
    import threading
    event = threading.Event()
    yield event
    event.set()


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

    sys.modules["boto3"].client = lambda name, **kw: BinaryClient()
    tokens_source.fetch_to_file(tmp_path / "t.json")
    assert json.loads((tmp_path / "t.json").read_text())["tokens"][0]["user"] == "alice"


# ------------------------------------------------------------------ refreshing


def test_nothing_is_refreshed_when_the_file_is_mounted(fake_boto, tmp_path):
    # Without a secret id there is nothing to re-fetch: a mounted file changes on
    # disk by itself, and a thread polling Secrets Manager would call nothing.
    assert tokens_source.refresh_forever(tmp_path / "t.json", lambda: None) is None


def test_a_new_person_is_picked_up_without_a_restart(fake_boto, tmp_path, monkeypatch, stopper):
    # ★ WHAT A LAMBDA GOT FOR FREE AND A POD DOES NOT. Registering somebody used
    # to take effect at the next cold start; a pod stays up for weeks.
    monkeypatch.setenv("HYPERUN_TOKENS_SECRET_ID", "s")
    target = tmp_path / "t.json"
    reloads = []

    thread = tokens_source.refresh_forever(target, lambda: reloads.append(1), seconds=0.01, stop=stopper)
    assert thread is not None

    fake_boto["body"] = json.dumps({"tokens": DIRECTORY["tokens"] + [
        {"email": "bob@example.com", "user": "bob", "namespace": "ddps-bob", "team": "ddps"}]})

    deadline = __import__("time").monotonic() + 5
    while not reloads and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)

    assert reloads, "the refresh thread never called back"
    assert len(json.loads(target.read_text())["tokens"]) == 2


def test_a_failed_refresh_does_not_kill_the_thread(fake_boto, tmp_path, monkeypatch, stopper):
    # The directory already in memory is still valid. Throwing here would take
    # down a server that is answering correctly because Secrets Manager blinked.
    monkeypatch.setenv("HYPERUN_TOKENS_SECRET_ID", "s")
    reloads = []
    thread = tokens_source.refresh_forever(tmp_path / "t.json", lambda: reloads.append(1),
                                           seconds=0.01, stop=stopper)

    fake_boto["body"] = None            # every fetch now raises
    __import__("time").sleep(0.1)
    assert thread.is_alive()

    fake_boto["body"] = json.dumps(DIRECTORY)
    deadline = __import__("time").monotonic() + 5
    while not reloads and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    assert reloads, "the thread stopped refreshing after one failure"


def test_setting_the_stop_event_ends_the_thread():
    # A thread nobody can stop is the defect above wearing a different hat: in a
    # server it leaks into shutdown, in a test it leaks into the next one.
    import os as _os, threading, time
    _os.environ["HYPERUN_TOKENS_SECRET_ID"] = "s"
    try:
        event = threading.Event()
        thread = tokens_source.refresh_forever(pathlib_Path("/dev/null"), lambda: None,
                                               seconds=0.01, stop=event)
        assert thread.is_alive()
        event.set()
        thread.join(timeout=3)
        assert not thread.is_alive(), "the thread ignored the stop event"
    finally:
        _os.environ.pop("HYPERUN_TOKENS_SECRET_ID", None)


def test_the_region_is_handed_to_the_client_and_not_left_to_the_environment(
        fake_boto, tmp_path, monkeypatch):
    # ★ botocore READS `AWS_DEFAULT_REGION`; a Deployment naturally sets
    # `AWS_REGION`, which is the name the rest of Kubernetes uses. The pod had a
    # region in its environment and still died with "You must specify a region"
    # at startup (2026-09-15). Reading both here removes the dependency on which
    # one a deployment happened to set.
    monkeypatch.setenv("HYPERUN_TOKENS_SECRET_ID", "s")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    tokens_source.fetch_to_file(tmp_path / "t.json")
    assert fake_boto["regions"] == ["us-west-2"]


def test_the_old_region_variable_is_honoured_too(fake_boto, tmp_path, monkeypatch):
    monkeypatch.setenv("HYPERUN_TOKENS_SECRET_ID", "s")
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")

    tokens_source.fetch_to_file(tmp_path / "t.json")
    assert fake_boto["regions"] == ["us-east-1"]
