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
        self.execs: list[tuple[str, str, int, list[str], int]] = []
        self.exec_answer: tuple[str, int | None] = ("", 0)

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

    def delete_job(self, namespace, name):
        try:
            del self.objects[(namespace, name)]
        except KeyError:
            raise k8s.NotFound(name) from None

    def exec_in_driver(self, namespace, job_name, slot, argv, timeout_seconds):
        self.execs.append((namespace, job_name, slot, argv, timeout_seconds))
        return self.exec_answer

    def recent_log_lines(self, namespace, job_name, since_seconds):
        lines = self.logs.get((namespace, job_name))
        if lines is None:
            raise k8s.NotFound(job_name)
        return lines

    def job_log_window(self, namespace, job_name, since_seconds, tail_lines):
        lines = self.logs.get((namespace, job_name))
        if lines is None:
            raise k8s.NotFound(job_name)
        # The real one asks the apiserver for timestamps and keeps them; the
        # fixture's lines already carry one.
        out = []
        for line in lines:
            stamp, _, body = line.partition(" ")
            cleaned = k8s.redact(body)
            if cleaned is not None:
                out.append(f"{stamp} {cleaned}")
        return out


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
                    # The operator account: the one kind of caller whose
                    # ?namespace= is honoured (DDPSRUN-ADMIN-NAMESPACE).
                    {"sha256": auth.hash_token("root-token"), "user": "root",
                     "namespace": "default", "team": "ddps", "admin": True},
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
    # capacity_type is required since 2026-09-01: the caller decides it, not the
    # server. Tests that are about something else send a value so they exercise
    # that something else.
    body = {"name": "bank-exp2", "image": "runpod/pytorch:1.1.0",
            "capacity_type": "on-demand"}
    body.update(overrides)
    return body


def as_alice(client, method, path, **kwargs):
    return client.request(
        method, path, headers={"Authorization": "Bearer alice-token"}, **kwargs
    )


def as_root(client, method, path, **kwargs):
    return client.request(
        method, path, headers={"Authorization": "Bearer root-token"}, **kwargs
    )


# ---------------------------------------------------------------- namespaces


def test_asking_for_another_namespace_without_admin_is_refused(client, cluster):
    cluster.objects[("lab-bob", OBJECT_NAME)] = {
        "metadata": {"name": OBJECT_NAME}, "spec": {}, "status": {},
    }
    for method, path in [
        ("GET", f"/v1/jobs?namespace=lab-bob"),
        ("GET", f"/v1/jobs/{JOB_ID}?namespace=lab-bob"),
        ("GET", f"/v1/jobs/{JOB_ID}/spec?namespace=lab-bob"),
        ("GET", f"/v1/jobs/{JOB_ID}/metrics?namespace=lab-bob"),
        ("GET", f"/v1/jobs/{JOB_ID}/logs?namespace=lab-bob"),
        ("DELETE", f"/v1/jobs/{JOB_ID}?namespace=lab-bob"),
    ]:
        response = as_alice(client, method, path)
        assert response.status_code == 403, (method, path, response.status_code)
    # Nothing was read or deleted on the way to any of those refusals.
    assert ("lab-bob", OBJECT_NAME) in cluster.objects


def test_naming_your_own_namespace_is_not_an_admin_ask(client, cluster):
    # ?namespace=<own> must behave exactly like no parameter at all, so a
    # screen can always send the value it is showing.
    response = as_alice(client, "GET", "/v1/jobs?namespace=lab-alice")
    assert response.status_code == 200


def test_an_admin_reads_the_namespace_they_asked_for(client, cluster):
    cluster.objects[("lab-alice", OBJECT_NAME)] = {
        "metadata": {"name": OBJECT_NAME}, "spec": {}, "status": {},
    }
    listed = as_root(client, "GET", "/v1/jobs?namespace=lab-alice").json()
    assert [j["job_id"] for j in listed["jobs"]] == [JOB_ID]
    got = as_root(client, "GET", f"/v1/jobs/{JOB_ID}?namespace=lab-alice")
    assert got.status_code == 200
    # Without the parameter the same admin reads their own (empty) namespace.
    assert as_root(client, "GET", "/v1/jobs").json()["total"] == 0


def test_the_secret_names_come_back_without_any_value(client):
    # DDPSRUN-SECRET-NAMES. `secrets: ["GITHUB_PAT"]` opens the vault; the
    # accepted words used to be learnable only from a refusal.
    answer = as_alice(client, "GET", "/v1/secrets")
    assert answer.status_code == 200
    body = answer.json()
    assert body["names"] == ["GITHUB_PAT"]
    # The Kubernetes Secret behind it is an internal name and must not appear:
    # the same rule DDPSRUN-SPEC-REDACT enforces on /v1/jobs/{id}/spec.
    assert "slm-rca-clone" not in answer.text


def test_secret_names_need_a_token(client):
    assert client.get("/v1/secrets").status_code == 401


# ------------------------------------------------------------------ artifacts


class FakeS3:
    """Answers list_objects_v2 from a fixed list and mints fake signed URLs."""

    def __init__(self, contents=None, truncated=False, refuse=None):
        self.contents = contents or []
        self.truncated = truncated
        self.refuse = refuse
        self.listed: list[tuple[str, str]] = []

    def list_objects_v2(self, Bucket, Prefix, MaxKeys):
        if self.refuse is not None:
            raise self.refuse
        self.listed.append((Bucket, Prefix))
        return {"Contents": self.contents, "IsTruncated": self.truncated}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://signed.example/{Params['Bucket']}/{Params['Key']}?ttl={ExpiresIn}"


# ---------------------------------------------------------------- images


def test_the_image_list_offers_what_this_lab_has_built(client, monkeypatch):
    """DDPSRUN-IMAGES-ROUTE. Addresses ready to paste into the Image box.

    The module's own behaviour is pinned in test_registry.py; what this asserts is the ROUTE's
    contract -- that the addresses are built server-side, so one place decides the shape rather
    than the browser assembling account id, region and path for itself.
    """
    from ddpsrun_server import registry

    class FakeECR:
        def describe_repositories(self, **_kwargs):
            return {"repositories": [{
                "repositoryName": "pacsrun/operator",
                "repositoryUri": "example.dkr.ecr.us-west-2.amazonaws.com/pacsrun/operator",
            }]}

        def describe_images(self, repositoryName, **_kwargs):
            return {"imageDetails": [
                {"imageTags": ["fd7c9b1c84e1"], "imagePushedAt": "2026-09-06T11:00:00Z"},
            ]}

    monkeypatch.setattr(registry, "ecr_client", lambda region="": FakeECR())
    answer = as_alice(client, "GET", "/v1/images").json()
    assert answer["images"][0]["addresses"] == [
        "example.dkr.ecr.us-west-2.amazonaws.com/pacsrun/operator:fd7c9b1c84e1"
    ]
    assert answer["truncated"] is False
    assert answer["note"] == ""


def test_a_registry_that_refuses_is_200_with_a_note_and_not_502(client, monkeypatch):
    """The Image box is free text with a datalist, not a select.

    So a caller who cannot see the list is inconvenienced and not blocked, and 502 would be a
    harder answer than the situation deserves. What the note MUST do is name the refusal: an
    empty list on its own reads as "this lab has built nothing", which would send an operator
    hunting in the wrong place when what is missing is the IAM policy (DDPSRUN-IMAGES-READ in
    terraform/lambda).
    """
    from ddpsrun_server import registry

    def explode(region=""):
        raise RuntimeError(
            "AccessDeniedException: not authorized to perform ecr:DescribeRepositories"
        )

    monkeypatch.setattr(registry, "ecr_client", explode)
    response = as_alice(client, "GET", "/v1/images")
    assert response.status_code == 200
    answer = response.json()
    assert answer["images"] == []
    assert "ecr:DescribeRepositories" in answer["note"]


def test_an_account_with_no_repositories_says_so(client, monkeypatch):
    """An empty list with no note is the one thing this route must never answer."""
    from ddpsrun_server import registry

    class Empty:
        def describe_repositories(self, **_kwargs):
            return {"repositories": []}

    monkeypatch.setattr(registry, "ecr_client", lambda region="": Empty())
    answer = as_alice(client, "GET", "/v1/images").json()
    assert answer["images"] == []
    assert "no container repositories" in answer["note"]


def test_the_image_list_needs_a_token(client):
    """The only access rule this route has. A shared lab account's repository names are not public."""
    assert client.get("/v1/images").status_code == 401


def seed_job_with_result_path(cluster, namespace, name, result_path):
    cluster.objects[(namespace, name)] = {
        "metadata": {"name": name},
        "spec": {"resultPath": result_path},
        "status": {"phase": "Succeeded"},
    }


def test_artifacts_lists_files_with_download_links(client, cluster, monkeypatch):
    from ddpsrun_server import artifacts

    seed_job_with_result_path(
        cluster, "lab-alice", OBJECT_NAME,
        "s3://<RESULT_BUCKET>/pacsrun/lab-alice/bank-exp2/",
    )
    fake = FakeS3(contents=[
        # The prefix itself can exist as a zero-byte "folder" key; it is not a
        # file and must not appear in the answer.
        {"Key": "pacsrun/lab-alice/bank-exp2/", "Size": 0},
        {"Key": "pacsrun/lab-alice/bank-exp2/run.sh", "Size": 19655},
    ])
    monkeypatch.setattr(artifacts, "s3_client", lambda: fake)

    answer = as_alice(client, "GET", f"/v1/jobs/{JOB_ID}/artifacts").json()
    assert answer["total"] == 1
    assert answer["files"][0]["name"] == "run.sh"
    assert answer["files"][0]["size_bytes"] == 19655
    assert answer["files"][0]["url"].startswith("https://signed.example/")
    assert "ttl=600" in answer["files"][0]["url"]
    assert fake.listed == [("<RESULT_BUCKET>", "pacsrun/lab-alice/bank-exp2/")]


def test_artifacts_with_no_result_path_is_a_note_not_an_error(client, cluster):
    cluster.objects[("lab-alice", OBJECT_NAME)] = {
        "metadata": {"name": OBJECT_NAME}, "spec": {}, "status": {},
    }
    answer = as_alice(client, "GET", f"/v1/jobs/{JOB_ID}/artifacts")
    assert answer.status_code == 200
    assert answer.json()["files"] == []
    assert "no result path" in answer.json()["note"]


def test_artifacts_refuses_a_foreign_bucket(client, cluster, monkeypatch):
    from ddpsrun_server import artifacts

    seed_job_with_result_path(
        cluster, "lab-alice", OBJECT_NAME, "s3://somebody-elses-bucket/loot/",
    )
    fake = FakeS3()
    monkeypatch.setattr(artifacts, "s3_client", lambda: fake)

    answer = as_alice(client, "GET", f"/v1/jobs/{JOB_ID}/artifacts").json()
    assert answer["files"] == []
    assert "outside" in answer["note"]
    # Refused BEFORE any S3 call, not after: the fence is ours, not IAM's.
    assert fake.listed == []


def test_artifacts_turns_an_s3_refusal_into_502(client, cluster, monkeypatch):
    from ddpsrun_server import artifacts

    seed_job_with_result_path(
        cluster, "lab-alice", OBJECT_NAME,
        "s3://<RESULT_BUCKET>/pacsrun/lab-alice/bank-exp2/",
    )
    monkeypatch.setattr(
        artifacts, "s3_client",
        lambda: FakeS3(refuse=RuntimeError("AccessDenied: nobody granted ListBucket")),
    )
    answer = as_alice(client, "GET", f"/v1/jobs/{JOB_ID}/artifacts")
    assert answer.status_code == 502
    assert "S3 refused" in answer.json()["detail"]


def test_logs_first_read_may_ask_for_no_time_window(client, cluster):
    # window_seconds=0 means "no time filter, just the newest tail_lines" —
    # what a screen opening on a long-running or finished job sends first,
    # because any window measured back from now misses hours-old output.
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    cluster.logs[("lab-alice", naming.object_name(job_id))] = [
        "2026-09-01T00:00:01.000Z output from hours before this request",
    ]
    answer = as_alice(
        client, "GET", f"/v1/jobs/{job_id}/logs?window_seconds=0&max_lines=500"
    )
    assert answer.status_code == 200
    assert answer.json()["lines"]


def test_metrics_window_reaches_a_week_back_and_no_further(client, cluster):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    cluster.logs[("lab-alice", naming.object_name(job_id))] = [
        "PACSRUN_GPU=96,42389,46068,79,338.66",
    ]
    ok = as_alice(client, "GET", f"/v1/jobs/{job_id}/metrics?window_seconds=604800")
    assert ok.status_code == 200
    too_far = as_alice(client, "GET", f"/v1/jobs/{job_id}/metrics?window_seconds=604801")
    assert too_far.status_code == 422


def test_exec_relays_one_command_and_the_exit_code(client, cluster):
    cluster.objects[("lab-alice", "hand-made")] = {
        "metadata": {"name": "hand-made"}, "spec": {},
        "status": {"phase": "Running"},
    }
    cluster.exec_answer = ("NVIDIA L40S, 48 GiB\n", 0)
    answer = as_alice(client, "POST", "/v1/jobs/hand-made/exec",
                      json={"command": "nvidia-smi -L"})
    assert answer.status_code == 200
    assert answer.json()["output"].startswith("NVIDIA")
    assert answer.json()["exit_code"] == 0
    namespace, job, slot, argv, _ = cluster.execs[0]
    assert (namespace, job, slot) == ("lab-alice", "hand-made", 0)
    # The user's line rides inside sh -lc, through the driver's shell relay.
    assert argv[:3] == ["python3", "/app/driver/aws/shell.py", "--"]
    assert argv[-2:] == ["-lc", "nvidia-smi -L"]


def test_exec_refuses_a_finished_job_with_the_reason(client, cluster):
    cluster.objects[("lab-alice", "done-job")] = {
        "metadata": {"name": "done-job"}, "spec": {},
        "status": {"phase": "Succeeded"},
    }
    answer = as_alice(client, "POST", "/v1/jobs/done-job/exec",
                      json={"command": "ls"})
    assert answer.status_code == 409
    assert "containers are gone" in answer.json()["detail"]
    assert cluster.execs == []


# --------------------------------------------------------------- jobs by name


def test_a_kubectl_job_opens_by_its_object_name(client, cluster):
    cluster.objects[("lab-alice", "hand-made")] = {
        "metadata": {"name": "hand-made"}, "spec": {}, "status": {"phase": "Running"},
    }
    answer = as_alice(client, "GET", "/v1/jobs/hand-made")
    assert answer.status_code == 200
    assert answer.json()["name"] == "hand-made"
    assert answer.json()["job_id"] == ""
    # And it can be cancelled by the same spelling.
    assert as_alice(client, "DELETE", "/v1/jobs/hand-made").status_code == 204
    assert ("lab-alice", "hand-made") not in cluster.objects


def test_a_name_that_is_not_a_kubernetes_name_is_404(client):
    assert as_alice(client, "GET", "/v1/jobs/Not-A-Name").status_code == 404


def test_namespaces_lists_everything_for_admin_and_self_for_others(client):
    mine = as_alice(client, "GET", "/v1/namespaces").json()
    assert mine == {"namespaces": ["lab-alice"], "own": "lab-alice",
                    "selectable": False}
    theirs = as_root(client, "GET", "/v1/namespaces").json()
    assert theirs["selectable"] is True
    assert theirs["own"] == "default"
    assert theirs["namespaces"] == ["default", "lab-alice", "lab-bob", "solo-ns"]


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


@pytest.mark.parametrize("bad", ["Not-A-Name", "under_score", "-dash", "dash-"])
def test_a_string_that_is_no_kind_of_name_never_reaches_the_cluster(
    client, cluster, bad, monkeypatch
):
    # Only strings that are neither a ddpsrun id nor a legal Kubernetes object
    # name are refused before any lookup. A legal name that happens not to
    # exist ("job-zzzz") now DOES reach the cluster — that is the by-name
    # lookup working (DDPSRUN-JOB-BY-NAME) — and 404s from the lookup itself,
    # which test_a_kubectl_job_opens_by_its_object_name exercises.
    def explode(namespace, name):
        raise AssertionError(f"the cluster was asked for {name!r}")

    monkeypatch.setattr(cluster, "get_job", explode)
    assert as_alice(client, "GET", f"/v1/jobs/{bad}").status_code == 404


def test_logs_come_back_redacted(client, cluster):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    cluster.logs[("lab-alice", naming.object_name(job_id))] = [
        "2026-09-01T00:00:01.000Z {'loss': 0.42}",
        "2026-09-01T00:00:02.000Z PACSRUN_KEEPALIVE",
        "2026-09-01T00:00:03.000Z PACSRUN_EXIT=0",
    ]
    result = as_alice(client, "GET", f"/v1/jobs/{job_id}/logs").json()
    joined = " ".join(result["lines"])
    assert "{'loss': 0.42}" in joined
    assert "KEEPALIVE" not in joined
    assert "<internal>=0" in joined


def test_a_second_poll_returns_only_what_is_new(client, cluster):
    # This is the whole reason the response carries last_timestamp: a Lambda
    # remembers nothing between calls, so the caller has to.
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    cluster.logs[("lab-alice", naming.object_name(job_id))] = [
        "2026-09-01T00:00:01.000Z line 1",
        "2026-09-01T00:00:02.000Z line 2",
        "2026-09-01T00:00:03.000Z line 3",
    ]
    first = as_alice(client, "GET", f"/v1/jobs/{job_id}/logs").json()
    assert len(first["lines"]) == 3
    assert first["last_timestamp"] == "2026-09-01T00:00:03.000Z"

    second = as_alice(
        client, "GET", f"/v1/jobs/{job_id}/logs?since={first['last_timestamp']}"
    ).json()
    assert second["lines"] == []
    assert second["last_timestamp"] is None


def test_a_poll_from_the_middle_returns_the_tail(client, cluster):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    cluster.logs[("lab-alice", naming.object_name(job_id))] = [
        "2026-09-01T00:00:01.000Z line 1",
        "2026-09-01T00:00:02.000Z line 2",
        "2026-09-01T00:00:03.000Z line 3",
    ]
    result = as_alice(
        client, "GET", f"/v1/jobs/{job_id}/logs?since=2026-09-01T00:00:01.000Z"
    ).json()
    assert [l.split(" ", 1)[1] for l in result["lines"]] == ["line 2", "line 3"]


def test_the_window_is_bounded_at_both_ends(client, cluster):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    # Unbounded would let one request read a thirty-hour log every few seconds.
    assert as_alice(
        client, "GET", f"/v1/jobs/{job_id}/logs?window_seconds=999999"
    ).status_code == 422
    assert as_alice(
        client, "GET", f"/v1/jobs/{job_id}/logs?max_lines=0"
    ).status_code == 422


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
    for internal in ("namespace", "ServiceAccount", "kubectl", "<RESULT_BUCKET>"):
        assert internal not in text

    # PACSRUN_ARTIFACT IS THE ONE EXCEPTION, and it is not an internal name at
    # all: it is the line a job's own script has to PRINT to get its results
    # collected, so a document that hides it hides the protocol. It was hidden
    # until 2026-09-08, when a session with only the repository and this tool
    # got a 21-hour job to the point of submission without ever learning it —
    # and would have written to S3 directly and lost everything.
    assert "PACSRUN_ARTIFACT" in text
    # No OTHER PACSRUN_ variable belongs here: those are the platform's own
    # (PACSRUN_FETCH_MODE, PACSRUN_DISK_GB) and nobody submitting a job sets one.
    others = [
        word for word in text.split()
        if word.startswith("PACSRUN_") and not word.startswith("PACSRUN_ARTIFACT")
    ]
    assert others == [], others


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
    # `training` sends exactly what they sent before, plus capacity_type.
    response = as_alice(client, "POST", "/v1/jobs", json=submit_body())
    assert response.status_code == 201


def test_a_submit_without_a_capacity_type_is_refused_not_guessed(client, cluster):
    # The server used to fill this from its own estimate, which let a thirty-hour
    # job land on reclaimable capacity without anyone being asked.
    body = submit_body()
    body.pop("capacity_type")
    response = as_alice(client, "POST", "/v1/jobs", json=body)
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "capacity_type is required" in detail
    assert "/v1/estimate recommends" in detail
    assert cluster.created == []


def test_the_callers_choice_is_what_reaches_the_object(client, cluster):
    as_alice(client, "POST", "/v1/jobs", json=submit_body(capacity_type="spot"))
    _, body = cluster.created[0]
    assert body["spec"]["placement"] == {"capacityType": "spot"}


def test_a_capacity_type_we_do_not_understand_is_refused(client):
    response = as_alice(client, "POST", "/v1/jobs", json=submit_body(capacity_type="reserved"))
    assert response.status_code == 422


# ------------------------------------------------ stage 3: estimate and validate


def judgement_body(**overrides):
    body = {
        "name": "bank-exp2v2",
        "image": "runpod/pytorch:1.1.0",
        "gpu": {"vram_gb": 48},
        "training": {"pairs": 1110, "epochs": 4, "row_tokens": 4100, "cap": 12288},
        "capacity_type": "on-demand",
    }
    body.update(overrides)
    return body


def test_estimate_reproduces_a_job_we_actually_ran(client, cluster):
    # bank-exp2v2: 556 steps, 6.54 hours, $6.47.
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
        "2026-09-01T00:00:01.000Z PACSRUN_GPU=94,38200,45440,71,298.5",
        "2026-09-01T00:00:02.000Z {'loss': 0.42}",
    ]
    metrics = as_alice(client, "GET", f"/v1/jobs/{job_id}/metrics").json()
    assert metrics["latest_gpu"]["memory_used_mib"] == 38200

    log = " ".join(as_alice(client, "GET", f"/v1/jobs/{job_id}/logs").json()["lines"])
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


def test_an_operators_own_spend_includes_the_admin_bucket(client, cluster):
    # Only an operator can apply a job with kubectl, and such a job has no
    # owner label, so the "admin" bucket is the operator's own work. Their
    # My spend card must not say $0.00 next to a team total they personally
    # spent — which is exactly what it said on 2026-09-07.
    cluster.objects[("default", "hand-made")] = {
        "metadata": {"name": "hand-made"},
        "spec": {"parallelism": 1},
        "status": {
            "phase": "Succeeded",
            "startedAt": "2026-09-07T00:00:00Z",
            "finishedAt": "2026-09-07T02:00:00Z",
            "currentOffering": {"vendor": "runpod", "instanceType": "NVIDIA L40S"},
        },
    }
    result = as_root(client, "GET", "/v1/stats").json()
    assert result["caller"] == "root"
    assert [m["user"] for m in result["members"]] == ["admin"]
    assert abs(result["caller_cost_usd"] - 2 * 0.99) < 0.01
    # A non-operator's figure stays their own row alone.
    assert as_alice(client, "GET", "/v1/stats").json()["caller_cost_usd"] == 0.0


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


def test_the_job_list_shows_only_this_callers_jobs(client, cluster):
    as_alice(client, "POST", "/v1/jobs", json=submit_body(name="alice-job"))
    client.post("/v1/jobs", json=submit_body(name="bobs-job"),
                headers={"Authorization": "Bearer bob-token"})

    result = as_alice(client, "GET", "/v1/jobs").json()
    names = [job["name"] for job in result["jobs"]]
    assert names == ["alice-job"]
    assert "bobs-job" not in str(result)


def test_the_job_list_is_newest_first(client, cluster):
    for i, stamp in enumerate(["2026-09-01T03:00:00Z", "2026-09-01T01:00:00Z",
                               "2026-09-01T02:00:00Z"]):
        obj = {"metadata": {"name": f"ddpsrun-00000000000{i}",
                            "creationTimestamp": stamp,
                            "annotations": {"ddpsrun.io/display-name": f"job-{i}"}},
               "spec": {}, "status": {}}
        cluster.objects[("lab-alice", obj["metadata"]["name"])] = obj

    result = as_alice(client, "GET", "/v1/jobs").json()
    assert [j["name"] for j in result["jobs"]] == ["job-0", "job-2", "job-1"]


def test_a_job_with_no_timestamp_yet_does_not_break_the_sort(client, cluster):
    cluster.objects[("lab-alice", "ddpsrun-0000000000f9")] = {
        "metadata": {"name": "ddpsrun-0000000000f9"}, "spec": {}, "status": {}}
    as_alice(client, "POST", "/v1/jobs", json=submit_body())
    assert as_alice(client, "GET", "/v1/jobs").status_code == 200


def test_the_job_list_needs_a_token(client):
    assert client.get("/v1/jobs").status_code == 401


# ---------------------------------------------------------------------------
# DDPSRUN-SCREENS: the four additions `docs/15-screens.md` found missing when
# the five screens were designed. Each test names the screen that needs it.
# ---------------------------------------------------------------------------


def _job(name, *, phase="", stamp="2026-09-01T00:00:00Z", **status):
    """Build one raw PacsJob the way the apiserver would return it.

    Args:
        name: the object name, which also carries the job id.
        phase: status.phase, empty for a job the controller has not seen.
        stamp: metadata.creationTimestamp.
        status: extra status fields, e.g. startedAt, finishedAt.

    Returns:
        A dict shaped like the real object, ready to drop into FakeCluster.
    """
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": stamp,
            "labels": {"ddpsrun.io/job-id": name.replace("ddpsrun-", "job-"),
                       "ddpsrun.io/owner": "alice"},
            "annotations": {"ddpsrun.io/display-name": name},
        },
        "spec": {},
        "status": {"phase": phase, **status},
    }


def test_the_job_list_reports_who_submitted_each_job(client, cluster):
    """The jobs screen's 'submitted by' column (docs/15-screens.md 15.5)."""
    cluster.objects[("lab-alice", "ddpsrun-0000000000a1")] = _job("ddpsrun-0000000000a1")
    result = as_alice(client, "GET", "/v1/jobs").json()
    assert result["jobs"][0]["user"] == "alice"


def test_a_job_carries_the_two_clock_stamps(client, cluster):
    """The elapsed column needs run time, not age (PACSRUN-JOB-CLOCK)."""
    cluster.objects[("lab-alice", "ddpsrun-0000000000a2")] = _job(
        "ddpsrun-0000000000a2", phase="Succeeded",
        startedAt="2026-09-01T00:01:00Z", finishedAt="2026-09-01T02:30:00Z")
    view = as_alice(client, "GET", "/v1/jobs/job-0000000000a2").json()
    assert view["started_at"] == "2026-09-01T00:01:00Z"
    assert view["finished_at"] == "2026-09-01T02:30:00Z"


def test_a_waiting_job_has_no_start_stamp(client, cluster):
    """Queue time is visible precisely because startedAt is absent until it runs."""
    cluster.objects[("lab-alice", "ddpsrun-0000000000a3")] = _job(
        "ddpsrun-0000000000a3", phase="Pending")
    view = as_alice(client, "GET", "/v1/jobs/job-0000000000a3").json()
    assert view["created_at"] == "2026-09-01T00:00:00Z"
    assert view["started_at"] is None
    assert view["finished_at"] is None


def test_the_active_filter_drops_finished_jobs(client, cluster):
    """The jobs screen's default tab."""
    for i, phase in enumerate(["Running", "Succeeded", "Failed", "Pending"]):
        name = f"ddpsrun-00000000001{i}"
        cluster.objects[("lab-alice", name)] = _job(name, phase=phase)

    active = as_alice(client, "GET", "/v1/jobs?phase=active").json()
    assert sorted(j["phase"] for j in active["jobs"]) == ["Pending", "Running"]

    finished = as_alice(client, "GET", "/v1/jobs?phase=finished").json()
    assert sorted(j["phase"] for j in finished["jobs"]) == ["Failed", "Succeeded"]


def test_a_job_with_no_phase_yet_counts_as_active(client, cluster):
    """A job the controller has not looked at is still one to watch."""
    cluster.objects[("lab-alice", "ddpsrun-0000000000b0")] = _job("ddpsrun-0000000000b0")
    result = as_alice(client, "GET", "/v1/jobs?phase=active").json()
    assert len(result["jobs"]) == 1


def test_an_exact_phase_can_be_asked_for(client, cluster):
    for i, phase in enumerate(["Failed", "Succeeded"]):
        name = f"ddpsrun-00000000003{i}"
        cluster.objects[("lab-alice", name)] = _job(name, phase=phase)
    result = as_alice(client, "GET", "/v1/jobs?phase=Failed").json()
    assert [j["phase"] for j in result["jobs"]] == ["Failed"]


def test_the_limit_caps_the_list_but_total_still_counts_everything(client, cluster):
    """So the screen can say 'showing 2 of 5' rather than hiding three jobs."""
    for i in range(5):
        name = f"ddpsrun-00000000004{i}"
        cluster.objects[("lab-alice", name)] = _job(
            name, stamp=f"2026-09-01T0{i}:00:00Z")
    result = as_alice(client, "GET", "/v1/jobs?limit=2").json()
    assert len(result["jobs"]) == 2
    assert result["total"] == 5


def test_a_limit_outside_the_allowed_range_is_refused(client):
    assert as_alice(client, "GET", "/v1/jobs?limit=0").status_code == 422
    assert as_alice(client, "GET", "/v1/jobs?limit=1001").status_code == 422


def test_the_spec_route_returns_the_submission(client, cluster):
    """The detail screen's 'what exactly did I run' panel."""
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    result = as_alice(client, "GET", f"/v1/jobs/{job_id}/spec").json()
    assert result["job_id"] == job_id
    assert result["spec"]["image"] == submit_body()["image"]


def test_the_spec_route_removes_the_secret_source_but_keeps_the_name(client, cluster):
    """DDPSRUN-SPEC-REDACT: the Kubernetes Secret's name and key never leave."""
    job_id = as_alice(
        client, "POST", "/v1/jobs", json=submit_body(secrets=["GITHUB_PAT"])
    ).json()["job_id"]

    result = as_alice(client, "GET", f"/v1/jobs/{job_id}/spec").json()
    names = [entry["name"] for entry in result["spec"]["env"]]
    assert "GITHUB_PAT" in names
    assert result["redacted"] == ["GITHUB_PAT"]
    assert "secretKeyRef" not in json.dumps(result)
    assert "slm-rca-clone" not in json.dumps(result)


def test_the_spec_route_removes_the_service_account_name(client, cluster):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    result = as_alice(client, "GET", f"/v1/jobs/{job_id}/spec").json()
    assert "serviceAccountName" not in result["spec"]


def test_the_spec_route_hides_someone_elses_job(client, cluster):
    job_id = client.post(
        "/v1/jobs", json=submit_body(), headers={"Authorization": "Bearer bob-token"}
    ).json()["job_id"]
    assert as_alice(client, "GET", f"/v1/jobs/{job_id}/spec").status_code == 404


def test_the_spec_route_needs_a_token(client):
    assert client.get("/v1/jobs/job-0000000000a1/spec").status_code == 401


def test_compared_counts_as_finished_not_as_still_running(client, cluster):
    """A real defect found on 2026-09-01: 12 of 24 live jobs were Compared and
    the active tab showed every one of them.

    Compared means a mode=compare job priced every candidate offering and bought
    nothing (`api/v1alpha1/pacsjob_types.go:85`). It is terminal and it is not a
    failure, so it belongs under 'finished' with a label of its own.
    """
    cluster.objects[("lab-alice", "ddpsrun-0000000000c5")] = _job(
        "ddpsrun-0000000000c5", phase="Compared")

    assert as_alice(client, "GET", "/v1/jobs?phase=active").json()["jobs"] == []
    finished = as_alice(client, "GET", "/v1/jobs?phase=finished").json()["jobs"]
    assert [j["phase"] for j in finished] == ["Compared"]


# ---------------------------------------------------------------------------
# DDPSRUN-CANCEL. Added 2026-09-02 after a job sat in Pending with no way out:
# it asked for an L40S on spot, which RunPod refuses before reading the
# catalogue and which no AWS row matched, so the controller retried the same
# failure forever. `kubectl` was the only way to stop it, and needing kubectl is
# the thing this service exists to remove.
# ---------------------------------------------------------------------------


def test_cancelling_removes_the_job(client, cluster):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    assert as_alice(client, "GET", f"/v1/jobs/{job_id}").status_code == 200

    assert as_alice(client, "DELETE", f"/v1/jobs/{job_id}").status_code == 204
    assert as_alice(client, "GET", f"/v1/jobs/{job_id}").status_code == 404


def test_a_cancelled_job_leaves_the_list(client, cluster):
    job_id = as_alice(client, "POST", "/v1/jobs", json=submit_body()).json()["job_id"]
    as_alice(client, "DELETE", f"/v1/jobs/{job_id}")
    assert as_alice(client, "GET", "/v1/jobs").json()["total"] == 0


def test_a_finished_job_can_be_cancelled_too(client, cluster):
    """Nothing is stopped; the row goes away. That is the other thing the button
    is for, and refusing it would leave failed rows on screen forever."""
    cluster.objects[("lab-alice", "ddpsrun-0000000000d1")] = _job(
        "ddpsrun-0000000000d1", phase="Failed")
    assert as_alice(client, "DELETE", "/v1/jobs/job-0000000000d1").status_code == 204


def test_cancelling_someone_elses_job_is_404_and_leaves_it_alone(client, cluster):
    """404 rather than 403: confirming the job exists would let anyone map
    another namespace by guessing ids."""
    job_id = client.post(
        "/v1/jobs", json=submit_body(), headers={"Authorization": "Bearer bob-token"}
    ).json()["job_id"]

    assert as_alice(client, "DELETE", f"/v1/jobs/{job_id}").status_code == 404
    # And it is still there for its owner.
    still = client.request("GET", f"/v1/jobs/{job_id}",
                           headers={"Authorization": "Bearer bob-token"})
    assert still.status_code == 200


def test_cancelling_an_unknown_id_is_404(client):
    assert as_alice(client, "DELETE", "/v1/jobs/job-0000000000ff").status_code == 404
    assert as_alice(client, "DELETE", "/v1/jobs/not-an-id").status_code == 404


def test_cancelling_needs_a_token(client):
    assert client.delete("/v1/jobs/job-0000000000a1").status_code == 401

# ---------------------------------------------------------------- DDPSRUN-SCRIPTS


def seed_job_with_args(cluster, namespace, name, args, job_id="", display="", created="",
                       owner=""):
    """A PacsJob shaped like the ones the screen creates, with the args it would carry.

    `owner` defaults to EMPTY so the old callers keep testing the shape they meant
    to -- a job with no submitter recorded, which is what `kubectl apply` makes.
    Every job this gateway creates carries one (`models.to_pacsjob` stamps
    `ddpsrun.io/owner` from principal.user), so a test about the normal path has
    to pass it.
    """
    labels = {}
    if job_id:
        labels["ddpsrun.io/job-id"] = job_id
    if display:
        labels["ddpsrun.io/name"] = display
    if owner:
        labels["ddpsrun.io/owner"] = owner
    cluster.objects[(namespace, name)] = {
        "metadata": {"name": name, "namespace": namespace, "labels": labels,
                     "creationTimestamp": created},
        "spec": {"image": "img", "args": args},
        "status": {"phase": "Succeeded"},
    }


def test_a_script_is_read_back_out_of_the_job_that_ran_it(client, cluster):
    """DDPSRUN-SCRIPTS. Nothing is stored; the text is on the PacsJob already.

    The Script box sends the same text twice -- as `args` (what runs) and as `script` (what
    validate reads) -- and the server throws `script` away, exactly as its field description
    promises. `args` stays for as long as the job does, so the script is already durable,
    already scoped to the caller's namespace, and already deleted when the job is. A bucket for
    scripts would be a second copy that can disagree with the first.
    """
    seed_job_with_args(
        cluster, "lab-alice", "ddpsrun-aaaaaaaaaaaa",
        ["bash", "-lc", "set -euo pipefail\npython train.py"],
        job_id="job-aaaaaaaaaaaa", display="train", created="2026-09-08T01:00:00Z",
        owner="alice",
    )
    answer = as_alice(client, "GET", "/v1/scripts").json()
    assert len(answer["scripts"]) == 1
    only = answer["scripts"][0]
    assert only["script"] == "set -euo pipefail\npython train.py"
    assert only["job_id"] == "job-aaaaaaaaaaaa"
    assert only["name"] == "train"
    assert only["lines"] == 2
    assert only["used"] == 1
    assert only["owner"] == "alice"
    assert answer["owners"] == ["alice"]
    assert answer["note"] == ""


def test_the_same_script_five_times_is_one_entry(client, cluster):
    """A list where every retry is its own row is a list nobody scrolls.

    And the entry names the MOST RECENT job that ran it, because that is the one worth opening.
    """
    for i, day in enumerate(("05", "06", "07")):
        seed_job_with_args(
            cluster, "lab-alice", f"ddpsrun-bbbbbbbbbbb{i}",
            ["bash", "-lc", "python same.py"],
            job_id=f"job-bbbbbbbbbbb{i}", display=f"run-{day}",
            created=f"2026-09-{day}T01:00:00Z",
        )
    answer = as_alice(client, "GET", "/v1/scripts").json()
    assert len(answer["scripts"]) == 1
    assert answer["scripts"][0]["used"] == 3
    assert answer["scripts"][0]["name"] == "run-07", "the newest job of the three names it"


def test_a_job_whose_args_are_not_a_script_is_left_out_and_the_note_says_which_emptiness(
    client, cluster
):
    """An empty list with no note reads as "you have never submitted a script".

    That is a different fact from "none of your jobs was submitted in a shape this route
    recognises", and a kubectl job or an argv list is the second one. Guessing which part of an
    arbitrary argv is "the script" would put text in front of somebody as if we knew.
    """
    seed_job_with_args(cluster, "lab-alice", "ddpsrun-cccccccccccc",
                       ["python", "train.py", "--epochs", "4"])
    seed_job_with_args(cluster, "lab-alice", "ddpsrun-dddddddddddd", [])
    seed_job_with_args(cluster, "lab-alice", "ddpsrun-eeeeeeeeeeee",
                       ["bash", "-lc", "   "])
    answer = as_alice(client, "GET", "/v1/scripts").json()
    assert answer["scripts"] == []
    assert "in that shape" in answer["note"]


def test_scripts_are_the_callers_own_and_nobody_elses(client, cluster):
    """The same boundary every other route uses: read from the token's namespace and nowhere else."""
    seed_job_with_args(cluster, "lab-bob", "ddpsrun-ffffffffffff",
                       ["bash", "-lc", "bob's private thing"])
    assert as_alice(client, "GET", "/v1/scripts").json()["scripts"] == []
    mine = as_alice(client, "GET", "/v1/scripts")
    assert mine.status_code == 200
    assert client.get("/v1/scripts").status_code == 401


# ------------------------------------------------- DDPSRUN-PRICES: /v1/prices
#
# WHY THESE EXIST AT ALL, and it is a lesson rather than a formality. /v1/prices
# shipped with 24 unit tests behind its DATA and not one behind the ROUTE, and
# the route raised NameError on its first real call: `measurements` was never
# imported into main.py. Everything passed, because nothing asked the route
# anything. `test_every_route_appears_in_the_api_reference` checks that a route
# is DOCUMENTED, which is not the same as answering.


def test_the_price_table_answers_without_a_token(client):
    """No token, for the same reason /v1/schema needs none: a published list
    price is not this lab's information, and it is what somebody reads BEFORE
    deciding whether to ask for an account."""
    answer = client.get("/v1/prices")
    assert answer.status_code == 200
    body = answer.json()
    assert len(body["rows"]) == 610
    assert len(body["regions"]) == 22
    assert body["default_region"] == "us-west-2"


def test_the_price_table_filters(client):
    """Three filters, because 610 rows is the whole table and a person looking
    for one card should not have to receive all of it."""
    one = client.get("/v1/prices?card=H100&vendor=aws&region=us-west-2").json()
    assert {r["card"] for r in one["rows"]} == {"H100"}
    assert {r["region"] for r in one["rows"]} == {"us-west-2"}
    assert {r["vendor"] for r in one["rows"]} == {"aws"}
    # The region list is the whole one regardless of the filter: it is what
    # placement.regions accepts, not what the filter matched.
    assert len(one["regions"]) == 22

    assert client.get("/v1/prices?card=nosuchcard").json()["rows"] == []


def test_the_price_table_says_which_basis_each_row_is(client):
    """AWS prices a whole machine, GCP prices the cards alone. A screen that
    sorted the two together would put GCP on top whenever it is not cheaper."""
    body = client.get("/v1/prices").json()
    bases = {(r["vendor"], r["basis"]) for r in body["rows"]}
    assert bases == {("aws", "machine"), ("gcp", "accelerator")}
    assert "whole machine" in body["note"] or "price a whole machine" in body["note"]


def test_regions_reach_the_estimate_through_the_route(client):
    """The same job, priced in two regions, over the real route. The H100 is 25%
    dearer in ap-northeast-1 and that difference was invisible while the table
    held one region."""
    def ask(regions):
        body = {"name": "r", "image": "nvidia/cuda:12.4.1-base-ubuntu22.04",
                "args": ["bash", "-lc", "python train.py"],
                "gpu": {"name": "H100", "count": 1}, "capacity_type": "on-demand",
                "vendors": ["aws"], "regions": regions,
                "training": {"pairs": 5000, "epochs": 1, "row_tokens": 4100,
                             "cap": 12288}}
        answer = as_alice(client, "POST", "/v1/estimate", json=body)
        assert answer.status_code == 200, answer.text
        return answer.json()["rate"]

    home = ask([])
    seoul = ask(["aws/ap-northeast-1"])
    assert home["usd_per_hour_low"] == 6.88
    assert seoul["usd_per_hour_low"] == 8.60
    assert "us-west-2" in home["basis"] and "ap-northeast-1" in seoul["basis"]


# ------------------------------------- DDPSRUN-SCRIPTS: who ran what, per person


def test_two_people_running_the_same_script_are_two_entries(client, cluster):
    """★ THE QUESTION THIS ROUTE IS ASKED is "which scripts has each person run",
    and keying on the text alone could not answer it. Two people with the same
    run.sh collapsed into ONE entry that kept the first job's metadata and merely
    counted the second, so the listing named one of them and silently dropped the
    other."""
    shared = "python train.py --config shared.yaml"
    seed_job_with_args(cluster, "lab-alice", "ddpsrun-aaaaaaaaaaaa",
                       ["bash", "-lc", shared], job_id="job-aaaaaaaaaaaa",
                       display="alice-run", created="2026-09-08T01:00:00Z", owner="alice")
    seed_job_with_args(cluster, "lab-alice", "ddpsrun-bbbbbbbbbbbb",
                       ["bash", "-lc", shared], job_id="job-bbbbbbbbbbbb",
                       display="bob-run", created="2026-09-08T02:00:00Z", owner="bob")

    answer = as_alice(client, "GET", "/v1/scripts").json()
    assert len(answer["scripts"]) == 2
    assert answer["owners"] == ["alice", "bob"]
    by_owner = {s["owner"]: s for s in answer["scripts"]}
    assert set(by_owner) == {"alice", "bob"}
    # Each is one run of it, not one entry claiming two.
    assert by_owner["alice"]["used"] == 1 and by_owner["bob"]["used"] == 1
    assert by_owner["alice"]["name"] == "alice-run"
    assert by_owner["bob"]["name"] == "bob-run"


def test_one_person_running_it_five_times_is_still_one_entry(client, cluster):
    """The count is per (person, text). Splitting on the person must not undo the
    de-duplication that made the screen readable in the first place."""
    same = "python train.py"
    for n, stamp in enumerate(["01", "02", "03", "04", "05"]):
        seed_job_with_args(cluster, "lab-alice", f"ddpsrun-cccccccccc{n}0",
                           ["bash", "-lc", same], job_id=f"job-cccccccccc{n}0",
                           display=f"run-{n}", created=f"2026-09-08T{stamp}:00:00Z",
                           owner="alice")
    answer = as_alice(client, "GET", "/v1/scripts").json()
    assert len(answer["scripts"]) == 1
    assert answer["scripts"][0]["used"] == 5
    assert answer["owners"] == ["alice"]
    # The newest run is the one named: the objects are walked newest first.
    assert answer["scripts"][0]["name"] == "run-4"


def test_a_job_with_no_owner_says_so_rather_than_guessing(client, cluster):
    """A job made with `kubectl apply` carries no owner and the submitter cannot be
    recovered afterwards. The field stays EMPTY rather than holding "unknown",
    which would sit in the owner column looking like somebody's username -- and
    the note explains the unnamed group, so it reads as jobs predating the
    labelling rather than as broken grouping."""
    seed_job_with_args(cluster, "lab-alice", "ddpsrun-dddddddddddd",
                       ["bash", "-lc", "python train.py"],
                       job_id="job-dddddddddddd", created="2026-09-08T01:00:00Z")
    answer = as_alice(client, "GET", "/v1/scripts").json()
    assert answer["scripts"][0]["owner"] == ""
    assert answer["owners"] == [""]
    assert "ddpsrun.io/owner" in answer["note"]
    assert "kubectl apply" in answer["note"]


def test_the_namespace_is_reported_as_a_namespace_and_not_as_a_person(client, cluster):
    """★ A NAMESPACE IS NOT A PERSON, and treating it as one was the defect. It is
    a tenancy boundary that may hold a whole team, and in this deployment it does:
    all three principals in the deployed token file sit in `default`. So the
    answer reports the namespace it READ and the people it FOUND as two separate
    fields."""
    seed_job_with_args(cluster, "lab-alice", "ddpsrun-eeeeeeeeeeee",
                       ["bash", "-lc", "a"], job_id="job-eeeeeeeeeeee",
                       created="2026-09-08T01:00:00Z", owner="alice")
    seed_job_with_args(cluster, "lab-alice", "ddpsrun-ffffffffffff",
                       ["bash", "-lc", "b"], job_id="job-ffffffffffff",
                       created="2026-09-08T02:00:00Z", owner="bob")
    answer = as_alice(client, "GET", "/v1/scripts").json()
    assert answer["namespace"] == "lab-alice"      # one namespace
    assert answer["owners"] == ["alice", "bob"]    # two people in it


# ---------------------------------------------- DDPSRUN-OWNER-GATE: two people,
#                                                one namespace
#
# ★ WHAT THESE MEASURE, AND WHY NONE OF THE 390 TESTS ABOVE CAUGHT IT. Every
# fixture in this file puts alice and bob in DIFFERENT namespaces, so the
# namespace scoping did all the work and the missing owner check never showed.
# The DEPLOYED token file puts all three principals in `default`. With two
# principals in one namespace, before the gate existed, measured through these
# same routes:
#
#     GET  /v1/jobs             200   the other person's job, with their owner
#                                     name and S3 result prefix on the row
#     GET  /v1/jobs/{id}        200   their detail
#     GET  /v1/jobs/{id}/spec   200   their run.sh, verbatim
#     POST /v1/jobs/{id}/exec   200   a shell line inside their RUNNING container
#     DELETE /v1/jobs/{id}      204   their job gone, and the only copy of that
#                                     script with it
#
# while seven docstrings in main.py said "someone else's job reads as 404".


@pytest.fixture
def shared_ns_client(tmp_path, monkeypatch, cluster):
    """alice and bob in ONE namespace, which is the deployed shape."""
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"tokens": [
        {"sha256": auth.hash_token("alice-token"), "user": "alice",
         "namespace": "shared", "team": "lab"},
        {"sha256": auth.hash_token("bob-token"), "user": "bob",
         "namespace": "shared", "team": "lab"},
        {"sha256": auth.hash_token("root-token"), "user": "root",
         "namespace": "shared", "team": "lab", "admin": True},
    ]}))
    monkeypatch.setenv("DDPSRUN_RESULT_BUCKET", "<RESULT_BUCKET>")
    monkeypatch.setenv("DDPSRUN_TOKENS_PATH", str(tokens))
    monkeypatch.setenv("DDPSRUN_SECRET_BINDINGS", "{}")
    monkeypatch.setattr(main.Cluster, "connect", staticmethod(lambda: cluster))
    with TestClient(main.app) as test_client:
        yield test_client


ALICE_SCRIPT = "python train.py --data /alice/private.jsonl"


def _alice_job(cluster, phase="Running"):
    """One job of alice's, in the shared namespace, labelled as hers."""
    cluster.objects[("shared", "ddpsrun-a11ce0000001")] = {
        "metadata": {"name": "ddpsrun-a11ce0000001", "namespace": "shared",
                     "labels": {"ddpsrun.io/job-id": "job-a11ce0000001",
                                "ddpsrun.io/name": "alice-secret",
                                "ddpsrun.io/owner": "alice"},
                     "creationTimestamp": "2026-09-08T01:00:00Z"},
        "spec": {"image": "img", "args": ["bash", "-lc", ALICE_SCRIPT],
                 "resultPath": "s3://b/pacsrun/shared/alice-secret-a11ce0000001/"},
        "status": {"phase": phase},
    }
    return "job-a11ce0000001"


def as_bob(client, method, path, **kwargs):
    return client.request(method, path,
                          headers={"Authorization": "Bearer bob-token"}, **kwargs)


def test_a_namespace_mate_cannot_see_the_job_in_the_list(shared_ns_client, cluster):
    """This is where it started: nobody had to guess an id, because the list
    handed over every namespace-mate's job with their owner name on it."""
    _alice_job(cluster)
    seen = as_bob(shared_ns_client, "GET", "/v1/jobs").json()["jobs"]
    assert seen == []
    mine = as_alice(shared_ns_client, "GET", "/v1/jobs").json()["jobs"]
    assert [j["user"] for j in mine] == ["alice"]


def test_a_namespace_mate_cannot_read_the_job(shared_ns_client, cluster):
    job = _alice_job(cluster)
    assert as_bob(shared_ns_client, "GET", f"/v1/jobs/{job}").status_code == 404
    assert as_alice(shared_ns_client, "GET", f"/v1/jobs/{job}").status_code == 200


def test_a_namespace_mate_cannot_read_the_script_off_the_spec(shared_ns_client, cluster):
    """★ THE PATH THAT WALKED AROUND THE /v1/scripts FIX. `spec.args` on the job
    detail is the same text the Scripts screen groups by person, so gating one and
    not the other gated nothing."""
    job = _alice_job(cluster)
    answer = as_bob(shared_ns_client, "GET", f"/v1/jobs/{job}/spec")
    assert answer.status_code == 404
    assert ALICE_SCRIPT not in answer.text
    assert ALICE_SCRIPT in as_alice(shared_ns_client, "GET", f"/v1/jobs/{job}/spec").text


def test_a_namespace_mate_cannot_exec_in_the_running_container(shared_ns_client, cluster):
    """The worst of them: an arbitrary shell line inside somebody else's running
    training job, on a machine their money is renting."""
    job = _alice_job(cluster)
    answer = as_bob(shared_ns_client, "POST", f"/v1/jobs/{job}/exec",
                    json={"command": "cat /alice/private.jsonl"})
    assert answer.status_code == 404
    assert cluster.execs == []


def test_a_namespace_mate_cannot_cancel_the_job(shared_ns_client, cluster):
    """Deleting a job destroys the only copy of its script -- nothing else stores
    it -- so this was unrecoverable loss of someone else's work."""
    job = _alice_job(cluster)
    assert as_bob(shared_ns_client, "DELETE", f"/v1/jobs/{job}").status_code == 404
    assert ("shared", "ddpsrun-a11ce0000001") in cluster.objects
    assert as_alice(shared_ns_client, "DELETE", f"/v1/jobs/{job}").status_code == 204
    assert ("shared", "ddpsrun-a11ce0000001") not in cluster.objects


def test_a_namespace_mate_cannot_read_the_logs(shared_ns_client, cluster):
    """Logs and metrics never fetched the job, so they read straight from the
    driver pod behind an id -- another person's training output, and their GPU
    samples. The owner is written on the JOB and nowhere else."""
    job = _alice_job(cluster)
    assert as_bob(shared_ns_client, "GET", f"/v1/jobs/{job}/logs").status_code == 404
    assert as_bob(shared_ns_client, "GET", f"/v1/jobs/{job}/metrics").status_code == 404


def test_an_operator_still_reaches_everything(shared_ns_client, cluster):
    """DELIBERATE EXEMPTION. An operator can already name any namespace with
    ?namespace=, so gating them would remove the only way to help somebody with a
    stuck job while changing nothing about what they can reach."""
    job = _alice_job(cluster)
    assert as_root(shared_ns_client, "GET", f"/v1/jobs/{job}").status_code == 200
    assert ALICE_SCRIPT in as_root(shared_ns_client, "GET", f"/v1/jobs/{job}/spec").text
    assert len(as_root(shared_ns_client, "GET", "/v1/jobs").json()["jobs"]) == 1


def test_a_job_with_no_owner_stays_readable_by_anyone_in_the_namespace(
        shared_ns_client, cluster):
    """DELIBERATE EXEMPTION, and it is not laxness. Every one of the 35 jobs on
    this cluster carries no owner label -- they were applied with kubectl, before
    the label existed -- so nobody owns them, and gating them would empty the
    screen of the jobs it mostly shows. A job created through this service always
    has an owner (`models.to_pacsjob` stamps it), so the exemption shrinks to
    nothing as the old jobs age out."""
    seed_job_with_args(cluster, "shared", "ddpsrun-0000000000ab",
                       ["bash", "-lc", "echo legacy"], job_id="job-0000000000ab",
                       created="2026-09-01T00:00:00Z")          # no owner=
    assert as_bob(shared_ns_client, "GET", "/v1/jobs/job-0000000000ab").status_code == 200
    assert len(as_bob(shared_ns_client, "GET", "/v1/jobs").json()["jobs"]) == 1


def test_the_answer_is_404_and_not_403(shared_ns_client, cluster):
    """403 would confirm that a job with that id exists and belongs to somebody,
    which is one bit more than the caller is entitled to -- and 404 is what the
    docstrings promised all along."""
    job = _alice_job(cluster)
    answer = as_bob(shared_ns_client, "GET", f"/v1/jobs/{job}")
    assert answer.status_code == 404
    assert answer.json()["detail"] == "no such job"
    # Indistinguishable from an id that never existed.
    missing = as_bob(shared_ns_client, "GET", "/v1/jobs/job-ffffffffffff")
    assert missing.status_code == 404 and missing.json()["detail"] == answer.json()["detail"]
