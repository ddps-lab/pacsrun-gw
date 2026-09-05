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


@pytest.fixture
def fake_aws_credentials(monkeypatch):
    """Give botocore something to sign with, so no real credentials are needed.

    WHY THIS EXISTS. `eks_token` calls `session.get_credentials()` and signs a
    presigned URL with it. On a developer machine that quietly picks up
    ~/.aws/credentials and the test passes; on CI there is nothing to pick up
    and botocore raises NoCredentialsError. That is exactly what happened on
    2026-09-01 (run 33482005912): two tests passed locally and failed on CI.

    Fake keys are enough because these tests check the SHAPE of the token, not
    that STS accepts it. The signature is computed the same way either way, and
    nothing here talks to AWS.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing-not-a-real-key")
    monkeypatch.setenv(
        "AWS_SECRET_ACCESS_KEY", "testing-not-a-real-secret"
    )
    # A profile or an SSO cache on the machine running the tests would otherwise
    # win over the two variables above.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)


def test_the_minted_token_has_the_shape_the_apiserver_expects(fake_aws_credentials):
    # Verified against the live apiserver on 2026-09-01: a token built this way
    # answered HTTP 200.
    from ddpsrun_server import eks_token

    credential = eks_token.exec_credential("pacsrun", "us-west-2")
    assert credential["kind"] == "ExecCredential"
    token = credential["status"]["token"]
    assert token.startswith("k8s-aws-v1.")
    # Unpadded base64url. The apiserver adds nothing back, so padding breaks it.
    assert "=" not in token.split(".", 1)[1]


def test_the_cluster_name_is_signed_into_the_token(fake_aws_credentials):
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


# ---------------------------------------------------------------------------
# DDPSRUN-BUILD-ONCE. The defect this guards: Mangum's lifespan="auto" runs the
# ASGI lifespan around every invocation, so the whole startup ran per request.
# Measured on the deployed function 2026-09-02: 48 requests, 48 startup log
# lines, 3 real cold starts, and /healthz taking 1.78 s to return a two-key dict.
# ---------------------------------------------------------------------------


def handler_source() -> str:
    """The file's own text.

    The fixture above deliberately stops reading at `# COLD START WORK`, because
    everything after it calls AWS. The two lines these tests care about are after
    that mark, so they are checked in the source. That is a weaker test than
    running them, and it is the right strength for what it guards: someone
    changing one string back.
    """
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "ddpsrun_server"
            / "lambda_handler.py").read_text()


def test_mangum_is_told_not_to_run_the_lifespan():
    """`auto` runs the ASGI lifespan around EVERY invocation, so the whole
    startup ran per request: a new kubernetes client, a new EKS token minted in
    a subprocess, and Cognito's JWKS refetched. /healthz took 1.78 s."""
    import mangum

    # Assert the option still exists and is spelled this way, so the string
    # check below is not quietly testing a name mangum has since renamed.
    assert mangum.Mangum(lambda *a: None, lifespan="off").lifespan == "off"
    assert 'Mangum(app, lifespan="off")' in handler_source()


def test_the_state_is_built_at_import():
    """With the lifespan off, nothing else would ever build it, and every route
    would fail on a missing app.state."""
    source = handler_source()
    assert "build_state(app)" in source
    # Order matters: Mangum wraps an app that is already ready.
    assert source.index("build_state(app)") < source.index("Mangum(app")


def test_building_twice_does_not_rebuild(monkeypatch):
    """What makes calling it at import safe even if something calls it again."""
    from ddpsrun_server import main

    app = main.FastAPI()
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        raise RuntimeError("should not be reached the second time")

    app.state.ready = True
    monkeypatch.setattr(main.Settings, "from_env", staticmethod(counted))
    main.build_state(app)          # guarded: returns at once
    assert calls["n"] == 0

    with pytest.raises(RuntimeError):
        main.build_state(app, force=True)   # lifespan's path: always rebuilds
    assert calls["n"] == 1
