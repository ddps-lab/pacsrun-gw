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

    def list_jobs(self, namespace):
        return [obj for (ns, _), obj in self.objects.items() if ns == namespace]

    def get_job(self, namespace, name):
        try:
            return self.objects[(namespace, name)]
        except KeyError:
            raise k8s.NotFound(name) from None

    def recent_log_lines(self, namespace, job_name, since_seconds):
        lines = self.logs.get((namespace, job_name))
        if lines is None:
            raise k8s.NotFound(job_name)
        return lines

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
                    {"sha256": auth.hash_token("alice-token"), "user": "alice",
                     "namespace": "lab-alice", "team": "lab"},
                    {"sha256": auth.hash_token("bob-token"), "user": "bob",
                     "namespace": "lab-bob", "team": "lab"},
                    {"sha256": auth.hash_token("solo-token"), "user": "solo",
                     "namespace": "solo-ns"},
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
    # parallelism is the USER's: it is how many independent worker pods run, and
    # with gpu.count it is how a job fills a multi-GPU machine. It was briefly in
    # the list below, pinned at 1 by the server, which silently took that away.
    assert "parallelism" in properties

    # Absent because the server fills them from the token or from its own judgement.
    for field in ("namespace", "serviceAccountName", "resultPath", "placement"):
        assert field not in properties
    assert document["required"] == ["name", "image"]


def test_schema_carries_the_descriptions_a_stranger_reads(client):
    properties = client.get("/v1/schema").json()["properties"]
    assert "never travels through" in properties["secrets"]["description"]


def test_a_submission_now_carries_a_capacity_type(client, cluster):
    # Stage 3. Without it PACSrun defaults to spot and RunPod is never a
    # candidate, which is silent: the job runs, just never on RunPod.
    as_alice(client, "POST", "/v1/jobs", json=submit_body(gpu={"vram_gb": 48}))
    _, body = cluster.created[0]
    assert body["spec"]["placement"] == {"capacityType": "on-demand"}


def test_a_submit_body_from_stage_one_still_works(client, cluster):
    # JudgementRequest extends SubmitRequest, so a caller who never heard of
    # `training` sends exactly what they sent before.
    response = as_alice(client, "POST", "/v1/jobs", json=submit_body())
    assert response.status_code == 201


# ------------------------------------------------ stage 3: estimate and validate


def judgement_body(**overrides):
    body = {
        "name": "bank-exp2v2",
        "image": "runpod/pytorch:1.1.0",
        "gpu": {"vram_gb": 48},
        "training": {"pairs": 1110, "epochs": 4, "row_tokens": 4100, "cap": 12288},
    }
    body.update(overrides)
    return body


def test_estimate_reproduces_a_job_we_actually_ran(client, cluster):
    # bank 실험2': 556 steps, 6.54 hours, $6.47.
    result = as_alice(client, "POST", "/v1/estimate", json=judgement_body()).json()
    assert result["steps"] == 556
    assert result["hours"]["confidence"] == "measured"
    assert result["hours"]["low"] < 6.54 < result["hours"]["high"]
    assert result["cost_usd"]["low"] < 6.47 < result["cost_usd"]["high"]
    # And it submitted nothing.
    assert cluster.created == []


def test_estimate_needs_a_token(client):
    assert client.post("/v1/estimate", json=judgement_body()).status_code == 401


def test_estimate_says_unknown_rather_than_guessing(client):
    body = judgement_body()
    body["training"]["row_tokens"] = 30000
    result = as_alice(client, "POST", "/v1/estimate", json=body).json()
    assert result["hours"]["confidence"] == "unknown"
    assert result["hours"]["low"] is None
    assert "96%" in result["basis"]


def test_validate_finds_the_two_mitigations_missing(client, cluster):
    result = as_alice(client, "POST", "/v1/validate", json=judgement_body()).json()
    codes = {finding["code"] for finding in result["findings"]}
    assert {"alloc-conf-missing", "trl-patch-missing", "gpu-too-small"} <= codes
    assert result["ok"] is False
    assert cluster.created == []


def test_validate_reads_the_cap_out_of_a_script_when_it_is_not_given(client):
    body = judgement_body(script="python train.py --max-len 18432 --max-prompt-len 17408")
    body["training"].pop("cap")
    result = as_alice(client, "POST", "/v1/validate", json=body).json()
    message = next(f["message"] for f in result["findings"] if f["code"] == "gpu-too-small")
    assert "18,432" in message


def test_validate_always_says_what_it_could_not_look_at(client):
    result = as_alice(client, "POST", "/v1/validate", json=judgement_body()).json()
    assert result["not_checked"]


def test_the_three_routes_take_the_same_body(client):
    # A caller must be able to check a job and then submit THAT job, unchanged.
    body = judgement_body()
    assert as_alice(client, "POST", "/v1/estimate", json=body).status_code == 200
    assert as_alice(client, "POST", "/v1/validate", json=body).status_code == 200
    assert as_alice(client, "POST", "/v1/jobs", json=body).status_code == 201


def test_the_schema_describes_what_the_routes_actually_accept(client):
    # It described SubmitRequest while the routes took JudgementRequest, so an
    # agent reading it could not discover `training` or `script`. A generated
    # schema that does not match the routes is worse than none: it is
    # confidently incomplete. Found 2026-08-31.
    document = client.get("/v1/schema").json()
    assert "training" in document["properties"]
    assert "script" in document["properties"]
    # Still only two required fields, so a stage-1 caller is unaffected.
    assert document["required"] == ["name", "image"]


def test_the_schema_explains_the_training_facts_it_asks_for(client):
    facts = client.get("/v1/schema").json()["$defs"]["TrainingFacts"]["properties"]
    assert "no step count" in facts["pairs"]["description"]
    assert "no runtime" in facts["row_tokens"]["description"]


# ------------------------------------------------------ stage 4: metrics


def test_metrics_are_read_out_of_the_job_s_own_log(client, cluster):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    cluster.logs[("lab-alice", naming.object_name(job_id))] = [
        "PACSRUN_GPU=94,38200,45440,71,298.5",
        "{'loss': 0.42}",
        " 63%|######3   | 350/556 [4:02:35<2:22:44, 41.57s/it]",
    ]
    result = as_alice(client, "GET", f"/v1/jobs/{job_id}/metrics").json()
    assert result["latest_gpu"]["memory_percent"] == 84.1
    assert result["progress"]["step"] == 350
    assert result["progress"]["projected_total_hours"] == 6.42
    assert result["note"] == ""


def test_the_two_endpoints_read_the_same_line_and_want_opposite_things(client, cluster):
    # /metrics exists to read PACSRUN_GPU=. The user-facing log drops it whole,
    # because it is telemetry rather than the user's output and it arrives every
    # 30 seconds for the life of the job. Same source, opposite treatment, which
    # is why the redaction is at the point of use rather than in the reader.
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    cluster.logs[("lab-alice", naming.object_name(job_id))] = [
        "PACSRUN_GPU=94,38200,45440,71,298.5",
        "{'loss': 0.42}",
    ]
    metrics = as_alice(client, "GET", f"/v1/jobs/{job_id}/metrics").json()
    assert metrics["latest_gpu"]["memory_used_mib"] == 38200

    log = as_alice(client, "GET", f"/v1/jobs/{job_id}/logs").text
    assert "38200" not in log
    assert "{'loss': 0.42}" in log


def test_metrics_before_a_container_exists_say_so(client):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    response = as_alice(client, "GET", f"/v1/jobs/{job_id}/metrics")
    assert response.status_code == 404
    assert "not started a container" in response.json()["detail"]


def test_another_users_metrics_are_absent_not_forbidden(client, cluster):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    cluster.logs[("lab-alice", naming.object_name(job_id))] = ["PACSRUN_GPU=1,2,3,4,5.0"]
    response = client.get(
        f"/v1/jobs/{job_id}/metrics", headers={"Authorization": "Bearer bob-token"}
    )
    assert response.status_code == 404


def test_the_window_is_bounded_so_a_caller_cannot_ask_for_the_whole_log(client):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    # A 25-hour training log runs to hundreds of thousands of lines; an
    # unbounded window would let one request read all of it every few seconds.
    assert as_alice(
        client, "GET", f"/v1/jobs/{job_id}/metrics?window_seconds=999999"
    ).status_code == 422
    assert as_alice(
        client, "GET", f"/v1/jobs/{job_id}/metrics?window_seconds=1"
    ).status_code == 422


# --------------------------------------------------- stage 4b: team statistics


def test_stats_add_up_every_namespace_of_the_team(client, cluster):
    # alice and bob are both on team "lab", so each sees the same team total
    # even though neither can read the other's jobs.
    as_alice(client, "POST", "/v1/jobs", json=submit_body())
    client.post("/v1/jobs", json=submit_body(name="bob-job"),
                headers={"Authorization": "Bearer bob-token"})

    result = as_alice(client, "GET", "/v1/stats").json()
    assert result["team"] == "lab"
    assert result["jobs"] == 2
    assert sorted(m["user"] for m in result["members"]) == ["alice", "bob"]


def test_stats_are_aggregate_and_carry_no_job_names(client, cluster):
    # Being on a team does not entitle you to read a member's jobs.
    client.post("/v1/jobs", json=submit_body(name="bobs-secret-experiment"),
                headers={"Authorization": "Bearer bob-token"})
    body = as_alice(client, "GET", "/v1/stats").text
    assert "bobs-secret-experiment" not in body


def test_a_teammates_job_is_still_unreachable_by_id(client, cluster):
    # The isolation is the namespace's, and /v1/stats does not weaken it.
    job_id = client.post(
        "/v1/jobs", json=submit_body(), headers={"Authorization": "Bearer bob-token"}
    ).json()["job_id"]
    assert as_alice(client, "GET", f"/v1/jobs/{job_id}").status_code == 404


def test_a_token_with_no_team_gets_zeroes_and_an_explanation(client):
    result = client.get("/v1/stats", headers={"Authorization": "Bearer solo-token"}).json()
    assert result["jobs"] == 0
    assert "names no team" in result["note"]


def test_stats_needs_a_token(client):
    assert client.get("/v1/stats").status_code == 401
