"""The two files the handler writes at cold start.

WHAT THESE DEFEND. A pod gets its token list and its cluster credentials as
mounted files; Lambda gets neither, so the handler writes both before the app
imports. If either is wrong the failure is a 500 from inside a function nobody
can attach a debugger to, so the shape is pinned here.

The handler module itself is NOT imported: importing it runs the cold-start
writes, which call Secrets Manager and EKS. The functions are exercised directly
with a fake boto3 instead.
"""

import base64
import json
import sys
import types

import pytest


@pytest.fixture
def handler_module(monkeypatch, tmp_path):
    """Import the handler with its cold-start calls faked out."""
    calls = {"secret": None, "cluster": None}

    class FakeSecrets:
        def get_secret_value(self, SecretId):  # noqa: N803 - boto3's own spelling
            calls["secret"] = SecretId
            return {"SecretString": json.dumps({"tokens": [
                {"sha256": "a" * 64, "user": "alice", "namespace": "ddps-alice"}]})}

    class FakeEks:
        def describe_cluster(self, name):
            calls["cluster"] = name
            return {"cluster": {
                "endpoint": "https://ABC.gr7.us-west-2.eks.amazonaws.com",
                "certificateAuthority": {"data": base64.b64encode(b"-----BEGIN CERT").decode()},
            }}

    fake_boto3 = types.SimpleNamespace(
        client=lambda service: {"secretsmanager": FakeSecrets(), "eks": FakeEks()}[service]
    )
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setenv("DDPSRUN_TOKENS_SECRET_ID", "ddpsrun-gw/tokens")
    monkeypatch.setenv("DDPSRUN_CLUSTER_NAME", "pacsrun")
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    # Import the source without executing the module-level cold-start calls.
    import importlib.util
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "ddpsrun_server" / "lambda_handler.py"
    text = source.read_text().split("# COLD START WORK")[0]
    module = types.ModuleType("lh")
    module.__dict__["__file__"] = str(source)
    exec(compile(text, str(source), "exec"), module.__dict__)  # noqa: S102

    monkeypatch.setattr(module, "TOKENS_PATH", tmp_path / "tokens.json")
    monkeypatch.setattr(module, "KUBECONFIG_PATH", tmp_path / "kubeconfig.yaml")
    monkeypatch.setattr(module, "CA_PATH", tmp_path / "ca.crt")
    module._calls = calls
    return module


def test_the_token_list_comes_from_secrets_manager_and_lands_where_the_server_looks(
    handler_module, monkeypatch
):
    import os

    handler_module._write_token_file()
    assert handler_module._calls["secret"] == "ddpsrun-gw/tokens"
    # config.Settings reads this path; the handler is what points it at /tmp.
    assert os.environ["DDPSRUN_TOKENS_PATH"] == str(handler_module.TOKENS_PATH)
    written = json.loads(handler_module.TOKENS_PATH.read_text())
    assert written["tokens"][0]["namespace"] == "ddps-alice"


def test_a_missing_secret_id_fails_at_cold_start_not_at_the_first_request(
    handler_module, monkeypatch
):
    # A 500 from inside a Lambda is far harder to explain than a startup crash
    # that names the variable in the function's own log.
    monkeypatch.delenv("DDPSRUN_TOKENS_SECRET_ID")
    with pytest.raises(RuntimeError, match="DDPSRUN_TOKENS_SECRET_ID"):
        handler_module._write_token_file()


def test_the_kubeconfig_names_the_cluster_and_mints_a_token_per_call(handler_module):
    import os

    handler_module._write_kubeconfig()
    assert handler_module._calls["cluster"] == "pacsrun"
    assert os.environ["KUBECONFIG"] == str(handler_module.KUBECONFIG_PATH)

    config = json.loads(handler_module.KUBECONFIG_PATH.read_text())
    assert config["clusters"][0]["cluster"]["server"].endswith(".eks.amazonaws.com")

    # THE exec STANZA IS THE POINT. It makes the client sign an STS request with
    # the function's execution role for every call; that role is the EKS access
    # entry. Without it there is no way for a Lambda to authenticate at all.
    exec_stanza = config["users"][0]["user"]["exec"]
    assert exec_stanza["command"] == "aws"
    assert exec_stanza["args"][:2] == ["eks", "get-token"]
    assert "pacsrun" in exec_stanza["args"]


def test_the_ca_is_decoded_rather_than_left_base64(handler_module):
    # The kubeconfig points at this file by path, so it has to be the certificate
    # itself. Leaving it base64 fails TLS verification with an opaque error.
    handler_module._write_kubeconfig()
    assert handler_module.CA_PATH.read_bytes().startswith(b"-----BEGIN")


def test_an_undescribable_cluster_fails_at_cold_start(handler_module, monkeypatch):
    monkeypatch.delenv("DDPSRUN_CLUSTER_NAME")
    with pytest.raises(RuntimeError, match="DDPSRUN_CLUSTER_NAME"):
        handler_module._write_kubeconfig()
