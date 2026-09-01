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

    # THE exec STANZA IS THE POINT. It makes the client mint a freshly signed
    # token for every call, using the function's execution role; that role is the
    # EKS access entry. Without it a Lambda cannot authenticate at all.
    exec_stanza = config["users"][0]["user"]["exec"]
    assert exec_stanza["args"][:2] == ["-m", "ddpsrun_server.eks_token"]
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


def test_the_exec_stanza_runs_our_own_python_and_not_the_aws_cli(handler_module):
    # The first deployment used `aws eks get-token` and every cluster call failed
    # with "[Errno 2] No such file or directory: 'aws'". The Lambda Python
    # runtime has no AWS CLI, and shipping one would add about 100 MB to a cold
    # start already dominated by reading the package.
    import json
    import sys

    handler_module._write_kubeconfig()
    stanza = json.loads(handler_module.KUBECONFIG_PATH.read_text())["users"][0]["user"]["exec"]

    assert stanza["command"] != "aws"
    assert stanza["command"] == sys.executable
    assert stanza["args"][:2] == ["-m", "ddpsrun_server.eks_token"]


def test_the_subprocess_is_told_where_to_find_its_imports(handler_module):
    # It does not inherit sys.path, and it needs both our package and the
    # runtime's botocore.
    import json

    handler_module._write_kubeconfig()
    stanza = json.loads(handler_module.KUBECONFIG_PATH.read_text())["users"][0]["user"]["exec"]
    names = {entry["name"] for entry in stanza["env"]}
    assert "PYTHONPATH" in names


def test_the_minted_token_has_the_shape_the_apiserver_expects():
    # Verified against the live apiserver on 2026-09-01: a token built this way
    # answered HTTP 200.
    from ddpsrun_server import eks_token

    credential = eks_token.exec_credential("pacsrun", "us-west-2")
    assert credential["kind"] == "ExecCredential"
    token = credential["status"]["token"]
    assert token.startswith("k8s-aws-v1.")
    # Unpadded base64url. The apiserver adds nothing back, so padding breaks it.
    assert "=" not in token.split(".", 1)[1]


def test_the_cluster_name_is_signed_into_the_token():
    # This is what stops a token minted for one cluster being replayed against
    # another: the receiving apiserver puts its OWN name in that header before
    # calling STS, and the signature then does not match.
    import base64
    import urllib.parse

    from ddpsrun_server import eks_token

    token = eks_token.eks_token("pacsrun", "us-west-2")
    payload = token.split(".", 1)[1]
    url = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert "x-k8s-aws-id" in query["X-Amz-SignedHeaders"]
