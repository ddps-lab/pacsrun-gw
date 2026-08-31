"""The four routes, end to end, against a stand-in for kube-apiserver.

WHY A STAND-IN RATHER THAN A REAL CLUSTER. What these tests check is the wiring:
that a request with no token is refused before anything else happens, that the
namespace used for the lookup comes from the token, and that an error from the
cluster becomes the right status code. None of that needs a real API server, and
a test that needs one does not run in CI.

The one thing this cannot check is whether PACSrun's CRD accepts the object we
build. `test_models.py` checks the shape against the CRD's own schema by hand;
the real answer comes from a live submission.
"""

import json

import pytest
from fastapi.testclient import TestClient

from ddpsrun_server import auth, k8s, main, naming

JOB_ID = "job-a8acdef80a07"
OBJECT_NAME = naming.object_name(JOB_ID)


class FakeCluster:
    """Records what it was asked to do and answers from a dict."""

    def __init__(self):
        self.created: list[tuple[str, dict]] = []
        self.objects: dict[tuple[str, str], dict] = {}
        self.logs: dict[tuple[str, str], list[str]] = {}

    def create_job(self, namespace, body):
        self.created.append((namespace, body))
        self.objects[(namespace, body["metadata"]["name"])] = body
        return body

    def get_job(self, namespace, name):
        try:
            return self.objects[(namespace, name)]
        except KeyError:
            raise k8s.NotFound(name) from None

    def job_logs(self, namespace, job_name, follow, tail_lines):
        lines = self.logs.get((namespace, job_name))
        if lines is None:
            raise k8s.NotFound(job_name)
        for line in lines:
            cleaned = k8s.redact(line)
            if cleaned is not None:
                yield cleaned + "\n"


@pytest.fixture
def cluster():
    return FakeCluster()


@pytest.fixture
def client(tmp_path, monkeypatch, cluster):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(
        json.dumps(
            {
                "tokens": [
                    {"sha256": auth.hash_token("alice-token"),
                     "user": "alice", "namespace": "lab-alice"},
                    {"sha256": auth.hash_token("bob-token"),
                     "user": "bob", "namespace": "lab-bob"},
                ]
            }
        )
    )
    monkeypatch.setenv("DDPSRUN_RESULT_BUCKET", "<RESULT_BUCKET>")
    monkeypatch.setenv("DDPSRUN_TOKENS_PATH", str(tokens))
    monkeypatch.setenv(
        "DDPSRUN_SECRET_BINDINGS",
        json.dumps({"GITHUB_PAT": {"name": "slm-rca-clone", "key": "token"}}),
    )
    monkeypatch.setattr(main.Cluster, "connect", staticmethod(lambda: cluster))
    with TestClient(main.app) as test_client:
        yield test_client


def submit_body(**overrides):
    body = {"name": "bank-exp2", "image": "runpod/pytorch:1.1.0"}
    body.update(overrides)
    return body


def as_alice(client, method, path, **kwargs):
    return client.request(
        method, path, headers={"Authorization": "Bearer alice-token"}, **kwargs
    )


def test_healthz_needs_no_token(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_a_request_with_no_token_is_refused_before_anything_happens(client, cluster):
    response = client.post("/v1/jobs", json=submit_body())
    assert response.status_code == 401
    assert cluster.created == []


def test_a_request_with_a_wrong_token_is_refused(client, cluster):
    response = client.post(
        "/v1/jobs", json=submit_body(), headers={"Authorization": "Bearer nope"}
    )
    assert response.status_code == 401
    assert cluster.created == []


def test_a_submission_lands_in_the_namespace_the_token_names(client, cluster):
    response = as_alice(client, "POST", "/v1/jobs", json=submit_body())
    assert response.status_code == 201
    namespace, body = cluster.created[0]
    assert namespace == "lab-alice"
    assert body["metadata"]["namespace"] == "lab-alice"


def test_the_response_hands_back_an_id_and_a_result_path(client):
    payload = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()
    assert naming.JOB_ID_PATTERN.match(payload["job_id"])
    assert payload["result_path"].startswith("s3://<RESULT_BUCKET>/pacsrun/lab-alice/")


def test_a_namespace_in_the_body_is_ignored(client, cluster):
    # The field is not in the model, so pydantic drops it. This test exists to
    # notice if somebody ever adds it.
    as_alice(client, "POST", "/v1/jobs", json=submit_body(namespace="kube-system"))
    namespace, body = cluster.created[0]
    assert namespace == "lab-alice"
    assert "kube-system" not in json.dumps(body)


def test_an_unknown_secret_is_a_400_and_nothing_is_created(client, cluster):
    response = as_alice(client, "POST", "/v1/jobs", json=submit_body(secrets=["NOPE"]))
    assert response.status_code == 400
    assert "NOPE" in response.json()["detail"]
    assert cluster.created == []


def test_a_bad_gpu_ask_is_a_422_from_the_model(client):
    response = as_alice(
        client, "POST", "/v1/jobs", json=submit_body(gpu={"vram_gb": 48, "name": "L40S"})
    )
    assert response.status_code == 422


def test_a_cluster_refusal_becomes_a_400_carrying_its_message(client, cluster, monkeypatch):
    def refuse(namespace, body):
        raise k8s.ClusterError("specify exactly one of gpus.name or gpus.vramGB")

    monkeypatch.setattr(cluster, "create_job", refuse)
    response = as_alice(client, "POST", "/v1/jobs", json=submit_body())
    assert response.status_code == 400
    assert "gpus.vramGB" in response.json()["detail"]


def test_a_job_can_be_read_back(client):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    view = as_alice(client, "GET", f"/v1/jobs/{job_id}").json()
    assert view["job_id"] == job_id
    assert view["name"] == "bank-exp2"


def test_another_users_job_reads_as_absent_not_as_forbidden(client, cluster):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    response = client.get(
        f"/v1/jobs/{job_id}", headers={"Authorization": "Bearer bob-token"}
    )
    # 404, not 403: confirming the job exists would tell bob something about alice.
    assert response.status_code == 404


@pytest.mark.parametrize("bad", ["not-an-id", "../../secrets", "job-zzzz"])
def test_a_malformed_id_never_reaches_the_cluster(client, cluster, bad, monkeypatch):
    def explode(namespace, name):
        raise AssertionError(f"the cluster was asked for {name!r}")

    monkeypatch.setattr(cluster, "get_job", explode)
    assert as_alice(client, "GET", f"/v1/jobs/{bad}").status_code == 404


def test_logs_come_back_redacted(client, cluster):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    cluster.logs[("lab-alice", naming.object_name(job_id))] = [
        "{'loss': 0.42}",
        "PACSRUN_KEEPALIVE",
        "PACSRUN_EXIT=0",
    ]
    body = as_alice(client, "GET", f"/v1/jobs/{job_id}/logs").text
    assert "{'loss': 0.42}" in body
    assert "KEEPALIVE" not in body
    assert "<internal>=0" in body


def test_logs_before_the_container_exists_say_so(client):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    response = as_alice(client, "GET", f"/v1/jobs/{job_id}/logs")
    assert response.status_code == 404
    assert "not started a container" in response.json()["detail"]


def test_explain_needs_no_token_and_names_no_internals(client):
    # This endpoint is the most public thing the server has, so what it must NOT
    # contain matters more than what it does.
    response = client.get("/v1/explain")
    assert response.status_code == 200
    text = response.text
    assert "POST /v1/jobs" in text
    for internal in ("PACSRUN_", "namespace", "ServiceAccount", "kubectl", "<RESULT_BUCKET>"):
        assert internal not in text


def test_schema_is_generated_from_the_model_so_it_cannot_drift(client):
    document = client.get("/v1/schema").json()
    properties = document["properties"]
    # Present because the user sends them.
    for field in ("name", "image", "args", "env", "secrets", "gpu", "expected_hours"):
        assert field in properties
    # Absent because the server fills them from the token.
    for field in ("namespace", "serviceAccountName", "resultPath", "placement", "parallelism"):
        assert field not in properties
    assert document["required"] == ["name", "image"]


def test_schema_carries_the_descriptions_a_stranger_reads(client):
    properties = client.get("/v1/schema").json()["properties"]
    assert "never travels through" in properties["secrets"]["description"]
