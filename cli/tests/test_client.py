"""What the client sends, and how it reports what came back.

The error translation is what these mostly check. The server writes its refusals
for a human — it passes the CRD's own validation messages through verbatim — and
a client that flattened them into "server returned 400" would throw away the
only sentence that says what to fix.
"""

import json

import pytest
import requests

from ddpsrun.client import Client, ServerError


class FakeResponse:
    def __init__(self, status_code, payload=None, text="", lines=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self._lines = lines or []

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def iter_lines(self, decode_unicode=False):
        return iter(self._lines)


class FakeSession:
    """Records the one request made and returns a prepared response."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self.raises:
            raise self.raises
        return self.response


def client_with(session):
    return Client("https://run.example", "s3cret", session=session)


def test_a_submit_sends_the_body_and_the_bearer_token():
    session = FakeSession(FakeResponse(201, {"job_id": "job-a8acdef80a07"}))
    result = client_with(session).submit({"name": "x", "image": "i"})
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://run.example/v1/jobs"
    assert call["json"] == {"name": "x", "image": "i"}
    assert call["headers"]["Authorization"] == "Bearer s3cret"
    assert result["job_id"] == "job-a8acdef80a07"


def test_explain_and_schema_send_no_token():
    # They need none, and asking for a credential to find out what a service is
    # would be the wrong way round.
    session = FakeSession(FakeResponse(200, text="ddpsrun — submit a batch job."))
    client_with(session).explain()
    assert "Authorization" not in session.calls[0]["headers"]


def test_a_trailing_slash_on_the_base_url_does_not_double():
    session = FakeSession(FakeResponse(200, {}))
    Client("https://run.example/", "s3cret", session=session).status("job-a8acdef80a07")
    assert session.calls[0]["url"] == "https://run.example/v1/jobs/job-a8acdef80a07"


def test_a_refusal_carries_the_servers_own_sentence():
    session = FakeSession(
        FakeResponse(400, {"detail": "there is no secret called 'NOPE'. Available: GITHUB_PAT"})
    )
    with pytest.raises(ServerError, match="Available: GITHUB_PAT"):
        client_with(session).submit({})


def test_a_pydantic_422_names_the_field_rather_than_dumping_json():
    session = FakeSession(
        FakeResponse(
            422,
            {
                "detail": [
                    {"loc": ["body", "gpu"], "msg": "give exactly one of gpu.vram_gb or gpu.name"}
                ]
            },
        )
    )
    with pytest.raises(ServerError, match=r"gpu: give exactly one"):
        client_with(session).submit({})


def test_a_response_that_is_not_json_still_produces_a_readable_error():
    session = FakeSession(FakeResponse(502, payload=None, text="<html>bad gateway</html>"))
    with pytest.raises(ServerError, match="server returned 502"):
        client_with(session).status("job-a8acdef80a07")


def test_an_unreachable_server_names_the_address_that_failed():
    session = FakeSession(raises=requests.ConnectionError("connection refused"))
    with pytest.raises(ServerError, match="cannot reach https://run.example"):
        client_with(session).status("job-a8acdef80a07")


def test_logs_yields_line_by_line():
    session = FakeSession(FakeResponse(200, lines=["one", "two", "three"]))
    assert list(client_with(session).logs("job-a8acdef80a07")) == ["one", "two", "three"]


def test_following_asks_for_it_and_lifts_the_read_timeout():
    # A training run can be minutes between lines. A read timeout would cut the
    # stream every time the model was busy, which looks exactly like a crash.
    session = FakeSession(FakeResponse(200, lines=[]))
    list(client_with(session).logs("job-a8acdef80a07", follow=True))
    call = session.calls[0]
    assert call["url"].endswith("/logs?follow=true")
    assert call["timeout"][1] is None
    assert call["stream"] is True


def test_not_following_keeps_a_read_timeout():
    session = FakeSession(FakeResponse(200, lines=[]))
    list(client_with(session).logs("job-a8acdef80a07"))
    assert isinstance(session.calls[0]["timeout"], int)


def test_a_submit_is_given_more_patience_than_a_read():
    # A submit creates an object and starts a rental; a status read does not.
    submit_session = FakeSession(FakeResponse(201, {}))
    client_with(submit_session).submit({})
    read_session = FakeSession(FakeResponse(200, {}))
    client_with(read_session).status("job-a8acdef80a07")
    assert submit_session.calls[0]["timeout"] > read_session.calls[0]["timeout"]
